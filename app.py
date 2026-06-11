"""
Flask-сервер велосипедного оптимизатора.
- Две погодные модели, параллельный фетч.
- Авто-пересчёт каждые 30 мин.
- Калибровка мощности по реальной позиции + времени.
- Стоянки с длительностью.
"""

import json
import threading
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, jsonify

import core

app = Flask(__name__)

MSK = timezone(timedelta(hours=3))

CONFIG_PATH = Path("config.json")
STATE_PATH  = Path("state.json")

DEFAULT_CONFIG = {
    "gpx_file": "route.gpx",
    "start_time": "",
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
    "recalc_interval_min": 30,
}

MODELS = ["icon_seamless", "ecmwf_ifs025"]

# ---------------------------------------------------------------------------
# Jinja-фильтры
# ---------------------------------------------------------------------------

@app.template_filter("msk")
def to_msk(iso_str: str) -> str:
    """ISO UTC → 'HH:MM МСК'"""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str).astimezone(MSK)
        return dt.strftime("%H:%M")
    except Exception:
        return iso_str[11:16]


@app.template_filter("msk_full")
def to_msk_full(iso_str: str) -> str:
    """ISO UTC → 'дд.мм HH:MM МСК'"""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str).astimezone(MSK)
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return iso_str[:16]


# ---------------------------------------------------------------------------
# Глобальное состояние
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state = {
    "current_km":        0.0,
    "position_time":     None,
    "actual_stops":      [],
    "effective_power":   None,
    "overnight_disabled": False,   # True — плановая ночёвка отключена
    "models":            {m: {"ride": [], "grid": []} for m in MODELS},
    "last_calc":         None,
    "last_error":        None,
    "total_km":          0.0,
    "config":            {},
}


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return DEFAULT_CONFIG.copy()


def _save_state():
    out = {k: v for k, v in _state.items() if k != "models"}
    out["grids"] = {m: _state["models"][m]["grid"] for m in MODELS}
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def _load_persisted():
    if not STATE_PATH.exists():
        return
    try:
        with open(STATE_PATH) as f:
            saved = json.load(f)
        with _lock:
            _state["current_km"]        = saved.get("current_km", 0.0)
            _state["position_time"]     = saved.get("position_time")
            _state["actual_stops"]      = saved.get("actual_stops", [])
            _state["effective_power"]   = saved.get("effective_power")
            _state["overnight_disabled"]= saved.get("overnight_disabled", False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Старт и эффективная мощность
# ---------------------------------------------------------------------------

def _start_wall(cfg: dict) -> datetime:
    s = cfg.get("start_time", "")
    if s:
        return datetime.fromisoformat(s)
    now = datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _effective_start(cfg: dict):
    """
    Возвращает (start_km, sim_wall, route_start).
    - route_start: фиксированный старт из конфига (6:50 МСК). Не меняется.
    - sim_wall:    стена в точке start_km (position_time, если маршрут уже идёт).
    - start_km:    км откуда начинать симуляцию.
    elapsed_h всегда считается от route_start.
    """
    route_start = _start_wall(cfg)

    with _lock:
        km    = _state["current_km"]
        pos_t = _state["position_time"]

    if pos_t:
        sim_wall = datetime.fromisoformat(pos_t)
        # Если позиция зафиксирована ДО старта — игнорируем, считаем от конфига
        if sim_wall <= route_start:
            sim_wall = route_start
            km = 0.0
    else:
        sim_wall = route_start
        km = 0.0

    return km, sim_wall, route_start


def _get_power(cfg: dict) -> float:
    with _lock:
        ep = _state["effective_power"]
    return ep if ep is not None else cfg["power_w"]


# ---------------------------------------------------------------------------
# Пересчёт маршрута
# ---------------------------------------------------------------------------

def recalculate():
    cfg = _load_config()
    gpx_path = cfg["gpx_file"]

    if not Path(gpx_path).exists():
        with _lock:
            _state["last_error"] = f"GPX не найден: {gpx_path}"
        return

    start_km, start_wall, route_start = _effective_start(cfg)
    power = _get_power(cfg)

    with _lock:
        overnight_disabled = _state["overnight_disabled"]

    try:
        segments  = core.parse_gpx(gpx_path)
        total_km  = core.total_route_km(segments)
        print(f"[calc] Старт симуляции: km={start_km:.0f}, wall={start_wall.isoformat()}, "
              f"route_start={route_start.isoformat()}, power={power:.0f}W, "
              f"overnight={'OFF' if overnight_disabled else 'ON'}")

        # Параллельный фетч двух моделей
        results = {}
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = {ex.submit(core.fetch_weather_parallel, segments,
                              cfg["weather_samples"], m): m
                    for m in MODELS}
            weather_by_model = {}
            for fut in as_completed(futs):
                weather_by_model[futs[fut]] = fut.result()

        for model, weather in weather_by_model.items():
            ride = core.simulate(
                segments=segments,
                start_time=start_wall,
                weather_points=weather,
                power_w=power,
                mass_kg=cfg["mass_kg"],
                cda=cfg["cda"],
                crr=cfg["crr"],
                time_limit_h=cfg["time_limit_h"],
                overnight_km=cfg["overnight_km"] if not overnight_disabled else 1e9,
                overnight_h=cfg["overnight_h"],
                rain_threshold=cfg["rain_threshold"],
                max_rain_wait_h=cfg["max_rain_wait_h"],
                current_km=start_km,
                route_start_time=route_start,
            )
            grid = core.grid_10km(ride)
            results[model] = {
                "ride": core.ride_to_dict(ride),
                "grid": core.ride_to_dict(grid),
            }

        for m, r in results.items():
            last = r["ride"][-1] if r["ride"] else None
            if last:
                print(f"[calc] {m}: финиш {last['wall_time'][11:16]} UTC, "
                      f"{last['elapsed_h']:.1f}ч")

        with _lock:
            _state["models"]     = results
            _state["total_km"]   = total_km
            _state["last_calc"]  = datetime.now(timezone.utc).isoformat()
            _state["last_error"] = None
            _state["config"]     = cfg

        _save_state()
        print("[calc] Готово.")

    except Exception as e:
        import traceback
        with _lock:
            _state["last_error"] = str(e)
            _state["last_calc"]  = datetime.now(timezone.utc).isoformat()
        traceback.print_exc()


def _bg_loop():
    """Пересчёт каждые recalc_interval_min минут. Первый запуск — сразу."""
    while True:
        cfg = _load_config()
        interval = cfg.get("recalc_interval_min", 30) * 60
        time.sleep(interval)
        print(f"[bg] Автопересчёт погоды ({interval//60} мин прошло)…")
        recalculate()


# ---------------------------------------------------------------------------
# Калибровка мощности по реальной скорости
# ---------------------------------------------------------------------------

def _calibrate_async(cfg, segments, weather_by_model,
                      start_km, pos_km, start_wall, pos_wall):
    """Вычисляет эффективную мощность из реального пройденного времени."""
    with _lock:
        stops = list(_state["actual_stops"])

    stop_h = sum(s.get("duration_h", 0) for s in stops
                 if start_km <= s.get("km", 0) <= pos_km)

    total_elapsed_h = (pos_wall - start_wall).total_seconds() / 3600.0
    riding_h = total_elapsed_h - stop_h

    if riding_h < 0.5 or pos_km - start_km < 5:
        return   # слишком мало данных

    # Калибруем по первой доступной модели
    weather = next(iter(weather_by_model.values()))
    new_power = core.calibrate_power(
        segments=segments,
        from_km=start_km, to_km=pos_km,
        start_time=start_wall,
        actual_riding_h=riding_h,
        weather_points=weather,
        mass_kg=cfg["mass_kg"],
        cda=cfg["cda"],
        crr=cfg["crr"],
        default_power=cfg["power_w"],
    )

    with _lock:
        _state["effective_power"] = round(new_power, 1)


# ---------------------------------------------------------------------------
# Маршруты Flask
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    with _lock:
        state = {
            "current_km":         _state["current_km"],
            "position_time":      _state["position_time"],
            "actual_stops":       _state["actual_stops"],
            "effective_power":    _state["effective_power"],
            "overnight_disabled": _state["overnight_disabled"],
            "last_calc":          _state["last_calc"],
            "last_error":         _state["last_error"],
            "total_km":           _state["total_km"],
            "config":             _state["config"],
            "models":             {m: _state["models"][m]["grid"] for m in MODELS},
            "rides_last":         {m: (_state["models"][m]["ride"][-1]
                                       if _state["models"][m]["ride"] else None)
                                   for m in MODELS},
        }
    return render_template("index.html", state=state, model_list=MODELS)


@app.route("/update_position", methods=["POST"])
def update_position():
    try:
        km       = float(request.form["km"])
        time_str = request.form.get("pos_time", "").strip()

        if time_str:
            today = datetime.now(MSK).date()
            naive = datetime.strptime(time_str, "%H:%M")
            wall_msk = datetime(today.year, today.month, today.day,
                                naive.hour, naive.minute, tzinfo=MSK)
            pos_wall = wall_msk.astimezone(timezone.utc)
        else:
            pos_wall = datetime.now(timezone.utc)

        pos_iso = pos_wall.isoformat()

        with _lock:
            _state["current_km"]    = max(0.0, km)
            _state["position_time"] = pos_iso

        # Пересчёт + калибровка мощности
        def _recalc_with_calib():
            cfg = _load_config()
            if not Path(cfg["gpx_file"]).exists():
                return
            segments = core.parse_gpx(cfg["gpx_file"])
            start_wall = _start_wall(cfg)   # route_start для калибровки

            # Быстрый фетч только для калибровки (ICON)
            try:
                weather_icon = core.fetch_weather_parallel(
                    segments, cfg["weather_samples"], MODELS[0])
                weather_by_model = {MODELS[0]: weather_icon}
                _calibrate_async(cfg, segments, weather_by_model,
                                  0.0, km, start_wall, pos_wall)
            except Exception:
                pass

            recalculate()

        threading.Thread(target=_recalc_with_calib, daemon=True).start()

    except (KeyError, ValueError):
        pass
    return redirect(url_for("index"))


@app.route("/add_stop", methods=["POST"])
def add_stop():
    try:
        km         = float(request.form["stop_km"])
        time_str   = request.form.get("stop_time", "").strip()   # HH:MM МСК
        dur_h      = float(request.form.get("stop_duration", 0))
        note       = request.form.get("stop_note", "").strip()

        stop = {"km": km, "time_msk": time_str,
                "duration_h": round(dur_h, 2), "note": note}

        with _lock:
            _state["actual_stops"].append(stop)

        _save_state()
        threading.Thread(target=recalculate, daemon=True).start()
    except (KeyError, ValueError):
        pass
    return redirect(url_for("index"))


@app.route("/clear_stops", methods=["POST"])
def clear_stops():
    with _lock:
        _state["actual_stops"] = []
    _save_state()
    threading.Thread(target=recalculate, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/clear_state", methods=["POST"])
def clear_state():
    """Сброс позиции, стоянок и калибровки — оставляем только конфиг."""
    with _lock:
        _state["current_km"]         = 0.0
        _state["position_time"]      = None
        _state["actual_stops"]       = []
        _state["effective_power"]    = None
        _state["overnight_disabled"] = False
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    threading.Thread(target=recalculate, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/toggle_overnight", methods=["POST"])
def toggle_overnight():
    with _lock:
        _state["overnight_disabled"] = not _state["overnight_disabled"]
    _save_state()
    threading.Thread(target=recalculate, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/reset_power", methods=["POST"])
def reset_power():
    with _lock:
        _state["effective_power"] = None
    threading.Thread(target=recalculate, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/recalc", methods=["POST"])
def force_recalc():
    threading.Thread(target=recalculate, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/api/state")
def api_state():
    with _lock:
        return jsonify({
            "current_km":      _state["current_km"],
            "effective_power": _state["effective_power"],
            "last_calc":       _state["last_calc"],
            "last_error":      _state["last_error"],
            "total_km":        _state["total_km"],
            "grids":           {m: _state["models"][m]["grid"] for m in MODELS},
        })


if __name__ == "__main__":
    _load_persisted()
    # Первый расчёт при старте, затем демон раз в 30 мин
    threading.Thread(target=recalculate, daemon=True).start()
    threading.Thread(target=_bg_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    print(f"[app] Сервер запущен на порту {port}")
    print(f"[app] Пересчёт погоды каждые {_load_config().get('recalc_interval_min',30)} мин")
    app.run(host="0.0.0.0", port=port, debug=False)
