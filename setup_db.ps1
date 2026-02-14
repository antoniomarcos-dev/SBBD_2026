# ============================================
# Script de Setup do Banco de Dados PostGIS
# Execute: powershell -ExecutionPolicy Bypass -File setup_db.ps1
# ============================================

# Configurações (altere conforme necessário)
$DB_HOST = "localhost"
$DB_PORT = "5432"
$DB_NAME = "sbbd"
$DB_USER = "postgres"
$DB_PASS = "postgres"

# Encontrar psql automaticamente
$psqlPath = $null
$pgDirs = Get-ChildItem "C:\Program Files\PostgreSQL" -Directory -ErrorAction SilentlyContinue
foreach ($dir in $pgDirs) {
    $candidate = Join-Path $dir.FullName "bin\psql.exe"
    if (Test-Path $candidate) {
        $psqlPath = $candidate
        break
    }
}

if (-not $psqlPath) {
    Write-Host "ERRO: psql.exe nao encontrado em C:\Program Files\PostgreSQL\" -ForegroundColor Red
    Write-Host "Verifique se o PostgreSQL esta instalado corretamente." -ForegroundColor Red
    exit 1
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  SBBD - Setup do Banco de Dados" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "psql encontrado em: $psqlPath" -ForegroundColor Green
Write-Host ""

# Definir senha via variável de ambiente (evita prompt)
$env:PGPASSWORD = $DB_PASS

# 1. Criar o banco de dados
Write-Host "[1/3] Criando banco de dados '$DB_NAME'..." -ForegroundColor Yellow
& $psqlPath -h $DB_HOST -p $DB_PORT -U $DB_USER -c "CREATE DATABASE $DB_NAME;" 2>&1 | Out-String | ForEach-Object {
    if ($_ -match "already exists") {
        Write-Host "  -> Banco '$DB_NAME' ja existe. Continuando..." -ForegroundColor DarkYellow
    } elseif ($_ -match "CREATE DATABASE") {
        Write-Host "  -> Banco '$DB_NAME' criado com sucesso!" -ForegroundColor Green
    } else {
        Write-Host "  -> $_" -ForegroundColor Gray
    }
}

# 2. Executar o script de inicialização
Write-Host "[2/3] Criando extensoes PostGIS e tabelas..." -ForegroundColor Yellow
$initSql = Join-Path $PSScriptRoot "init_db.sql"
$result = & $psqlPath -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -f $initSql 2>&1 | Out-String
if ($LASTEXITCODE -eq 0) {
    Write-Host "  -> Extensoes e tabelas criadas com sucesso!" -ForegroundColor Green
} else {
    Write-Host "  -> Resultado:" -ForegroundColor Gray
    Write-Host $result
}

# 3. Verificar instalação
Write-Host "[3/3] Verificando instalacao..." -ForegroundColor Yellow
$check = & $psqlPath -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -c "SELECT PostGIS_Version();" 2>&1 | Out-String
if ($check -match "\d+\.\d+") {
    $version = $Matches[0]
    Write-Host "  -> PostGIS versao $version instalado!" -ForegroundColor Green
} else {
    Write-Host "  -> AVISO: Nao foi possivel verificar a versao do PostGIS" -ForegroundColor Red
    Write-Host $check
}

# Limpar variável de senha
$env:PGPASSWORD = ""

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Setup concluido!" -ForegroundColor Green
Write-Host "  Agora execute: python entrada.py" -ForegroundColor Green
Write-Host "  Acesse: http://localhost:5000" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
