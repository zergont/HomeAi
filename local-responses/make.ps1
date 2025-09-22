# make.ps1 - Developer shortcuts for PowerShell
param(
  [Parameter(Position=0)] [string]$Target = "Help",
  [string]$Msg = "migration"
)

$ErrorActionPreference = "Stop"

function Exec($cmd) {
  Write-Host "→ $cmd" -ForegroundColor Cyan
  & powershell -NoLogo -NoProfile -Command $cmd
  if ($LASTEXITCODE -ne 0) { throw "Command failed: $cmd" }
}

switch ($Target) {
  "Help" { Write-Host "Targets: Init, Run, Dev, Alembic-Init, Alembic-Rev, Alembic-Up, Fmt, Lint, Test"; break }
  "Init" {
    Exec "py -3.13 -m venv .venv"
    Exec ".\.venv\\Scripts\\Activate.ps1; python -m pip install --upgrade pip"
    Exec ".\.venv\\Scripts\\Activate.ps1; pip install -e .[dev]"
    break
  }
  "Run" {
    Exec ".\.venv\\Scripts\\Activate.ps1; uvicorn apps.api.main:app --host 127.0.0.1 --port 8000"
    break
  }
  "Dev" {
    Exec ".\.venv\\Scripts\\Activate.ps1; uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8000"
    break
  }
  "Alembic-Init" {
    if (-Not (Test-Path alembic.ini)) {
      Exec ".\.venv\\Scripts\\Activate.ps1; alembic init alembic"
    } else {
      Write-Host "alembic already initialized" -ForegroundColor Yellow
    }
    break
  }
  "Alembic-Rev" {
    Exec ".\.venv\\Scripts\\Activate.ps1; $env:DB_URL=\"$((Get-Content .env | Select-String '^DB_URL=').ToString().Split('=')[1])\"; alembic revision -m \"$Msg\""
    break
  }
  "Alembic-Up" {
    if (-Not (Test-Path data)) { New-Item -ItemType Directory -Path data | Out-Null }
    Exec ".\.venv\\Scripts\\Activate.ps1; if (Test-Path .env) { foreach ($line in Get-Content .env) { if ($line -match '^DB_URL=') { $env:DB_URL = $line.Substring(7) } } }; alembic upgrade head"
    break
  }
  "Fmt" {
    Exec ".\.venv\\Scripts\\Activate.ps1; ruff format ."
    break
  }
  "Lint" {
    Exec ".\.venv\\Scripts\\Activate.ps1; ruff check ."
    break
  }
  "Test" {
    Exec ".\.venv\\Scripts\\Activate.ps1; pytest"
    break
  }
  default { Write-Host "Unknown target: $Target" -ForegroundColor Red; exit 1 }
}
