"""
SBBD — Spatio-Temporal Hotspot Indexing
========================================
Monitoramento de Perda de Vegetação Nativa no Cerrado.
Todo processamento é feito via PostGIS.
"""

import os
import io
import json
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, jsonify, Response
)
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
from PIL import Image

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")

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
# Helpers
# ---------------------------------------------------------------------------
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    return conn


def extract_metadata(filepath: str) -> dict:
    """Extrai metadados com rasterio (fallback para Pillow)."""
    try:
        import rasterio
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
        img = Image.open(filepath)
        return {
            "largura": img.width,
            "altura": img.height,
            "formato": img.format or "UNKNOWN",
            "bandas": len(img.getbands()),
            "srid": 0,
        }


def generate_thumbnail(filepath: str, max_size=(300, 300)) -> bytes:
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


# ---------------------------------------------------------------------------
# Rotas — Página Principal
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    rasters = []
    hotspots_resumo = []
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Listar rasters
        cur.execute("""
            SELECT id, nome, ano, formato, largura, altura, bandas, srid,
                   metadata, data_upload,
                   (thumbnail IS NOT NULL) AS tem_thumbnail
            FROM rasters_temporais
            ORDER BY ano DESC, data_upload DESC
        """)
        rasters = cur.fetchall()

        # Resumo de hotspots existentes
        cur.execute("""
            SELECT ano_inicio, ano_fim,
                   COUNT(*) AS total_hotspots,
                   COALESCE(SUM(area_ha), 0) AS area_total_ha
            FROM hotspot_deltas
            GROUP BY ano_inicio, ano_fim
            ORDER BY ano_inicio DESC
        """)
        hotspots_resumo = cur.fetchall()

        cur.close()
        conn.close()
    except Exception as e:
        flash(f"Erro ao conectar ao banco: {e}", "error")

    return render_template("index.html", rasters=rasters, hotspots_resumo=hotspots_resumo)


# ---------------------------------------------------------------------------
# Rotas — Upload (com ano)
# ---------------------------------------------------------------------------
@app.route("/upload", methods=["POST"])
def upload():
    if "imagem" not in request.files:
        flash("Nenhum arquivo selecionado.", "error")
        return redirect(url_for("index"))

    file = request.files["imagem"]
    ano = request.form.get("ano", "").strip()

    if file.filename == "":
        flash("Nenhum arquivo selecionado.", "error")
        return redirect(url_for("index"))

    if not ano or not ano.isdigit():
        flash("Informe o ano do raster (ex: 2020).", "error")
        return redirect(url_for("index"))

    if not allowed_file(file.filename):
        flash("Formato não suportado. Use TIFF, PNG ou JPEG.", "error")
        return redirect(url_for("index"))

    ano = int(ano)
    filename = file.filename
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    try:
        meta = extract_metadata(filepath)
        thumb_bytes = generate_thumbnail(filepath)

        with open(filepath, "rb") as f:
            raster_bytes = f.read()

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO rasters_temporais
                (nome, ano, formato, largura, altura, bandas, srid, rast, thumbnail, metadata)
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                ST_FromGDALRaster(%s::bytea),
                %s, %s::jsonb
            )
            RETURNING id
        """, (
            filename, ano,
            meta.get("formato", "UNKNOWN"),
            meta.get("largura", 0),
            meta.get("altura", 0),
            meta.get("bandas", 1),
            meta.get("srid", 0) or 0,
            psycopg2.Binary(raster_bytes),
            psycopg2.Binary(thumb_bytes),
            json.dumps(meta, default=str),
        ))
        new_id = cur.fetchone()[0]
        cur.close()
        conn.close()

        flash(f"Raster '{filename}' (ano {ano}) enviado com sucesso! ID: {new_id}", "success")

    except Exception as e:
        flash(f"Erro ao processar: {e}", "error")
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
            return Response(bytes(row[0]), mimetype="image/png")
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


# ---------------------------------------------------------------------------
# Rotas — Detectar Mudança (Delta via PostGIS)
# ---------------------------------------------------------------------------
@app.route("/processar-delta", methods=["POST"])
def processar_delta():
    """Chama fn_extrair_hotspots no PostGIS para os dois rasters selecionados."""
    t1_id = request.form.get("raster_t1_id", type=int)
    t2_id = request.form.get("raster_t2_id", type=int)

    if not t1_id or not t2_id:
        flash("Selecione dois rasters para comparar.", "error")
        return redirect(url_for("index"))

    if t1_id == t2_id:
        flash("Selecione dois rasters diferentes.", "error")
        return redirect(url_for("index"))

    try:
        conn = get_db()
        cur = conn.cursor()

        # Chamar a função PostGIS que faz todo o processamento
        cur.execute("SELECT fn_extrair_hotspots(%s, %s)", (t1_id, t2_id))
        count = cur.fetchone()[0]

        cur.close()
        conn.close()

        flash(f"Detecção concluída! {count} hotspot(s) de mudança encontrado(s).", "success")

    except Exception as e:
        flash(f"Erro no processamento: {e}", "error")

    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Rotas — Consultar Hotspots
# ---------------------------------------------------------------------------
@app.route("/hotspots")
def hotspots():
    """Retorna hotspots filtrados por transição e/ou período."""
    codigo = request.args.get("transicao", type=int)
    ano_ini = request.args.get("ano_inicio", type=int)
    ano_fim = request.args.get("ano_fim", type=int)
    limit = request.args.get("limit", 100, type=int)

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
            query += " AND hd.codigo_transicao = %s"
            params.append(codigo)
        if ano_ini:
            query += " AND hd.ano_inicio >= %s"
            params.append(ano_ini)
        if ano_fim:
            query += " AND hd.ano_fim <= %s"
            params.append(ano_fim)

        query += " ORDER BY hd.area_ha DESC LIMIT %s"
        params.append(limit)

        cur.execute(query, params)
        results = cur.fetchall()

        cur.close()
        conn.close()

        # Converter Decimal/datetime para serialização
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
# Rotas — GeoJSON dos Hotspots
# ---------------------------------------------------------------------------
@app.route("/hotspots/geojson")
def hotspots_geojson():
    """Retorna polígonos de mudança em formato GeoJSON."""
    codigo = request.args.get("transicao", type=int)
    ano_ini = request.args.get("ano_inicio", type=int)
    ano_fim = request.args.get("ano_fim", type=int)

    try:
        conn = get_db()
        cur = conn.cursor()

        query = """
            SELECT json_build_object(
                'type', 'FeatureCollection',
                'features', COALESCE(json_agg(
                    json_build_object(
                        'type', 'Feature',
                        'geometry', ST_AsGeoJSON(hd.geom)::json,
                        'properties', json_build_object(
                            'id', hd.id,
                            'ano_inicio', hd.ano_inicio,
                            'ano_fim', hd.ano_fim,
                            'classe_origem', hd.classe_origem,
                            'classe_destino', hd.classe_destino,
                            'codigo_transicao', hd.codigo_transicao,
                            'area_ha', hd.area_ha,
                            'nome_origem', COALESCE(lo.nome, 'Desconhecida'),
                            'nome_destino', COALESCE(ld.nome, 'Desconhecida')
                        )
                    )
                ), '[]'::json)
            )
            FROM hotspot_deltas hd
            LEFT JOIN legenda_classes lo ON lo.codigo = hd.classe_origem
            LEFT JOIN legenda_classes ld ON ld.codigo = hd.classe_destino
            WHERE 1=1
        """
        params = []

        if codigo:
            query += " AND hd.codigo_transicao = %s"
            params.append(codigo)
        if ano_ini:
            query += " AND hd.ano_inicio >= %s"
            params.append(ano_ini)
        if ano_fim:
            query += " AND hd.ano_fim <= %s"
            params.append(ano_fim)

        cur.execute(query, params)
        geojson = cur.fetchone()[0]

        cur.close()
        conn.close()

        return Response(
            json.dumps(geojson, default=str),
            mimetype="application/geo+json"
        )

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


# ---------------------------------------------------------------------------
# Rotas — Estatísticas de Perda (via função PostGIS)
# ---------------------------------------------------------------------------
@app.route("/estatisticas")
def estatisticas():
    """Chama fn_estatisticas_perda no PostGIS."""
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
# Rotas — Processar Raster Individual (metadados PostGIS)
# ---------------------------------------------------------------------------
@app.route("/processar/<int:raster_id>", methods=["POST"])
def processar(raster_id: int):
    resultados = {}
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
            SELECT ST_Summary(rast) AS summary FROM rasters_temporais WHERE id = %s
        """, (raster_id,))
        row = cur.fetchone()
        resultados["resumo"] = row["summary"] if row else None

        cur.execute("""
            SELECT ST_Width(rast) AS largura, ST_Height(rast) AS altura,
                   ST_NumBands(rast) AS bandas
            FROM rasters_temporais WHERE id = %s
        """, (raster_id,))
        row = cur.fetchone()
        if row:
            resultados["dimensoes"] = dict(row)

        cur.execute("""
            SELECT ST_AsGeoJSON(ST_Envelope(rast)) AS envelope
            FROM rasters_temporais WHERE id = %s
        """, (raster_id,))
        row = cur.fetchone()
        if row and row["envelope"]:
            resultados["envelope_geojson"] = json.loads(row["envelope"])

        cur.execute("""
            SELECT ST_BandPixelType(rast, 1) AS pixel_type,
                   ST_BandNoDataValue(rast, 1) AS nodata
            FROM rasters_temporais WHERE id = %s
        """, (raster_id,))
        row = cur.fetchone()
        if row:
            resultados["banda_1"] = dict(row)

        cur.execute("""
            SELECT (ST_SummaryStats(rast, 1)).*
            FROM rasters_temporais WHERE id = %s
        """, (raster_id,))
        row = cur.fetchone()
        if row:
            resultados["estatisticas_banda_1"] = {
                k: float(v) if hasattr(v, '__float__') else v
                for k, v in dict(row).items()
            }

        cur.close()
        conn.close()

    except Exception as e:
        return jsonify({"erro": str(e)}), 500

    return jsonify(resultados)


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
        flash("Raster excluído com sucesso.", "success")
    except Exception as e:
        flash(f"Erro ao excluir: {e}", "error")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 55)
    print("  SBBD — Spatio-Temporal Hotspot Indexing")
    print("  Cerrado — Monitoramento de Vegetação Nativa")
    print("  Acesse: http://localhost:5000")
    print("=" * 55)
    app.run(debug=True, host="0.0.0.0", port=5000)
