import csv
import io
import math
import os

import requests


AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
STREAMS_URL = "https://www.strava.com/api/v3/activities/{activity_id}/streams"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

MAX_TRACK_POINTS_FOR_PLACES = 300
PLACE_NODE_MATCH_RADIUS_METERS = 200
OVERPASS_BBOX_PADDING_DEGREES = 0.015
STRAVA_CLIENT_ID = ""
STRAVA_CLIENT_SECRET = ""


def read_config(session=None):
    session = session or {}
    client_id = session.get("client_id") or os.getenv("STRAVA_CLIENT_ID") or STRAVA_CLIENT_ID
    client_secret = session.get("client_secret") or os.getenv("STRAVA_CLIENT_SECRET") or STRAVA_CLIENT_SECRET

    if not client_id or not client_secret:
        raise RuntimeError(
            "Ustaw zmienne srodowiskowe STRAVA_CLIENT_ID oraz STRAVA_CLIENT_SECRET "
            "albo podaj wartosci w aplikacji."
        )

    return client_id, client_secret


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


def fetch_activities_page(access_token, page=1, per_page=20):
    response = requests.get(
        ACTIVITIES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"per_page": per_page, "page": page},
        timeout=20,
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


def build_places_csv(activity_id, activity, places):
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    activity_date = activity.get("start_date_local", "")[:10]
    writer.writerow(["activity_id", "activity_date", "activity_name", "place"])
    for place in places:
        writer.writerow([activity_id, activity_date, activity.get("name", ""), place])
    return csv_buffer.getvalue()
