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
    "voltage": (0.0, 300.0),
    "current": (0.0, 100.0),
    "power": (-50000.0, 50000.0),
}


def influx_query(flux: str, timeout: int = 15) -> list[dict]:
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
    with urllib.request.urlopen(req, timeout=timeout) as response:
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
    allowed = {"1h", "3h", "6h", "12h", "24h", "7d"}
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
    for row in influx_query(flux, timeout=8):  # live = rapid; nu blocăm request-ul
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
    "7d": "1h",
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
    measured = 0  # cate valori numerice am primit
    spikes = 0    # cate au fost respinse ca aberante (in afara limitelor fizice)
    for row in influx_query(flux, timeout=90):  # istoric = greu; rulează în fundal
        timestamp = row.get("_time")
        if not timestamp:
            continue
        point = {"time": timestamp}
        has_value = False
        for key, entity in ENTITIES.items():
            raw = row.get(entity)
            if raw is None or raw == "":
                continue  # nu a fost masurat in fereastra asta (nu e spike)
            measured += 1
            value = sanitize(key, raw)
            if value is None:
                spikes += 1  # valoare aberanta -> filtrata
                continue
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

    valid_pct = round(100.0 * (measured - spikes) / measured, 1) if measured else 100.0
    quality = {"validPct": valid_pct, "spikes": spikes, "samples": measured}
    return points, quality


# ── Cache pe istoric (stale-while-revalidate) ──
# Istoricul se schimba lent dar interogarea e scumpa (12h ~5s, 24h ~10s, 7d >15s).
# Strategie: request-ul utilizatorului NU asteapta niciodata interogarea grea.
#   - daca avem cache proaspat -> il returnam;
#   - daca e vechi/lipsa -> returnam ce avem (sau gol) si recalculam in FUNDAL.
# Asa nu apar request-uri blocate / „API indisponibil", indiferent de interval.
# 7d e foarte scump (InfluxDB scaneaza ~o saptamana de date brute, ~70s) -> TTL mare,
# il recalculam rar si doar cat e vizionat activ (vezi _warmer_loop).
TTL_BY_RANGE = {"1h": 15, "3h": 20, "6h": 30, "12h": 30, "24h": 60, "7d": 600}
DEFAULT_TTL = 30
WARM_ACTIVE_WINDOW = 600  # mentinem cald un interval cerut in ultimele 10 min

_history_cache = {}        # range -> (timestamp, points, quality)
_history_computing = {}    # range -> bool
_warm_ranges = {"12h": time.time()}  # range -> ultimul moment cerut (12h e default)
_cache_lock = threading.Lock()


def ttl_for(time_range: str) -> int:
    return TTL_BY_RANGE.get(time_range, DEFAULT_TTL)


def _recompute(time_range: str):
    try:
        points, quality = history_values(time_range)
        with _cache_lock:
            _history_cache[time_range] = (time.time(), points, quality)
    except Exception:
        pass  # pastram cache-ul vechi; reincercam la urmatoarea tura
    finally:
        with _cache_lock:
            _history_computing[time_range] = False


def _trigger(time_range: str):
    # porneste un recalcul in fundal daca nu ruleaza deja unul pentru acest interval
    with _cache_lock:
        if _history_computing.get(time_range):
            return
        _history_computing[time_range] = True
    threading.Thread(target=_recompute, args=(time_range,), daemon=True).start()


def history_values_cached(time_range: str):
    """Returneaza (points, pending, quality). Nu blocheaza pe interogarea grea."""
    now = time.time()
    with _cache_lock:
        entry = _history_cache.get(time_range)
    fresh = entry and (now - entry[0] < ttl_for(time_range))
    if not fresh:
        _trigger(time_range)  # reimprospatam in fundal
    points = entry[1] if entry else []
    quality = entry[2] if entry else None
    pending = entry is None  # inca nu avem deloc date pentru acest interval
    return points, pending, quality


def mark_range(time_range: str):
    with _cache_lock:
        _warm_ranges[time_range] = time.time()


def _warmer_loop():
    # la pornire, incalzim intervalul default ca prima incarcare sa aiba date
    _trigger("12h")
    while True:
        now = time.time()
        with _cache_lock:
            active = [r for r, t in _warm_ranges.items() if now - t < WARM_ACTIVE_WINDOW]
            stale = []
            for r in active:
                e = _history_cache.get(r)
                if (not e) or (now - e[0] > ttl_for(r) * 0.8):
                    stale.append(r)
        for r in stale:
            _trigger(r)
        time.sleep(10)


def build_summary(time_range: str):
    live_raw, updated_at = latest_values()
    history, history_pending, quality = history_values_cached(time_range)
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
        "historyPending": history_pending,
        "quality": quality,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send_json(self, status: int, payload: dict, include_body: bool = True):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data) if include_body else 0))
        self.end_headers()
        if include_body:
            self.wfile.write(data)

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/health", "/api/health"}:
            self.send_json(200, {"ok": True, "service": "eb104lab-api"}, include_body=False)
            return
        self.send_json(404, {"ok": False, "error": "not_found"}, include_body=False)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/health", "/api/health"}:
            self.send_json(200, {"ok": True, "service": "eb104lab-api"})
            return
        if parsed.path == "/api/janitza/summary":
            params = parse_qs(parsed.query)
            time_range = normalize_range(params.get("range", ["12h"])[0])
            mark_range(time_range)
            try:
                self.send_json(200, build_summary(time_range))
            except Exception as exc:
                print(f"api_error path={parsed.path} range={time_range}: {exc}", flush=True)
                self.send_json(502, {"ok": False, "error": "upstream_unavailable"})
            return
        self.send_json(404, {"ok": False, "error": "not_found"})


if __name__ == "__main__":
    warmer = threading.Thread(target=_warmer_loop, daemon=True)
    warmer.start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"eb104lab-api listening on 127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


