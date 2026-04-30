import csv
import html
import io
import math
import os
import secrets
from typing import Optional
from urllib.parse import urlencode

import requests
import uvicorn
from fastapi import Cookie, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse


AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
STREAMS_URL = "https://www.strava.com/api/v3/activities/{activity_id}/streams"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))
BASE_URL = os.getenv("APP_BASE_URL", f"http://localhost:{WEB_PORT}").rstrip("/")
REDIRECT_URI = f"{BASE_URL}/callback"
MAX_TRACK_POINTS_FOR_PLACES = 300
PLACE_NODE_MATCH_RADIUS_METERS = 200
OVERPASS_BBOX_PADDING_DEGREES = 0.015
SESSION_COOKIE_NAME = "strava_places_session"

# Tymczasowo mozna wpisac tutaj dane aplikacji Strava.
# Nie wrzucaj tego pliku z prawdziwym sekretem do publicznego repozytorium.
STRAVA_CLIENT_ID = ""
STRAVA_CLIENT_SECRET = ""

app = FastAPI(title="Strava Places Parser")
sessions = {}
oauth_states = {}


def read_config(session=None):
    session = session or {}
    client_id = session.get("client_id") or os.getenv("STRAVA_CLIENT_ID") or STRAVA_CLIENT_ID
    client_secret = session.get("client_secret") or os.getenv("STRAVA_CLIENT_SECRET") or STRAVA_CLIENT_SECRET

    if not client_id or not client_secret:
        raise RuntimeError(
            "Ustaw zmienne srodowiskowe STRAVA_CLIENT_ID oraz STRAVA_CLIENT_SECRET "
            "albo wpisz tymczasowo wartosci w stale STRAVA_CLIENT_ID i STRAVA_CLIENT_SECRET."
        )

    return client_id, client_secret


def get_or_create_session_id(session_id):
    if session_id and session_id in sessions:
        return session_id

    session_id = secrets.token_urlsafe(32)
    sessions[session_id] = {}
    return session_id


def get_session(session_id):
    if not session_id or session_id not in sessions:
        raise HTTPException(status_code=401, detail="Brak aktywnej sesji.")
    return sessions[session_id]


def build_strava_login_url(session_id):
    session = get_session(session_id)
    client_id, _ = read_config(session)
    state = secrets.token_urlsafe(24)
    oauth_states[state] = session_id

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": "read,activity:read_all",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code_for_token(code, session=None):
    client_id, client_secret = read_config(session)
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def fetch_activities(access_token, limit=10):
    response = requests.get(
        ACTIVITIES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"per_page": limit, "page": 1},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def fetch_activity_latlng(access_token, activity_id):
    response = requests.get(
        STREAMS_URL.format(activity_id=activity_id),
        headers={"Authorization": f"Bearer {access_token}"},
        params={"keys": "latlng", "key_by_type": "true"},
        timeout=20,
    )
    response.raise_for_status()
    stream = response.json().get("latlng", {})
    return stream.get("data", [])


def sample_points(points, limit=MAX_TRACK_POINTS_FOR_PLACES):
    if len(points) <= limit:
        return points

    step = (len(points) - 1) / (limit - 1)
    return [points[round(index * step)] for index in range(limit)]


def calculate_bbox(points, padding=OVERPASS_BBOX_PADDING_DEGREES):
    latitudes = [point[0] for point in points]
    longitudes = [point[1] for point in points]
    return (
        min(latitudes) - padding,
        min(longitudes) - padding,
        max(latitudes) + padding,
        max(longitudes) + padding,
    )


def fetch_osm_places_for_bbox(bbox):
    south, west, north, east = bbox
    query = f"""
    [out:json][timeout:45];
    (
      node["place"~"^(city|town|village|hamlet|locality)$"]({south},{west},{north},{east});
      way["place"~"^(city|town|village|hamlet|locality)$"]({south},{west},{north},{east});
      relation["place"~"^(city|town|village|hamlet|locality)$"]({south},{west},{north},{east});
    );
    out tags geom;
    """

    response = requests.post(
        OVERPASS_URL,
        data={"data": query},
        headers={"User-Agent": "strava-places-parser/1.0"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json().get("elements", [])


def parse_osm_places(elements):
    places = []
    for element in elements:
        tags = element.get("tags", {})
        name = tags.get("name")
        place_type = tags.get("place")
        if not name or not place_type:
            continue

        if element.get("type") == "node":
            places.append(
                {
                    "name": name,
                    "type": place_type,
                    "point": (element["lat"], element["lon"]),
                    "polygons": [],
                }
            )
            continue

        polygons = extract_polygons_from_osm_element(element)
        if polygons:
            places.append(
                {
                    "name": name,
                    "type": place_type,
                    "point": None,
                    "polygons": polygons,
                }
            )

    return places


def extract_polygons_from_osm_element(element):
    if element.get("type") == "way":
        geometry = element.get("geometry", [])
        polygon = geometry_to_polygon(geometry)
        return [polygon] if polygon else []

    outer_segments = []
    for member in element.get("members", []):
        if member.get("role") not in ("outer", ""):
            continue

        segment = geometry_to_segment(member.get("geometry", []))
        if segment:
            outer_segments.append(segment)

    return assemble_polygons(outer_segments)


def geometry_to_polygon(geometry):
    segment = geometry_to_segment(geometry)
    if not segment or len(segment) < 4:
        return None

    if segment[0] != segment[-1]:
        segment.append(segment[0])

    return segment


def geometry_to_segment(geometry):
    if len(geometry) < 2:
        return None

    return [(point["lat"], point["lon"]) for point in geometry]


def assemble_polygons(segments):
    remaining_segments = [segment[:] for segment in segments]
    polygons = []

    while remaining_segments:
        ring = remaining_segments.pop(0)
        changed = True

        while changed:
            changed = False
            for index, segment in enumerate(remaining_segments):
                if ring[-1] == segment[0]:
                    ring.extend(segment[1:])
                elif ring[-1] == segment[-1]:
                    ring.extend(reversed(segment[:-1]))
                elif ring[0] == segment[-1]:
                    ring = segment[:-1] + ring
                elif ring[0] == segment[0]:
                    ring = list(reversed(segment[1:])) + ring
                else:
                    continue

                remaining_segments.pop(index)
                changed = True
                break

        if len(ring) >= 4 and ring[0] == ring[-1]:
            polygons.append(ring)

    return polygons


def point_in_polygon(point, polygon):
    lat, lon = point
    inside = False
    previous_lat, previous_lon = polygon[-1]

    for current_lat, current_lon in polygon:
        crosses = (current_lon > lon) != (previous_lon > lon)
        if crosses:
            slope_lat = (previous_lat - current_lat) * (lon - current_lon)
            slope_lat /= previous_lon - current_lon
            intersect_lat = slope_lat + current_lat
            if lat < intersect_lat:
                inside = not inside

        previous_lat, previous_lon = current_lat, current_lon

    return inside


def distance_meters(first_point, second_point):
    first_lat, first_lon = first_point
    second_lat, second_lon = second_point
    radius = 6_371_000

    first_lat_rad = math.radians(first_lat)
    second_lat_rad = math.radians(second_lat)
    delta_lat = math.radians(second_lat - first_lat)
    delta_lon = math.radians(second_lon - first_lon)

    haversine = math.sin(delta_lat / 2) ** 2
    haversine += math.cos(first_lat_rad) * math.cos(second_lat_rad) * math.sin(delta_lon / 2) ** 2

    return 2 * radius * math.asin(math.sqrt(haversine))


def find_place_for_point(point, places, point_match_radius_meters=PLACE_NODE_MATCH_RADIUS_METERS):
    containing_places = []
    for place in places:
        if any(point_in_polygon(point, polygon) for polygon in place["polygons"]):
            containing_places.append(place)

    if containing_places:
        return sorted(containing_places, key=place_priority)[0]["name"]

    nearest_place = None
    nearest_distance = None
    for place in places:
        if not place["point"]:
            continue

        distance = distance_meters(point, place["point"])
        if distance <= point_match_radius_meters and (
            nearest_distance is None or distance < nearest_distance
        ):
            nearest_place = place
            nearest_distance = distance

    return nearest_place["name"] if nearest_place else None


def find_places_for_track(points, places, point_match_radius_meters=PLACE_NODE_MATCH_RADIUS_METERS):
    matches = []

    for point_index, point in enumerate(points):
        containing_places = [
            place
            for place in places
            if any(point_in_polygon(tuple(point), polygon) for polygon in place["polygons"])
        ]
        if containing_places:
            place = sorted(containing_places, key=place_priority)[0]
            matches.append((point_index, 0, place["name"]))

    for place in places:
        if not place["point"]:
            continue

        nearest_index = None
        nearest_distance = None
        for point_index, point in enumerate(points):
            distance = distance_meters(tuple(point), place["point"])
            if nearest_distance is None or distance < nearest_distance:
                nearest_index = point_index
                nearest_distance = distance

        if nearest_distance is not None and nearest_distance <= point_match_radius_meters:
            matches.append((nearest_index, nearest_distance, place["name"]))

    unique_places = []
    seen = set()
    for _, _, place_name in sorted(matches):
        if place_name not in seen:
            seen.add(place_name)
            unique_places.append(place_name)

    return unique_places


def place_priority(place):
    priorities = {
        "hamlet": 0,
        "locality": 1,
        "village": 2,
        "town": 3,
        "city": 4,
    }
    return priorities.get(place["type"], 99)


def extract_places_from_activity(
    access_token,
    activity_id,
    point_match_radius_meters=PLACE_NODE_MATCH_RADIUS_METERS,
):
    points = fetch_activity_latlng(access_token, activity_id)
    if not points:
        return []

    sampled_points = sample_points(points)
    bbox = calculate_bbox(sampled_points)
    osm_elements = fetch_osm_places_for_bbox(bbox)
    osm_places = parse_osm_places(osm_elements)
    return find_places_for_track(sampled_points, osm_places, point_match_radius_meters)


def format_distance(meters):
    return f"{meters / 1000:.2f} km"


def format_duration(seconds):
    hours, remainder = divmod(int(seconds), 3600)
    minutes, _ = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}min"
    return f"{minutes}min"


def activity_to_row(activity):
    activity_id = str(activity["id"])
    activity_type = activity.get("sport_type") or activity.get("type", "Unknown")
    return {
        "id": activity_id,
        "date": activity.get("start_date_local", "")[:10],
        "type": activity_type,
        "distance": format_distance(activity.get("distance", 0)),
        "duration": format_duration(activity.get("moving_time", 0)),
        "name": activity.get("name", "Bez nazwy"),
    }


def render_login_page(client_id=""):
    escaped_client_id = html.escape(client_id or os.getenv("STRAVA_CLIENT_ID") or STRAVA_CLIENT_ID)

    return f"""<!doctype html>
<html lang="pl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Strava Places Parser</title>
    <style>
        body {{
            align-items: center;
            background: #f5f6f8;
            color: #111827;
            display: flex;
            font-family: Arial, Helvetica, sans-serif;
            justify-content: center;
            margin: 0;
            min-height: 100vh;
        }}
        main {{
            background: #fff;
            border-radius: 16px;
            box-shadow: 0 16px 40px rgba(15, 23, 42, 0.08);
            max-width: 520px;
            padding: 32px;
        }}
        h1, p {{
            text-align: center;
        }}
        form {{
            display: flex;
            flex-direction: column;
            gap: 12px;
            margin-top: 20px;
        }}
        label {{
            color: #374151;
            font-size: 14px;
            font-weight: 700;
            text-align: left;
        }}
        input {{
            border: 1px solid #e5e7eb;
            border-radius: 10px;
            font-size: 15px;
            padding: 11px 12px;
        }}
        button {{
            background: #fc4c02;
            border: 0;
            border-radius: 999px;
            color: #fff;
            cursor: pointer;
            font-size: 15px;
            font-weight: 700;
            margin-top: 12px;
            padding: 12px 20px;
        }}
        p {{
            color: #6b7280;
        }}
        .hint {{
            font-size: 13px;
            margin: 0;
            text-align: left;
        }}
    </style>
</head>
<body>
    <main>
        <h1>Strava Places Parser</h1>
        <p>Zaloguj sie przez Strave, aby zobaczyc 10 ostatnich aktywnosci i wyeksportowac miejscowosci z trasy.</p>
        <form method="post" action="/set-strava-config">
            <label for="clientId">Client ID</label>
            <input id="clientId" name="client_id" value="{escaped_client_id}" autocomplete="off" required>

            <label for="clientSecret">Client Secret</label>
            <input id="clientSecret" name="client_secret" type="password" autocomplete="current-password" required>

            <p class="hint">Dla lokalnego uruchomienia ustaw w Stravie callback domain na <strong>localhost</strong>.</p>
            <button type="submit">Zaloguj przez Strave</button>
        </form>
    </main>
</body>
</html>"""


def render_activities_page(activities):
    rows = "\n".join(render_activity_row(activity_to_row(activity)) for activity in activities)
    if not rows:
        rows = """
        <tr>
            <td colspan="6" class="empty">Nie znaleziono aktywnosci.</td>
        </tr>
        """

    return f"""<!doctype html>
<html lang="pl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Strava - aktywnosci</title>
    <style>
        :root {{
            color-scheme: light;
            font-family: Arial, Helvetica, sans-serif;
            --accent: #fc4c02;
            --border: #e5e7eb;
            --muted: #6b7280;
        }}
        body {{
            background: #f5f6f8;
            margin: 0;
            padding: 32px;
            color: #111827;
        }}
        main {{
            max-width: 1100px;
            margin: 0 auto;
            background: #fff;
            border-radius: 16px;
            box-shadow: 0 16px 40px rgba(15, 23, 42, 0.08);
            padding: 28px;
        }}
        h1 {{
            margin: 0 0 6px;
            font-size: 28px;
        }}
        .hint {{
            margin: 0 0 24px;
            color: var(--muted);
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            overflow: hidden;
            border-radius: 12px;
        }}
        th, td {{
            border-bottom: 1px solid var(--border);
            padding: 13px 14px;
            text-align: left;
        }}
        th {{
            background: #f9fafb;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            color: #4b5563;
        }}
        tr:hover td {{
            background: #fff7ed;
        }}
        .number {{
            text-align: right;
            white-space: nowrap;
        }}
        .select {{
            width: 44px;
            text-align: center;
        }}
        .empty {{
            color: var(--muted);
            text-align: center;
        }}
        .actions {{
            display: flex;
            align-items: center;
            gap: 16px;
            margin-top: 22px;
        }}
        .export-options {{
            align-items: flex-end;
            display: flex;
            flex-wrap: wrap;
            gap: 16px;
            margin-top: 22px;
        }}
        .field {{
            display: flex;
            flex-direction: column;
            gap: 6px;
        }}
        .field label {{
            color: #374151;
            font-size: 14px;
            font-weight: 700;
        }}
        .field input {{
            border: 1px solid var(--border);
            border-radius: 10px;
            font-size: 15px;
            padding: 10px 12px;
            width: 150px;
        }}
        .field small {{
            color: var(--muted);
        }}
        button {{
            background: var(--accent);
            border: 0;
            border-radius: 999px;
            color: white;
            cursor: pointer;
            font-size: 15px;
            font-weight: 700;
            padding: 12px 20px;
        }}
        button:disabled {{
            cursor: wait;
            opacity: 0.7;
        }}
        #status {{
            color: var(--muted);
        }}
    </style>
</head>
<body>
    <main>
        <h1>10 ostatnich aktywnosci Strava</h1>
        <p class="hint">Zaznacz jedna aktywnosc i kliknij eksport, aby pobrac CSV z miejscowosciami z trasy.</p>
        <form method="post" action="/export" id="exportForm">
            <table>
                <thead>
                    <tr>
                        <th class="select">Wybierz</th>
                        <th>Data</th>
                        <th>Typ</th>
                        <th>Dystans</th>
                        <th>Czas</th>
                        <th>Nazwa</th>
                    </tr>
                </thead>
                <tbody>
                    {rows}
                </tbody>
            </table>
            <div class="export-options">
                <div class="field">
                    <label for="pointMatchRadius">Promien dopasowania punktow OSM</label>
                    <input
                        id="pointMatchRadius"
                        name="point_match_radius_meters"
                        type="number"
                        min="0"
                        max="5000"
                        step="50"
                        value="{PLACE_NODE_MATCH_RADIUS_METERS}"
                    >
                    <small>Domyslnie {PLACE_NODE_MATCH_RADIUS_METERS} m. Dotyczy miejscowosci bez poligonu.</small>
                </div>
                <div class="actions">
                    <button type="submit" id="exportButton">Eksportuj miejscowosci</button>
                    <span id="status">Eksport moze potrwac kilkadziesiat sekund.</span>
                </div>
            </div>
        </form>
    </main>
    <script>
        document.getElementById("exportForm").addEventListener("submit", function (event) {{
            const selected = document.querySelector("input[name='activity_id']:checked");
            if (!selected) {{
                event.preventDefault();
                alert("Najpierw zaznacz jedna aktywnosc.");
                return;
            }}

            document.getElementById("exportButton").disabled = true;
            document.getElementById("status").textContent = "Eksportuje miejscowosci...";
        }});
    </script>
</body>
</html>"""


def render_activity_row(row):
    activity_id = html.escape(row["id"])
    return f"""
    <tr onclick="document.getElementById('activity-{activity_id}').checked = true;">
        <td class="select">
            <input id="activity-{activity_id}" type="radio" name="activity_id" value="{activity_id}">
        </td>
        <td>{html.escape(row["date"])}</td>
        <td>{html.escape(row["type"])}</td>
        <td class="number">{html.escape(row["distance"])}</td>
        <td class="number">{html.escape(row["duration"])}</td>
        <td>{html.escape(row["name"])}</td>
    </tr>
    """


def render_error_page(title, message):
    return f"""<!doctype html>
<html lang="pl">
<head>
    <meta charset="utf-8">
    <title>{html.escape(title)}</title>
</head>
<body style="font-family: Arial, sans-serif; padding: 32px;">
    <h1>{html.escape(title)}</h1>
    <p>{html.escape(message)}</p>
    <p><a href="/">Wroc do aplikacji</a></p>
</body>
</html>"""


def normalize_point_match_radius(radius_meters):
    return max(0, min(radius_meters, 5000))


def build_places_csv(activity_id, activity, places):
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    activity_date = activity.get("start_date_local", "")[:10]
    writer.writerow(["activity_id", "activity_date", "activity_name", "place"])
    for place in places:
        writer.writerow([activity_id, activity_date, activity.get("name", ""), place])
    return csv_buffer.getvalue()


@app.get("/", response_class=HTMLResponse)
def index(strava_places_session: Optional[str] = Cookie(default=None)):
    session = sessions.get(strava_places_session or "")
    if not session or "access_token" not in session:
        client_id = session.get("client_id") if session else ""
        return HTMLResponse(render_login_page(client_id=client_id))

    try:
        activities = fetch_activities(session["access_token"], limit=10)
    except requests.RequestException as error:
        return HTMLResponse(render_error_page("Blad Stravy", f"Nie udalo sie pobrac aktywnosci: {error}"), status_code=502)

    session["activities"] = activities
    return HTMLResponse(render_activities_page(activities))


@app.post("/set-strava-config")
def set_strava_config(
    client_id: str = Form(...),
    client_secret: str = Form(...),
    strava_places_session: Optional[str] = Cookie(default=None),
):
    session_id = get_or_create_session_id(strava_places_session)
    sessions[session_id]["client_id"] = client_id.strip()
    sessions[session_id]["client_secret"] = client_secret.strip()

    response = RedirectResponse("/login", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/login")
def login(strava_places_session: Optional[str] = Cookie(default=None)):
    session_id = get_or_create_session_id(strava_places_session)
    login_url = build_strava_login_url(session_id)
    response = RedirectResponse(login_url)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/callback")
def callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    if error:
        return HTMLResponse(render_error_page("Logowanie odrzucone", error), status_code=400)

    if not code or not state or state not in oauth_states:
        return HTMLResponse(render_error_page("Niepoprawny callback", "Brakuje kodu autoryzacji albo stanu OAuth."), status_code=400)

    session_id = oauth_states.pop(state)
    sessions.setdefault(session_id, {})
    session = sessions[session_id]

    try:
        access_token = exchange_code_for_token(code, session)
    except requests.RequestException as request_error:
        return HTMLResponse(render_error_page("Blad logowania", f"Nie udalo sie pobrac tokenu: {request_error}"), status_code=502)

    session["access_token"] = access_token
    response = RedirectResponse("/")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/export")
def export_places(
    activity_id: str = Form(...),
    point_match_radius_meters: int = Form(PLACE_NODE_MATCH_RADIUS_METERS),
    strava_places_session: Optional[str] = Cookie(default=None),
):
    session = get_session(strava_places_session)
    access_token = session.get("access_token")
    if not access_token:
        return HTMLResponse(render_error_page("Brak logowania", "Zaloguj sie ponownie przez Strave."), status_code=401)

    activities = session.get("activities") or fetch_activities(access_token, limit=10)
    activities_by_id = {str(activity["id"]): activity for activity in activities}
    activity = activities_by_id.get(activity_id)
    if not activity:
        return HTMLResponse(render_error_page("Brak aktywnosci", "Nie wybrano poprawnej aktywnosci."), status_code=400)

    point_match_radius_meters = normalize_point_match_radius(point_match_radius_meters)

    try:
        places = extract_places_from_activity(
            access_token,
            activity_id,
            point_match_radius_meters,
        )
    except requests.RequestException as request_error:
        return HTMLResponse(render_error_page("Blad eksportu", f"Blad sieci/API: {request_error}"), status_code=502)

    if not places:
        return HTMLResponse(
            render_error_page(
                "Brak miejscowosci",
                "Ta aktywnosc nie ma punktow GPS albo nie udalo sie znalezc miejscowosci.",
            ),
            status_code=404,
        )

    csv_content = build_places_csv(activity_id, activity, places)
    filename = f"miejscowosci_{activity_id}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(
        io.BytesIO(csv_content.encode("utf-8-sig")),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


@app.get("/logout")
def logout(strava_places_session: Optional[str] = Cookie(default=None)):
    if strava_places_session:
        sessions.pop(strava_places_session, None)

    response = RedirectResponse("/")
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


if __name__ == "__main__":
    print(f"Aplikacja startuje pod adresem: {BASE_URL}")
    print(f"Callback Stravy ustaw na: {REDIRECT_URI}")
    uvicorn.run("strava_activities:app", host=WEB_HOST, port=WEB_PORT, reload=True)
