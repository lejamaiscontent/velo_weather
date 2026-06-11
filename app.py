"""
Flask-сервер велосипедного оптимизатора.
- Раз в 30 минут пересчитывает маршрут с актуальным прогнозом погоды.
- Принимает текущий километр через форму.
- Отдаёт таблицу 10км-сетки с ETA и осадками.
"""

import json
import threading
import time
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, jsonify

import core

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Конфигурация — меняйте под свой маршрут
# ---------------------------------------------------------------------------

CONFIG_PATH = Path("config.json")
STATE_PATH = Path("state.json")

DEFAULT_CONFIG = {
    "gpx_file": "route.gpx",
    "start_time": "",           # ISO-формат UTC, например "2025-07-01T06:00:00+00:00"
    "power_w": 150,
    "mass_kg": 85,
    "cda": 0.36,
    "crr": 0.004,
    "time_limit_h": 40,
    "overnight_km": 300,
    "overnight_h": 8,
    "rain_threshold": 0.5,
    "max_rain_wait_h": 3.0,
    "weather_samples": 10,
    "weather_model": "icon_seamless",
    "recalc_interval_min": 30,
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        return {**DEFAULT_CONFIG, **cfg}
    return DEFAULT_CONFIG.copy()


# ---------------------------------------------------------------------------
# Глобальное состояние
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_state = {
    "current_km": 0.0,
    "last_calc": None,       # ISO timestamp
    "last_error": None,
    "ride": [],              # полная симуляция (список словарей)
    "grid": [],              # 10км-точки
    "total_km": 0.0,
    "config": {},
}


def _save_state():
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in _state.items() if k != "ride_raw"}, f,
                  ensure_ascii=False, indent=2)


def _load_persisted_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                saved = json.load(f)
            with _state_lock:
                _state["current_km"] = saved.get("current_km", 0.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Пересчёт маршрута
# ---------------------------------------------------------------------------

def recalculate():
    cfg = load_config()

    gpx_path = cfg["gpx_file"]
    if not Path(gpx_path).exists():
        with _state_lock:
            _state["last_error"] = f"GPX-файл не найден: {gpx_path}"
        return

    start_str = cfg["start_time"]
    if start_str:
        start_time = datetime.fromisoformat(start_str)
    else:
        # Ближайший час UTC
        now = datetime.now(timezone.utc)
        start_time = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    with _state_lock:
        current_km = _state["current_km"]

    try:
        segments = core.parse_gpx(gpx_path)
        weather = core.fetch_weather(
            segments,
            n_samples=cfg["weather_samples"],
            model=cfg["weather_model"],
        )
        ride = core.simulate(
            segments=segments,
            start_time=start_time,
            weather_points=weather,
            power_w=cfg["power_w"],
            mass_kg=cfg["mass_kg"],
            cda=cfg["cda"],
            crr=cfg["crr"],
            time_limit_h=cfg["time_limit_h"],
            overnight_km=cfg["overnight_km"],
            overnight_h=cfg["overnight_h"],
            rain_threshold=cfg["rain_threshold"],
            max_rain_wait_h=cfg["max_rain_wait_h"],
            current_km=current_km,
        )
        grid = core.grid_10km(ride)
        total_km = core.total_route_km(segments)

        ride_dicts = core.ride_to_dict(ride)
        grid_dicts = core.ride_to_dict(grid)

        with _state_lock:
            _state["ride"] = ride_dicts
            _state["grid"] = grid_dicts
            _state["total_km"] = total_km
            _state["last_calc"] = datetime.now(timezone.utc).isoformat()
            _state["last_error"] = None
            _state["config"] = cfg

        _save_state()

    except Exception as e:
        with _state_lock:
            _state["last_error"] = str(e)
            _state["last_calc"] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Фоновый поток пересчёта
# ---------------------------------------------------------------------------

def _background_loop():
    while True:
        recalculate()
        cfg = load_config()
        interval = cfg.get("recalc_interval_min", 30) * 60
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Маршруты Flask
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    with _state_lock:
        state = dict(_state)
    return render_template("index.html", state=state)


@app.route("/update_km", methods=["POST"])
def update_km():
    try:
        km = float(request.form["km"])
        with _state_lock:
            _state["current_km"] = max(0.0, km)
        # Немедленный пересчёт в отдельном потоке
        threading.Thread(target=recalculate, daemon=True).start()
    except (KeyError, ValueError):
        pass
    return redirect(url_for("index"))


@app.route("/recalc", methods=["POST"])
def force_recalc():
    """Ручной пересчёт."""
    threading.Thread(target=recalculate, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/api/state")
def api_state():
    """JSON-API для polling из браузера."""
    with _state_lock:
        return jsonify({
            "current_km": _state["current_km"],
            "last_calc": _state["last_calc"],
            "last_error": _state["last_error"],
            "total_km": _state["total_km"],
            "grid": _state["grid"],
        })


@app.route("/api/ride")
def api_ride():
    """Полная симуляция в JSON."""
    with _state_lock:
        return jsonify(_state["ride"])


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _load_persisted_state()

    # Первый пересчёт сразу при старте
    threading.Thread(target=recalculate, daemon=True).start()

    # Фоновый цикл пересчёта
    t = threading.Thread(target=_background_loop, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
