# SBBD — Upload de Imagens para PostGIS

Aplicação web em Flask para upload, armazenamento e processamento espacial de imagens raster usando PostGIS.

## Pré-requisitos

- **Python 3.9+**
- **PostgreSQL 14+** com extensão **PostGIS** e **postgis_raster**

## Instalação

```bash
# 1. Criar ambiente virtual
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Linux/Mac

# 2. Instalar dependências
pip install -r requirements.txt

# 3. Configurar banco de dados
#    Copie o .env.example para .env e preencha com suas credenciais
copy .env.example .env         # Windows
# cp .env.example .env         # Linux/Mac

# 4. Criar tabelas no banco
psql -U postgres -d sbbd -f init_db.sql

# 5. Executar a aplicação
python entrada.py
```

## Uso

1. Acesse **<http://localhost:5000>**
2. Arraste ou selecione uma imagem (GeoTIFF, PNG, JPEG)
3. Clique em **Enviar para o PostGIS**
4. Na galeria, clique em **Processar** para ver metadados espaciais

## Estrutura

```
sbbd/
├── entrada.py           # Backend Flask (rotas e lógica)
├── init_db.sql          # Script SQL de inicialização
├── requirements.txt     # Dependências Python
├── .env.example         # Variáveis de ambiente (modelo)
├── README.md            # Este arquivo
└── templates/
    └── index.html       # Interface web
```

## Processamentos PostGIS

Ao clicar em "Processar", a aplicação executa:

| Função PostGIS | Descrição |
|---|---|
| `ST_Summary` | Resumo geral do raster |
| `ST_Width/Height/NumBands` | Dimensões e número de bandas |
| `ST_SummaryStats` | Estatísticas (min, max, média, desvio padrão) |
| `ST_BandPixelType` | Tipo de pixel da banda |
| `ST_Envelope` + `ST_AsGeoJSON` | Bounding box em formato GeoJSON |
