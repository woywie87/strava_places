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

Write-Host ""
Write-Host "Gotowe. Aplikacja jest w: dist\StravaPlaces\StravaPlaces.exe"
