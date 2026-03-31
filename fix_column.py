import psycopg2

conn = psycopg2.connect(dbname='sbbd', user='postgres', password='postgres')
conn.autocommit = True
cur = conn.cursor()

# Save view definitions
cur.execute("SELECT pg_get_viewdef('vw_desmatamento', true)")
vw1 = cur.fetchone()[0]

cur.execute("SELECT pg_get_viewdef('vw_estimativa_desmatamento', true)")
vw2 = cur.fetchone()[0]

print("Views salvas.")

# Drop views CASCADE
cur.execute("DROP VIEW IF EXISTS vw_estimativa_desmatamento")
cur.execute("DROP VIEW IF EXISTS vw_desmatamento")
print("Views removidas.")

# Alter column
cur.execute("ALTER TABLE hotspot_deltas ALTER COLUMN geom TYPE geometry(Geometry, 4326)")
print("Coluna alterada para Geometry genérica.")

# Recreate views
cur.execute("CREATE VIEW vw_desmatamento AS " + vw1)
cur.execute("CREATE VIEW vw_estimativa_desmatamento AS " + vw2)
print("Views recriadas com sucesso!")

cur.close()
conn.close()
