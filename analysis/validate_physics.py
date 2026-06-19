"""
analysis/validate_physics.py — сверка физики (core.solve_speed) с РЕАЛЬНЫМ прохождением трека.

Идея: для каждого отрезка реального трека известны
  - фактическая скорость (из time + координат),
  - измеренная мощность (power meter),
  - уклон (из высот GPX/FIT),
а ветер берём из снепшота погоды. Предсказываем скорость по физике и сравниваем с фактом;
опция --fit подбирает CdA/Crr, лучше всего объясняющие реальные данные.

Примеры:
  python analysis/validate_physics.py analysis/data/ride.fit --mass 90 --fit
  python analysis/validate_physics.py analysis/data/ride.gpx --weather const:3,270
  python analysis/validate_physics.py analysis/data/ride.fit --weather snapshot:weather_now.jsonl
"""
import os
import sys
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, ROOT]                 # core.py (корень) + parse_ride.py (рядом)

import core
from parse_ride import parse_ride, TrackPoint


@dataclass
class RealSeg:
    t: datetime
    lat: float
    lon: float
    dist_m: float
    dt_s: float
    speed_kmh: float
    grade: float
    bearing: float
    power: Optional[float]


def build_segments(points: List[TrackPoint],
                   min_dist_m: float = 10.0,
                   max_speed_kmh: float = 90.0) -> List[RealSeg]:
    """Соседние точки -> отрезки с факт. скоростью, уклоном, азимутом, мощностью."""
    segs: List[RealSeg] = []
    for a, b in zip(points, points[1:]):
        dt = (b.t - a.t).total_seconds()
        if dt <= 0:
            continue
        dist = core._haversine(a.lat, a.lon, b.lat, b.lon)
        if dist < min_dist_m:
            continue
        speed_kmh = dist / dt * 3.6
        if speed_kmh > max_speed_kmh:        # выброс GPS
            continue
        grade = ((b.ele - a.ele) / dist) if (a.ele is not None and b.ele is not None) else 0.0
        bearing = core._bearing(a.lat, a.lon, b.lat, b.lon)
        pw = [x for x in (a.power, b.power) if x is not None]
        segs.append(RealSeg(
            t=a.t, lat=(a.lat + b.lat) / 2, lon=(a.lon + b.lon) / 2,
            dist_m=dist, dt_s=dt, speed_kmh=speed_kmh,
            grade=grade, bearing=bearing,
            power=(sum(pw) / len(pw) if pw else None)))
    return segs


# --- источники ветра -------------------------------------------------------

def weather_const(wind_ms=0.0, wind_dir_deg=0.0):
    return lambda lat, lon, t: (wind_ms, wind_dir_deg)


def weather_era5(points: List[TrackPoint]):
    """ERA5-реанализ (archive-api.open-meteo.com, бесплатно) для центра трека и его дат."""
    import requests
    lat = sum(p.lat for p in points) / len(points)
    lon = sum(p.lon for p in points) / len(points)
    r = requests.get("https://archive-api.open-meteo.com/v1/archive", params={
        "latitude": lat, "longitude": lon,
        "start_date": points[0].t.date().isoformat(),
        "end_date":   points[-1].t.date().isoformat(),
        "hourly": "wind_speed_10m,wind_direction_10m,precipitation",
        "wind_speed_unit": "ms", "timezone": "UTC"}, timeout=30)
    r.raise_for_status()
    h = r.json()["hourly"]
    times = [datetime.fromisoformat(x).replace(tzinfo=timezone.utc) for x in h["time"]]
    ws, wd = h["wind_speed_10m"], h["wind_direction_10m"]

    def at(la, lo, t):
        i = min(range(len(times)), key=lambda k: abs((times[k] - t).total_seconds()))
        return (ws[i] or 0.0, wd[i] or 0.0)
    return at


def weather_from_snapshot(path):
    """Ветер из нашего weather_now.jsonl: ближайшая по времени+координате точка."""
    samples = []
    for line in open(path, encoding='utf-8'):
        if not line.strip():
            continue
        r = json.loads(line)
        t = datetime.fromisoformat(r["t"])
        for p in r.get("points", []):
            samples.append((t, p["lat"], p["lon"], p.get("wind_ms", 0.0), p.get("wind_dir", 0.0)))
    if not samples:
        raise SystemExit(f"В {path} нет точек погоды")

    def at(la, lo, t):
        best = min(samples, key=lambda s: abs((s[0] - t).total_seconds()) + 5000 * (abs(s[1] - la) + abs(s[2] - lo)))
        return (best[3], best[4])
    return at


# --- сверка ----------------------------------------------------------------

def validate(segs, weather_at, mass, cda, crr):
    rows = []
    for s in segs:
        if s.power is None:
            continue
        ws, wd = weather_at(s.lat, s.lon, s.t)
        hw = core.effective_headwind(ws, wd, s.bearing)
        v_pred = core.solve_speed(s.power, s.grade, hw, mass, cda, crr) * 3.6
        rows.append((s, v_pred, s.speed_kmh - v_pred))
    return rows


def stats(rows):
    if not rows:
        return None
    res = [r[2] for r in rows]
    n = len(res)
    return {
        "segments":        n,
        "mean_resid_kmh":  round(sum(res) / n, 2),                 # факт − прогноз
        "rmse_kmh":        round(math.sqrt(sum(x * x for x in res) / n), 2),
        "mean_actual_kmh": round(sum(r[0].speed_kmh for r in rows) / n, 1),
        "mean_pred_kmh":   round(sum(r[1] for r in rows) / n, 1),
    }


def fit_cda_crr(segs, weather_at, mass):
    """Грубый перебор CdA/Crr под минимальный RMSE по реальным отрезкам."""
    best = None
    cda = 0.20
    while cda <= 0.601:
        crr = 0.003
        while crr <= 0.0081:
            st = stats(validate(segs, weather_at, mass, cda, round(crr, 4)))
            if st and (best is None or st["rmse_kmh"] < best["rmse_kmh"]):
                best = {**st, "cda": round(cda, 2), "crr": round(crr, 4)}
            crr += 0.0005
        cda += 0.02
    return best


def _make_weather(spec, points):
    if spec.startswith("const:"):
        w, d = spec[6:].split(",")
        return weather_const(float(w), float(d))
    if spec.startswith("snapshot:"):
        return weather_from_snapshot(spec[9:])
    return weather_era5(points)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Сверка физики с реальным прохождением трека")
    ap.add_argument("ride", help="файл записи (.fit/.tcx/.gpx)")
    ap.add_argument("--mass", type=float, default=85.0)
    ap.add_argument("--cda", type=float, default=0.36)
    ap.add_argument("--crr", type=float, default=0.004)
    ap.add_argument("--weather", default="era5", help="era5 | const:WIND,DIR | snapshot:PATH")
    ap.add_argument("--fit", action="store_true", help="подобрать CdA/Crr под данные")
    a = ap.parse_args()

    points = parse_ride(a.ride)
    segs = build_segments(points)
    n_pw = sum(1 for s in segs if s.power is not None)
    print(f"Точек: {len(points)}, отрезков: {len(segs)}, с мощностью: {n_pw}")
    if n_pw == 0:
        print("⚠ В файле нет мощности — нужен .fit/.tcx или Strava streams.")
        return

    weather_at = _make_weather(a.weather, points)
    print(f"CdA={a.cda} Crr={a.crr} mass={a.mass}:", stats(validate(segs, weather_at, a.mass, a.cda, a.crr)))
    if a.fit:
        print("Лучший подбор:", fit_cda_crr(segs, weather_at, a.mass))


if __name__ == "__main__":
    main()
