-- ============================================================
-- SBBD: Spatio-Temporal Hotspot Indexing
-- Monitoramento de Perda de Vegetação Nativa no Cerrado
-- ============================================================
-- Execução: psql -U postgres -d sbbd -f init_db.sql
-- ============================================================

-- Extensões
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_raster;

-- Habilitar drivers GDAL para ST_FromGDALRaster
ALTER DATABASE sbbd SET postgis.gdal_enabled_drivers TO 'ENABLE_ALL';
ALTER DATABASE sbbd SET postgis.enable_outdb_rasters TO true;

-- ============================================================
-- TABELAS
-- ============================================================

-- Legenda de classes MapBiomas (Cerrado)
CREATE TABLE IF NOT EXISTS legenda_classes (
    id          SERIAL PRIMARY KEY,
    codigo      INTEGER UNIQUE NOT NULL,
    nome        VARCHAR(100) NOT NULL,
    cor_hex     VARCHAR(7) DEFAULT '#CCCCCC',
    categoria   VARCHAR(50) NOT NULL
);

-- Popular legenda com classes principais do Cerrado
INSERT INTO legenda_classes (codigo, nome, cor_hex, categoria) VALUES
    (3,  'Formação Florestal',     '#1f8d49', 'Vegetação Nativa'),
    (4,  'Formação Savânica',      '#7dc975', 'Vegetação Nativa'),
    (5,  'Mangue',                 '#04381d', 'Vegetação Nativa'),
    (11, 'Campo Alagado e Área Pantanosa', '#519799', 'Vegetação Nativa'),
    (12, 'Formação Campestre',     '#d6bc74', 'Vegetação Nativa'),
    (13, 'Outra Formação Natural', '#d89f5c', 'Vegetação Nativa'),
    (15, 'Pastagem',               '#edde8e', 'Agropecuária'),
    (18, 'Agricultura',            '#E974ED', 'Agropecuária'),
    (19, 'Lavoura Temporária',     '#C27BA0', 'Agropecuária'),
    (20, 'Cana',                   '#db7093', 'Agropecuária'),
    (21, 'Mosaico Agricultura/Pastagem', '#FFEFC3', 'Agropecuária'),
    (23, 'Praia, Duna e Areal',    '#ffa07a', 'Não Vegetado'),
    (24, 'Área Urbanizada',        '#d4271e', 'Não Vegetado'),
    (25, 'Outra Área Não Vegetada','#db4d4f', 'Não Vegetado'),
    (29, 'Afloramento Rochoso',    '#ffaa5f', 'Não Vegetado'),
    (30, 'Mineração',              '#9c0027', 'Não Vegetado'),
    (33, 'Rio, Lago e Oceano',     '#0000FF', 'Água'),
    (34, 'Glaciar',                '#d5d5e5', 'Água'),
    (41, 'Silvicultura',           '#7a5900', 'Agropecuária'),
    (46, 'Café',                   '#d68f3b', 'Agropecuária'),
    (47, 'Citrus',                 '#9065d0', 'Agropecuária'),
    (48, 'Outras Lavouras Perenes','#e04cfa', 'Agropecuária')
ON CONFLICT (codigo) DO NOTHING;

-- Rasters temporais (um registro por imagem/ano)
CREATE TABLE IF NOT EXISTS rasters_temporais (
    id              SERIAL PRIMARY KEY,
    nome            VARCHAR(255) NOT NULL,
    ano             INTEGER NOT NULL,
    formato         VARCHAR(50) NOT NULL DEFAULT 'GTiff',
    largura         INTEGER NOT NULL DEFAULT 0,
    altura          INTEGER NOT NULL DEFAULT 0,
    bandas          INTEGER NOT NULL DEFAULT 1,
    srid            INTEGER DEFAULT 0,
    rast            RASTER,
    thumbnail       BYTEA,
    metadata        JSONB DEFAULT '{}',
    data_upload     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tabela de hotspot deltas: APENAS pixels que mudaram de classe
CREATE TABLE IF NOT EXISTS hotspot_deltas (
    id                  SERIAL PRIMARY KEY,
    raster_t1_id        INTEGER NOT NULL REFERENCES rasters_temporais(id) ON DELETE CASCADE,
    raster_t2_id        INTEGER NOT NULL REFERENCES rasters_temporais(id) ON DELETE CASCADE,
    ano_inicio          INTEGER NOT NULL,
    ano_fim             INTEGER NOT NULL,
    classe_origem       INTEGER NOT NULL,
    classe_destino      INTEGER NOT NULL,
    codigo_transicao    INTEGER NOT NULL,  -- classe_origem * 100 + classe_destino
    geom                GEOMETRY(Polygon, 4326),
    area_ha             DOUBLE PRECISION DEFAULT 0,
    data_processamento  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- ÍNDICES (Inovação: índice secundário apenas sobre os deltas)
-- ============================================================

-- Índice espacial GiST sobre as geometrias de mudança
CREATE INDEX IF NOT EXISTS idx_hotspot_geom
    ON hotspot_deltas USING GiST(geom);

-- Índice B-tree no código de transição (ex: 315 = floresta→pastagem)
CREATE INDEX IF NOT EXISTS idx_hotspot_transicao
    ON hotspot_deltas(codigo_transicao);

-- Índice composto período + transição para consultas temporais
CREATE INDEX IF NOT EXISTS idx_hotspot_periodo_trans
    ON hotspot_deltas(ano_inicio, ano_fim, codigo_transicao);

-- Índice espacial no raster temporal
CREATE INDEX IF NOT EXISTS idx_rasters_rast
    ON rasters_temporais USING GiST(ST_ConvexHull(rast));

-- ============================================================
-- FUNÇÕES DE PROCESSAMENTO (100% PostGIS)
-- ============================================================

-- Função 1: Calcular raster delta via ST_MapAlgebra
-- Gera raster onde cada pixel = classe_t1 * 100 + classe_t2
-- Reprojeção (ST_Transform) + Alinhamento (ST_Resample) automáticos
CREATE OR REPLACE FUNCTION fn_calcular_delta(
    p_raster_t1_id INTEGER,
    p_raster_t2_id INTEGER
) RETURNS RASTER AS $$
DECLARE
    v_rast1 RASTER;
    v_rast2 RASTER;
    v_rast2_reprojected RASTER;
    v_rast2_aligned RASTER;
    v_delta RASTER;
    v_srid1 INTEGER;
    v_srid2 INTEGER;
BEGIN
    SELECT rast INTO v_rast1 FROM rasters_temporais WHERE id = p_raster_t1_id;
    SELECT rast INTO v_rast2 FROM rasters_temporais WHERE id = p_raster_t2_id;

    IF v_rast1 IS NULL OR v_rast2 IS NULL THEN
        RAISE EXCEPTION 'Raster(s) não encontrado(s)';
    END IF;

    v_srid1 := ST_SRID(v_rast1);
    v_srid2 := ST_SRID(v_rast2);

    -- Se SRIDs diferentes, reprojetar rast2 para o SRID de rast1
    IF v_srid1 != v_srid2 THEN
        v_rast2_reprojected := ST_Transform(v_rast2, v_srid1, 'NearestNeighbor');
    ELSE
        v_rast2_reprojected := v_rast2;
    END IF;

    -- Alinhar rast2 ao grid de rast1 (NearestNeighbor preserva classes)
    v_rast2_aligned := ST_Resample(
        v_rast2_reprojected,
        v_rast1,
        'NearestNeighbor'
    );

    -- MapAlgebra: pixel = classe_t1 * 100 + classe_t2
    v_delta := ST_MapAlgebra(
        v_rast1, 1,
        v_rast2_aligned, 1,
        '[rast1.val] * 100 + [rast2.val]',
        '32BF',          -- pixel type float32
        'INTERSECTION',  -- apenas área de sobreposição
        '[rast1.val] * 100',  -- nodata expr rast2
        '[rast2.val]',        -- nodata expr rast1
        NULL                  -- nodata-nodata val
    );

    RETURN v_delta;
END;
$$ LANGUAGE plpgsql;

-- Função 2: Extrair hotspots (polígonos de mudança) do delta raster
-- Insere diretamente na tabela hotspot_deltas
-- Todos os valores são convertidos para INTEGER (pixels são classes inteiras)
CREATE OR REPLACE FUNCTION fn_extrair_hotspots(
    p_raster_t1_id INTEGER,
    p_raster_t2_id INTEGER
) RETURNS INTEGER AS $$
DECLARE
    v_delta RASTER;
    v_ano_ini INTEGER;
    v_ano_fim INTEGER;
    v_srid INTEGER;
    v_count INTEGER := 0;
    v_rec RECORD;
    v_cod INTEGER;
    v_origem INTEGER;
    v_destino INTEGER;
    v_geom_4326 GEOMETRY;
BEGIN
    -- Obter anos
    SELECT ano INTO v_ano_ini FROM rasters_temporais WHERE id = p_raster_t1_id;
    SELECT ano INTO v_ano_fim FROM rasters_temporais WHERE id = p_raster_t2_id;

    -- Calcular delta
    v_delta := fn_calcular_delta(p_raster_t1_id, p_raster_t2_id);
    v_srid := ST_SRID(v_delta);

    -- Remover deltas anteriores para o mesmo par
    DELETE FROM hotspot_deltas
    WHERE raster_t1_id = p_raster_t1_id AND raster_t2_id = p_raster_t2_id;

    -- Extrair polígonos dos pixels de mudança
    FOR v_rec IN
        SELECT
            ((gv).val)::INTEGER AS codigo_transicao,
            (gv).geom AS geom
        FROM (
            SELECT ST_DumpAsPolygons(v_delta) AS gv
        ) AS sub
        WHERE (gv).val IS NOT NULL
          AND (gv).val > 0
    LOOP
        v_cod := v_rec.codigo_transicao;
        v_origem := v_cod / 100;
        v_destino := v_cod % 100;

        -- Pular onde não houve mudança (ex: 303, 1515)
        IF v_origem = v_destino THEN
            CONTINUE;
        END IF;

        -- Converter geometria para 4326
        IF v_srid > 0 AND v_srid != 4326 THEN
            v_geom_4326 := ST_Transform(v_rec.geom, 4326);
        ELSIF v_srid = 4326 THEN
            v_geom_4326 := v_rec.geom;
        ELSE
            v_geom_4326 := ST_SetSRID(v_rec.geom, 4326);
        END IF;

        INSERT INTO hotspot_deltas (
            raster_t1_id, raster_t2_id,
            ano_inicio, ano_fim,
            classe_origem, classe_destino,
            codigo_transicao,
            geom, area_ha
        ) VALUES (
            p_raster_t1_id, p_raster_t2_id,
            v_ano_ini, v_ano_fim,
            v_origem, v_destino, v_cod,
            v_geom_4326,
            ST_Area(v_geom_4326::geography) / 10000.0
        );
        v_count := v_count + 1;
    END LOOP;

    RETURN v_count;
END;
$$ LANGUAGE plpgsql;


-- Função 3: Estatísticas de perda por tipo de transição
CREATE OR REPLACE FUNCTION fn_estatisticas_perda(
    p_ano_inicio INTEGER DEFAULT NULL,
    p_ano_fim    INTEGER DEFAULT NULL
) RETURNS TABLE (
    codigo_transicao  INTEGER,
    classe_origem     INTEGER,
    nome_origem       VARCHAR,
    classe_destino    INTEGER,
    nome_destino      VARCHAR,
    total_hotspots    BIGINT,
    area_total_ha     DOUBLE PRECISION
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        hd.codigo_transicao,
        hd.classe_origem,
        COALESCE(lo.nome, 'Desconhecida')::VARCHAR AS nome_origem,
        hd.classe_destino,
        COALESCE(ld.nome, 'Desconhecida')::VARCHAR AS nome_destino,
        COUNT(*)::BIGINT AS total_hotspots,
        COALESCE(SUM(hd.area_ha), 0) AS area_total_ha
    FROM hotspot_deltas hd
    LEFT JOIN legenda_classes lo ON lo.codigo = hd.classe_origem
    LEFT JOIN legenda_classes ld ON ld.codigo = hd.classe_destino
    WHERE (p_ano_inicio IS NULL OR hd.ano_inicio >= p_ano_inicio)
      AND (p_ano_fim IS NULL OR hd.ano_fim <= p_ano_fim)
    GROUP BY hd.codigo_transicao, hd.classe_origem, lo.nome,
             hd.classe_destino, ld.nome
    ORDER BY area_total_ha DESC;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- VIEWS ÚTEIS
-- ============================================================

-- View: hotspots de desmatamento (vegetação nativa → agropecuária)
CREATE OR REPLACE VIEW vw_desmatamento AS
SELECT
    hd.*,
    lo.nome AS nome_origem,
    ld.nome AS nome_destino,
    lo.categoria AS cat_origem,
    ld.categoria AS cat_destino
FROM hotspot_deltas hd
JOIN legenda_classes lo ON lo.codigo = hd.classe_origem
JOIN legenda_classes ld ON ld.codigo = hd.classe_destino
WHERE lo.categoria = 'Vegetação Nativa'
  AND ld.categoria IN ('Agropecuária', 'Não Vegetado');

-- Comentários
COMMENT ON TABLE rasters_temporais IS 'Rasters MapBiomas por ano para análise temporal';
COMMENT ON TABLE hotspot_deltas IS 'Polígonos de mudança de classe: índice secundário sobre deltas';
COMMENT ON FUNCTION fn_calcular_delta IS 'Gera raster delta (T1*100+T2) via ST_MapAlgebra';
COMMENT ON FUNCTION fn_extrair_hotspots IS 'Extrai polígonos de mudança e popula hotspot_deltas';
COMMENT ON FUNCTION fn_estatisticas_perda IS 'Estatísticas agregadas de perda por transição';
