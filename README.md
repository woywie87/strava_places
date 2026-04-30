# Strava Places Parser

Prosta aplikacja FastAPI, ktora loguje uzytkownika przez Strava OAuth, otwiera lokalna strone z 10 ostatnimi aktywnosciami i pozwala wyeksportowac miejscowosci z wybranej trasy do CSV.

## Konfiguracja Stravy

1. Wejdz na <https://www.strava.com/settings/api>.
2. Utworz aplikacje albo uzyj istniejacej.
3. Lokalnie w polu `Authorization Callback Domain` wpisz:

```text
localhost
```

4. Skopiuj `Client ID` i `Client Secret`.

Callback lokalny aplikacji to:

```text
http://localhost:8000/callback
```

## Uruchomienie

Zainstaluj zaleznosci:

```powershell
python -m pip install -r requirements.txt
```

Ustaw dane aplikacji Strava w `strava_activities.py` albo jako zmienne srodowiskowe:

```powershell
$env:STRAVA_CLIENT_ID = "twoj-client-id"
$env:STRAVA_CLIENT_SECRET = "twoj-client-secret"
```

Uruchom aplikacje:

```powershell
python strava_activities.py
```

Domyslnie web startuje na:

```text
http://localhost:8000/
```

Mozesz zmienic host, port i publiczny adres callbacku przez zmienne srodowiskowe:

```powershell
$env:WEB_HOST = "127.0.0.1"
$env:WEB_PORT = "9000"
$env:APP_BASE_URL = "http://localhost:9000"
python strava_activities.py
```

Wtedy callback bedzie:

```text
http://localhost:9000/callback
```

Albo bezposrednio przez uvicorn:

```powershell
uvicorn strava_activities:app --reload
```

Nastepnie wejdz w przegladarce na:

```text
http://localhost:8000/
```

Kliknij `Zaloguj przez Strave`. Po zatwierdzeniu dostepu Strava przekieruje na `http://localhost:8000/callback`, a aplikacja pokaze tabele aktywnosci.

Zaznacz jedna aktywnosc i kliknij `Eksportuj miejscowosci`, aby pobrac plik CSV z unikalnymi miejscowosciami z trasy.

Eksport korzysta z punktow GPS Stravy oraz danych OpenStreetMap pobieranych przez Overpass API. Aplikacja pobiera miejscowosci dla obszaru trasy jednym zapytaniem, a potem lokalnie sprawdza poligony i pobliskie punkty miejscowosci.

Dla miejscowosci bez poligonu aplikacja uzywa punktu `place=*` tylko wtedy, gdy znajduje sie bardzo blisko sladu. To ogranicza falszywe trafienia pobliskich wsi/czesci wsi.

Lokalny serwer zatrzymasz w terminalu skrotem `Ctrl+C`.

## Wersja desktop

Wersja desktop jest osobna aplikacja okienkowa `tkinter`. Nie uzywa WebView i nie uruchamia serwera FastAPI, ale korzysta z tych samych funkcji do logowania, pobierania aktywnosci i eksportu miejscowosci.

Desktop odpytuje API Stravy po 20 aktywnosci na strone. `Nastepna` pobiera kolejna strone API, a `Poprzednia` wraca do juz pobranej strony z cache. Wyszukiwanie po nazwie uruchamia sie dopiero po kliknieciu lupki i resetuje liste od pierwszej strony API.

Zainstaluj zaleznosci desktopowe:

```powershell
python -m pip install -r requirements-desktop.txt
```

Uruchom:

```powershell
python desktop_app.py
```

W Stravie nadal ustaw:

```text
Authorization Callback Domain: localhost
```

Desktop domyslnie uzywa osobnego lokalnego callbacku, zeby nie kolidowac z wersja web na porcie `8000`:

```text
http://localhost:8765/callback
```

Adres callbacku mozna zmienic bezposrednio w oknie desktopowym w polu `Callback URL`, np.:

```text
http://localhost:9001/callback
```

## VPS / domena

Na serwerze ustaw publiczny adres aplikacji:

```bash
export APP_BASE_URL="https://twojadomena.pl"
export STRAVA_CLIENT_ID="twoj-client-id"
export STRAVA_CLIENT_SECRET="twoj-client-secret"
uvicorn strava_activities:app --host 127.0.0.1 --port 8000
```

W Stravie ustaw wtedy `Authorization Callback Domain` na domene, np.:

```text
twojadomena.pl
```

Callback bedzie mial postac:

```text
https://twojadomena.pl/callback
```

Produkcjnie warto uruchomic aplikacje przez `systemd` i wystawic ja przez `nginx` jako reverse proxy.
