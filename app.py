#!/usr/bin/env python3
"""
Lawn Sprinkler Flask Backend
Runs on Raspberry Pi and controls GPIO pins for zone solenoids.
Serves the sprinkler.html frontend and exposes a REST API.

Install dependencies:
    pip install flask flask-cors requests gpiod

Run:
    python3 app.py
"""

import json
import threading
import time
import logging
import requests
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# ── GPIO setup (gpiod v2 API) ─────────────────────────────────────────────────
try:
    import gpiod
    from gpiod.line import Direction, Value
    _chip_path = '/dev/gpiochip0'
    # Verify chip is accessible
    with gpiod.Chip(_chip_path) as _c:
        pass
    ON_PI = True
except (ImportError, Exception) as e:
    ON_PI = False
    gpiod = None
    logging.warning(f"gpiod not available – running in simulation mode. ({e})")

# ── App & config ─────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=".")
CORS(app)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent / "config.json"

# Default zone config – overwritten by saved data on startup
DEFAULT_ZONES = [
    {"id": 1, "name": "Front Lawn",    "gpio": 4,  "duration": 10, "enabled": True},
    {"id": 2, "name": "Back Lawn",     "gpio": 17, "duration": 15, "enabled": True},
    {"id": 3, "name": "Side Garden",   "gpio": 27, "duration": 8,  "enabled": True},
    {"id": 4, "name": "Flower Beds",   "gpio": 22, "duration": 6,  "enabled": True},
    {"id": 5, "name": "Veggie Patch",  "gpio": 10, "duration": 12, "enabled": True},
    {"id": 6, "name": "Driveway Edge", "gpio": 9,  "duration": 5,  "enabled": True},
    {"id": 7, "name": "Back Garden",   "gpio": 11, "duration": 10, "enabled": True},
]

DEFAULT_SCHEDULES = [
    {"id": 1, "time": "06:00", "days": ["Mon","Wed","Fri"], "zones": [1,2,3], "enabled": True},
    {"id": 2, "time": "19:00", "days": ["Sat","Sun"],       "zones": [4,5,6,7], "enabled": True},
]

DEFAULT_WEATHER = {
    "api_key": "",
    "location": "",
    "skip_rain": True,
    "rain_threshold": 40,
    "skip_cold": True,
    "cold_threshold": 35,
    "skip_wind": False,
    "wind_threshold": 20,
}

# ── Persistent state ──────────────────────────────────────────────────────────
def load_data():
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return {
        "zones": DEFAULT_ZONES,
        "schedules": DEFAULT_SCHEDULES,
        "weather": DEFAULT_WEATHER,
        "active_high": True,
        "master_enabled": True,
        "last_skip": None,
        "force_skip_next": False,
    }

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(state, f, indent=2)

state = load_data()

# Ensure keys added in later versions exist in older config files
if "last_skip" not in state:
    state["last_skip"] = None
if "force_skip_next" not in state:
    state["force_skip_next"] = False

# ── GPIO helpers (gpiod v2) ───────────────────────────────────────────────────
def _gpio_set(pin: int, value: bool):
    """Set a GPIO pin high or low using gpiod v2 API."""
    if not ON_PI:
        log.info(f"[SIM] GPIO {pin} → {'HIGH' if value else 'LOW'}")
        return
    try:
        with gpiod.request_lines(
            _chip_path,
            consumer="sprinkler",
            config={pin: gpiod.LineSettings(direction=Direction.OUTPUT)},
        ) as req:
            req.set_value(pin, Value.ACTIVE if value else Value.INACTIVE)
    except Exception as e:
        log.error(f"GPIO error on pin {pin}: {e}")

def _relay_on(pin: int):
    _gpio_set(pin, True if state["active_high"] else False)

def _relay_off(pin: int):
    _gpio_set(pin, False if state["active_high"] else True)

def init_gpio():
    """Drive all zone pins to their OFF state on startup."""
    for zone in state["zones"]:
        _relay_off(zone["gpio"])
        log.info(f"Zone {zone['id']} ({zone['name']}) → GPIO {zone['gpio']} initialised")

init_gpio()

# ── Zone run state ────────────────────────────────────────────────────────────
running: dict = {}   # zone_id → {thread, stop_event, start, end, duration}
run_lock = threading.Lock()

# Tracks whether a "run all" sequence is in progress so the UI can reflect it
run_all_active = threading.Event()

def _run_zone_thread(zone_id: int, duration_secs: int, stop_event: threading.Event):
    zone = next((z for z in state["zones"] if z["id"] == zone_id), None)
    if not zone:
        return
    pin = zone["gpio"]
    log.info(f"Zone {zone_id} ({zone['name']}) ON  – pin {pin} for {duration_secs}s")
    _relay_on(pin)
    stop_event.wait(timeout=duration_secs)
    _relay_off(pin)
    log.info(f"Zone {zone_id} ({zone['name']}) OFF – pin {pin}")
    with run_lock:
        running.pop(zone_id, None)

def start_zone(zone_id: int, duration_secs=None) -> dict:
    zone = next((z for z in state["zones"] if z["id"] == zone_id), None)
    if not zone:
        return {"error": "Zone not found"}
    if not state["master_enabled"]:
        return {"error": "Master switch is disabled"}
    if not zone["enabled"]:
        return {"error": "Zone is disabled"}

    secs = duration_secs or zone["duration"] * 60

    with run_lock:
        # Stop any currently running zones before starting the new one
        for info in running.values():
            info["stop_event"].set()
        # Wait briefly for threads to shut down and relays to turn off
        if running:
            run_lock.release()
            time.sleep(0.5)
            run_lock.acquire()

        stop_event = threading.Event()
        t = threading.Thread(
            target=_run_zone_thread,
            args=(zone_id, secs, stop_event),
            daemon=True,
        )
        now = time.time()
        running[zone_id] = {
            "start": now,
            "end": now + secs,
            "duration": secs,
            "stop_event": stop_event,
            "thread": t,
        }
        t.start()

    return {"ok": True, "zone_id": zone_id, "duration_secs": secs}


def stop_zone(zone_id: int) -> dict:
    with run_lock:
        info = running.get(zone_id)
        if not info:
            return {"error": "Zone not running"}
        info["stop_event"].set()
    return {"ok": True, "zone_id": zone_id}

def stop_all():
    with run_lock:
        for info in running.values():
            info["stop_event"].set()

# ── Run-all sequence ──────────────────────────────────────────────────────────
def _run_all_thread():
    """
    Runs every enabled zone sequentially, each for its configured duration.
    Bypasses weather skip (manual override). Respects master switch and
    individual zone enabled flags.
    """
    run_all_active.set()
    log.info("Run-all sequence started")
    try:
        for zone in state["zones"]:
            # Re-check master each iteration in case it was disabled mid-run
            if not state["master_enabled"]:
                log.info("Run-all aborted: master switch disabled")
                break
            if not zone["enabled"]:
                log.info(f"Run-all: skipping zone {zone['id']} ({zone['name']}) – disabled")
                continue

            secs = zone["duration"] * 60
            result = start_zone(zone["id"], secs)
            if "error" in result:
                log.warning(f"Run-all: zone {zone['id']} skipped – {result['error']}")
                continue

            # Wait for this zone to finish (poll so stop_all can interrupt)
            zone_id = zone["id"]
            while True:
                with run_lock:
                    info = running.get(zone_id)
                if info is None:
                    break   # Zone finished naturally
                if not state["master_enabled"]:
                    stop_all()
                    break
                time.sleep(1)

            # Brief gap between zones
            time.sleep(2)
    finally:
        run_all_active.clear()
        log.info("Run-all sequence complete")

# ── Weather skip helpers ───────────────────────────────────────────────────────
def _should_skip_weather() -> bool:
    w = state.get("weather", {})
    cached = w.get("_cached", {})
    if not cached:
        return False
    if w.get("skip_rain") and cached.get("is_rainy"):
        return True
    if w.get("skip_cold") and cached.get("temp", 999) < w.get("cold_threshold", 35):
        return True
    if w.get("skip_wind") and cached.get("wind", 0) > w.get("wind_threshold", 20):
        return True
    return False

def _skip_reason() -> str:
    w = state.get("weather", {})
    cached = w.get("_cached", {})
    if w.get("skip_rain") and cached.get("is_rainy"):
        return "Rain detected"
    if w.get("skip_cold") and cached.get("temp", 999) < w.get("cold_threshold", 35):
        return f"Temp {cached.get('temp')}°F below {w.get('cold_threshold')}°F"
    if w.get("skip_wind") and cached.get("wind", 0) > w.get("wind_threshold", 20):
        return f"Wind {cached.get('wind')}mph above {w.get('wind_threshold')}mph"
    return "Weather conditions"

# ── Scheduler ─────────────────────────────────────────────────────────────────
def _scheduler_loop():
    last_triggered: set = set()
    while True:
        now = datetime.now()
        current_day = now.strftime("%a")
        current_time = now.strftime("%H:%M")
        minute_key = (current_day, current_time)

        if minute_key not in last_triggered:
            for sched in state["schedules"]:
                if (sched["enabled"]
                        and sched["time"] == current_time
                        and current_day in sched["days"]):
                    log.info(f"Scheduler firing: schedule {sched['id']}")

                    # Check force-skip flag first
                    if state.get("force_skip_next"):
                        reason = "Manually forced skip"
                        log.info(f"Skipping due to force skip flag")
                        state["force_skip_next"] = False
                        state["last_skip"] = {
                            "time": datetime.now().isoformat(),
                            "schedule_id": sched["id"],
                            "reason": reason,
                        }
                        save_data()
                        continue

                    if _should_skip_weather():
                        reason = _skip_reason()
                        log.info(f"Skipping due to weather: {reason}")
                        state["last_skip"] = {
                            "time": datetime.now().isoformat(),
                            "schedule_id": sched["id"],
                            "reason": reason,
                        }
                        save_data()
                        continue

                    for zone_id in sched["zones"]:
                        start_zone(zone_id)
                        z = next((z for z in state["zones"] if z["id"] == zone_id), None)
                        if z:
                            time.sleep(z["duration"] * 60 + 2)
            last_triggered.add(minute_key)
            if len(last_triggered) > 10:
                last_triggered.pop()

        time.sleep(15)

threading.Thread(target=_scheduler_loop, daemon=True).start()
log.info("Scheduler started")

# ── Weather fetcher ───────────────────────────────────────────────────────────
def fetch_weather_cache():
    w = state.get("weather", {})
    key = w.get("api_key", "")
    loc = w.get("location", "")
    if not key or not loc:
        return None
    try:
        url = (
            f"https://api.openweathermap.org/data/2.5/weather"
            f"?q={loc}&appid={key}&units=imperial"
        )
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("cod") != 200:
            log.warning(f"Weather API error: {data.get('message')}")
            return None
        cached = {
            "temp": round(data["main"]["temp"]),
            "humidity": data["main"]["humidity"],
            "wind": round(data["wind"]["speed"]),
            "description": data["weather"][0]["description"],
            "main": data["weather"][0]["main"],
            "is_rainy": data["weather"][0]["main"] in ["Rain","Drizzle","Thunderstorm","Snow"],
            "fetched_at": datetime.now().isoformat(),
        }
        state["weather"]["_cached"] = cached
        save_data()
        return cached
    except Exception as e:
        log.error(f"Weather fetch failed: {e}")
        return None

def _weather_refresh_loop():
    while True:
        fetch_weather_cache()
        time.sleep(30 * 60)

threading.Thread(target=_weather_refresh_loop, daemon=True).start()

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "sprinkler.html")

@app.route("/api/zones", methods=["GET"])
def get_zones():
    return jsonify(state["zones"])

@app.route("/api/zones", methods=["POST"])
def update_zones():
    zones = request.json
    if not isinstance(zones, list):
        return jsonify({"error": "Expected list"}), 400
    state["zones"] = zones
    for zone in zones:
        _relay_off(zone["gpio"])
    save_data()
    return jsonify({"ok": True})

@app.route("/api/zones/<int:zone_id>", methods=["PATCH"])
def patch_zone(zone_id):
    zone = next((z for z in state["zones"] if z["id"] == zone_id), None)
    if not zone:
        return jsonify({"error": "Not found"}), 404
    for k, v in request.json.items():
        zone[k] = v
    save_data()
    return jsonify(zone)

@app.route("/api/zones/<int:zone_id>/run", methods=["POST"])
def run_zone(zone_id):
    body = request.get_json(silent=True, force=True) or {}
    duration_secs = body.get("duration_secs")
    result = start_zone(zone_id, duration_secs)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)

@app.route("/api/zones/<int:zone_id>/stop", methods=["POST"])
def stop_zone_route(zone_id):
    result = stop_zone(zone_id)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)

@app.route("/api/stop_all", methods=["POST"])
def stop_all_route():
    stop_all()
    return jsonify({"ok": True})

@app.route("/api/run_all", methods=["POST"])
def run_all_route():
    """
    Start all enabled zones sequentially in a background thread.
    Bypasses weather skip (manual override). Respects master switch and
    per-zone enabled flags.
    """
    if not state["master_enabled"]:
        return jsonify({"error": "Master switch is disabled"}), 400
    if run_all_active.is_set():
        return jsonify({"error": "Run-all sequence already in progress"}), 409
    t = threading.Thread(target=_run_all_thread, daemon=True)
    t.start()
    return jsonify({"ok": True})

@app.route("/api/force_skip", methods=["POST"])
def force_skip_route():
    """
    Toggle the force-skip-next flag. When armed, the next scheduled run
    will be skipped regardless of weather, then the flag clears automatically.
    POST with {"cancel": true} to disarm without waiting for a run.
    """
    body = request.get_json(silent=True, force=True) or {}
    if body.get("cancel"):
        state["force_skip_next"] = False
    else:
        state["force_skip_next"] = not state.get("force_skip_next", False)
    save_data()
    return jsonify({"ok": True, "force_skip_next": state["force_skip_next"]})

@app.route("/api/status", methods=["GET"])
def status():
    now = time.time()
    with run_lock:
        active = {
            zone_id: {
                "elapsed": round(now - info["start"]),
                "remaining": max(0, round(info["end"] - now)),
                "duration": info["duration"],
            }
            for zone_id, info in running.items()
        }
    return jsonify({
        "running": active,
        "master_enabled": state["master_enabled"],
        "active_high": state["active_high"],
        "on_pi": ON_PI,
        "last_skip": state.get("last_skip"),
        "force_skip_next": state.get("force_skip_next", False),
        "run_all_active": run_all_active.is_set(),
    })

@app.route("/api/schedules", methods=["GET"])
def get_schedules():
    return jsonify(state["schedules"])

@app.route("/api/schedules", methods=["POST"])
def add_schedule():
    sched = request.json
    existing_ids = [s["id"] for s in state["schedules"]]
    sched["id"] = max(existing_ids, default=0) + 1
    state["schedules"].append(sched)
    save_data()
    return jsonify(sched), 201

@app.route("/api/schedules/<int:sched_id>", methods=["PATCH"])
def patch_schedule(sched_id):
    sched = next((s for s in state["schedules"] if s["id"] == sched_id), None)
    if not sched:
        return jsonify({"error": "Not found"}), 404
    for k, v in request.json.items():
        sched[k] = v
    save_data()
    return jsonify(sched)

@app.route("/api/schedules/<int:sched_id>", methods=["DELETE"])
def delete_schedule(sched_id):
    before = len(state["schedules"])
    state["schedules"] = [s for s in state["schedules"] if s["id"] != sched_id]
    if len(state["schedules"]) == before:
        return jsonify({"error": "Not found"}), 404
    save_data()
    return jsonify({"ok": True})

@app.route("/api/weather", methods=["GET"])
def get_weather():
    return jsonify(state.get("weather", {}))

@app.route("/api/weather", methods=["POST"])
def update_weather():
    body = request.json or {}
    state["weather"].update({k: v for k, v in body.items() if k != "_cached"})
    save_data()
    cached = fetch_weather_cache()
    return jsonify({"ok": True, "cached": cached})

@app.route("/api/system", methods=["GET"])
def get_system():
    return jsonify({
        "master_enabled": state["master_enabled"],
        "active_high": state["active_high"],
        "on_pi": ON_PI,
    })

@app.route("/api/system", methods=["POST"])
def update_system():
    body = request.json or {}
    if "master_enabled" in body:
        state["master_enabled"] = bool(body["master_enabled"])
        if not state["master_enabled"]:
            stop_all()
    if "active_high" in body:
        state["active_high"] = bool(body["active_high"])
    save_data()
    return jsonify({"ok": True})

@app.route("/api/logs", methods=["GET"])
def get_logs():
    log_file = Path(__file__).parent / "sprinkler.log"
    if log_file.exists():
        lines = log_file.read_text().splitlines()[-100:]
        return jsonify({"lines": lines})
    return jsonify({"lines": ["No log file yet"]})

# ── Cleanup on exit ────────────────────────────────────────────────────────────
import atexit
@atexit.register
def cleanup():
    stop_all()
    time.sleep(0.5)
    for zone in state["zones"]:
        _relay_off(zone["gpio"])
    log.info("GPIO cleaned up")

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    fh = logging.FileHandler(Path(__file__).parent / "sprinkler.log")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(fh)

    log.info(f"Starting sprinkler backend (Pi={ON_PI})")
    app.run(host="0.0.0.0", port=8080, debug=False)
