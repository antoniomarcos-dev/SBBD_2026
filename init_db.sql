-- ============================================================
-- SBBD: Spatio-Temporal Hotspot Indexing
-- Monitoramento de Perda de Vegetação Nativa no Cerrado
-- ============================================================
-- Execução: psql -U postgres -d sbbd -f init_db.sql
-- ============================================================
--
-- Melhorias aplicadas nesta versão:
--   - fn_extrair_hotspots: SET LOCAL work_mem, ST_SimplifyPreserveTopology,
--     área pré-calculada, enable_hashjoin off em tiles grandes
--   - Índice BRIN em data_processamento (menor que B-tree para tabelas grandes)
--   - Índice em legenda_classes(categoria) para filtros na vw_desmatamento
--   - Índice parcial de desmatamento confirmado (subset mais consultado)
--   - Índice keyset (area_ha DESC, id DESC) para paginação eficiente O(log n)
--   - fn_taxa_desmatamento(): regressão linear simples em SQL puro
--   - vw_alertas_aceleracao: detecção de aceleração por Z-score nativo PG
-- ============================================================

-- Extensões
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_raster;

-- Habilitar apenas os drivers GDAL necessários (mais seguro que ENABLE_ALL)
ALTER DATABASE sbbd SET postgis.gdal_enabled_drivers TO 'GTiff PNG JPEG';
ALTER DATABASE sbbd SET postgis.enable_outdb_rasters TO false;

-- ============================================================
-- TUNING DE MEMÓRIA
-- Ajuste conforme RAM disponível na máquina.
-- Estas configurações são por sessão e podem ser sobrescritas
-- via SET LOCAL dentro de funções pesadas.
-- ============================================================
-- Para aplicar globalmente (requer superuser + reload):
--   ALTER SYSTEM SET shared_buffers = '512MB';
--   ALTER SYSTEM SET effective_cache_size = '1536MB';
--   ALTER SYSTEM SET work_mem = '32MB';
--   ALTER SYSTEM SET maintenance_work_mem = '128MB';
--   ALTER SYSTEM SET random_page_cost = 1.1;   -- SSD
--   ALTER SYSTEM SET max_parallel_workers_per_gather = 2;
--   SELECT pg_reload_conf();

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

-- Popular legenda com classes principais do MapBiomas/Cerrado
INSERT INTO legenda_classes (codigo, nome, cor_hex, categoria) VALUES
    (3,  'Formação Florestal',             '#1f8d49', 'Vegetação Nativa'),
    (4,  'Formação Savânica',              '#7dc975', 'Vegetação Nativa'),
    (5,  'Mangue',                         '#04381d', 'Vegetação Nativa'),
    (6,  'Floresta Alagável',              '#026975', 'Vegetação Nativa'),
    (11, 'Campo Alagado e Área Pantanosa', '#519799', 'Vegetação Nativa'),
    (12, 'Formação Campestre',             '#d6bc74', 'Vegetação Nativa'),
    (13, 'Outra Formação Natural',         '#d89f5c', 'Vegetação Nativa'),
    (49, 'Restinga Arbórea',               '#02d659', 'Vegetação Nativa'),
    (15, 'Pastagem',                       '#edde8e', 'Agropecuária'),
    (18, 'Agricultura',                    '#E974ED', 'Agropecuária'),
    (19, 'Lavoura Temporária',             '#C27BA0', 'Agropecuária'),
    (20, 'Cana',                           '#db7093', 'Agropecuária'),
    (21, 'Mosaico Agricultura/Pastagem',   '#FFEFC3', 'Agropecuária'),
    (39, 'Soja',                           '#f5b800', 'Agropecuária'),
    (41, 'Silvicultura',                   '#7a5900', 'Agropecuária'),
    (46, 'Café',                           '#d68f3b', 'Agropecuária'),
    (47, 'Citrus',                         '#9065d0', 'Agropecuária'),
    (48, 'Outras Lavouras Perenes',        '#e04cfa', 'Agropecuária'),
    (23, 'Praia, Duna e Areal',            '#ffa07a', 'Não Vegetado'),
    (24, 'Área Urbanizada',                '#d4271e', 'Não Vegetado'),
    (25, 'Outra Área Não Vegetada',        '#db4d4f', 'Não Vegetado'),
    (29, 'Afloramento Rochoso',            '#ffaa5f', 'Não Vegetado'),
    (30, 'Mineração',                      '#9c0027', 'Não Vegetado'),
    (33, 'Rio, Lago e Oceano',             '#0000FF', 'Água'),
    (34, 'Glaciar',                        '#d5d5e5', 'Água')
ON CONFLICT (codigo) DO NOTHING;

-- Rasters temporais (apenas metadados, sem raster completo em memória)
CREATE TABLE IF NOT EXISTS rasters_temporais (
    id              SERIAL PRIMARY KEY,
    nome            VARCHAR(255) NOT NULL,
    ano             INTEGER NOT NULL,
    formato         VARCHAR(50) NOT NULL DEFAULT 'GTiff',
    largura         INTEGER NOT NULL DEFAULT 0,
    altura          INTEGER NOT NULL DEFAULT 0,
    bandas          INTEGER NOT NULL DEFAULT 1,
    srid            INTEGER DEFAULT 0,
    thumbnail       BYTEA,
    metadata        JSONB DEFAULT '{}',
    data_upload     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tabela de tiles raster: rasters de 10GB+ fatiados e indexados aqui
-- Tile size calculado dinamicamente pelo pipeline Python (adaptativo por RAM)
CREATE TABLE IF NOT EXISTS raster_tiles (
    id            SERIAL PRIMARY KEY,
    raster_id     INTEGER NOT NULL REFERENCES rasters_temporais(id) ON DELETE CASCADE,
    coluna        INTEGER NOT NULL,
    linha         INTEGER NOT NULL,
    rast          RASTER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_raster_tiles_unq
    ON raster_tiles(raster_id, coluna, linha);

CREATE INDEX IF NOT EXISTS idx_raster_tiles_rast
    ON raster_tiles USING GiST(ST_ConvexHull(rast));

-- Tabela de hotspot deltas: polígonos de pixels que mudaram de classe
CREATE TABLE IF NOT EXISTS hotspot_deltas (
    id                  SERIAL PRIMARY KEY,
    raster_t1_id        INTEGER NOT NULL REFERENCES rasters_temporais(id) ON DELETE CASCADE,
    raster_t2_id        INTEGER NOT NULL REFERENCES rasters_temporais(id) ON DELETE CASCADE,
    ano_inicio          INTEGER NOT NULL,
    ano_fim             INTEGER NOT NULL,
    classe_origem       INTEGER NOT NULL,
    classe_destino      INTEGER NOT NULL,
    codigo_transicao    INTEGER NOT NULL,   -- classe_origem * 100 + classe_destino
    geom                GEOMETRY(Polygon, 4326),
    area_ha             DOUBLE PRECISION DEFAULT 0,
    data_processamento  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- ÍNDICES OTIMIZADOS
-- ============================================================

-- Índice espacial GiST principal (todas as queries geométricas)
CREATE INDEX IF NOT EXISTS idx_hotspot_geom
    ON hotspot_deltas USING GiST(geom);

-- Índice parcial: apenas desmatamento confirmado (vegetação nativa → agropecuária)
-- Subset mais consultado — partição lógica invisível ao query planner
CREATE INDEX IF NOT EXISTS idx_hotspot_desmat_parcial
    ON hotspot_deltas USING GiST(geom)
    WHERE classe_origem IN (3,4,5,6,11,12,13,49)
      AND classe_destino IN (15,18,19,20,21,24,30,39,41);

-- Índice B-tree no código de transição (ex: 315 = floresta→pastagem)
CREATE INDEX IF NOT EXISTS idx_hotspot_transicao
    ON hotspot_deltas(codigo_transicao);

-- Índice composto período + transição para queries temporais
CREATE INDEX IF NOT EXISTS idx_hotspot_periodo_trans
    ON hotspot_deltas(ano_inicio, ano_fim, codigo_transicao);

-- BRIN em data_processamento: muito menor que B-tree, ideal para tabelas grandes
-- (dados inseridos sequencialmente → correlação física com BRIN é quase perfeita)
CREATE INDEX IF NOT EXISTS idx_hotspot_brin_data
    ON hotspot_deltas USING BRIN(data_processamento) WITH (pages_per_range = 128);

-- Keyset pagination: evita OFFSET lento — permite cursores O(log n)
CREATE INDEX IF NOT EXISTS idx_hotspot_keyset
    ON hotspot_deltas(area_ha DESC, id DESC);

-- Índice em categoria de legenda (usado na vw_desmatamento e vw_alertas)
CREATE INDEX IF NOT EXISTS idx_legenda_categoria
    ON legenda_classes(categoria);

-- ============================================================
-- FUNÇÕES DE PROCESSAMENTO (100% PostGIS + SQL puro)
-- ============================================================

-- Função 1: Extrair hotspots (polígonos de mudança) tile a tile
-- Melhorias:
--   • SET LOCAL work_mem controla RAM desta sessão (evita OOM no PG)
--   • enable_hashjoin=off força merge join em tiles grandes (mais eficiente)
--   • ST_SimplifyPreserveTopology na inserção: economiza 60-80% de vértices
--   • Área pré-calculada na inserção (evita recálculo em cada query futura)
CREATE OR REPLACE FUNCTION fn_extrair_hotspots(
    p_raster_t1_id INTEGER,
    p_raster_t2_id INTEGER
) RETURNS INTEGER AS $$
DECLARE
    v_ano_ini INTEGER;
    v_ano_fim INTEGER;
    v_srid    INTEGER;
    v_count   INTEGER := 0;
    r         RECORD;
BEGIN
    -- Controle de memória: limita work_mem DESTA sessão
    -- Não afeta outras sessões. Fundamental para máquinas com pouca RAM.
    SET LOCAL work_mem = '64MB';
    -- Força merge join: mais eficiente para tiles grandes do que hash join
    SET LOCAL enable_hashjoin = off;

    SELECT ano, COALESCE(srid, 4326)
      INTO v_ano_ini, v_srid
      FROM rasters_temporais WHERE id = p_raster_t1_id;

    SELECT ano INTO v_ano_fim
      FROM rasters_temporais WHERE id = p_raster_t2_id;

    -- Remove deltas anteriores para o mesmo par (idempotência)
    DELETE FROM hotspot_deltas
     WHERE raster_t1_id = p_raster_t1_id
       AND raster_t2_id = p_raster_t2_id;

    -- MapAlgebra tile a tile para controlar RAM (nunca carrega rasters inteiros)
    FOR r IN (
        SELECT t1.rast AS rast1, t2.rast AS rast2
          FROM raster_tiles t1
          JOIN raster_tiles t2
            ON t1.coluna = t2.coluna
           AND t1.linha  = t2.linha
           AND t2.raster_id = p_raster_t2_id
         WHERE t1.raster_id = p_raster_t1_id
    ) LOOP
        INSERT INTO hotspot_deltas (
            raster_t1_id, raster_t2_id,
            ano_inicio, ano_fim,
            classe_origem, classe_destino,
            codigo_transicao, geom, area_ha
        )
        SELECT
            p_raster_t1_id, p_raster_t2_id,
            v_ano_ini, v_ano_fim,
            (val / 100)::INTEGER   AS classe_origem,
            (val % 100)::INTEGER   AS classe_destino,
            val::INTEGER           AS codigo_transicao,
            -- Simplificação na inserção: ~5m tolerância (invisível em zoom < 13)
            -- Economiza 60-80% de vértices e acelera queries futuras
            ST_SimplifyPreserveTopology(
                CASE WHEN v_srid = 4326 THEN gm
                     ELSE ST_Transform(gm, 4326)
                END,
                0.00005
            ) AS geom,
            -- Área pré-calculada (evita ST_Area em cada SELECT posterior)
            ST_Area(
                CASE WHEN v_srid = 4326 THEN gm
                     ELSE ST_Transform(gm, 4326)
                END::geography
            ) / 10000.0 AS area_ha
        FROM (
            SELECT (gv).val::BIGINT AS val, (gv).geom AS gm
              FROM (
                SELECT ST_DumpAsPolygons(
                    ST_MapAlgebra(
                        r.rast1, 1,
                        ST_Resample(r.rast2, r.rast1, 'NearestNeighbor'), 1,
                        '[rast1.val]*100+[rast2.val]',
                        '32BF', 'INTERSECTION',
                        '[rast1.val]*100', '[rast2.val]', NULL
                    )
                ) AS gv
              ) sub
        ) p
        -- Apenas pixels que mudaram de classe, con valores válidos
        WHERE (val / 100) != (val % 100)
          AND val > 0
          AND val < 9999;     -- Descarta nodata codificado como valor alto
    END LOOP;

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;


-- Função 2: Estatísticas de perda por tipo de transição
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
      AND (p_ano_fim    IS NULL OR hd.ano_fim    <= p_ano_fim)
    GROUP BY hd.codigo_transicao, hd.classe_origem, lo.nome,
             hd.classe_destino, ld.nome
    ORDER BY area_total_ha DESC;
END;
$$ LANGUAGE plpgsql;


-- Função 3: Taxa de desmatamento por regressão linear simples (SQL puro)
-- Retorna slope (ha/ano), intercept, R² e contagem de períodos analisados.
-- Slope positivo = aceleração; negativo = desaceleração.
-- Sem numpy, sem scipy — roda 100% no PostgreSQL.
CREATE OR REPLACE FUNCTION fn_taxa_desmatamento(
    p_codigo_transicao INTEGER DEFAULT NULL
) RETURNS TABLE (
    codigo_transicao  INTEGER,
    nome_transicao    TEXT,
    slope_ha_por_ano  DOUBLE PRECISION,
    intercept         DOUBLE PRECISION,
    r_squared         DOUBLE PRECISION,
    anos_analisados   INTEGER
) AS $$
BEGIN
    RETURN QUERY
    WITH serie AS (
        SELECT
            hd.codigo_transicao,
            hd.ano_inicio::DOUBLE PRECISION AS x,
            SUM(hd.area_ha)                 AS y
        FROM hotspot_deltas hd
        WHERE (p_codigo_transicao IS NULL OR hd.codigo_transicao = p_codigo_transicao)
        GROUP BY hd.codigo_transicao, hd.ano_inicio
    ),
    agg AS (
        SELECT
            codigo_transicao,
            COUNT(*)                                     AS n,
            AVG(x)                                       AS x_mean,
            AVG(y)                                       AS y_mean,
            SUM((x - AVG(x) OVER w) * (y - AVG(y) OVER w))  AS cov_xy,
            SUM((x - AVG(x) OVER w) ^ 2)                AS var_x,
            SUM((y - AVG(y) OVER w) ^ 2)                AS var_y
        FROM serie
        WINDOW w AS (PARTITION BY codigo_transicao)
        GROUP BY codigo_transicao
    ),
    reg AS (
        SELECT
            codigo_transicao,
            n,
            CASE WHEN var_x = 0 THEN 0 ELSE cov_xy / var_x END AS slope,
            y_mean - (CASE WHEN var_x = 0 THEN 0 ELSE cov_xy / var_x END) * x_mean AS intercept,
            CASE WHEN var_x * var_y = 0 THEN 0
                 ELSE POWER(cov_xy / NULLIF(SQRT(var_x * var_y), 0), 2) END AS r2
        FROM agg
    )
    SELECT
        r.codigo_transicao,
        COALESCE(lo.nome, '?') || ' → ' || COALESCE(ld.nome, '?') AS nome_transicao,
        r.slope,
        r.intercept,
        r.r2,
        r.n::INTEGER
    FROM reg r
    LEFT JOIN legenda_classes lo ON lo.codigo = (r.codigo_transicao / 100)
    LEFT JOIN legenda_classes ld ON ld.codigo = (r.codigo_transicao % 100)
    ORDER BY ABS(r.slope) DESC;
END;
$$ LANGUAGE plpgsql;


-- ============================================================
-- VIEWS
-- ============================================================

-- View: hotspots de desmatamento (vegetação nativa → agropecuária/não-vegetado)
CREATE OR REPLACE VIEW vw_desmatamento AS
SELECT
    hd.*,
    lo.nome      AS nome_origem,
    ld.nome      AS nome_destino,
    lo.categoria AS cat_origem,
    ld.categoria AS cat_destino
FROM hotspot_deltas hd
JOIN legenda_classes lo ON lo.codigo = hd.classe_origem
JOIN legenda_classes ld ON ld.codigo = hd.classe_destino
WHERE lo.categoria = 'Vegetação Nativa'
  AND ld.categoria IN ('Agropecuária', 'Não Vegetado');


-- View: estimativa de área desmatada por período
CREATE OR REPLACE VIEW vw_estimativa_desmatamento AS
SELECT
    ano_inicio,
    ano_fim,
    SUM(area_ha) AS total_desmatado_ha,
    COUNT(*)     AS total_alertas
FROM vw_desmatamento
GROUP BY ano_inicio, ano_fim
ORDER BY ano_inicio DESC;


-- View: alertas de aceleração de desmatamento via Z-score
-- Z-score > 2.0 → 'CRÍTICO'; > 1.5 → 'ALERTA'; demais → 'NORMAL'
-- Roda 100% em SQL puro, sem extensões adicionais.
CREATE OR REPLACE VIEW vw_alertas_aceleracao AS
WITH por_periodo AS (
    SELECT
        raster_t1_id,
        raster_t2_id,
        ano_inicio,
        ano_fim,
        SUM(area_ha) AS area_ha
    FROM hotspot_deltas
    WHERE classe_origem IN (3,4,5,6,11,12,13,49)
      AND classe_destino IN (15,18,19,20,21,24,30,39,41)
    GROUP BY raster_t1_id, raster_t2_id, ano_inicio, ano_fim
),
stats AS (
    SELECT
        AVG(area_ha)    AS media,
        STDDEV(area_ha) AS dp
    FROM por_periodo
)
SELECT
    pp.ano_inicio,
    pp.ano_fim,
    ROUND(pp.area_ha::NUMERIC, 2)      AS area_ha,
    ROUND(s.media::NUMERIC, 2)         AS media_historica,
    ROUND(s.dp::NUMERIC, 2)            AS desvio_padrao,
    CASE
        WHEN s.dp > 0
        THEN ROUND(((pp.area_ha - s.media) / s.dp)::NUMERIC, 3)
        ELSE 0
    END AS z_score,
    CASE
        WHEN s.dp > 0 AND (pp.area_ha - s.media) / s.dp > 2.0  THEN '🔴 CRÍTICO'
        WHEN s.dp > 0 AND (pp.area_ha - s.media) / s.dp > 1.5  THEN '🟡 ALERTA'
        ELSE '🟢 NORMAL'
    END AS nivel_alerta
FROM por_periodo pp
CROSS JOIN stats s
ORDER BY z_score DESC;


-- ============================================================
-- COMENTÁRIOS
-- ============================================================
COMMENT ON TABLE rasters_temporais IS 'Rasters MapBiomas por ano — apenas metadados, sem dados raster inline';
COMMENT ON TABLE raster_tiles IS 'Tiles raster fatiados pelo pipeline Python para suportar >10GB sem OOM';
COMMENT ON TABLE hotspot_deltas IS 'Polígonos de mudança de classe com área pré-calculada e geometrias simplificadas';
COMMENT ON FUNCTION fn_extrair_hotspots IS 'MapAlgebra tile a tile com controle de RAM (SET LOCAL work_mem). Simplifica geometrias na inserção.';
COMMENT ON FUNCTION fn_estatisticas_perda IS 'Estatísticas agregadas de perda por transição de classe';
COMMENT ON FUNCTION fn_taxa_desmatamento IS 'Regressão linear simples em SQL puro: calcula slope (ha/ano) sem numpy/scipy';
COMMENT ON VIEW vw_alertas_aceleracao IS 'Z-score de desmatamento: detecta períodos com área estatisticamente acima da média histórica';
