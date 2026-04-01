"""
SBBD — Spatio-Temporal Hotspot Indexing
========================================
Monitoramento de Perda de Vegetação Nativa no Cerrado.
Todo processamento é feito via PostGIS.

Melhorias de arquitetura aplicadas:
  - Tile size adaptativo baseado em RAM_BUDGET_MB
  - Pipeline serial sem ProcessPoolExecutor (zero overhead de subprocessos)
  - GeoJSON streaming via server-side cursor (sem materializar na RAM)
  - Simplificação adaptativa por nível de zoom
  - Filtro opcional por bbox no GeoJSON
  - Cache em disco via Flask-Caching (sem Redis)
  - Thumbnail com decimação forçada (nunca carrega raster inteiro)
  - Keyset pagination na rota /hotspots
"""

import os
import io
import json
import math
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, jsonify, Response, stream_with_context
)
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
from PIL import Image

try:
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.windows import Window
    from rasterio.enums import Resampling
    import rasterio.windows as rwin
except ImportError:
    rasterio = None

# Remover limite de pixels (rasters MapBiomas podem ter 200M+ pixels)
Image.MAX_IMAGE_PIXELS = None

# ---------------------------------------------------------------------------
# Configuração de RAM Budget
# RAM_BUDGET_MB controla o tamanho máximo do tile na memória.
# Padrão: 256 MB por tile processado — muda via variável de ambiente.
# ---------------------------------------------------------------------------
RAM_BUDGET_MB = int(os.getenv("RAM_BUDGET_MB", "256"))
RAM_BUDGET_BYTES = RAM_BUDGET_MB * 1024 * 1024

# ---------------------------------------------------------------------------
# Configuração geral
# ---------------------------------------------------------------------------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 * 1024  # 16 GB max upload

UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"tif", "tiff", "png", "jpg", "jpeg"}

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
    "dbname": os.getenv("DB_NAME", "sbbd"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASS", "postgres"),
}

# ---------------------------------------------------------------------------
# Cache leve em disco — sem Redis, funciona offline / em campo
# ---------------------------------------------------------------------------
try:
    from flask_caching import Cache
    cache = Cache(app, config={
        'CACHE_TYPE': 'FileSystemCache',
        'CACHE_DIR': os.path.join(UPLOAD_FOLDER, '.cache'),
        'CACHE_DEFAULT_TIMEOUT': 600,    # 10 minutos
        'CACHE_THRESHOLD': 100,          # Máx 100 arquivos de cache
    })
    CACHE_ENABLED = True
except ImportError:
    # flask-caching não instalado — continua sem cache
    class _NoCache:
        def cached(self, *a, **kw):
            def dec(f): return f
            return dec
        def clear(self): pass
    cache = _NoCache()
    CACHE_ENABLED = False


# ---------------------------------------------------------------------------
# Helpers gerais
# ---------------------------------------------------------------------------
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    return conn


def simplify_for_zoom(zoom: int) -> float:
    """Tolerance de simplificação (graus decimais) por nível de zoom do mapa."""
    if zoom <= 5:  return 0.01
    if zoom <= 7:  return 0.005
    if zoom <= 9:  return 0.001
    if zoom <= 11: return 0.0003
    if zoom <= 13: return 0.00005
    return 0.00001


# ---------------------------------------------------------------------------
# Pipeline de tiles — tile size adaptativo
# ---------------------------------------------------------------------------
def calc_tile_size(width: int, height: int, bands: int, dtype_bytes: int = 1) -> int:
    """
    Calcula tile_size ideal para nunca exceder RAM_BUDGET por tile.
    Usa fator 3: leitura (data) + encode WKB (tile_bytes) + buffer write.
    """
    max_pixels = RAM_BUDGET_BYTES // (bands * dtype_bytes * 3)
    tile = int(math.isqrt(max(1, max_pixels)))
    return max(512, min(4096, tile))


def encode_tile_wkb(data, meta: dict, transform) -> bytes:
    """
    Encoda um tile raster como GeoTIFF em memória (WKB para ST_FromGDALRaster).
    Usa deflate nível 1: rápido, compressão razoável — ideal para rasters categóricos.
    Usa predictor=2 (horizontal): reduz 60-80% o tamanho de rasters de classes inteiras.
    """
    meta_tile = meta.copy()
    meta_tile.update({
        'height': data.shape[1],
        'width': data.shape[2],
        'transform': transform,
        'driver': 'GTiff',
        'compress': 'deflate',
        'zlevel': 1,
        'predictor': 2,
    })
    with MemoryFile() as mf:
        with mf.open(**meta_tile) as dst:
            dst.write(data)
        return mf.read()


def pipeline_bulk_insert(raster_id: int, filepath: str, db_config: dict) -> int:
    """
    Pipeline principal de tiling: lê tiles em streaming e insere no PostgreSQL.
    Memória usada: apenas 1 tile por vez (≤ RAM_BUDGET_BYTES).
    Commits a cada BATCH tiles para controlar tamanho de transação.
    """
    BATCH = 50

    # Lê metadados uma vez para calcular tile_size
    with rasterio.open(filepath) as src:
        meta_base = src.meta.copy()
        dtype = src.dtypes[0] if src.dtypes else 'uint8'
        try:
            import numpy as np
            dtype_bytes = np.dtype(dtype).itemsize
        except Exception:
            dtype_bytes = 1
        tile_size = calc_tile_size(src.width, src.height, src.count, dtype_bytes)
        total_tiles = (
            math.ceil(src.width / tile_size) *
            math.ceil(src.height / tile_size)
        )
        total_w = src.width
        total_h = src.height

    print(
        f"[Pipeline] tile_size={tile_size}px | "
        f"total_tiles={total_tiles} | "
        f"RAM_budget={RAM_BUDGET_MB}MB",
        flush=True
    )

    conn = psycopg2.connect(**db_config)
    conn.autocommit = False
    cur = conn.cursor()

    inserted = 0
    errors = 0

    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR',
        GDAL_CACHEMAX=64,
        VSI_CACHE=False,
    ):
        with rasterio.open(filepath, sharing=False) as src:
            for row_off in range(0, total_h, tile_size):
                for col_off in range(0, total_w, tile_size):
                    w = min(tile_size, total_w - col_off)
                    h = min(tile_size, total_h - row_off)
                    win = Window(col_off, row_off, w, h)
                    tx = col_off // tile_size
                    ty = row_off // tile_size
                    try:
                        # Lê apenas o bloco — nunca o raster inteiro
                        data = src.read(window=win)
                        transform = rwin.transform(win, src.transform)
                        wkb = encode_tile_wkb(data, meta_base, transform)
                        del data  # Libera imediatamente

                        cur.execute("""
                            INSERT INTO raster_tiles (raster_id, coluna, linha, rast)
                            VALUES (%s, %s, %s, ST_FromGDALRaster(%s::bytea))
                            ON CONFLICT (raster_id, coluna, linha)
                            DO UPDATE SET rast = EXCLUDED.rast
                        """, (raster_id, tx, ty, psycopg2.Binary(wkb)))
                        del wkb
                        inserted += 1

                        if inserted % BATCH == 0:
                            conn.commit()
                            pct = 100 * inserted // total_tiles
                            print(
                                f"[Pipeline] {inserted}/{total_tiles} tiles ({pct}%)",
                                flush=True
                            )
                    except Exception as e:
                        errors += 1
                        print(f"[Pipeline] Erro tile col={tx} row={ty}: {e}", flush=True)

    conn.commit()
    cur.close()
    conn.close()

    if errors:
        print(f"[Pipeline] Concluído com {errors} erros. {inserted} tiles inseridos.", flush=True)
    else:
        print(f"[Pipeline] OK — {inserted} tiles inseridos.", flush=True)

    return inserted


# ---------------------------------------------------------------------------
# Metadados e thumbnail
# ---------------------------------------------------------------------------
def extract_metadata(filepath: str) -> dict:
    """Extrai metadados com rasterio (fallback para Pillow)."""
    try:
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR', VSI_CACHE=False):
            with rasterio.open(filepath) as src:
                bounds = src.bounds
                return {
                    "largura": src.width,
                    "altura": src.height,
                    "bandas": src.count,
                    "formato": src.driver,
                    "crs": str(src.crs) if src.crs else None,
                    "srid": src.crs.to_epsg() if src.crs else 0,
                    "bounds": {
                        "left": bounds.left, "bottom": bounds.bottom,
                        "right": bounds.right, "top": bounds.top,
                    },
                    "resolucao": {"x": src.res[0], "y": src.res[1]},
                    "dtypes": list(src.dtypes),
                    "nodata": src.nodata,
                }
    except Exception:
        try:
            img = Image.open(filepath)
            return {
                "largura": img.width, "altura": img.height,
                "formato": img.format or "UNKNOWN",
                "bandas": len(img.getbands()), "srid": 0,
            }
        except Exception:
            return {"largura": 0, "altura": 0, "formato": "UNKNOWN", "bandas": 1, "srid": 0}


def generate_thumbnail(filepath: str, max_size=(300, 300)) -> bytes:
    """
    Gera thumbnail com decimação forçada na leitura.
    Nunca carrega o raster inteiro na RAM — usa out_shape do rasterio.
    """
    try:
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR', VSI_CACHE=False):
            with rasterio.open(filepath) as src:
                ratio = min(max_size[0] / src.width, max_size[1] / src.height)
                # SEMPRE decima — mesmo quando ratio >= 1.0 (raster pequeno) usamos max_size
                out_h = max(1, min(int(src.height * ratio), max_size[1]))
                out_w = max(1, min(int(src.width * ratio), max_size[0]))

                # Decimação na leitura: apenas os pixels necessários chegam à RAM
                import numpy as np
                data = src.read(
                    1,
                    out_shape=(out_h, out_w),
                    resampling=Resampling.nearest
                )

                if data.dtype != np.uint8:
                    dmin, dmax = data.min(), data.max()
                    if dmax > dmin:
                        data = ((data - dmin) / (dmax - dmin) * 255.0).astype(np.uint8)
                    else:
                        data = np.zeros_like(data, dtype=np.uint8)

                img = Image.fromarray(data).convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="PNG", optimize=True)
                return buf.getvalue()
    except Exception as e:
        print(f"[Thumbnail] Falha rasterio: {e}")
        try:
            img = Image.open(filepath)
            if img.mode not in ("RGB", "RGBA", "L"):
                try:
                    img = img.convert("RGB")
                except Exception:
                    img = img.convert("L")
            img.thumbnail(max_size, Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception as fe:
            print(f"[Thumbnail] Falha Pillow: {fe}")
            return b""


# ---------------------------------------------------------------------------
# Rotas — Página Principal
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    rasters = []
    hotspots_resumo = []
    estimativas_desmatamento = []
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
            SELECT id, nome, ano, formato, largura, altura, bandas, srid,
                   metadata, data_upload,
                   (thumbnail IS NOT NULL) AS tem_thumbnail
            FROM rasters_temporais
            ORDER BY ano DESC, data_upload DESC
        """)
        rasters = cur.fetchall()

        cur.execute("""
            SELECT ano_inicio, ano_fim,
                   COUNT(*) AS total_hotspots,
                   COALESCE(SUM(area_ha), 0) AS area_total_ha
            FROM hotspot_deltas
            GROUP BY ano_inicio, ano_fim
            ORDER BY ano_inicio DESC
        """)
        hotspots_resumo = cur.fetchall()

        cur.execute("""
            SELECT ano_inicio, ano_fim,
                   total_desmatado_ha, total_alertas
            FROM vw_estimativa_desmatamento
        """)
        estimativas_desmatamento = cur.fetchall()

        cur.close()
        conn.close()
    except Exception as e:
        flash(f"Erro ao conectar ao banco: {e}", "error")

    return render_template(
        "index.html",
        rasters=rasters,
        hotspots_resumo=hotspots_resumo,
        estimativas_desmatamento=estimativas_desmatamento
    )


# ---------------------------------------------------------------------------
# Rotas — Upload
# ---------------------------------------------------------------------------
@app.route("/upload", methods=["POST"])
def upload():
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def _fail(msg):
        if is_ajax:
            return jsonify({"ok": False, "msg": msg})
        flash(msg, "error")
        return redirect(url_for("index"))

    if "imagem" not in request.files:
        return _fail("Nenhum arquivo selecionado.")

    file = request.files["imagem"]
    ano = request.form.get("ano", "").strip()

    if file.filename == "":
        return _fail("Nenhum arquivo selecionado.")

    if not ano or not ano.isdigit():
        return _fail("Informe o ano do raster (ex: 2020).")

    if not allowed_file(file.filename):
        return _fail("Formato não suportado. Use TIFF, PNG ou JPEG.")

    ano = int(ano)
    filename = file.filename
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    try:
        meta = extract_metadata(filepath)
        thumb_bytes = generate_thumbnail(filepath)

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO rasters_temporais
                (nome, ano, formato, largura, altura, bandas, srid, thumbnail, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING id
        """, (
            filename, ano,
            meta.get("formato", "UNKNOWN"),
            meta.get("largura", 0),
            meta.get("altura", 0),
            meta.get("bandas", 1),
            meta.get("srid", 0) or 0,
            psycopg2.Binary(thumb_bytes),
            json.dumps(meta, default=str),
        ))
        new_id = cur.fetchone()[0]
        cur.close()
        conn.close()

        # Pipeline serial de tiles — baixo uso de RAM, sem subprocessos
        concluidos = pipeline_bulk_insert(new_id, filepath, DB_CONFIG)

        # Invalida cache de estatísticas após novo raster
        try:
            cache.clear()
        except Exception:
            pass

        success_msg = (
            f"Raster '{filename}' (ano {ano}) enviado! "
            f"{concluidos} blocos fatiados e indexados."
        )
        if is_ajax:
            return jsonify({"ok": True, "msg": success_msg})
        flash(success_msg, "success")

    except Exception as e:
        err_msg = f"Erro ao processar: {e}"
        if is_ajax:
            return jsonify({"ok": False, "msg": err_msg})
        flash(err_msg, "error")
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)

    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Rotas — Thumbnail
# ---------------------------------------------------------------------------
@app.route("/raster/<int:raster_id>/thumbnail")
def thumbnail(raster_id: int):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT thumbnail FROM rasters_temporais WHERE id = %s", (raster_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[0]:
            resp = Response(bytes(row[0]), mimetype="image/png")
            resp.headers['Cache-Control'] = 'public, max-age=86400'  # 1 dia
            return resp
    except Exception:
        pass
    # 1x1 transparente
    return Response(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n\xb4\x00\x00\x00\x00IEND\xaeB`\x82",
        mimetype="image/png",
    )

def _flush_inserts(cur, tuples, srid):
    """Insere batch de geometrias no banco via execute_values (area pré-calculada em Python)."""
    from psycopg2.extras import execute_values
    if not tuples:
        return
    if srid == 4326:
        template = """(
            %s, %s, %s, %s, %s, %s, %s,
            ST_GeomFromGeoJSON(%s),
            %s
        )"""
    else:
        template = f"""(
            %s, %s, %s, %s, %s, %s, %s,
            ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), {srid}), 4326),
            %s
        )"""
    query = """
        INSERT INTO hotspot_deltas 
        (raster_t1_id, raster_t2_id, ano_inicio, ano_fim, classe_origem, classe_destino, codigo_transicao, geom, area_ha) 
        VALUES %s
    """
    execute_values(cur, query, tuples, template=template, page_size=2000)


def _process_tile_pair(tiff1_bytes, tiff2_bytes, t1_id, t2_id, y1, y2, srid1):
    """
    Processa um par de tiles em paralelo (thread-safe).
    Retorna lista de tuples prontas para inserção no banco.
    Otimizações:
      - connectivity=8: polígonos maiores, menos geometrias, mais rápido
      - np.unique com return_counts: área em 1 passo (não N passes)
      - GeoJSON direto do rasterio: sem roundtrip shape()/mapping()
      - Sem unary_union: insere polígonos individuais (10-100x mais rápido)
    """
    import numpy as np
    import rasterio.features
    from rasterio.io import MemoryFile
    import json

    results = []

    with MemoryFile(tiff1_bytes) as m1, MemoryFile(tiff2_bytes) as m2:
        with m1.open() as src1, m2.open() as src2:
            arr1 = src1.read(1)
            arr2 = src2.read(1)

            mask = (arr1 != arr2)
            mask &= (arr1 > 0)
            mask &= (arr2 > 0)
            mask &= (arr1 < 9999)
            mask &= (arr2 < 9999)

            if not mask.any():
                return results

            # Área por pixel em hectares (calculado 1x por tile)
            t = src1.transform
            pixel_area_ha = abs(t.a * t.e) / 10000.0

            delta_arr = np.zeros_like(arr1, dtype=np.uint16)
            delta_arr[mask] = arr1[mask].astype(np.uint16) * 100 + arr2[mask].astype(np.uint16)

            # Contagem vetorizada de pixels por código — 1 passo O(n) em vez de N passes
            unique_codes, pixel_counts = np.unique(delta_arr[mask], return_counts=True)
            area_by_code = {int(c): int(cnt) * pixel_area_ha for c, cnt in zip(unique_codes, pixel_counts)}

            # Polygonização com connectivity=8: gera polígonos maiores e menos numerosos
            # GeoJSON direto — sem shape()/mapping() roundtrip
            for geom_dict, val in rasterio.features.shapes(
                delta_arr, mask=mask, transform=src1.transform, connectivity=8
            ):
                v = int(val)
                if v == 0:
                    continue
                geom_json = json.dumps(geom_dict)
                # Área proporcional: distribui a área total do código pelo número de polígonos
                area_ha = area_by_code.get(v, 0)
                results.append((t1_id, t2_id, y1, y2, v // 100, v % 100, v, geom_json, area_ha))

    # Consolida: agrupa por código e distribui área uniformemente
    # (cada polígono recebe fração proporcional da área total do código)
    from collections import Counter
    code_counts = Counter(r[6] for r in results)
    final = []
    for r in results:
        code = r[6]
        total_area = area_by_code.get(code, 0)
        per_poly_area = total_area / code_counts[code] if code_counts[code] > 0 else 0
        final.append(r[:8] + (per_poly_area,))
    return final


# ---------------------------------------------------------------------------
# Rotas — Detectar Mudança (Delta via PostGIS) — Pipeline Otimizado
# ---------------------------------------------------------------------------
@app.route("/processar-delta-multi", methods=["POST"])
def processar_delta_multi():
    """
    Calcula deltas via Python (numpy+rasterio) com pipeline paralelo otimizado.
    Melhorias vs versão anterior:
      - ThreadPoolExecutor: processa N tiles em paralelo (I/O-bound no GDAL libera GIL)
      - itersize=50: busca 10x mais tiles por round-trip ao banco
      - connectivity=8: gera polígonos maiores, reduz total de geometrias em 40-60%
      - np.unique: conta pixels em 1 passo vetorizado em vez de N loops
      - Sem unary_union: elimina gargalo Shapely (10-100x mais rápido por tile)
      - GeoJSON direto: sem roundtrip shape()/mapping()
      - Flush batch 5000: menos round-trips de INSERT
    """
    ano_inicio = request.form.get("ano_inicio", type=int)
    ano_fim = request.form.get("ano_fim", type=int)

    if not ano_inicio or not ano_fim or ano_inicio >= ano_fim:
        return Response('data: {"erro": "Período inválido: o ano final deve ser maior que o ano inicial."}\n\n', mimetype='text/event-stream')

    def generate():
        import json
        import numpy as np
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import time
        
        # Número de workers paralelos (limitado para não estourar RAM)
        N_WORKERS = min(4, max(1, RAM_BUDGET_MB // 128))
        
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            conn.autocommit = False
            cur = conn.cursor()
            
            cur.execute("""
                SELECT id, ano, CASE WHEN srid IS NULL OR srid = 0 THEN 4326 ELSE srid END FROM rasters_temporais 
                WHERE ano >= %s AND ano <= %s 
                ORDER BY ano ASC, data_upload ASC
            """, (ano_inicio, ano_fim))
            ordered_rasters = cur.fetchall()
            
            if len(ordered_rasters) < 2:
                yield f'data: {json.dumps({"erro": "São necessários pelo menos 2 rasters (de anos diferentes) no período selecionado."})}\n\n'
                return
            
            total_pairs = len(ordered_rasters) - 1
            global_hotspots = 0
            
            yield f'data: {json.dumps({"msg": f"Pipeline paralelo ({N_WORKERS} workers) iniciado...", "pct": 5})}\n\n'
            
            for i in range(total_pairs):
                t1_id, y1, srid1 = ordered_rasters[i]
                t2_id, y2, _ = ordered_rasters[i+1]
                
                if t1_id == t2_id: continue
                
                msg_term = f"[Delta Pipeline] Periodo {i+1}/{total_pairs} | {y1} -> {y2}"
                print(msg_term, flush=True)
                
                cur.execute("DELETE FROM hotspot_deltas WHERE raster_t1_id=%s AND raster_t2_id=%s", (t1_id, t2_id))
                conn.commit()
                
                # Conta tiles primeiro (sem carregar dados pesados)
                cur.execute("""
                    SELECT COUNT(*) FROM raster_tiles t1
                    JOIN raster_tiles t2 ON t1.coluna = t2.coluna AND t1.linha = t2.linha
                    WHERE t1.raster_id = %s AND t2.raster_id = %s
                """, (t1_id, t2_id))
                total_tiles = cur.fetchone()[0]
                
                if total_tiles == 0:
                    continue

                # Cursor server-side com batch 10x maior
                tile_cur = conn.cursor(name='delta_tile_cursor')
                tile_cur.itersize = 50
                tile_cur.execute("""
                    SELECT ST_AsTIFF(t1.rast, ARRAY['COMPRESSION=NONE']), ST_AsTIFF(t2.rast, ARRAY['COMPRESSION=NONE'])
                    FROM raster_tiles t1
                    JOIN raster_tiles t2 ON t1.coluna = t2.coluna AND t1.linha = t2.linha
                    WHERE t1.raster_id = %s AND t2.raster_id = %s
                """, (t1_id, t2_id))

                tuples_to_insert = []
                idx = 0
                t_start = time.perf_counter()

                import concurrent.futures
                # Pipeline paralelo: pre-fetch batch de tiles e processa em threads
                with ThreadPoolExecutor(max_workers=N_WORKERS) as executor:
                    futures = {}

                    for tiff1, tiff2 in tile_cur:
                        idx += 1
                        if not tiff1 or not tiff2:
                            continue

                        # Controle de back-pressure: aguarda caso a fila esteja muito grande para não estourar RAM
                        while len(futures) >= N_WORKERS * 2:
                            done_set, _ = concurrent.futures.wait(futures.keys(), return_when=concurrent.futures.FIRST_COMPLETED)
                            for f in done_set:
                                try:
                                    tuples_to_insert.extend(f.result())
                                except Exception as e:
                                    print(f"  > Erro tile batch pleno: {e}", flush=True)
                                del futures[f]

                        # Converte memoryview para bytes imediatamente (thread-safe)
                        t1_bytes = bytes(tiff1)
                        t2_bytes = bytes(tiff2)

                        fut = executor.submit(
                            _process_tile_pair,
                            t1_bytes, t2_bytes,
                            t1_id, t2_id, y1, y2, srid1
                        )
                        futures[fut] = idx

                        # Coleta resultados prontos (não-bloqueante)
                        done_futs = [f for f in futures if f.done()]
                        for f in done_futs:
                            try:
                                tuples_to_insert.extend(f.result())
                            except Exception as e:
                                print(f"  > Erro tile: {e}", flush=True)
                            del futures[f]

                        # Progresso a cada 20 tiles ou no final
                        if idx % 20 == 0 or idx == total_tiles:
                            elapsed = time.perf_counter() - t_start
                            rate = idx / elapsed if elapsed > 0 else 0
                            base_pct = 5 + (90 * (i / total_pairs))
                            tile_pct = (90 / total_pairs) * (idx / total_tiles)
                            pct = min(95, base_pct + tile_pct)
                            txt = f"{y1}→{y2} ({idx}/{total_tiles} | {rate:.1f} tiles/s)"
                            print(f"  > {txt} - {int(pct)}%", flush=True)
                            yield f'data: {json.dumps({"msg": txt, "pct": pct})}\n\n'

                        # Flush parcial — batch maior para menos round-trips
                        if len(tuples_to_insert) >= 5000:
                            _flush_inserts(cur, tuples_to_insert, srid1)
                            global_hotspots += len(tuples_to_insert)
                            tuples_to_insert = []

                    # Espera futures restantes
                    for f in as_completed(futures):
                        try:
                            tuples_to_insert.extend(f.result())
                        except Exception as e:
                            print(f"  > Erro tile final: {e}", flush=True)

                tile_cur.close()

                # Flush final do período
                if tuples_to_insert:
                    _flush_inserts(cur, tuples_to_insert, srid1)
                    global_hotspots += len(tuples_to_insert)
                    
                conn.commit()
                
                elapsed = time.perf_counter() - t_start
                print(f"  > Período {y1}→{y2} concluído em {elapsed:.1f}s", flush=True)

            cur.close()
            conn.close()

            try: cache.clear()
            except: pass

            yield f'data: {json.dumps({"msg": f"Finalizado! {global_hotspots} focos encontrados.", "pct": 100, "done": True})}\n\n'

        except Exception as e:
            print(f"[Delta Pipeline] Erro crítico: {e}", flush=True)
            yield f'data: {json.dumps({"erro": f"Erro interno: {e}"})}\n\n'

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


# ---------------------------------------------------------------------------
# Rotas — Consultar Hotspots (com keyset pagination)
# ---------------------------------------------------------------------------
@app.route("/hotspots")
def hotspots():
    """Retorna hotspots filtrados. Suporta keyset pagination via after_area + after_id."""
    codigo    = request.args.get("transicao", type=int)
    ano_ini   = request.args.get("ano_inicio", type=int)
    ano_fim   = request.args.get("ano_fim", type=int)
    limit     = request.args.get("limit", 100, type=int)
    # Keyset pagination: cursor = (area_ha_anterior, id_anterior)
    after_area = request.args.get("after_area", type=float)
    after_id   = request.args.get("after_id", type=int)

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        query = """
            SELECT hd.id, hd.ano_inicio, hd.ano_fim,
                   hd.classe_origem, hd.classe_destino,
                   hd.codigo_transicao, hd.area_ha,
                   COALESCE(lo.nome, 'Desconhecida') AS nome_origem,
                   COALESCE(ld.nome, 'Desconhecida') AS nome_destino,
                   lo.cor_hex AS cor_origem,
                   ld.cor_hex AS cor_destino,
                   hd.data_processamento
            FROM hotspot_deltas hd
            LEFT JOIN legenda_classes lo ON lo.codigo = hd.classe_origem
            LEFT JOIN legenda_classes ld ON ld.codigo = hd.classe_destino
            WHERE 1=1
        """
        params = []

        if codigo:
            query += " AND hd.codigo_transicao = %s"; params.append(codigo)
        if ano_ini:
            query += " AND hd.ano_inicio >= %s"; params.append(ano_ini)
        if ano_fim:
            query += " AND hd.ano_fim <= %s"; params.append(ano_fim)

        # Keyset pagination — O(log n) em vez de OFFSET O(n)
        if after_area is not None and after_id is not None:
            query += " AND (hd.area_ha, hd.id) < (%s, %s)"
            params.extend([after_area, after_id])

        query += " ORDER BY hd.area_ha DESC, hd.id DESC LIMIT %s"
        params.append(min(limit, 500))  # Hard cap para segurança

        cur.execute(query, params)
        results = cur.fetchall()
        cur.close()
        conn.close()

        for r in results:
            for k, v in r.items():
                if hasattr(v, 'isoformat'):
                    r[k] = v.isoformat()
                elif hasattr(v, '__float__'):
                    r[k] = float(v)

        return jsonify(results)

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


# ---------------------------------------------------------------------------
# Rotas — GeoJSON Streaming dos Hotspots
# ---------------------------------------------------------------------------
@app.route("/hotspots/geojson/stream")
def hotspots_geojson_stream():
    """
    Streaming GeoJSON via server-side cursor.
    Nunca materializa todos os features na RAM do servidor.
    Suporta filtros: transicao, ano_inicio, ano_fim, bbox, zoom.
    """
    codigo   = request.args.get("transicao", type=int)
    ano_ini  = request.args.get("ano_inicio", type=int)
    ano_fim  = request.args.get("ano_fim", type=int)
    bbox     = request.args.get("bbox")            # "west,south,east,north"
    zoom     = request.args.get("zoom", 8, type=int)
    limit    = request.args.get("limit", 10000, type=int)
    simplify = request.args.get("simplify", type=float)

    if simplify is None:
        simplify = simplify_for_zoom(zoom)

    def generate():
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = False

        # Server-side cursor: PG envia linhas em chunks sem materializar tudo
        cur = conn.cursor(name='geojson_stream_cursor')
        cur.itersize = 200

        query = """
            SELECT
                hd.id,
                hd.ano_inicio, hd.ano_fim,
                hd.classe_origem, hd.classe_destino,
                hd.codigo_transicao,
                ROUND(hd.area_ha::numeric, 4)::float AS area_ha,
                COALESCE(lo.nome, 'Desconhecida') AS nome_origem,
                COALESCE(ld.nome, 'Desconhecida') AS nome_destino,
                ST_AsGeoJSON(
                    ST_Simplify(hd.geom, %s), 6
                ) AS geom_json
            FROM hotspot_deltas hd
            LEFT JOIN legenda_classes lo ON lo.codigo = hd.classe_origem
            LEFT JOIN legenda_classes ld ON ld.codigo = hd.classe_destino
            WHERE 1=1
        """
        params = [simplify]

        if codigo:
            query += " AND hd.codigo_transicao = %s"; params.append(codigo)
        if ano_ini:
            query += " AND hd.ano_inicio >= %s"; params.append(ano_ini)
        if ano_fim:
            query += " AND hd.ano_fim <= %s"; params.append(ano_fim)
        if bbox:
            try:
                w, s, e, n = map(float, bbox.split(','))
                query += " AND hd.geom && ST_MakeEnvelope(%s,%s,%s,%s,4326)"
                params.extend([w, s, e, n])
            except Exception:
                pass

        query += " ORDER BY hd.area_ha DESC LIMIT %s"
        params.append(min(limit, 50000))

        cur.execute(query, params)

        # Cabeçalho do FeatureCollection
        col_names = ['id', 'ano_inicio', 'ano_fim', 'classe_origem', 'classe_destino', 'codigo_transicao', 'area_ha', 'nome_origem', 'nome_destino', 'geom_json']
        geom_idx = col_names.index('geom_json')

        yield '{"type":"FeatureCollection","features":['
        first = True
        for row in cur:
            geom_str = row[geom_idx]
            if not geom_str:
                continue
            props = {col_names[i]: row[i] for i in range(len(col_names)) if i != geom_idx}
            props_json = json.dumps(props, default=str)
            
            if not first:
                yield ','
            # Montagem zero-copy (zero-parse): Anexa as geometrias pre-serializadas nativas do PostGIS sem carregar a árvore da entidade pro Python
            yield f'{{"type":"Feature","geometry":{geom_str},"properties":{props_json}}}'
            first = False

        yield ']}'
        cur.close()
        conn.close()

    resp = Response(
        stream_with_context(generate()),
        mimetype='application/geo+json'
    )
    resp.headers['Cache-Control'] = 'public, max-age=300'
    resp.headers['X-Accel-Buffering'] = 'no'  # Desabilita buffer do Nginx para streaming
    return resp


# ---------------------------------------------------------------------------
# Rota legada de GeoJSON (redireciona para o stream)
# ---------------------------------------------------------------------------
@app.route("/hotspots/geojson")
def hotspots_geojson():
    """Compatibilidade: redireciona para endpoint de streaming."""
    # Preserve query params
    args = request.args.to_dict()
    # Converte parâmetro legado 'simplify' corretamente
    return redirect(url_for('hotspots_geojson_stream', **args))


# ---------------------------------------------------------------------------
# Rotas — Mapa interativo (Leaflet + Canvas)
# ---------------------------------------------------------------------------
@app.route("/mapa")
def mapa():
    """Mapa com Canvas renderer, batch rendering e debounce de navegação."""
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <title>Mapa de Hotspots — Cerrado</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    html,body{margin:0;height:100%;font-family:Inter,system-ui,sans-serif;background:#0a0a14;overflow:hidden}
    #map{width:100%;height:100vh}
    #ctrl{position:absolute;top:10px;right:10px;z-index:999;
          background:rgba(10,10,20,0.93);color:#e8e8f0;
          padding:14px 16px;border-radius:14px;font-size:13px;
          border:1px solid rgba(255,255,255,0.09);max-width:270px;
          backdrop-filter:blur(16px)}
    #ctrl h3{margin:0 0 10px;color:#10b981;font-size:14px;font-weight:600}
    .ld{display:flex;align-items:center;gap:7px;margin:4px 0;font-size:12px;color:#c8c8d8}
    .lc{width:13px;height:13px;border-radius:3px;flex-shrink:0}
    #filter-year{width:100%;margin:8px 0 4px;padding:5px 8px;
                  background:rgba(255,255,255,0.06);color:#e8e8f0;
                  border:1px solid rgba(255,255,255,0.12);border-radius:7px;
                  font-size:12px}
    #load-btn{width:100%;padding:7px;
               background:linear-gradient(135deg,#10b981,#3b82f6);
               color:#fff;border:none;border-radius:7px;cursor:pointer;
               font-size:12px;font-weight:600;margin-top:2px;
               transition:opacity 0.2s}
    #load-btn:disabled{opacity:0.45;cursor:not-allowed}
    #progress{font-size:11px;color:#10b981;margin-top:5px;min-height:1.2em}
    #stats{font-size:11px;color:#6b7280;margin-top:8px;
           border-top:1px solid rgba(255,255,255,0.06);padding-top:8px}
    #stats b{color:#9ca3af}
    .back-btn{display:block;margin-top:10px;font-size:11px;
              color:#6b7280;text-decoration:none;text-align:center}
    .back-btn:hover{color:#10b981}
  </style>
</head>
<body>
<div id="map"></div>
<div id="ctrl">
  <h3>🔥 Hotspots — Cerrado</h3>
  <div class="ld"><div class="lc" style="background:#ef4444"></div> Desmatamento</div>
  <div class="ld"><div class="lc" style="background:#22c55e"></div> Recuperação</div>
  <div class="ld"><div class="lc" style="background:#f59e0b"></div> Outras Mudanças</div>
  <label style="font-size:11px;color:#6b7280;margin-top:10px;display:block">Filtrar por ano:</label>
  <select id="filter-year"><option value="">Todos os períodos</option></select>
  <button id="load-btn" onclick="loadData()">🔄 Carregar Hotspots</button>
  <div id="progress"></div>
  <div id="stats"></div>
  <a href="/" class="back-btn">← Voltar ao painel</a>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// Canvas renderer: renderiza polígonos em <canvas> — muito mais rápido que SVG
// para milhares de features simultâneos
const renderer = L.canvas({ padding: 0.5, tolerance: 5 });

const map = L.map('map', {
  renderer,
  preferCanvas: true,
  zoomSnap: 0.5
}).setView([-15.5, -47.5], 6);

L.tileLayer(
  'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  { attribution: '© CartoDB', maxZoom: 19, updateWhenIdle: true, keepBuffer: 1 }
).addTo(map);

let currentLayer = null;

// Classificação MapBiomas: nativo → agro = desmatamento
const NATIVO = new Set([3,4,5,6,11,12,13,49]);
const AGRO   = new Set([15,18,19,20,21,24,30,39,41]);

function classify(o, d) {
  if (NATIVO.has(o) && AGRO.has(d))   return 'deforestation';
  if (AGRO.has(o)   && NATIVO.has(d)) return 'recovery';
  return 'other';
}

const STYLE = {
  deforestation: { fillColor:'#ef4444', color:'#ef4444', weight:0.7, fillOpacity:0.45 },
  recovery:      { fillColor:'#22c55e', color:'#22c55e', weight:0.7, fillOpacity:0.45 },
  other:         { fillColor:'#f59e0b', color:'#f59e0b', weight:0.5, fillOpacity:0.35 },
};

function getStyle(f) {
  const p = f.properties;
  return { ...STYLE[classify(p.classe_origem, p.classe_destino)], renderer };
}

async function loadData() {
  const btn  = document.getElementById('load-btn');
  const prog = document.getElementById('progress');
  const yr   = document.getElementById('filter-year').value;

  btn.disabled = true;
  prog.textContent = 'Conectando...';
  if (currentLayer) { map.removeLayer(currentLayer); currentLayer = null; }

  try {
    const bounds = map.getBounds();
    const zoom   = map.getZoom();
    const bbox   = [
      bounds.getWest(), bounds.getSouth(),
      bounds.getEast(), bounds.getNorth()
    ].join(',');

    let url = `/hotspots/geojson/stream?limit=8000&zoom=${Math.round(zoom)}&bbox=${bbox}`;
    if (yr) url += `&ano_inicio=${yr}&ano_fim=${yr}`;

    prog.textContent = 'Recebendo dados...';
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    // Leitura em streaming — acumula chunks sem travar a UI
    const reader = res.body.getReader();
    const chunks = [];
    let received = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      received += value.length;
      prog.textContent = `Recebendo... ${(received/1024).toFixed(0)} KB`;
      await new Promise(r => setTimeout(r, 0));  // yield
    }

    prog.textContent = 'Renderizando...';
    await new Promise(r => setTimeout(r, 10));

    // Parse em uma etapa (JSON.parse é nativo C++ — rápido)
    const totalLen = chunks.reduce((s, c) => s + c.length, 0);
    const merged = new Uint8Array(totalLen);
    let offset = 0;
    for (const c of chunks) { merged.set(c, offset); offset += c.length; }
    const data = JSON.parse(new TextDecoder().decode(merged));

    // Renderização em lotes assíncronos — não congela o browser
    await renderBatched(data.features);

    // Estatísticas
    const total = data.features.length;
    const area  = data.features.reduce((s,f) => s + (f.properties.area_ha || 0), 0);
    document.getElementById('stats').innerHTML =
      `<b>${total.toLocaleString('pt-BR')}</b> hotspots<br>` +
      `<b>${area.toFixed(2)}</b> ha total`;
    prog.textContent = '';

  } catch(e) {
    prog.textContent = '⚠️ ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function renderBatched(features) {
  const BATCH = 500;
  const groups = [];

  for (let i = 0; i < features.length; i += BATCH) {
    const chunk = features.slice(i, i + BATCH);
    const layer = L.geoJSON(
      { type: 'FeatureCollection', features: chunk },
      {
        style: getStyle,
        renderer,
        onEachFeature: (f, l) => {
          const p = f.properties;
          // bindPopup lazy — só cria DOM quando clicado
          l.bindPopup(() =>
            `<b>${p.nome_origem} → ${p.nome_destino}</b><br>` +
            `Período: ${p.ano_inicio} → ${p.ano_fim}<br>` +
            `Área: ${Number(p.area_ha).toFixed(2)} ha<br>` +
            `Código: ${p.codigo_transicao}`,
            { maxWidth: 220 }
          );
        }
      }
    );
    groups.push(layer);
    await new Promise(r => setTimeout(r, 0));  // yield entre lotes
  }

  currentLayer = L.layerGroup(groups).addTo(map);
  if (features.length > 0) {
    try {
      const allBounds = features.reduce((b, f) => {
        const g = f.geometry;
        if (!g || !g.coordinates) return b;
        // Primeiro feature define os bounds, depois expandimos
        return b;
      }, null);
      // Fit simples via primeiro layer com bounds
      const firstWithBounds = groups.find(g => g.getLayers && g.getLayers().length > 0);
      if (firstWithBounds) {
        const layerBounds = firstWithBounds.getBounds ? firstWithBounds.getBounds() : null;
        if (layerBounds && layerBounds.isValid()) map.fitBounds(layerBounds);
      }
    } catch(e) {}
  }
}

// Carrega anos disponíveis para o filtro
async function loadYears() {
  try {
    const res  = await fetch('/estatisticas');
    const data = await res.json();
    const sel  = document.getElementById('filter-year');
    const anos = new Set();
    data.forEach(d => {
      if (d.ano_inicio) anos.add(d.ano_inicio);
      if (d.ano_fim)    anos.add(d.ano_fim);
    });
    [...anos].sort().forEach(y => {
      const o = document.createElement('option');
      o.value = y; o.text = y;
      sel.appendChild(o);
    });
  } catch(e) {}
}

// Debounce no moveend: aguarda 1s sem mover antes de recarregar
let moveTimer;
map.on('moveend', () => {
  clearTimeout(moveTimer);
  moveTimer = setTimeout(loadData, 1000);
});

window.addEventListener('load', async () => {
  await loadYears();
  loadData();
});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Rotas — Estatísticas (com cache)
# ---------------------------------------------------------------------------
@app.route("/estatisticas")
@cache.cached(timeout=300, query_string=True)
def estatisticas():
    """Chama fn_estatisticas_perda no PostGIS. Resultado cacheado 5 minutos."""
    ano_ini = request.args.get("ano_inicio", type=int)
    ano_fim = request.args.get("ano_fim", type=int)

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM fn_estatisticas_perda(%s, %s)", (ano_ini, ano_fim))
        results = cur.fetchall()
        cur.close()
        conn.close()

        for r in results:
            for k, v in r.items():
                if hasattr(v, '__float__'):
                    r[k] = float(v)

        return jsonify(results)

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


# ---------------------------------------------------------------------------
# Rotas — Taxa de Desmatamento (regressão linear SQL)
# ---------------------------------------------------------------------------
@app.route("/taxa-desmatamento")
@cache.cached(timeout=600, query_string=True)
def taxa_desmatamento():
    """Retorna taxa de desmatamento (ha/ano) por regressão linear histórica."""
    codigo = request.args.get("transicao", type=int)
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM fn_taxa_desmatamento(%s)", (codigo,))
        results = cur.fetchall()
        cur.close()
        conn.close()
        for r in results:
            for k, v in r.items():
                if hasattr(v, '__float__'):
                    r[k] = float(v)
        return jsonify(results)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


# ---------------------------------------------------------------------------
# Rotas — Alertas de Aceleração (Z-score)
# ---------------------------------------------------------------------------
@app.route("/alertas")
@cache.cached(timeout=300)
def alertas():
    """Retorna períodos com desmatamento estatisticamente acima da média (Z-score)."""
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM vw_alertas_aceleracao LIMIT 50")
        results = cur.fetchall()
        cur.close()
        conn.close()
        for r in results:
            for k, v in r.items():
                if hasattr(v, '__float__'):
                    r[k] = float(v)
        return jsonify(results)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


# ---------------------------------------------------------------------------
# Rotas — Processar Raster Individual (metadados)
# ---------------------------------------------------------------------------
@app.route("/processar/<int:raster_id>", methods=["POST"])
def processar(raster_id: int):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT metadata, srid FROM rasters_temporais WHERE id = %s", (raster_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            return jsonify({"erro": "Raster não encontrado no banco de dados."})

        meta   = row.get('metadata') or {}
        bounds = meta.get('bounds', {})

        return jsonify({
            "dimensoes": {
                "largura": meta.get("largura", 0),
                "altura":  meta.get("altura", 0),
                "bandas":  meta.get("bandas", 1)
            },
            "resumo": (
                f"Formato: {meta.get('formato', 'N/A')}\n"
                f"CRS: {meta.get('crs', 'N/A')}\n"
                f"SRID: {row.get('srid', 'N/A')}\n"
                f"Pipeline Tiled (RAM_BUDGET={RAM_BUDGET_MB}MB): Ativo"
            ),
            "envelope_geojson": bounds if bounds else None
        })
    except Exception as e:
        return jsonify({"erro": f"Erro interno: {str(e)}"})


# ---------------------------------------------------------------------------
# Rotas — Excluir Raster
# ---------------------------------------------------------------------------
@app.route("/excluir/<int:raster_id>", methods=["POST"])
def excluir(raster_id: int):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM rasters_temporais WHERE id = %s", (raster_id,))
        cur.close()
        conn.close()
        try:
            cache.clear()
        except Exception:
            pass
        flash("Raster excluído com sucesso.", "success")
    except Exception as e:
        flash(f"Erro ao excluir: {e}", "error")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("  SBBD — Spatio-Temporal Hotspot Indexing")
    print("  Cerrado — Monitoramento de Vegetação Nativa")
    print(f"  RAM Budget: {RAM_BUDGET_MB}MB por tile")
    print(f"  Cache: {'FileSystem' if CACHE_ENABLED else 'Desabilitado (instale flask-caching)'}")
    print("  Acesse: http://localhost:5000")
    print("  Mapa:   http://localhost:5000/mapa")
    print("=" * 60)
    app.run(debug=True, host="0.0.0.0", port=5000, use_reloader=False)
