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
from zoneinfo import ZoneInfo

import core
try:
    from timezonefinder import TimezoneFinder as _TZF
    _tzf = _TZF()
except Exception:
    _tzf = None

app = Flask(__name__)

MSK = timezone(timedelta(hours=3))


def _route_tz() -> timezone:
    """Часовой пояс текущей позиции на маршруте (или старта). Fallback — МСК."""
    if _tzf is None:
        return MSK
    # ищем ближайший RidePoint к current_km
    with _lock:
        km = _state["current_km"]
        ride = None
        for m in MODELS:
            r = _state["models"][m]["ride"]
            if r:
                ride = r
                break
    if ride:
        pt = min(ride, key=lambda p: abs((p.get("km") if isinstance(p, dict) else p.km) - km))
        lat = pt.get("lat") if isinstance(pt, dict) else pt.lat
        lon = pt.get("lon") if isinstance(pt, dict) else pt.lon
    else:
        # берём первую точку GPX как старт
        try:
            cfg = _load_config()
            segs = core.load_gpx(cfg.get("gpx_file", "route.gpx"))
            if segs:
                lat, lon = segs[0].lat, segs[0].lon
            else:
                return MSK
        except Exception:
            return MSK
    tz_name = _tzf.timezone_at(lat=lat, lng=lon)
    if tz_name is None:
        return MSK
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return MSK

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
    "rain_threshold": 0.5,
    "max_rain_wait_h": 3.0,
    "weather_samples": 10,
    "recalc_interval_min": 30,
    "planned_stop_budget_h": 0.0,
}

MODELS = ["icon_seamless", "ecmwf_ifs025"]

POWER_LOG       = Path("power_log.jsonl")
WEATHER_LOG     = Path("weather_log.jsonl")

# Расписание фетча для каждой модели
MODEL_SCHED_CFG = {
    "icon_seamless": {
        "quiet_h":            2.75,   # 2ч45м после обновления не трогаем
        "search_start_h":     151/60, # 2ч31м: переключаемся на быстрый поллинг (без истории)
        "initial_interval_min": 50,
        "poll_interval_min":  15,
    },
    "ecmwf_ifs025": {
        "quiet_h":            10.0,
        "search_start_h":     11.0,
        "initial_interval_min": 170,  # 2ч50м
        "poll_interval_min":  15,
    },
}

# Состояние расписания per-model (живёт только в памяти, сбрасывается при рестарте)
_model_sched: dict = {m: {
    "last_update_at":  None,  # datetime: последнее обнаруженное обновление модели
    "last_fetch_at":   None,  # datetime: последний фетч
    "first_fetch_at":  None,  # datetime: самый первый фетч (для расчёта фазы)
    "last_weather":    None,  # List[WeatherPoint]: снепшот последнего фетча
} for m in MODELS}

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
_fetch_lock = threading.Lock()   # только один фетч погоды одновременно
_state = {
    "current_km":          0.0,
    "position_time":       None,
    "prev_km":             0.0,
    "prev_position_time":  None,
    "actual_stops":        [],
    "effective_power":     None,
    "manual_power":        False,   # True — мощность задана вручную, не калибровкой
    "models":              {m: {"ride": [], "grid": []} for m in MODELS},
    "last_calc":           None,
    "last_error":          None,
    "total_km":            0.0,
    "config":              {},
    "api_calls_today":     0,
    "api_calls_date":      "",
    "weather_cache":       {},   # {model: [WeatherPoint, ...]}
    "weather_gpx":         "",   # GPX-путь, для которого закеширована погода
    "last_weather_fetch":  None, # когда последний раз забирали погоду
}


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return DEFAULT_CONFIG.copy()


def _log_power(source: str, power_w, **kwargs):
    """Пишет событие мощности в power_log.jsonl."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(),
             "source": source, "power_w": power_w}
    entry.update(kwargs)
    with open(POWER_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[power] {source}: {power_w}W  {kwargs}")


def _inc_api_calls(n: int):
    """Счётчик запросов к Open-Meteo за сегодня."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _lock:
        if _state["api_calls_date"] != today:
            _state["api_calls_date"] = today
            _state["api_calls_today"] = 0
        _state["api_calls_today"] += n


def _model_update_time(model: str) -> datetime:
    """Расчётное время последнего доступного прогона модели."""
    now = datetime.now(timezone.utc)
    if "ecmwf" in model:
        runs, delay_h = [0, 12], 5.0
    else:  # icon_seamless, best_match
        runs, delay_h = [0, 6, 12, 18], 2.5
    for h in sorted(runs, reverse=True):
        avail = now.replace(hour=h, minute=0, second=0, microsecond=0) + timedelta(hours=delay_h)
        if avail <= now:
            return avail
    last_h = max(runs)
    return (now - timedelta(days=1)).replace(
        hour=last_h, minute=0, second=0, microsecond=0) + timedelta(hours=delay_h)


def _save_state():
    skip = {"models", "weather_cache"}
    out = {k: v for k, v in _state.items() if k not in skip}
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
            _state["current_km"]         = saved.get("current_km", 0.0)
            _state["position_time"]      = saved.get("position_time")
            _state["prev_km"]            = saved.get("prev_km", 0.0)
            _state["prev_position_time"] = saved.get("prev_position_time")
            _state["actual_stops"]       = saved.get("actual_stops", [])
            _state["effective_power"]    = saved.get("effective_power")
            _state["manual_power"]       = saved.get("manual_power", False)
            _state["api_calls_today"]    = saved.get("api_calls_today", 0)
            _state["api_calls_date"]     = saved.get("api_calls_date", "")
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
# Детектор изменений снепшотов погоды
# ---------------------------------------------------------------------------

def _weather_changed(old: list, new: list) -> bool:
    """True если новые данные значимо отличаются от старых (не просто погрешность)."""
    if old is None or not old or len(old) != len(new):
        return True
    step = max(1, len(old) // 8)
    diffs = []
    for op, np_ in zip(old[::step], new[::step]):
        n = min(len(op.wind_speed), len(np_.wind_speed))
        for i in range(6, n, 6):   # каждые 6 часов прогноза
            diffs.append(abs(op.wind_speed[i] - np_.wind_speed[i]))
            diffs.append(abs(op.precip[i]     - np_.precip[i]) * 2)
    if not diffs:
        return True
    return max(diffs) > 0.5 or (sum(diffs) / len(diffs)) > 0.15


def _should_fetch_model(model: str, now: datetime) -> bool:
    """Нужно ли сейчас дёргать API для этой модели?"""
    s = _model_sched[model]
    c = MODEL_SCHED_CFG[model]
    lu = s["last_update_at"]
    lf = s["last_fetch_at"]
    ff = s["first_fetch_at"]

    def mins(dt):  return (now - dt).total_seconds() / 60
    def hours(dt): return (now - dt).total_seconds() / 3600

    if lu is not None:
        # Знаем время последнего обновления
        if hours(lu) < c["quiet_h"]:
            return False   # тихая зона — данных точно не будет
        return lf is None or mins(lf) >= c["poll_interval_min"]
    else:
        # Нет истории — фазовый поллинг
        if lf is None:
            return True    # ни разу не запрашивали
        if ff is None:
            return True
        age_h = hours(ff)
        if age_h < c["search_start_h"]:
            return mins(lf) >= c["initial_interval_min"]
        else:
            return mins(lf) >= c["poll_interval_min"]


def _log_weather_event(model: str, is_new: bool, now: datetime):
    """Пишет событие в weather_log.jsonl."""
    s = _model_sched[model]
    c = MODEL_SCHED_CFG[model]
    lu = s["last_update_at"]
    ff = s["first_fetch_at"]

    if lu is not None:
        hrs = (now - lu).total_seconds() / 3600
        if hrs < c["quiet_h"]:
            mode = f"quiet_{c['quiet_h']}h"
        else:
            mode = f"search_{c['poll_interval_min']}min"
    else:
        age_h = (now - ff).total_seconds() / 3600 if ff else 0
        if age_h < c["search_start_h"]:
            mode = f"slow_{c['initial_interval_min']}min"
        else:
            mode = f"search_{c['poll_interval_min']}min"

    entry = {
        "t":          now.isoformat(),
        "model":      model,
        "new_run":    is_new,
        "mode":       mode,
        "last_update": lu.isoformat() if lu else None,
    }
    with open(WEATHER_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    label = "🆕 НОВЫЙ ПРОГОН" if is_new else "= без изменений"
    next_note = ""
    if is_new:
        next_eta = now + timedelta(hours=c["quiet_h"])
        next_note = f" → тихая зона до {next_eta.strftime('%H:%M')} UTC"
    print(f"[sched] {model}: {label}, режим={mode}{next_note}")


# ---------------------------------------------------------------------------
# Фетч погоды и симуляция — разделены
# ---------------------------------------------------------------------------

def _fetch_models(models: list, now: datetime) -> bool:
    """
    Загружает погоду для указанных моделей, сравнивает со снепшотами,
    обновляет расписание, логирует. Возвращает True если хоть одна модель обновилась.
    Вызывать только внутри захваченного _fetch_lock.
    """
    cfg = _load_config()
    gpx_path = cfg["gpx_file"]
    if not Path(gpx_path).exists():
        with _lock:
            _state["last_error"] = f"GPX не найден: {gpx_path}"
        return False
    try:
        segments = core.parse_gpx(gpx_path)
        with _lock:
            current_km = _state["current_km"]

        print(f"[weather] Загрузка {models}, km={current_km:.0f}…")

        fetched = {}
        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = {
                ex.submit(core.fetch_weather_parallel, segments,
                          cfg.get("weather_samples", 10), m,
                          cfg.get("weather_step_km", 5.0), current_km): m
                for m in models
            }
            for fut in as_completed(futs):
                m = futs[fut]
                wp = fut.result()
                fetched[m] = wp
                _inc_api_calls(len(wp))

        any_new = False
        for m, new_wp in fetched.items():
            old_wp = _model_sched[m]["last_weather"]
            is_new = _weather_changed(old_wp, new_wp)

            if _model_sched[m]["first_fetch_at"] is None:
                _model_sched[m]["first_fetch_at"] = now
            _model_sched[m]["last_fetch_at"] = now
            _model_sched[m]["last_weather"]   = new_wp
            if is_new:
                _model_sched[m]["last_update_at"] = now
                any_new = True

            _log_weather_event(m, is_new, now)

        with _lock:
            for m, wp in fetched.items():
                _state["weather_cache"][m] = wp
            _state["weather_gpx"]        = gpx_path
            _state["last_weather_fetch"] = now.isoformat()

        print(f"[weather] Готово. Новых прогонов: {sum(1 for m in fetched if _model_sched[m]['last_update_at'] == now)}/{len(fetched)}")
        return any_new

    except Exception as e:
        import traceback
        with _lock:
            _state["last_error"] = str(e)
        traceback.print_exc()
        return False


def fetch_weather(force: bool = False):
    """
    Принудительный фетч всех моделей (старт, /recalc, смена GPX).
    В фоновом цикле используется _smart_fetch вместо этой функции.
    """
    if not _fetch_lock.acquire(blocking=False):
        print("[weather] Уже идёт загрузка, пропускаем.")
        return False
    try:
        return _fetch_models(MODELS, datetime.now(timezone.utc))
    finally:
        _fetch_lock.release()


def simulate_only():
    """
    Пересчитывает симуляцию по закешированной погоде. Не делает запросов к API.
    Вызывается при любом изменении пользовательских данных.
    """
    cfg = _load_config()
    gpx_path = cfg["gpx_file"]
    if not Path(gpx_path).exists():
        with _lock:
            _state["last_error"] = f"GPX не найден: {gpx_path}"
        return

    with _lock:
        weather_by_model = _state.get("weather_cache", {})
        actual_stops     = list(_state["actual_stops"])

    if not weather_by_model:
        # Погода ещё не загружена — загружаем сейчас
        if not fetch_weather():
            return
        with _lock:
            weather_by_model = _state.get("weather_cache", {})

    start_km, start_wall, route_start = _effective_start(cfg)
    power = _get_power(cfg)

    try:
        segments = core.parse_gpx(gpx_path)
        total_km = core.total_route_km(segments)
        print(f"[sim] km={start_km:.0f}, power={power:.0f}W")

        results = {}
        sim_kwargs_base = dict(
            segments=segments,
            start_time=start_wall,
            power_w=power,
            mass_kg=cfg["mass_kg"],
            cda=cfg["cda"],
            crr=cfg["crr"],
            time_limit_h=cfg["time_limit_h"],
            max_rain_wait_h=cfg["max_rain_wait_h"],
            current_km=start_km,
            route_start_time=route_start,
            actual_stops=actual_stops,
            planned_stop_budget_h=cfg.get("planned_stop_budget_h", 0.0),
        )
        for model, weather in weather_by_model.items():
            ride = core.simulate(weather_points=weather,
                                 rain_threshold=cfg["rain_threshold"],
                                 **sim_kwargs_base)
            ride_no_rain = core.simulate(weather_points=weather,
                                         rain_threshold=9999.0,
                                         **sim_kwargs_base)
            grid         = core.grid_10km(ride)
            grid_no_rain = core.grid_10km(ride_no_rain)
            results[model] = {
                "ride":         core.ride_to_dict(ride),
                "grid":         core.ride_to_dict(grid),
                "ride_no_rain": core.ride_to_dict(ride_no_rain),
                "grid_no_rain": core.ride_to_dict(grid_no_rain),
            }

        for m, r in results.items():
            last    = r["ride"][-1]         if r["ride"]         else None
            last_nr = r["ride_no_rain"][-1] if r["ride_no_rain"] else None
            if last:
                print(f"[sim] {m}: финиш {last['wall_time'][11:16]} UTC, {last['elapsed_h']:.1f}ч")
            if last_nr:
                print(f"[sim] {m} без дождя: финиш {last_nr['wall_time'][11:16]} UTC, {last_nr['elapsed_h']:.1f}ч")

        with _lock:
            _state["models"]     = results
            _state["total_km"]   = total_km
            _state["last_calc"]  = datetime.now(timezone.utc).isoformat()
            _state["last_error"] = None
            _state["config"]     = cfg

        _save_state()
        print("[sim] Готово.")

    except Exception as e:
        import traceback
        with _lock:
            _state["last_error"] = str(e)
            _state["last_calc"]  = datetime.now(timezone.utc).isoformat()
        traceback.print_exc()


def recalculate():
    """Полный цикл: фетч погоды + симуляция. Для старта и ручного /recalc."""
    fetch_weather()
    simulate_only()


def _bg_loop():
    """
    Умный фоновый цикл: проверяет раз в минуту, нужно ли фетчить каждую модель.
    Расписание: тихая зона → медленный поллинг → быстрый поллинг при ожидании нового прогона.
    """
    while True:
        time.sleep(60)
        now = datetime.now(timezone.utc)
        to_fetch = [m for m in MODELS if _should_fetch_model(m, now)]
        if not to_fetch:
            continue
        if not _fetch_lock.acquire(blocking=False):
            print("[bg] Фетч уже идёт, пропускаем тик.")
            continue
        try:
            _fetch_models(to_fetch, now)
        finally:
            _fetch_lock.release()
        # Пересчитываем симуляцию после любого успешного фетча
        simulate_only()


# ---------------------------------------------------------------------------
# Калибровка мощности по реальной скорости
# ---------------------------------------------------------------------------

def _calibrate_async(cfg, segments, weather_by_model,
                      from_km, pos_km, from_wall, pos_wall):
    """Вычисляет эффективную мощность по последнему отрезку from_km→pos_km."""
    with _lock:
        stops = list(_state["actual_stops"])
        manual = _state["manual_power"]

    if manual:
        return  # не перетираем вручную заданную мощность

    stop_h = sum(s.get("duration_h", 0) for s in stops
                 if from_km <= s.get("km", 0) <= pos_km)

    total_elapsed_h = (pos_wall - from_wall).total_seconds() / 3600.0
    riding_h = total_elapsed_h - stop_h

    if riding_h < 0.5 or pos_km - from_km < 5:
        return

    weather = weather_by_model.get("icon_seamless") or next(iter(weather_by_model.values()))
    new_power = core.calibrate_power(
        segments=segments,
        from_km=from_km, to_km=pos_km,
        start_time=from_wall,
        actual_riding_h=riding_h,
        weather_points=weather,
        mass_kg=cfg["mass_kg"],
        cda=cfg["cda"],
        crr=cfg["crr"],
        default_power=cfg["power_w"],
    )
    new_power = round(new_power, 1)

    with _lock:
        _state["effective_power"] = new_power

    _log_power("calibration", new_power,
               from_km=from_km, to_km=pos_km, riding_h=round(riding_h, 2))


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
            "manual_power":       _state["manual_power"],
            "last_calc":          _state["last_calc"],
            "last_error":         _state["last_error"],
            "total_km":           _state["total_km"],
            "config":             _state["config"],
            "models":             {m: _state["models"][m]["grid"] for m in MODELS},
            "models_no_rain":     {m: _state["models"][m].get("grid_no_rain", []) for m in MODELS},
            "rides_last":         {m: (_state["models"][m]["ride"][-1]
                                       if _state["models"][m]["ride"] else None)
                                   for m in MODELS},
            "rides_last_no_rain": {m: (_state["models"][m]["ride_no_rain"][-1]
                                       if _state["models"][m].get("ride_no_rain") else None)
                                   for m in MODELS},
            "api_calls_today":    _state["api_calls_today"],
            "last_weather_fetch": _state.get("last_weather_fetch"),
        }
    local_tz = _route_tz()

    def _fmt_local(iso_str):
        if not iso_str:
            return "—"
        try:
            return datetime.fromisoformat(iso_str).astimezone(local_tz).strftime("%d.%m %H:%M")
        except Exception:
            return iso_str[:16]

    model_update_times = {m: _model_update_time(m).astimezone(local_tz).strftime("%d.%m %H:%M") for m in MODELS}
    last_calc_local    = _fmt_local(state["last_calc"])
    last_weather_local = _fmt_local(state["last_weather_fetch"])
    cfg = _load_config()
    gpx_name = Path(cfg.get("gpx_file", "")).name or "не задан"
    planned_h   = int(cfg.get("planned_stop_budget_h", 0))
    planned_min = round((cfg.get("planned_stop_budget_h", 0) - planned_h) * 60)
    route_start = _start_wall(cfg)
    start_msk = route_start.astimezone(MSK).strftime("%d.%m %H:%M")
    start_date_val = route_start.astimezone(MSK).strftime("%Y-%m-%d")
    start_time_val = route_start.astimezone(MSK).strftime("%H:%M")
    deadline = route_start + timedelta(hours=cfg.get("time_limit_h", 40))
    deadline_msk = deadline.astimezone(MSK).strftime("%d.%m %H:%M")
    return render_template("index.html", state=state, model_list=MODELS,
                           model_update_times=model_update_times,
                           last_calc_local=last_calc_local,
                           last_weather_local=last_weather_local,
                           start_msk=start_msk,
                           start_date_val=start_date_val,
                           start_time_val=start_time_val,
                           deadline_msk=deadline_msk,
                           deadline_iso=deadline.isoformat(),
                           gpx_name=gpx_name,
                           planned_h=planned_h,
                           planned_min=planned_min)


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
            prev_km   = _state["current_km"]
            prev_pos  = _state["position_time"]
            _state["prev_km"]            = prev_km
            _state["prev_position_time"] = prev_pos
            _state["current_km"]         = max(0.0, km)
            _state["position_time"]      = pos_iso

        # Пересчёт + калибровка мощности
        def _recalc_with_calib():
            cfg = _load_config()
            if not Path(cfg["gpx_file"]).exists():
                return
            segments = core.parse_gpx(cfg["gpx_file"])
            route_start = _start_wall(cfg)

            # from_km/from_wall — предыдущая зафиксированная позиция
            if prev_pos and prev_km > 0:
                from_km   = prev_km
                from_wall = datetime.fromisoformat(prev_pos)
            else:
                from_km   = 0.0
                from_wall = route_start

            try:
                with _lock:
                    weather_by_model = dict(_state["weather_cache"])
                if weather_by_model:
                    _calibrate_async(cfg, segments, weather_by_model,
                                      from_km, km, from_wall, pos_wall)
            except Exception:
                pass

            simulate_only()

        threading.Thread(target=_recalc_with_calib, daemon=True).start()

    except (KeyError, ValueError):
        pass
    return redirect(url_for("index"))


@app.route("/add_stop", methods=["POST"])
def add_stop():
    try:
        km         = float(request.form["stop_km"])
        time_str   = request.form.get("stop_time", "").strip()
        h          = int(request.form.get("stop_hours", 0) or 0)
        m          = int(request.form.get("stop_minutes", 0) or 0)
        dur_h      = round(h + m / 60, 4)
        note       = request.form.get("stop_note", "").strip()

        stop = {"km": km, "time_msk": time_str,
                "duration_h": round(dur_h, 2), "note": note}

        with _lock:
            _state["actual_stops"].append(stop)

        _save_state()
        threading.Thread(target=simulate_only, daemon=True).start()
    except (KeyError, ValueError):
        pass
    return redirect(url_for("index"))


@app.route("/clear_stops", methods=["POST"])
def clear_stops():
    with _lock:
        _state["actual_stops"] = []
    _save_state()
    threading.Thread(target=simulate_only, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/clear_state", methods=["POST"])
def clear_state():
    """Сброс позиции, стоянок и калибровки — оставляем только конфиг."""
    with _lock:
        _state["current_km"]          = 0.0
        _state["position_time"]       = None
        _state["prev_km"]             = 0.0
        _state["prev_position_time"]  = None
        _state["actual_stops"]        = []
        _state["effective_power"]     = None
        _state["manual_power"]        = False
    _log_power("reset", None)
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    threading.Thread(target=simulate_only, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/set_power", methods=["POST"])
def set_power():
    try:
        pw = float(request.form["power_w"])
        if pw <= 0:
            raise ValueError
        with _lock:
            _state["effective_power"] = round(pw, 1)
            _state["manual_power"]    = True
        _log_power("manual", round(pw, 1))
        _save_state()
        threading.Thread(target=simulate_only, daemon=True).start()
    except (KeyError, ValueError):
        pass
    return redirect(url_for("index"))


@app.route("/reset_power", methods=["POST"])
def reset_power():
    with _lock:
        _state["effective_power"] = None
        _state["manual_power"]    = False
    _log_power("reset", None)
    threading.Thread(target=simulate_only, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/upload_gpx", methods=["POST"])
def upload_gpx():
    f = request.files.get("gpx_file")
    if not f or not f.filename.endswith(".gpx"):
        return redirect(url_for("index"))
    dest = Path(f.filename)
    f.save(str(dest))
    cfg = _load_config()
    cfg["gpx_file"] = str(dest)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    print(f"[gpx] Загружен файл: {dest}")
    # Новый GPX — нужен новый фетч погоды
    threading.Thread(target=recalculate, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/set_planned_stops", methods=["POST"])
def set_planned_stops():
    try:
        h   = int(request.form.get("budget_h", 0) or 0)
        m   = int(request.form.get("budget_min", 0) or 0)
        budget = round(h + m / 60, 4)
        cfg = _load_config()
        cfg["planned_stop_budget_h"] = budget
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"[cfg] Бюджет стоянок: {budget:.2f} ч")
        threading.Thread(target=simulate_only, daemon=True).start()
    except (KeyError, ValueError):
        pass
    return redirect(url_for("index"))


@app.route("/set_start_time", methods=["POST"])
def set_start_time():
    try:
        date_str = request.form["start_date"].strip()
        time_str = request.form["start_time_msk"].strip()
        naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        wall_msk = datetime(naive.year, naive.month, naive.day,
                            naive.hour, naive.minute, tzinfo=MSK)
        iso_utc = wall_msk.astimezone(timezone.utc).isoformat()
        cfg = _load_config()
        cfg["start_time"] = iso_utc
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"[cfg] Старт обновлён: {iso_utc}")
        threading.Thread(target=simulate_only, daemon=True).start()
    except (KeyError, ValueError):
        pass
    return redirect(url_for("index"))


@app.route("/recalc", methods=["POST"])
def force_recalc():
    # FAB — форсированный полный пересчёт с новым фетчем погоды
    threading.Thread(target=recalculate, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/api/state")
def api_state():
    with _lock:
        return jsonify({
            "current_km":      _state["current_km"],
            "effective_power": _state["effective_power"],
            "last_calc":          _state["last_calc"],
            "last_weather_fetch": _state.get("last_weather_fetch"),
            "last_error":         _state["last_error"],
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
