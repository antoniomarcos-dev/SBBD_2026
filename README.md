# 🌿 Spatio-Temporal Hotspot Indexing — Cerrado

Sistema de monitoramento de perda de vegetação nativa no Cerrado, utilizando **indexação espaço-temporal de hotspots** com processamento inteiramente via **PostGIS**.

A inovação principal é indexar apenas os **deltas** (mudanças) entre classes de uso do solo, evitando varredura completa dos mapas. Inspirado em técnicas de indexação de dados de mobilidade (MIDET).

## Arquitetura

```
Upload GeoTIFF (ano) → PostGIS (ST_FromGDALRaster)
                              ↓
              fn_calcular_delta (ST_MapAlgebra)
              pixel = classe_T1 × 100 + classe_T2
                              ↓
              fn_extrair_hotspots (ST_DumpAsPolygons)
              → tabela hotspot_deltas (apenas mudanças)
              → Índice GiST + B-tree secundário
                              ↓
              Consultas, GeoJSON, Mapa Leaflet
```

## Requisitos

- **Python 3.9+**
- **PostgreSQL 14+** com extensões **PostGIS** e **PostGIS Raster**
- Bibliotecas Python: Flask, psycopg2, rasterio, Pillow

## Instalação

```bash
# 1. Clonar o repositório
git clone <url-do-repositorio>
cd sbbd

# 2. Criar ambiente virtual e instalar dependências
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac
pip install -r requirements.txt

# 3. Configurar variáveis de ambiente
copy .env.example .env
# Editar .env com suas credenciais do PostgreSQL

# 4. Criar banco e aplicar schema
# Opção A — Script automático (Windows):
powershell -ExecutionPolicy Bypass -File setup_db.ps1

# Opção B — Manual:
psql -U postgres -c "CREATE DATABASE sbbd;"
psql -U postgres -d sbbd -f init_db.sql

# 5. Rodar a aplicação
python entrada.py
```

## Rotas da Aplicação

### Interface Web

| Rota | Descrição |
|---|---|
| `GET /` | Página principal — upload, galeria, detecção de mudança |
| `GET /mapa` | **Mapa interativo Leaflet** com hotspots de mudança |

### Upload e Processamento

| Rota | Método | Descrição |
|---|---|---|
| `POST /upload` | POST | Upload de raster GeoTIFF com ano |
| `POST /processar-delta` | POST | Detecta mudanças entre dois rasters via PostGIS |
| `POST /processar/<id>` | POST | Metadados PostGIS de um raster (ST_Summary, etc.) |
| `POST /excluir/<id>` | POST | Remove um raster e seus hotspots |

### Consulta de Hotspots

| Rota | Descrição |
|---|---|
| `GET /hotspots` | Lista hotspots em JSON (filtros: `transicao`, `ano_inicio`, `ano_fim`, `limit`) |
| `GET /hotspots/geojson` | Polígonos de mudança em **GeoJSON** (max 5000 features por padrão) |
| `GET /hotspots/geojson?limit=1000` | Limitar a 1000 features para carregamento rápido |
| `GET /hotspots/geojson?transicao=315` | Filtrar por código de transição (ex: 315 = Floresta→Pastagem) |
| `GET /estatisticas` | Estatísticas agregadas de perda via `fn_estatisticas_perda` |

### Thumbnails

| Rota | Descrição |
|---|---|
| `GET /raster/<id>/thumbnail` | Thumbnail PNG de um raster armazenado |

## Funções PostGIS

| Função | O que faz |
|---|---|
| `fn_calcular_delta(t1, t2)` | `ST_MapAlgebra` — gera raster delta com reprojeção automática (`ST_Transform` + `ST_Resample`) |
| `fn_extrair_hotspots(t1, t2)` | `ST_DumpAsPolygons` — converte pixels de mudança em polígonos na tabela `hotspot_deltas` |
| `fn_estatisticas_perda(ini, fim)` | Agrega área total e contagem de hotspots por tipo de transição |

## Estrutura do Banco

```
legenda_classes       — Códigos MapBiomas (22 classes)
rasters_temporais     — Rasters multi-temporais com ano
hotspot_deltas        — Apenas as MUDANÇAS (polígonos + área em ha)
  ├── GiST index      → consultas espaciais rápidas
  ├── B-tree index     → filtro por código de transição
  └── Composite index  → (ano_inicio, ano_fim, codigo_transicao)
vw_desmatamento       — View: vegetação nativa → agropecuária
```

## Formato de Dados

Use **GeoTIFF com georreferenciamento** (CRS definido). Rasters sem coordenadas geográficas (PNG, JPEG) gerarão hotspots com coordenadas incorretas.

Fontes recomendadas:

- [MapBiomas](https://mapbiomas.org/) — cobertura e uso do solo
- [PRODES/INPE](http://terrabrasilis.dpi.inpe.br/) — desmatamento

## Código de Transição

O código de transição é calculado como `classe_origem × 100 + classe_destino`. Exemplos:

| Código | Transição |
|---|---|
| 315 | Formação Florestal (3) → Pastagem (15) |
| 415 | Formação Savânica (4) → Pastagem (15) |
| 321 | Formação Florestal (3) → Mosaico Agric/Past (21) |

## Estrutura de Arquivos

```
sbbd/
├── entrada.py          # Backend Flask
├── init_db.sql         # Schema PostGIS completo
├── setup_db.ps1        # Script de setup do banco (Windows)
├── requirements.txt    # Dependências Python
├── .env.example        # Variáveis de ambiente (modelo)
├── README.md
└── templates/
    └── index.html      # Interface web
```
