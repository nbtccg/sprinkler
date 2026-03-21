#!/usr/bin/env python3
"""
Lawn Sprinkler Flask Backend
Runs on Raspberry Pi and controls GPIO pins for zone solenoids.
Serves the sprinkler.html frontend and exposes a REST API.

Install dependencies:
    pip install flask flask-cors RPi.GPIO requests

Run:
    python3 app.py
"""

import json
import os
import threading
import time
import logging
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# ── GPIO setup ───────────────────────────────────────────────────────────────
# On a real Pi, use RPi.GPIO. On any other machine we fall back to a stub so
# you can develop / test without hardware attached.
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    ON_PI = True
except (ImportError, RuntimeError):
    ON_PI = False
    logging.warning("RPi.GPIO not available – running in simulation mode.")

    class _GPIO:
        BCM = OUT = IN = HIGH = LOW = 0
        @staticmethod
        def setmode(_): pass
        @staticmethod
        def setwarnings(_): pass
        @staticmethod
        def setup(pin, mode): pass
        @staticmethod
        def output(pin, state):
            logging.info(f"[SIM] GPIO {pin} → {'HIGH' if state else 'LOW'}")
        @staticmethod
        def cleanup(): pass

    GPIO = _GPIO()

# ── App & config ─────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=".")
CORS(app)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

DATA_FILE = Path("sprinkler_data.json")
ACTIVE_HIGH = True          # Set False if your relay board is active-low

# Default zone config – overwritten by saved data on startup
DEFAULT_ZONES = [
    {"id": 1, "name": "Front Lawn",    "gpio": 17, "duration": 10, "enabled": True},
    {"id": 2, "name": "Back Lawn",     "gpio": 18, "duration": 15, "enabled": True},
    {"id": 3, "name": "Side Garden",   "gpio": 22, "duration": 8,  "enabled": True},
    {"id": 4, "name": "Flower Beds",   "gpio": 23, "duration": 6,  "enabled": True},
    {"id": 5, "name": "Veggie Patch",  "gpio": 24, "duration": 12, "enabled": True},
    {"id": 6, "name": "Driveway Edge", "gpio": 25, "duration": 5,  "enabled": True},
    {"id": 7, "name": "Back Garden",   "gpio": 27, "duration": 10, "enabled": True},
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
    }

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(state, f, indent=2)

state = load_data()

# ── GPIO helpers ──────────────────────────────────────────────────────────────
def _relay_on(pin):
    GPIO.output(pin, GPIO.HIGH if state["active_high"] else GPIO.LOW)

def _relay_off(pin):
    GPIO.output(pin, GPIO.LOW if state["active_high"] else GPIO.HIGH)

def init_gpio():
    for zone in state["zones"]:
        pin = zone["gpio"]
        GPIO.setup(pin, GPIO.OUT)
        _relay_off(pin)           # ensure all relays start OFF
        log.info(f"Zone {zone['id']} ({zone['name']}) → GPIO {pin} initialised")

init_gpio()

# ── Zone run state ────────────────────────────────────────────────────────────
running: dict[int, dict] = {}   # zone_id → {thread, stop_event, start, end}
run_lock = threading.Lock()

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

def start_zone(zone_id: int, duration_secs: int | None = None) -> dict:
    zone = next((z for z in state["zones"] if z["id"] == zone_id), None)
    if not zone:
        return {"error": "Zone not found"}
    if not state["master_enabled"]:
        return {"error": "Master switch is disabled"}
    if not zone["enabled"]:
        return {"error": "Zone is disabled"}

    secs = duration_secs or zone["duration"] * 60

    with run_lock:
        if zone_id in running:
            return {"error": "Zone already running"}
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

# ── Scheduler ─────────────────────────────────────────────────────────────────
DAY_MAP = {"Mon":0,"Tue":1,"Wed":2,"Thu":3,"Fri":4,"Sat":5,"Sun":6}

def _scheduler_loop():
    last_triggered: set[tuple] = set()
    while True:
        now = datetime.now()
        current_day = now.strftime("%a")       # Mon … Sun
        current_time = now.strftime("%H:%M")
        minute_key = (current_day, current_time)

        if minute_key not in last_triggered:
            for sched in state["schedules"]:
                if (sched["enabled"]
                        and sched["time"] == current_time
                        and current_day in sched["days"]):
                    log.info(f"Scheduler firing: schedule {sched['id']}")
                    # Check weather skip
                    if _should_skip_weather():
                        log.info("Skipping due to weather conditions")
                        continue
                    for zone_id in sched["zones"]:
                        start_zone(zone_id)
                        # Stagger zone starts by their duration to run sequentially
                        z = next((z for z in state["zones"] if z["id"] == zone_id), None)
                        if z:
                            time.sleep(z["duration"] * 60 + 2)
            last_triggered.add(minute_key)
            # Prune old keys (keep last 5 minutes worth)
            if len(last_triggered) > 10:
                last_triggered.pop()

        time.sleep(15)

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

threading.Thread(target=_scheduler_loop, daemon=True).start()
log.info("Scheduler started")

# ── Weather fetcher ───────────────────────────────────────────────────────────
def fetch_weather_cache():
    """Fetch weather from OpenWeatherMap and cache result in state."""
    import requests as req
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
        r = req.get(url, timeout=10)
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
        time.sleep(30 * 60)   # refresh every 30 minutes

threading.Thread(target=_weather_refresh_loop, daemon=True).start()

# ── Routes ────────────────────────────────────────────────────────────────────

# Serve the frontend
@app.route("/")
def index():
    return send_from_directory(".", "sprinkler.html")

# ── Zones ──────────────────────────────────────────────────────────────────────
@app.route("/api/zones", methods=["GET"])
def get_zones():
    return jsonify(state["zones"])

@app.route("/api/zones", methods=["POST"])
def update_zones():
    """Replace full zones list (from settings save)."""
    zones = request.json
    if not isinstance(zones, list):
        return jsonify({"error": "Expected list"}), 400
    # Re-init GPIO for any pin changes
    old_pins = {z["id"]: z["gpio"] for z in state["zones"]}
    state["zones"] = zones
    for zone in zones:
        if zone["gpio"] != old_pins.get(zone["id"]):
            GPIO.setup(zone["gpio"], GPIO.OUT)
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

# ── Zone control ───────────────────────────────────────────────────────────────
@app.route("/api/zones/<int:zone_id>/run", methods=["POST"])
def run_zone(zone_id):
    body = request.json or {}
    duration_secs = body.get("duration_secs")   # optional override
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

# ── Running status ─────────────────────────────────────────────────────────────
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
    })

# ── Schedules ──────────────────────────────────────────────────────────────────
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

# ── Weather ────────────────────────────────────────────────────────────────────
@app.route("/api/weather", methods=["GET"])
def get_weather():
    return jsonify(state.get("weather", {}))

@app.route("/api/weather", methods=["POST"])
def update_weather():
    body = request.json or {}
    state["weather"].update({k: v for k, v in body.items() if k != "_cached"})
    save_data()
    # Trigger immediate refresh
    cached = fetch_weather_cache()
    return jsonify({"ok": True, "cached": cached})

# ── System settings ────────────────────────────────────────────────────────────
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

# ── Logs (last 100 lines of app log) ──────────────────────────────────────────
@app.route("/api/logs", methods=["GET"])
def get_logs():
    log_file = Path("sprinkler.log")
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
    GPIO.cleanup()
    log.info("GPIO cleaned up")

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Add file logging
    fh = logging.FileHandler("sprinkler.log")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(fh)

    log.info(f"Starting sprinkler backend (Pi={ON_PI})")
    app.run(host="0.0.0.0", port=8080, debug=False)
