#!/usr/bin/env python3
import csv
import io
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

INFLUX_URL = os.environ.get("INFLUX_URL", "http://192.168.50.213:8086").rstrip("/")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "EB104")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "homeassistant")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN", "")
PORT = int(os.environ.get("API_PORT", "3001"))

ENTITIES = {
    "voltage_l1": "hydro_tensiune_l1n",
    "voltage_l2": "hydro_tensiune_l2n",
    "voltage_l3": "hydro_tensiune_l3n",
    "current_l1": "hydro_curent_l1",
    "current_l2": "hydro_curent_l2",
    "current_l3": "hydro_curent_l3",
    "power_l1": "hydro_putere_activa_l1",
    "power_l2": "hydro_putere_activa_l2",
    "power_l3": "hydro_putere_activa_l3",
    "power_total": "hydro_putere_activa_total",
}

ENTITY_TO_KEY = {v: k for k, v in ENTITIES.items()}
ENTITY_SET = ", ".join([f'"{entity}"' for entity in ENTITIES.values()])

# Limite fizice plauzibile — valorile din afara intervalului sunt considerate
# erori de masura (spike-uri, registre brute) si sunt eliminate inainte de afisare.
BOUNDS = {
    "voltage": (0.0, 600.0),
    "current": (0.0, 3000.0),
    "power": (-100000.0, 100000.0),
}


def influx_query(flux: str) -> list[dict]:
    if not INFLUX_TOKEN:
        raise RuntimeError("INFLUX_TOKEN is not configured")
    body = json.dumps({"query": flux, "type": "flux"}).encode("utf-8")
    req = urllib.request.Request(
        f"{INFLUX_URL}/api/v2/query?org={INFLUX_ORG}",
        data=body,
        headers={
            "Authorization": f"Token {INFLUX_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "text/csv",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        text = response.read().decode("utf-8", errors="replace")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("error"):
            raise RuntimeError(row.get("error"))
        rows.append(row)
    return rows


def to_float(value):
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def kind_for_key(key: str) -> str:
    if key.startswith("voltage"):
        return "voltage"
    if key.startswith("current"):
        return "current"
    return "power"


def sanitize(key: str, value) -> float | None:
    """Converteste si valideaza o valoare; returneaza None daca e implauzibila."""
    v = to_float(value)
    if v is None:
        return None
    lo, hi = BOUNDS[kind_for_key(key)]
    if v < lo or v > hi:
        return None
    return v


def normalize_range(value: str) -> str:
    allowed = {"1h", "3h", "6h", "12h", "24h"}
    return value if value in allowed else "12h"


def latest_values():
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -30m)
  |> filter(fn: (r) => r["_field"] == "value")
  |> filter(fn: (r) => r["domain"] == "sensor")
  |> filter(fn: (r) => contains(value: r["entity_id"], set: [{ENTITY_SET}]))
  |> group(columns: ["entity_id"])
  |> last()
'''
    result = {}
    latest_time = None
    for row in influx_query(flux):
        entity = row.get("entity_id")
        key = ENTITY_TO_KEY.get(entity)
        if not key:
            continue
        value = sanitize(key, row.get("_value"))
        if value is None:
            continue
        result[key] = value
        row_time = row.get("_time")
        if row_time and (latest_time is None or row_time > latest_time):
            latest_time = row_time
    return result, latest_time


# Bucket adaptiv: pe intervale mari folosim ferestre mai mari -> mai putine puncte,
# interogare mult mai rapida (24h trecea de 10s cu fereastra de 2m).
WINDOW_BY_RANGE = {
    "1h": "30s",
    "3h": "1m",
    "6h": "2m",
    "12h": "5m",
    "24h": "10m",
}


def history_values(time_range: str):
    window = WINDOW_BY_RANGE.get(time_range, "2m")
    # pivot => fiecare timestamp devine un rand cu toate fazele aliniate pe acelasi x
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{time_range})
  |> filter(fn: (r) => r["_field"] == "value")
  |> filter(fn: (r) => r["domain"] == "sensor")
  |> filter(fn: (r) => contains(value: r["entity_id"], set: [{ENTITY_SET}]))
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "entity_id", "_value"])
  |> group()
  |> pivot(rowKey: ["_time"], columnKey: ["entity_id"], valueColumn: "_value")
'''
    points = []
    for row in influx_query(flux):
        timestamp = row.get("_time")
        if not timestamp:
            continue
        point = {"time": timestamp}
        has_value = False
        for key, entity in ENTITIES.items():
            value = sanitize(key, row.get(entity))
            if value is not None:
                point[key] = value
                has_value = True
        if has_value:
            points.append(point)
    points.sort(key=lambda p: p["time"])

    # forward-fill: Home Assistant logheaza doar la schimbare, asa ca unele faze
    # au mai putine puncte. Purtam ultima valoare cunoscuta -> linii continue.
    last = {}
    for point in points:
        for key in ENTITIES:
            if key in point:
                last[key] = point[key]
            elif key in last:
                point[key] = last[key]
    return points


# ── Cache pe istoric ──
# Istoricul se schimba lent (puncte la 30s-10m), dar interogarea e scumpa (5-10s).
# Il pastram in cache cateva secunde, ca refresh-urile dese (la 5s) sa fie instant.
# Valorile "live" raman proaspete la fiecare request (interogare rapida, ~0.3s).
HISTORY_TTL = 30  # secunde
_history_cache = {}        # range -> (timestamp, points)
_history_computing = {}    # range -> bool
_cache_lock = threading.Lock()


def history_values_cached(time_range: str):
    now = time.time()
    with _cache_lock:
        entry = _history_cache.get(time_range)
        if entry and now - entry[0] < HISTORY_TTL:
            return entry[1]
        # daca altcineva recalculeaza deja si avem date vechi, le servim pe alea
        # (evitam mai multe interogari grele simultane pe acelasi interval)
        if _history_computing.get(time_range) and entry:
            return entry[1]
        _history_computing[time_range] = True
    try:
        points = history_values(time_range)
        with _cache_lock:
            _history_cache[time_range] = (time.time(), points)
        return points
    finally:
        with _cache_lock:
            _history_computing[time_range] = False


# ── Pre-incalzire cache ──
# Un thread de fundal recalculeaza periodic istoricul intervalelor folosite recent,
# ca request-urile utilizatorului sa gaseasca mereu cache cald (~0.2s), nu interogarea
# lenta de 5-10s. Un interval se "incalzeste" dupa ce a fost cerut cel putin o data.
WARM_INTERVAL = 25  # secunde (< HISTORY_TTL ca sa nu expire intre incalziri)
WARM_ACTIVE_WINDOW = 300  # incalzim doar intervalele cerute in ultimele 5 min
_warm_ranges = {"12h": time.time()}  # range -> ultimul moment cerut (12h e default)


def mark_range(time_range: str):
    with _cache_lock:
        _warm_ranges[time_range] = time.time()


def _warmer_loop():
    while True:
        now = time.time()
        with _cache_lock:
            ranges = [r for r, t in _warm_ranges.items() if now - t < WARM_ACTIVE_WINDOW]
        for time_range in ranges:
            try:
                points = history_values(time_range)
                with _cache_lock:
                    _history_cache[time_range] = (time.time(), points)
            except Exception:
                pass  # la urmatoarea tura reincercam
        time.sleep(WARM_INTERVAL)


def build_summary(time_range: str):
    live_raw, updated_at = latest_values()
    history = history_values_cached(time_range)
    live = {
        "voltage": {
            "l1": live_raw.get("voltage_l1"),
            "l2": live_raw.get("voltage_l2"),
            "l3": live_raw.get("voltage_l3"),
            "unit": "V",
        },
        "current": {
            "l1": live_raw.get("current_l1"),
            "l2": live_raw.get("current_l2"),
            "l3": live_raw.get("current_l3"),
            "unit": "A",
        },
        "power": {
            "l1": live_raw.get("power_l1"),
            "l2": live_raw.get("power_l2"),
            "l3": live_raw.get("power_l3"),
            "total": live_raw.get("power_total"),
            "unit": "W",
        },
    }
    return {
        "source": "Janitza / Home Assistant / InfluxDB",
        "range": time_range,
        "serverTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "updatedAt": updated_at,
        "live": live,
        "history": history,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send_json(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_json(200, {"ok": True, "service": "eb104lab-api"})
            return
        if parsed.path == "/api/janitza/summary":
            params = parse_qs(parsed.query)
            time_range = normalize_range(params.get("range", ["12h"])[0])
            mark_range(time_range)
            try:
                self.send_json(200, build_summary(time_range))
            except Exception as exc:
                self.send_json(502, {"ok": False, "error": str(exc)})
            return
        self.send_json(404, {"ok": False, "error": "not_found"})


if __name__ == "__main__":
    warmer = threading.Thread(target=_warmer_loop, daemon=True)
    warmer.start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"eb104lab-api listening on 127.0.0.1:{PORT}", flush=True)
    server.serve_forever()
