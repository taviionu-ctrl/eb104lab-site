#!/usr/bin/env python3
import csv
import io
import json
import os
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


def history_values(time_range: str):
    # pivot => fiecare timestamp devine un rand cu toate fazele aliniate pe acelasi x
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{time_range})
  |> filter(fn: (r) => r["_field"] == "value")
  |> filter(fn: (r) => r["domain"] == "sensor")
  |> filter(fn: (r) => contains(value: r["entity_id"], set: [{ENTITY_SET}]))
  |> aggregateWindow(every: 2m, fn: mean, createEmpty: false)
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


def build_summary(time_range: str):
    live_raw, updated_at = latest_values()
    history = history_values(time_range)
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
            try:
                self.send_json(200, build_summary(time_range))
            except Exception as exc:
                self.send_json(502, {"ok": False, "error": str(exc)})
            return
        self.send_json(404, {"ok": False, "error": "not_found"})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"eb104lab-api listening on 127.0.0.1:{PORT}", flush=True)
    server.serve_forever()
