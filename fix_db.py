import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
    "dbname": os.getenv("DB_NAME", "sbbd"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASS", "postgres"),
}

func_sql = """
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
    SET LOCAL work_mem = '64MB';
    SET LOCAL enable_hashjoin = off;

    SELECT ano, COALESCE(srid, 4326)
      INTO v_ano_ini, v_srid
      FROM rasters_temporais WHERE id = p_raster_t1_id;

    SELECT ano INTO v_ano_fim
      FROM rasters_temporais WHERE id = p_raster_t2_id;

    DELETE FROM hotspot_deltas
     WHERE raster_t1_id = p_raster_t1_id
       AND raster_t2_id = p_raster_t2_id;

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
            ST_SimplifyPreserveTopology(
                ST_Transform(ST_SetSRID(gm, CASE WHEN v_srid = 0 THEN 4326 ELSE v_srid END), 4326),
                0.00005
            ) AS geom,
            ST_Area(
                ST_Transform(ST_SetSRID(gm, CASE WHEN v_srid = 0 THEN 4326 ELSE v_srid END), 4326)::geography
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
        WHERE (val / 100) != (val % 100)
          AND val > 0
          AND val < 9999;
    END LOOP;

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;
"""

print("Connecting to DB...")
conn = psycopg2.connect(**DB_CONFIG)
conn.autocommit = True
cur = conn.cursor()
print("Executing CREATE OR REPLACE FUNCTION...")
cur.execute(func_sql)
print("Fix applied.")
