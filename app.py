"""Nepal GeoGuessr — a pure-Python street-view guessing game engine.

Run with:  python app.py
Then open http://127.0.0.1:8000 in your browser.

Python does ALL the logic: picking random street-view spawns across
Kathmandu (via the free Mapillary API), session state, distance
calculation and scoring. JavaScript is used only to render the map /
street view and animate the UI.

Free street view needs a Mapillary client token — see config.py.
Without it, the game falls back to hint-based landmark guessing.
"""

import argparse
import json
import math
import os
import random
import secrets
import socket
import threading
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from config import MAPILLARY_TOKEN as _CONFIG_TOKEN
    from config import GEMINI_API_KEY as _CONFIG_GEMINI
except ImportError:
    _CONFIG_TOKEN = ""
    _CONFIG_GEMINI = ""

MAPILLARY_TOKEN = os.environ.get("MAPILLARY_TOKEN", _CONFIG_TOKEN).strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", _CONFIG_GEMINI).strip()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# ---------------------------------------------------------------------------
# Game configuration
# ---------------------------------------------------------------------------

TOTAL_ROUNDS = 5
ROUND_TIME = 45  # seconds
MAX_SCORE = 5000  # points available per round
PERFECT_RADIUS = 50  # meters — a guess within this earns the full 5,000
SCORE_DROP = 5000  # meters — a guess this far away earns 0 points

# Kathmandu city core (lat/lng bounds for random street spawns)
KATHMANDU_BBOX = (85.28, 27.66, 85.38, 27.76)  # minLng, minLat, maxLng, maxLat

# Random street images are found by querying small map cells: Mapillary's
# `images` endpoint errors on large bounding boxes, but small cells work.
CELL_SIZE = 0.01  # degrees (~1.1 km) — bbox area 0.0001 sq deg
RETRIES_PER_ROUND = 8

# Landmarks used for hint-based play (no street view) and to give the
# player context for where a random spawn actually was.
LANDMARKS = [
    {"name": "Kathmandu Durbar Square", "emoji": "🏛️", "lat": 27.7042, "lng": 85.3076},
    {"name": "Thamel", "emoji": "🎒", "lat": 27.7135, "lng": 85.3119},
    {"name": "Durbar Marg", "emoji": "🛍️", "lat": 27.7146, "lng": 85.3176},
    {"name": "New Road (Ghantaghar)", "emoji": "⏰", "lat": 27.7042, "lng": 85.3158},
    {"name": "Patan Durbar Square", "emoji": "🏯", "lat": 27.6727, "lng": 85.3256},
    {"name": "Boudhanath Stupa", "emoji": "🕉️", "lat": 27.7217, "lng": 85.3619},
    {"name": "Swayambhunath Stupa", "emoji": "🙏", "lat": 27.715, "lng": 85.2904},
    {"name": "Pashupatinath Temple", "emoji": "🛕", "lat": 27.7108, "lng": 85.3487},
    {"name": "Ratna Park", "emoji": "🌳", "lat": 27.7059, "lng": 85.3180},
    {"name": "Kamal Pokhari", "emoji": "🪷", "lat": 27.7160, "lng": 85.3210},
    {"name": "Lazimpat", "emoji": "🏙️", "lat": 27.7189, "lng": 85.3180},
    {"name": "Jhamsikhel", "emoji": "☕", "lat": 27.6903, "lng": 85.3075},
]

LANDMARK_HINTS = {
    "Kathmandu Durbar Square": [
        "A royal square in the old city, guarded by a living goddess.",
        "Nine-story Basantapur Tower stands nearby.",
    ],
    "Thamel": [
        "The buzzing tourist hub — cafés, trekking shops and guesthouses.",
        "Northwest of the city centre, full of music and nightlife.",
    ],
    "Durbar Marg": [
        "A grand boulevard connecting the royal palace to the old town.",
        "Flanked by shops, embassies and the Narayanhiti Palace park.",
    ],
    "New Road (Ghantaghar)": [
        "A wide street famous for its big clock tower and textile shops.",
        "Central shopping street east of Rani Pokhari.",
    ],
    "Patan Durbar Square": [
        "A palace square across the Bagmati River, the 'City of Fine Arts'.",
        "South of Kathmandu, home of the golden Krishna temple.",
    ],
    "Boudhanath Stupa": [
        "One of the largest stupas in the world, ringed by prayer wheels.",
        "A Tibetan Buddhist hub northeast of the capital.",
    ],
    "Swayambhunath Stupa": [
        "Known as the 'Monkey Temple', perched on a hill.",
        "West of the city with all-seeing Buddha eyes.",
    ],
    "Pashupatinath Temple": [
        "A sacred Hindu temple on the banks of the Bagmati River.",
        "East of downtown, the holiest shrine in Nepal.",
    ],
    "Ratna Park": [
        "A busy public park and bus junction in central Kathmandu.",
        "South of the old palace grounds.",
    ],
    "Kamal Pokhari": [
        "A pond in a roundabout named after a lotus-filled lake.",
        "On the busy ring of roads east of Durbar Marg.",
    ],
    "Lazimpat": [
        "An upmarket diplomatic quarter full of hotels and embassies.",
        "North of the royal palace park.",
    ],
    "Jhamsikhel": [
        "A trendy dining street south of the city, near the Patan ring road.",
        "Known for restaurants and a lively night scene.",
    ],
}


def landmark_hints(name):
    return LANDMARK_HINTS.get(name, ["A well-known spot somewhere in Kathmandu."])


# ---------------------------------------------------------------------------
# Free street-view source: Mapillary Graph API (needs a client token)
# ---------------------------------------------------------------------------

def _query_cell_images(center_lng, center_lat, limit=5):
    """Query Mapillary for images inside a small cell around a point."""
    half = CELL_SIZE / 2
    url = (
        "https://graph.mapillary.com/images"
        "?fields=id,geometry,captured_at"
        f"&bbox={center_lng - half},{center_lat - half},{center_lng + half},{center_lat + half}"
        f"&limit={limit}"
    )
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"OAuth {MAPILLARY_TOKEN}")
    request.add_header("User-Agent", "NepalGeoGuessr/1.0")

    try:
        with urllib.request.urlopen(request, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
        return []

    images = []
    for item in payload.get("data", []):
        geo = item.get("geometry") or {}
        coords = geo.get("coordinates")
        if not coords or len(coords) < 2:
            continue
        images.append(
            {
                "id": item["id"],
                "lat": coords[1],
                "lng": coords[0],
                "captured_at": item.get("captured_at", ""),
            }
        )
    return images


def _random_cell():
    min_lng, min_lat, max_lng, max_lat = KATHMANDU_BBOX
    pad = CELL_SIZE / 2
    return (
        random.uniform(min_lng + pad, max_lng - pad),
        random.uniform(min_lat + pad, max_lat - pad),
    )


def fetch_random_kathmandu_images(count):
    """Return `count` random street images scattered across Kathmandu.

    Each image comes from a random small cell, so spawns are spread
    across the whole city. Cell queries run in parallel — Mapillary is
    slow per-request, so sequential calls made game start take ~40s.
    Returns a list of {"id", "lat", "lng", "captured_at"}. Empty on
    error or when no token is configured.
    """
    if not MAPILLARY_TOKEN:
        return []

    candidates = [(_random_cell()) for _ in range(count * 3)]
    picked = []

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_query_cell_images, lng, lat) for lng, lat in candidates]
        for future in futures:
            try:
                hits = future.result()
            except Exception:
                hits = []
            if hits:
                picked.append(random.choice(hits))

    random.shuffle(picked)
    return picked[:count]


# ---------------------------------------------------------------------------
# Geometry & scoring
# ---------------------------------------------------------------------------

def haversine(lat1, lng1, lat2, lng2):
    """Great-circle distance between two points, in meters."""
    r = 6371000.0
    to_rad = math.radians
    d_lat = to_rad(lat2 - lat1)
    d_lng = to_rad(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(to_rad(lat1)) * math.cos(to_rad(lat2)) * math.sin(d_lng / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def calculate_score(distance_m):
    """Full 5,000 points within 50 m, falling to 0 at 5,000 m."""
    if distance_m <= PERFECT_RADIUS:
        return MAX_SCORE
    return max(0, round(MAX_SCORE * (1 - (distance_m - PERFECT_RADIUS) / SCORE_DROP)))


def nearest_landmark(lat, lng):
    """Closest Kathmandu landmark to a point, for post-guess context."""
    best = min(
        LANDMARKS,
        key=lambda lm: haversine(lat, lng, lm["lat"], lm["lng"]),
    )
    return {
        "name": best["name"],
        "emoji": best["emoji"],
        "distance": round(haversine(lat, lng, best["lat"], best["lng"])),
    }


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

sessions = {}
_sessions_lock = threading.Lock()


class Session:
    """One running game: the 5 rounds (spawns) and the scores so far."""

    def __init__(self, mode, rounds):
        self.mode = mode  # "street" or "hint"
        self.rounds = rounds  # list of round dicts
        self.scores = [None] * TOTAL_ROUNDS

    @property
    def total_score(self):
        return sum(s for s in self.scores if s is not None)

    def spawn(self, round_index):
        """Return the data the UI needs to show the round (never the answer)."""
        r = self.rounds[round_index]
        if self.mode == "street":
            return {
                "mode": "street",
                "image_id": r["image_id"],
                "hint": r.get("hint", ""),
            }
        return {"mode": "hint", "hint": r["hint"]}

    def guess(self, round_index, lat, lng):
        if not (0 <= round_index < TOTAL_ROUNDS):
            raise ValueError("invalid round")

        r = self.rounds[round_index]
        distance = haversine(lat, lng, r["lat"], r["lng"])
        points = calculate_score(distance)
        self.scores[round_index] = points

        result = {
            "distance": round(distance),
            "points": points,
            "total_score": self.total_score,
            "lat": r["lat"],
            "lng": r["lng"],
        }
        if self.mode == "street":
            result["mode"] = "street"
            result["captured_at"] = r.get("captured_at", "")
            result["nearest_landmark"] = nearest_landmark(r["lat"], r["lng"])
        else:
            result["mode"] = "hint"
            result["name"] = r["name"]
            result["emoji"] = r.get("emoji", "📍")
        return result


def build_rounds():
    """Pick 5 rounds. Street mode if street imagery is available."""
    images = fetch_random_kathmandu_images(TOTAL_ROUNDS)
    if images:
        rounds = [
            {
                "image_id": img["id"],
                "lat": img["lat"],
                "lng": img["lng"],
                "captured_at": img["captured_at"],
            }
            for img in images
        ]

        if GEMINI_API_KEY:
            contexts = [
                f"street-view spawn at ({r['lat']}, {r['lng']}); "
                f"nearest landmark: {nearest_landmark(r['lat'], r['lng'])['name']}, "
                f"about {nearest_landmark(r['lat'], r['lng'])['distance']} m away"
                for r in rounds
            ]
            hints = gemini_generate_hints(
                "You write hints for a Kathmandu street-view guessing game. "
                "For each hidden street-view location, write ONE fair, cryptic clue (max 18 words) "
                "that hints at the neighbourhood WITHOUT naming the nearest landmark or exact place. "
                f"Return ONLY a JSON array of {len(contexts)} strings.\n\n"
                + "\n".join(f"{i + 1}. {c}" for i, c in enumerate(contexts))
            )
            if len(hints) == len(rounds):
                for r, h in zip(rounds, hints):
                    r["hint"] = h

        for r in rounds:
            r.setdefault("hint", "Look around — which part of Kathmandu is this?")
        return "street", rounds

    # Fallback: hint-based landmark guessing
    picked = random.sample(LANDMARKS, TOTAL_ROUNDS)
    rounds = [
        {
            "name": lm["name"],
            "emoji": lm["emoji"],
            "lat": lm["lat"],
            "lng": lm["lng"],
        }
        for lm in picked
    ]

    if GEMINI_API_KEY:
        contexts = [f"landmark: {r['name']} (at {r['lat']}, {r['lng']})" for r in rounds]
        hints = gemini_generate_hints(
            "For each famous Kathmandu landmark, write ONE short clue (max 18 words) describing "
            "the place so a player could identify it from a map, WITHOUT naming it. "
            f"Return ONLY a JSON array of {len(contexts)} strings.\n\n"
            + "\n".join(f"{i + 1}. {c}" for i, c in enumerate(contexts))
        )
        if len(hints) == len(rounds):
            for r, h in zip(rounds, hints):
                r["hint"] = h

    for r in rounds:
        r.setdefault("hint", random.choice(landmark_hints(r["name"])))
    return "hint", rounds


def gemini_generate_hints(prompt):
    """Ask Gemini for a JSON array of hints. Returns [] on any failure."""
    if not GEMINI_API_KEY:
        return []

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.0-flash:generateContent?key="
        + urllib.parse.quote(GEMINI_API_KEY)
    )
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.85, "maxOutputTokens": 1024},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "NepalGeoGuessr/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=25) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        text = payload["candidates"][0]["content"]["parts"][0]["text"].strip()
        # Strip Markdown code fences if present
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(h).strip() for h in parsed]
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError, IndexError):
        return []
    return []


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


class GeoGuessrHandler(BaseHTTPRequestHandler):
    server_version = "NepalGeoGuessr/1.0"

    # ---- helpers ----------------------------------------------------------

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, message, status=400):
        self._send_json({"error": message}, status)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("empty request body")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # ---- static files -----------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/config":
            self._api_config()
            return

        if path in ("/", "/index.html"):
            path = "index.html"
        elif path in ("/app.js", "/style.css"):
            path = path.lstrip("/")
        else:
            self._send_error("not found", 404)
            return

        file_path = STATIC_DIR / path
        try:
            content = file_path.read_bytes()
        except OSError:
            self._send_error("not found", 404)
            return

        self.send_response(200)
        self.send_header("Content-Type", MIME_TYPES.get(file_path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def _api_config(self):
        self._send_json(
            {
                "has_street_view": bool(MAPILLARY_TOKEN),
                "mapillary_token": MAPILLARY_TOKEN or None,
            }
        )

    # ---- API --------------------------------------------------------------

    def do_POST(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/game/start":
            self._api_start()
        elif path == "/api/round/spawn":
            self._api_spawn()
        elif path == "/api/guess":
            self._api_guess()
        else:
            self._send_error("not found", 404)

    def _api_start(self):
        session_id = secrets.token_hex(8)
        mode, rounds = build_rounds()

        with _sessions_lock:
            sessions[session_id] = Session(mode, rounds)

        self._send_json(
            {
                "session_id": session_id,
                "mode": mode,
                "total_rounds": TOTAL_ROUNDS,
                "round_time": ROUND_TIME,
                "max_score": MAX_SCORE,
                "perfect_radius": PERFECT_RADIUS,
            }
        )

    def _api_spawn(self):
        try:
            data = self._read_json()
            session_id = data.get("session_id")
            round_index = int(data["round"])
        except (ValueError, KeyError, TypeError):
            self._send_error("invalid request payload")
            return

        with _sessions_lock:
            session = sessions.get(session_id)
        if session is None:
            self._send_error("unknown session", 404)
            return
        if not (0 <= round_index < TOTAL_ROUNDS):
            self._send_error("invalid round")
            return

        self._send_json(session.spawn(round_index))

    def _api_guess(self):
        try:
            data = self._read_json()
            session_id = data.get("session_id")
            round_index = int(data["round"])
            lat = float(data["lat"])
            lng = float(data["lng"])
        except (ValueError, KeyError, TypeError):
            self._send_error("invalid request payload")
            return

        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            self._send_error("coordinates out of range")
            return

        with _sessions_lock:
            session = sessions.get(session_id)
        if session is None:
            self._send_error("unknown session", 404)
            return

        try:
            result = session.guess(round_index, lat, lng)
        except ValueError as exc:
            self._send_error(str(exc))
            return

        self._send_json(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Nepal GeoGuessr")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    host, port = args.host, args.port

    # If the port is busy, keep trying the next one instead of crashing.
    while True:
        try:
            server = ThreadingHTTPServer((host, port), GeoGuessrHandler)
            break
        except OSError as exc:
            if exc.errno != socket.errno.EADDRINUSE:
                raise
            print(f"⚠️  Port {port} is in use, trying port {port + 1}…")
            port += 1

    mode = "street view (Mapillary)" if MAPILLARY_TOKEN else "hint-based (no token)"
    print(f"🎯 Nepal GeoGuessr running at  http://{host}:{port}")
    print(f"   Mode: {mode}  |  Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()