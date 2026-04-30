$ErrorActionPreference = "Stop"

python -m pip install -r requirements-desktop.txt
python -m pip install pyinstaller

python -m PyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --windowed `
  --exclude-module fastapi `
  --exclude-module uvicorn `
  --exclude-module pydantic `
  --exclude-module starlette `
  --exclude-module matplotlib `
  --exclude-module IPython `
  --exclude-module numpy `
  --name StravaPlaces `
  desktop_app.py

$outDir = Join-Path $PSScriptRoot "dist\StravaPlaces"
$configDest = Join-Path $outDir "strava_desktop_config.json"
$localConfig = Join-Path $PSScriptRoot "strava_desktop_config.json"

if (Test-Path $localConfig) {
  Copy-Item -LiteralPath $localConfig -Destination $configDest -Force
  Write-Host "Skopiowano strava_desktop_config.json do paczki (obok exe)."
} else {
  Write-Warning "Brak strava_desktop_config.json w katalogu projektu - dodaj plik obok StravaPlaces.exe przed uruchomieniem albo zbuduj ponownie po utworzeniu pliku w projekcie."
}

Write-Host ""
Write-Host 'Gotowe. Aplikacja jest w: dist\StravaPlaces\StravaPlaces.exe'
