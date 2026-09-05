#!/usr/bin/env python3
"""
Lawn Sprinkler Flask Backend
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

try:
    import gpiod
    from gpiod.line import Direction, Value
    _chip_path = '/dev/gpiochip0'
    with gpiod.Chip(_chip_path) as _c:
        pass
    ON_PI = True
except (ImportError, Exception) as e:
    ON_PI = False
    gpiod = None
    logging.warning(f"gpiod not available – running in simulation mode. ({e})")

app = Flask(__name__, static_folder=".")
CORS(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DATA_FILE   = Path(__file__).parent / "config.json"
EVENTS_FILE = Path(__file__).parent / "events.json"
MAX_EVENTS  = 200

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
    {"id": 1, "time": "06:00", "days": ["Mon","Wed","Fri"], "zones": [1,2,3], "enabled": True, "duration_mins": None},
    {"id": 2, "time": "19:00", "days": ["Sat","Sun"],       "zones": [4,5,6,7], "enabled": True, "duration_mins": None},
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

for _key, _default in [("last_skip", None), ("force_skip_next", False)]:
    if _key not in state:
        state[_key] = _default

events_lock = threading.Lock()

def load_events() -> list:
    if EVENTS_FILE.exists():
        try:
            with open(EVENTS_FILE) as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_events(evts: list):
    with open(EVENTS_FILE, "w") as f:
        json.dump(evts[-MAX_EVENTS:], f, indent=2)

def append_event(event: dict):
    with events_lock:
        evts = load_events()
        evts.append(event)
        save_events(evts)

def _gpio_set(pin: int, value: bool):
    if not ON_PI:
        log.info(f"[SIM] GPIO {pin} -> {'HIGH' if value else 'LOW'}")
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
    for zone in state["zones"]:
        _relay_off(zone["gpio"])
        log.info(f"Zone {zone['id']} ({zone['name']}) -> GPIO {zone['gpio']} initialised")

init_gpio()

running: dict  = {}
run_lock       = threading.Lock()
run_all_active    = threading.Event()
run_all_stop      = threading.Event()

schedule_lock         = threading.Lock()
schedules_queued      = 0
schedules_queued_lock = threading.Lock()


def _run_zone_thread(zone_id: int, duration_secs: int, stop_event: threading.Event,
                     trigger: str = "manual", schedule_id: int = None):
    zone = next((z for z in state["zones"] if z["id"] == zone_id), None)
    if not zone:
        return
    pin       = zone["gpio"]
    zone_name = zone["name"]
    start_ts  = time.time()
    log.info(f"Zone {zone_id} ({zone_name}) ON  - pin {pin} for {duration_secs}s [{trigger}]")
    _relay_on(pin)
    stop_event.wait(timeout=duration_secs)
    _relay_off(pin)
    elapsed = round(time.time() - start_ts)
    stopped = stop_event.is_set() and elapsed < duration_secs - 1
    outcome = "stopped" if stopped else "ran"
    log.info(f"Zone {zone_id} ({zone_name}) OFF - {elapsed}s elapsed, outcome={outcome}")
    append_event({
        "timestamp":     datetime.now().isoformat(),
        "trigger":       trigger,
        "outcome":       outcome,
        "zone_id":       zone_id,
        "zone_name":     zone_name,
        "duration_secs": elapsed,
        "reason":        "Manually stopped" if stopped else None,
        "schedule_id":   schedule_id,
    })
    with run_lock:
        running.pop(zone_id, None)


def start_zone(zone_id: int, duration_secs=None,
               trigger: str = "manual", schedule_id: int = None) -> dict:
    zone = next((z for z in state["zones"] if z["id"] == zone_id), None)
    if not zone:
        return {"error": "Zone not found"}
    if not state["master_enabled"]:
        return {"error": "Master switch is disabled"}
    if not zone["enabled"]:
        return {"error": "Zone is disabled"}
    secs = duration_secs or zone["duration"] * 60
    with run_lock:
        for info in running.values():
            info["stop_event"].set()
        if running:
            run_lock.release()
            time.sleep(0.5)
            run_lock.acquire()
        stop_event = threading.Event()
        t = threading.Thread(
            target=_run_zone_thread,
            args=(zone_id, secs, stop_event, trigger, schedule_id),
            daemon=True,
        )
        now = time.time()
        running[zone_id] = {
            "start":       now,
            "end":         now + secs,
            "duration":    secs,
            "stop_event":  stop_event,
            "thread":      t,
            "trigger":     trigger,
            "schedule_id": schedule_id,
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
    run_all_stop.set()
    with run_lock:
        for info in running.values():
            info["stop_event"].set()


def _run_all_thread():
    global schedules_queued
    with schedules_queued_lock:
        schedules_queued += 1
    log.info(f"Run-all queued (queue depth: {schedules_queued})")
    with schedule_lock:
        with schedules_queued_lock:
            schedules_queued -= 1
        run_all_active.set()
        run_all_stop.clear()
        log.info("Run-all sequence started")
        try:
            for zone in state["zones"]:
                if run_all_stop.is_set():
                    log.info("Run-all aborted by stop signal")
                    break
                if not state["master_enabled"]:
                    log.info("Run-all aborted: master switch disabled")
                    break
                if not zone["enabled"]:
                    log.info(f"Run-all: skipping zone {zone['id']} ({zone['name']}) - disabled")
                    continue
                secs   = zone["duration"] * 60
                result = start_zone(zone["id"], secs, trigger="run_all")
                if "error" in result:
                    log.warning(f"Run-all: zone {zone['id']} skipped - {result['error']}")
                    continue
                zone_id = zone["id"]
                while True:
                    if run_all_stop.is_set():
                        stop_zone(zone_id)
                        log.info("Run-all: stop signal mid-zone, aborting sequence")
                        break
                    if not state["master_enabled"]:
                        stop_all()
                        break
                    with run_lock:
                        still_running = zone_id in running
                    if not still_running:
                        break
                    time.sleep(1)
                if run_all_stop.is_set() or not state["master_enabled"]:
                    break
                time.sleep(2)
        finally:
            run_all_active.clear()
            log.info("Run-all sequence complete")


# ── Weather fetcher ────────────────────────────────────────────────────────────
def fetch_weather_cache():
    """
    Fetches two OWM endpoints and merges results into a single cache object.

    Current conditions (/data/2.5/weather):
        temp, humidity, wind, description — used for cold/wind skip checks.

    5-day/3-hour forecast (/data/2.5/forecast):
        Scans the next 24 hours of intervals (cnt=8) and records the highest
        precipitation probability (pop, 0.0-1.0) found. This is what the
        rain_threshold percentage is compared against, so skip decisions are
        based on what's actually coming rather than current conditions.
    """
    w   = state.get("weather", {})
    key = w.get("api_key", "")
    loc = w.get("location", "")
    if not key or not loc:
        return None
    try:
        # ── Current conditions (temp, wind, humidity) ──────────────────────
        cur_url  = (
            f"https://api.openweathermap.org/data/2.5/weather"
            f"?q={loc}&appid={key}&units=imperial"
        )
        cur_r    = requests.get(cur_url, timeout=10)
        cur_data = cur_r.json()
        if cur_data.get("cod") != 200:
            log.warning(f"Weather API (current) error: {cur_data.get('message')}")
            return None

        # ── 3-hour forecast — next 24 hours (8 x 3h intervals) ────────────
        fcast_url  = (
            f"https://api.openweathermap.org/data/2.5/forecast"
            f"?q={loc}&appid={key}&units=imperial&cnt=8"
        )
        fcast_r    = requests.get(fcast_url, timeout=10)
        fcast_data = fcast_r.json()

        max_rain_pop   = 0.0
        forecast_hours = 0

        if str(fcast_data.get("cod")) == "200":
            intervals      = fcast_data.get("list", [])
            forecast_hours = len(intervals) * 3
            for interval in intervals:
                pop = interval.get("pop", 0.0)
                if pop > max_rain_pop:
                    max_rain_pop = pop
            log.info(
                f"Forecast: max rain prob {round(max_rain_pop*100)}% "
                f"over next {forecast_hours}h ({len(intervals)} intervals)"
            )
        else:
            log.warning(f"Weather API (forecast) error: {fcast_data.get('message')}")
            # Fall back: treat currently rainy conditions as 100% probability
            is_currently_rainy = cur_data["weather"][0]["main"] in [
                "Rain", "Drizzle", "Thunderstorm", "Snow"
            ]
            max_rain_pop   = 1.0 if is_currently_rainy else 0.0
            forecast_hours = 0

        cached = {
            "temp":           round(cur_data["main"]["temp"]),
            "humidity":       cur_data["main"]["humidity"],
            "wind":           round(cur_data["wind"]["speed"]),
            "description":    cur_data["weather"][0]["description"],
            "main":           cur_data["weather"][0]["main"],
            # is_rainy = current conditions only; skip logic uses max_rain_pop
            "is_rainy":       cur_data["weather"][0]["main"] in [
                                  "Rain", "Drizzle", "Thunderstorm", "Snow"
                              ],
            "max_rain_pop":   round(max_rain_pop * 100),  # stored 0-100 for display
            "forecast_hours": forecast_hours,
            "fetched_at":     datetime.now().isoformat(),
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


# ── Weather skip helpers ───────────────────────────────────────────────────────
def _should_skip_weather() -> bool:
    w      = state.get("weather", {})
    cached = w.get("_cached", {})
    if not cached:
        return False
    # Rain: compare highest forecast pop in next 24h against threshold
    if w.get("skip_rain"):
        max_pop   = cached.get("max_rain_pop", 0)   # 0-100
        threshold = w.get("rain_threshold", 40)      # 0-100
        if max_pop >= threshold:
            return True
    if w.get("skip_cold") and cached.get("temp", 999) < w.get("cold_threshold", 35):
        return True
    if w.get("skip_wind") and cached.get("wind", 0) > w.get("wind_threshold", 20):
        return True
    return False

def _skip_reason() -> str:
    w      = state.get("weather", {})
    cached = w.get("_cached", {})
    if w.get("skip_rain"):
        max_pop   = cached.get("max_rain_pop", 0)
        threshold = w.get("rain_threshold", 40)
        hours     = cached.get("forecast_hours", 24)
        if max_pop >= threshold:
            return f"{max_pop}% rain chance in next {hours}h (threshold {threshold}%)"
    if w.get("skip_cold") and cached.get("temp", 999) < w.get("cold_threshold", 35):
        return f"Temp {cached.get('temp')}°F below {w.get('cold_threshold')}°F"
    if w.get("skip_wind") and cached.get("wind", 0) > w.get("wind_threshold", 20):
        return f"Wind {cached.get('wind')}mph above {w.get('wind_threshold')}mph"
    return "Weather conditions"


# ── Scheduler ─────────────────────────────────────────────────────────────────
def _scheduler_loop():
    last_triggered: set = set()
    while True:
        now          = datetime.now()
        current_day  = now.strftime("%a")
        current_time = now.strftime("%H:%M")
        minute_key   = (current_day, current_time)

        if minute_key not in last_triggered:
            for sched in state["schedules"]:
                if (sched["enabled"]
                        and sched["time"] == current_time
                        and current_day in sched["days"]):
                    log.info(f"Scheduler firing: schedule {sched['id']}")

                    if state.get("force_skip_next"):
                        reason = "Manually forced skip"
                        log.info("Skipping: force skip flag set")
                        state["force_skip_next"] = False
                        state["last_skip"] = {
                            "time":        datetime.now().isoformat(),
                            "schedule_id": sched["id"],
                            "reason":      reason,
                        }
                        save_data()
                        for zone_id in sched["zones"]:
                            z = next((z for z in state["zones"] if z["id"] == zone_id), None)
                            append_event({
                                "timestamp":     datetime.now().isoformat(),
                                "trigger":       "scheduled",
                                "outcome":       "skipped",
                                "zone_id":       zone_id,
                                "zone_name":     z["name"] if z else f"Zone {zone_id}",
                                "duration_secs": None,
                                "reason":        reason,
                                "schedule_id":   sched["id"],
                            })
                        continue

                    if _should_skip_weather():
                        reason = _skip_reason()
                        log.info(f"Skipping due to weather: {reason}")
                        state["last_skip"] = {
                            "time":        datetime.now().isoformat(),
                            "schedule_id": sched["id"],
                            "reason":      reason,
                        }
                        save_data()
                        for zone_id in sched["zones"]:
                            z = next((z for z in state["zones"] if z["id"] == zone_id), None)
                            append_event({
                                "timestamp":     datetime.now().isoformat(),
                                "trigger":       "scheduled",
                                "outcome":       "skipped",
                                "zone_id":       zone_id,
                                "zone_name":     z["name"] if z else f"Zone {zone_id}",
                                "duration_secs": None,
                                "reason":        reason,
                                "schedule_id":   sched["id"],
                            })
                        continue

                    sched_copy = dict(sched)
                    def _run_schedule(s=sched_copy):
                        global schedules_queued
                        with schedules_queued_lock:
                            schedules_queued += 1
                        log.info(f"Schedule {s['id']} queued (queue depth: {schedules_queued})")
                        with schedule_lock:
                            with schedules_queued_lock:
                                schedules_queued -= 1
                            log.info(f"Schedule {s['id']} starting zone sequence")
                            sched_dur = s.get("duration_mins")
                            sched_dur_secs = int(sched_dur) * 60 if sched_dur else None
                            for zone_id in s["zones"]:
                                if not state["master_enabled"]:
                                    log.info(f"Schedule {s['id']} aborted: master disabled")
                                    break
                                result = start_zone(zone_id, duration_secs=sched_dur_secs, trigger="scheduled", schedule_id=s["id"])
                                if "error" in result:
                                    log.warning(f"Schedule {s['id']} zone {zone_id} skipped - {result['error']}")
                                    continue
                                while True:
                                    with run_lock:
                                        still_running = zone_id in running
                                    if not still_running:
                                        break
                                    time.sleep(1)
                                time.sleep(2)
                            log.info(f"Schedule {s['id']} zone sequence complete")
                    threading.Thread(target=_run_schedule, daemon=True).start()

            last_triggered.add(minute_key)
            if len(last_triggered) > 10:
                last_triggered.pop()

        time.sleep(15)

threading.Thread(target=_scheduler_loop, daemon=True).start()
log.info("Scheduler started")


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
    body          = request.get_json(silent=True, force=True) or {}
    duration_secs = body.get("duration_secs")
    result        = start_zone(zone_id, duration_secs, trigger="manual")
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
    if not state["master_enabled"]:
        return jsonify({"error": "Master switch is disabled"}), 400
    if run_all_active.is_set():
        return jsonify({"error": "Run-all sequence already in progress"}), 409
    threading.Thread(target=_run_all_thread, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/force_skip", methods=["POST"])
def force_skip_route():
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
                "elapsed":   round(now - info["start"]),
                "remaining": max(0, round(info["end"] - now)),
                "duration":  info["duration"],
            }
            for zone_id, info in running.items()
        }
    with schedules_queued_lock:
        queued = schedules_queued
    return jsonify({
        "running":          active,
        "master_enabled":   state["master_enabled"],
        "active_high":      state["active_high"],
        "on_pi":            ON_PI,
        "last_skip":        state.get("last_skip"),
        "force_skip_next":  state.get("force_skip_next", False),
        "run_all_active":   run_all_active.is_set(),
        "schedule_busy":    schedule_lock.locked(),
        "schedules_queued": queued,
    })

@app.route("/api/events", methods=["GET"])
def get_events():
    limit = request.args.get("limit", MAX_EVENTS, type=int)
    with events_lock:
        evts = load_events()
    return jsonify(list(reversed(evts[-MAX_EVENTS:]))[:limit])

@app.route("/api/events", methods=["DELETE"])
def clear_events():
    with events_lock:
        save_events([])
    return jsonify({"ok": True})

@app.route("/api/schedules", methods=["GET"])
def get_schedules():
    return jsonify(state["schedules"])

@app.route("/api/schedules", methods=["POST"])
def add_schedule():
    sched        = request.json
    existing_ids = [s["id"] for s in state["schedules"]]
    sched["id"]  = max(existing_ids, default=0) + 1
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

@app.route("/api/schedules/<int:sched_id>", methods=["PUT"])
def replace_schedule(sched_id):
    sched = next((s for s in state["schedules"] if s["id"] == sched_id), None)
    if not sched:
        return jsonify({"error": "Not found"}), 404
    body = request.json or {}
    sched["time"]         = body.get("time",         sched["time"])
    sched["days"]         = body.get("days",         sched["days"])
    sched["zones"]        = body.get("zones",        sched["zones"])
    sched["enabled"]      = body.get("enabled",      sched["enabled"])
    sched["duration_mins"] = body.get("duration_mins", sched.get("duration_mins"))
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
        "active_high":    state["active_high"],
        "on_pi":          ON_PI,
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


import atexit
@atexit.register
def cleanup():
    stop_all()
    time.sleep(0.5)
    for zone in state["zones"]:
        _relay_off(zone["gpio"])
    log.info("GPIO cleaned up")


if __name__ == "__main__":
    fh = logging.FileHandler(Path(__file__).parent / "sprinkler.log")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(fh)
    log.info(f"Starting sprinkler backend (Pi={ON_PI})")
    app.run(host="0.0.0.0", port=8080, debug=False)
