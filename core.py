"""
Велосипедный оптимизатор маршрута.
Физика, парсинг GPX, погода, симуляция с остановками.
"""

import math
import gpxpy
import requests
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Структуры данных
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    lat: float
    lon: float
    ele: float
    dist_m: float
    cum_dist_m: float
    bearing_deg: float
    grade: float          # (ele_b - ele_a) / dist_m, безразмерный


@dataclass
class WeatherPoint:
    lat: float
    lon: float
    times: List[datetime]
    wind_speed: List[float]   # м/с
    wind_dir: List[float]     # откуда дует (метеорологическое соглашение)
    precip: List[float]       # мм/ч


@dataclass
class RidePoint:
    km: float
    lat: float
    lon: float
    wall_time: datetime
    elapsed_h: float
    speed_ms: float
    precip_mm_h: float
    wind_ms: float
    headwind_ms: float
    stop_here_h: float = 0.0    # длительность стоянки ПОСЛЕ этой точки
    stop_reason: str = ""


# ---------------------------------------------------------------------------
# GPX
# ---------------------------------------------------------------------------

def parse_gpx(path: str) -> List[Segment]:
    with open(path, encoding="utf-8") as f:
        gpx = gpxpy.parse(f)

    points = []
    for track in gpx.tracks:
        for seg in track.segments:
            points.extend(seg.points)
    for route in gpx.routes:
        points.extend(route.points)

    if len(points) < 2:
        raise ValueError("GPX должен содержать минимум 2 точки")

    segments = []
    cum = 0.0
    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        d = _haversine(a.latitude, a.longitude, b.latitude, b.longitude)
        if d < 0.1:
            continue
        ele_a = a.elevation or 0.0
        ele_b = b.elevation or 0.0
        grade = (ele_b - ele_a) / d
        bearing = _bearing(a.latitude, a.longitude, b.latitude, b.longitude)
        cum += d
        segments.append(Segment(
            lat=a.latitude, lon=a.longitude, ele=ele_a,
            dist_m=d, cum_dist_m=cum,
            bearing_deg=bearing, grade=grade,
        ))

    return segments


def _haversine(lat1, lon1, lat2, lon2):
    R = 6_371_000.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing(lat1, lon1, lat2, lon2):
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dλ = math.radians(lon2 - lon1)
    x = math.sin(dλ) * math.cos(φ2)
    y = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(dλ)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


# ---------------------------------------------------------------------------
# Физика
# ---------------------------------------------------------------------------

def effective_headwind(wind_speed_ms: float, wind_dir_deg: float, bearing_deg: float) -> float:
    """Положительное значение = встречный, отрицательное = попутный."""
    wind_going = (wind_dir_deg + 180) % 360
    angle_diff = math.radians(wind_going - bearing_deg)
    return -wind_speed_ms * math.cos(angle_diff)


_MAX_SPEED_MS = 14.0   # ~50 км/ч — потолок скорости (спуск с торможением)


def solve_speed(power_w: float, grade: float, headwind_ms: float,
                mass_kg=85.0, cda=0.36, crr=0.004, rho=1.225) -> float:
    """
    Решает уравнение баланса мощности методом Ньютона.
    P = (F_aero + F_roll + F_grav) * v

    На спусках: если сила тяжести превышает сопротивление качению,
    возможна скорость выше 'без педалей'. Решение ищем от высокой стартовой
    точки и ограничиваем _MAX_SPEED_MS.
    """
    g = 9.81
    grade_rad = math.atan(grade)
    F_roll = crr * mass_kg * g * math.cos(grade_rad)
    F_grav = mass_kg * g * math.sin(grade_rad)   # < 0 на спуске
    net_static = F_roll + F_grav

    if net_static <= 0:
        # Гравитация превышает качение — скорость без педалей уже высокая.
        # Скорость свободного качения: 0.5*rho*cda*(v+hw)^2 = -net_static
        # Приближение без встречного ветра:
        v_free = math.sqrt(max(0.0, -2 * net_static / (rho * cda)))
        if v_free >= _MAX_SPEED_MS:
            return _MAX_SPEED_MS
        # Стартуем чуть выше v_free, чтобы Newton шёл в нужную сторону
        v = min(_MAX_SPEED_MS, v_free + 1.0)
    else:
        v = max(1.0, (power_w / (net_static + 1.0)) ** (1.0 / 3.0))

    for _ in range(200):
        vw = v + headwind_ms
        F_aero = 0.5 * rho * cda * vw ** 2
        P_calc = (F_aero + F_roll + F_grav) * v
        dP = (F_aero + F_roll + F_grav) + v * rho * cda * vw
        delta = (P_calc - power_w) / dP if abs(dP) > 1e-9 else 0.0
        v = max(0.3, min(_MAX_SPEED_MS, v - delta))
        if abs(delta) < 1e-5:
            break

    return min(v, _MAX_SPEED_MS)


# ---------------------------------------------------------------------------
# Погода
# ---------------------------------------------------------------------------

def fetch_weather_parallel(segments: List[Segment], n_samples: int = 10,
                           model: str = "icon_seamless") -> List[WeatherPoint]:
    """Параллельный фетч погоды — все точки одновременно."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    total = segments[-1].cum_dist_m
    targets = [i * total / (n_samples - 1) for i in range(n_samples)]
    sample_segs = [min(segments, key=lambda s: abs(s.cum_dist_m - t)) for t in targets]

    seen, unique = set(), []
    for s in sample_segs:
        key = (round(s.lat, 3), round(s.lon, 3))
        if key not in seen:
            seen.add(key)
            unique.append(s)

    def _fetch_one(seg):
        params = {
            "latitude": seg.lat, "longitude": seg.lon,
            "hourly": "wind_speed_10m,wind_direction_10m,precipitation",
            "wind_speed_unit": "ms", "timezone": "UTC",
            "forecast_days": 3, "models": model,
        }
        for attempt in range(4):
            r = requests.get("https://api.open-meteo.com/v1/forecast",
                             params=params, timeout=15)
            if r.status_code == 429:
                import time as _t
                _t.sleep(2 ** attempt)   # 1, 2, 4, 8 сек
                continue
            r.raise_for_status()
            h = r.json()["hourly"]
            times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
                     for t in h["time"]]
            return WeatherPoint(lat=seg.lat, lon=seg.lon, times=times,
                                wind_speed=h["wind_speed_10m"],
                                wind_dir=h["wind_direction_10m"],
                                precip=h["precipitation"])
        r.raise_for_status()   # последняя попытка — пробрасываем исключение

    result = [None] * len(unique)
    # max_workers=3 — не ронять Open-Meteo rate limit
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(_fetch_one, s): i for i, s in enumerate(unique)}
        for fut in as_completed(futures):
            result[futures[fut]] = fut.result()
    return result


def fetch_weather(segments: List[Segment], n_samples: int = 10,
                  model: str = "icon_seamless") -> List[WeatherPoint]:
    """Запрашивает погоду в n_samples равноотстоящих точках маршрута."""
    total = segments[-1].cum_dist_m
    targets = [i * total / (n_samples - 1) for i in range(n_samples)]

    sample_segs = []
    for t in targets:
        seg = min(segments, key=lambda s: abs(s.cum_dist_m - t))
        sample_segs.append(seg)

    # Убираем дубликаты по rounded координатам
    seen, unique = set(), []
    for s in sample_segs:
        key = (round(s.lat, 3), round(s.lon, 3))
        if key not in seen:
            seen.add(key)
            unique.append(s)

    result = []
    for seg in unique:
        params = {
            "latitude": seg.lat, "longitude": seg.lon,
            "hourly": "wind_speed_10m,wind_direction_10m,precipitation",
            "wind_speed_unit": "ms",
            "timezone": "UTC",
            "forecast_days": 3,
            "models": model,
        }
        r = requests.get("https://api.open-meteo.com/v1/forecast",
                         params=params, timeout=15)
        r.raise_for_status()
        h = r.json()["hourly"]
        times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
                 for t in h["time"]]
        result.append(WeatherPoint(
            lat=seg.lat, lon=seg.lon, times=times,
            wind_speed=h["wind_speed_10m"],
            wind_dir=h["wind_direction_10m"],
            precip=h["precipitation"],
        ))

    return result


def _nearest_weather(wps: List[WeatherPoint], lat: float, lon: float) -> WeatherPoint:
    return min(wps, key=lambda w: (w.lat - lat) ** 2 + (w.lon - lon) ** 2)


def _interp(wp: WeatherPoint, t: datetime) -> Tuple[float, float, float]:
    """Линейная интерполяция погоды в момент t."""
    ts = wp.times
    if t <= ts[0]:
        return wp.wind_speed[0], wp.wind_dir[0], wp.precip[0]
    if t >= ts[-1]:
        return wp.wind_speed[-1], wp.wind_dir[-1], wp.precip[-1]
    for i in range(len(ts) - 1):
        if ts[i] <= t <= ts[i + 1]:
            f = (t - ts[i]).total_seconds() / 3600
            ws = wp.wind_speed[i] + f * (wp.wind_speed[i + 1] - wp.wind_speed[i])
            pr = wp.precip[i] + f * (wp.precip[i + 1] - wp.precip[i])
            return ws, wp.wind_dir[i], max(0.0, pr)
    return wp.wind_speed[-1], wp.wind_dir[-1], wp.precip[-1]


def precip_at_location_future(wp: WeatherPoint, from_time: datetime,
                               max_h: float = 6.0) -> List[Tuple[float, float]]:
    """Прогноз осадков в данной точке на ближайшие max_h часов. [(hours_from_now, mm_h)]"""
    result = []
    for i, t in enumerate(wp.times):
        delta_h = (t - from_time).total_seconds() / 3600
        if 0 <= delta_h <= max_h:
            result.append((delta_h, wp.precip[i]))
    return result


# ---------------------------------------------------------------------------
# Симуляция с планировщиком остановок
# ---------------------------------------------------------------------------

def simulate(
    segments: List[Segment],
    start_time: datetime,
    weather_points: List[WeatherPoint],
    power_w: float = 150.0,
    mass_kg: float = 85.0,
    cda: float = 0.36,
    crr: float = 0.004,
    time_limit_h: float = 40.0,
    overnight_km: float = 300.0,
    overnight_h: float = 8.0,
    rain_threshold: float = 0.5,
    max_rain_wait_h: float = 3.0,
    current_km: float = 0.0,
    sample_every_km: float = 1.0,
    route_start_time: Optional[datetime] = None,  # фиксированный старт для elapsed_h
) -> List[RidePoint]:
    """
    Симулирует поездку сегмент за сегментом.

    start_time       — стена в точке current_km (откуда начинаем считать погоду/скорость).
    route_start_time — фиксированный старт маршрута (6:50); elapsed_h считается от него.
                       Если None — совпадает с start_time.
    """
    if not weather_points:
        raise ValueError("Нет данных о погоде")

    current_time = start_time
    # Смещение elapsed_h: сколько часов прошло от route_start до start_time
    _route_start = route_start_time if route_start_time is not None else start_time
    elapsed_h = (start_time - _route_start).total_seconds() / 3600.0
    last_sample_km = current_km - 1.0

    overnight_done = False
    in_rain_stop = False
    rain_stop_budget_h = time_limit_h  # будет уточнён

    ride: List[RidePoint] = []

    for seg in segments:
        cum_km = seg.cum_dist_m / 1000.0

        if cum_km < current_km:
            continue

        wp = _nearest_weather(weather_points, seg.lat, seg.lon)
        ws, wd, precip = _interp(wp, current_time)
        hw = effective_headwind(ws, wd, seg.bearing_deg)
        speed = solve_speed(power_w, seg.grade, hw, mass_kg, cda, crr)
        seg_time_h = (seg.dist_m / speed) / 3600.0

        stop_h = 0.0
        stop_reason = ""

        # --- Ночёвка ---
        if (not overnight_done
                and cum_km >= overnight_km
                and (time_limit_h - elapsed_h) > overnight_h + 1):
            stop_h = overnight_h
            stop_reason = "ночёвка"
            overnight_done = True

        # --- Дождевая остановка ---
        elif precip >= rain_threshold and not in_rain_stop:
            future = precip_at_location_future(wp, current_time, max_rain_wait_h)
            # Ищем момент, когда дождь прекратится
            clear_h: Optional[float] = None
            for h_offset, pr in future:
                if pr < rain_threshold:
                    clear_h = h_offset
                    break

            time_budget_left = time_limit_h - elapsed_h
            if clear_h is not None and clear_h <= max_rain_wait_h and clear_h <= time_budget_left:
                # Стоим до конца дождя + 15 мин буфер
                wait = min(clear_h + 0.25, max_rain_wait_h, time_budget_left)
                stop_h = wait
                stop_reason = f"дождь {precip:.1f}мм/ч, жду {wait*60:.0f}мин"
                in_rain_stop = True

        # Сбрасываем флаг дождевой стоянки, когда снова сухо
        if precip < rain_threshold:
            in_rain_stop = False

        # --- Запись точки ---
        if cum_km - last_sample_km >= sample_every_km:
            ride.append(RidePoint(
                km=round(cum_km, 1),
                lat=seg.lat, lon=seg.lon,
                wall_time=current_time,
                elapsed_h=round(elapsed_h, 3),
                speed_ms=round(speed, 2),
                precip_mm_h=round(precip, 2),
                wind_ms=round(ws, 1),
                headwind_ms=round(hw, 2),
                stop_here_h=round(stop_h, 2),
                stop_reason=stop_reason,
            ))
            last_sample_km = cum_km

        current_time += timedelta(hours=seg_time_h + stop_h)
        elapsed_h += seg_time_h + stop_h

    return ride


# ---------------------------------------------------------------------------
# Вспомогательные утилиты
# ---------------------------------------------------------------------------

def total_route_km(segments: List[Segment]) -> float:
    return segments[-1].cum_dist_m / 1000.0 if segments else 0.0


def grid_10km(ride: List[RidePoint]) -> List[RidePoint]:
    """
    Точки каждые 10 км + все точки с плановыми остановками.
    Остановки всегда попадают в сетку независимо от км-шага.
    """
    result = []
    next_km = 0.0
    for rp in ride:
        if rp.stop_here_h > 0:
            result.append(rp)
            next_km = rp.km + 10.0   # следующий 10км-маркер от точки стоянки
        elif rp.km >= next_km:
            result.append(rp)
            next_km = rp.km + 10.0
    return result


def _sim_riding_time(segments: List[Segment], from_km: float, to_km: float,
                     start_time: datetime, weather_points: List[WeatherPoint],
                     power_w: float, mass_kg: float, cda: float, crr: float) -> float:
    """Чистое время езды (без стоянок) на отрезке from_km..to_km."""
    current_time = start_time
    total_h = 0.0
    for s in segments:
        km = s.cum_dist_m / 1000.0
        if km < from_km or km > to_km:
            continue
        wp = _nearest_weather(weather_points, s.lat, s.lon)
        ws, wd, _ = _interp(wp, current_time)
        hw = effective_headwind(ws, wd, s.bearing_deg)
        v = solve_speed(power_w, s.grade, hw, mass_kg, cda, crr)
        dt = s.dist_m / v / 3600.0
        total_h += dt
        current_time += timedelta(hours=dt)
    return total_h


def calibrate_power(segments: List[Segment],
                    from_km: float, to_km: float,
                    start_time: datetime,
                    actual_riding_h: float,
                    weather_points: List[WeatherPoint],
                    mass_kg: float, cda: float, crr: float,
                    default_power: float) -> float:
    """
    Бинарный поиск мощности, при которой симуляция даёт actual_riding_h
    на отрезке from_km..to_km.

    actual_riding_h — реальное время в движении (общее время минус стоянки).
    """
    if actual_riding_h <= 0 or to_km <= from_km:
        return default_power

    lo, hi = 30.0, 500.0
    for _ in range(30):
        mid = (lo + hi) / 2.0
        t = _sim_riding_time(segments, from_km, to_km, start_time,
                             weather_points, mid, mass_kg, cda, crr)
        if t < actual_riding_h:
            hi = mid   # симуляция быстрее реальности → мощность завышена
        else:
            lo = mid   # симуляция медленнее → мощность занижена

    result = (lo + hi) / 2.0
    # Ограничиваем разумным диапазоном от дефолта
    return max(default_power * 0.35, min(default_power * 2.5, result))


def ride_to_dict(ride: List[RidePoint]) -> list:
    out = []
    for rp in ride:
        out.append({
            "km": rp.km,
            "lat": rp.lat,
            "lon": rp.lon,
            "wall_time": rp.wall_time.isoformat(),
            "elapsed_h": rp.elapsed_h,
            "speed_kmh": round(rp.speed_ms * 3.6, 1),
            "precip_mm_h": rp.precip_mm_h,
            "wind_ms": rp.wind_ms,
            "headwind_ms": rp.headwind_ms,
            "stop_here_h": rp.stop_here_h,
            "stop_reason": rp.stop_reason,
        })
    return out
