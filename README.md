# Strava Places Parser

A small FastAPI app that signs the user in with Strava OAuth, opens a local page with activities, and lets you export settlements (localities) from a selected route to CSV.

## Strava setup

1. Open <https://www.strava.com/settings/api>.
2. Create an app or use an existing one.
3. For local use, set `Authorization Callback Domain` to:

```text
localhost
```

4. Copy `Client ID` and `Client Secret`.

The app’s local callback URL is:

```text
http://localhost:8000/callback
```

## Running the web app

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Configure your Strava app either in `strava_activities.py` or with environment variables:

```powershell
$env:STRAVA_CLIENT_ID = "your-client-id"
$env:STRAVA_CLIENT_SECRET = "your-client-secret"
```

Start the app:

```powershell
python strava_activities.py
```

By default the web UI is at:

```text
http://localhost:8000/
```

You can change host, port, and the public callback base URL with environment variables:

```powershell
$env:WEB_HOST = "127.0.0.1"
$env:WEB_PORT = "9000"
$env:APP_BASE_URL = "http://localhost:9000"
python strava_activities.py
```

The callback URL then becomes:

```text
http://localhost:9000/callback
```

Or run uvicorn directly:

```powershell
uvicorn strava_activities:app --reload
```

Then open:

```text
http://localhost:8000/
```

Click **Log in with Strava**. After you approve access, Strava redirects to `http://localhost:8000/callback` and the app shows the activity table.

Select one activity and click **Export settlements** to download a CSV of unique settlements along the route.

The export uses Strava GPS points and OpenStreetMap data from the Overpass API. The app queries settlements for the route area in one request, then checks polygons and nearby settlement nodes locally.

## Desktop version

The desktop build is a separate **tkinter** windowed app. It does not use WebView or run the FastAPI server, but reuses the same logic for login, fetching activities, and exporting settlements.

The desktop client requests **20 activities per page** from the Strava API. **Next** loads the next API page; **Previous** goes back to a page already loaded in memory.

Install desktop dependencies:

```powershell
python -m pip install -r requirements-desktop.txt
```

Run:

```powershell
python desktop_app.py
```

In Strava, set `Authorization Callback Domain` to match the host in your callback URL (e.g. `localhost`).

You can keep Strava app credentials locally in `strava_desktop_config.json` next to `desktop_app.py` or next to the built `.exe`. On startup the form fields are filled from that file; you can also type or edit **Callback URL** in the UI. The callback must use `http`, and the same URL must be configured in your Strava API application settings. Using a different port than the web app (`8000`) avoids port clashes—for example:

```text
http://localhost:8765/callback
```

The desktop app does **not** read `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` from the environment; use the JSON file or the form fields.

To build a Windows folder bundle, run `build_desktop.ps1`. If `strava_desktop_config.json` exists in the project root, it is copied next to `StravaPlaces.exe` in `dist\StravaPlaces\`.

