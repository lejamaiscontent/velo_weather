"""
analysis/parse_ride.py — парсер записи прохождения трека в нормализованные точки.

Поддержка: .fit (нужен `pip install fitparse`), .tcx, .gpx.
Мощность есть в FIT и TCX (<Watts>); в Strava-экспорте GPX её обычно НЕТ.

CLI:  python analysis/parse_ride.py <файл>   — печатает сводку (точки/время/наличие power)
"""
import os
import sys
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class TrackPoint:
    t: datetime
    lat: float
    lon: float
    ele: Optional[float] = None
    power: Optional[float] = None
    hr: Optional[float] = None
    cad: Optional[float] = None
    temp: Optional[float] = None


def _local(tag: str) -> str:
    """Локальное имя XML-тега без namespace."""
    return tag.rsplit('}', 1)[-1].lower()


def _parse_time(s: str) -> datetime:
    dt = datetime.fromisoformat(s.strip().replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_gpx(path) -> List[TrackPoint]:
    root = ET.parse(path).getroot()
    pts: List[TrackPoint] = []
    for trkpt in root.iter():
        if _local(trkpt.tag) != 'trkpt':
            continue
        p = TrackPoint(t=None, lat=float(trkpt.get('lat')), lon=float(trkpt.get('lon')))
        for ch in trkpt.iter():
            lt, txt = _local(ch.tag), (ch.text or '').strip()
            if not txt:
                continue
            if lt == 'time':                     p.t = _parse_time(txt)
            elif lt == 'ele':                    p.ele = float(txt)
            elif lt in ('power', 'watts', 'pwr'): p.power = float(txt)
            elif lt == 'hr':                     p.hr = float(txt)
            elif lt == 'cad':                    p.cad = float(txt)
            elif lt in ('atemp', 'temp'):        p.temp = float(txt)
        if p.t is not None:
            pts.append(p)
    return pts


def parse_tcx(path) -> List[TrackPoint]:
    root = ET.parse(path).getroot()
    pts: List[TrackPoint] = []
    for tp in root.iter():
        if _local(tp.tag) != 'trackpoint':
            continue
        p = TrackPoint(t=None, lat=None, lon=None)
        for ch in tp.iter():
            lt, txt = _local(ch.tag), (ch.text or '').strip()
            if not txt:
                continue
            if lt == 'time':              p.t = _parse_time(txt)
            elif lt == 'latitudedegrees':  p.lat = float(txt)
            elif lt == 'longitudedegrees': p.lon = float(txt)
            elif lt == 'altitudemeters':   p.ele = float(txt)
            elif lt == 'watts':            p.power = float(txt)
            elif lt == 'value':            p.hr = float(txt)    # HeartRateBpm/Value
            elif lt == 'cadence':          p.cad = float(txt)
        if p.t is not None and p.lat is not None and p.lon is not None:
            pts.append(p)
    return pts


def parse_fit(path) -> List[TrackPoint]:
    try:
        from fitparse import FitFile
    except ImportError:
        raise SystemExit("Для .fit нужен fitparse:  pip install fitparse")
    SC = 180.0 / 2 ** 31            # полуокружности FIT -> градусы
    pts: List[TrackPoint] = []
    for rec in FitFile(path).get_messages('record'):
        d = {f.name: f.value for f in rec.fields}
        lat, lon = d.get('position_lat'), d.get('position_long')
        if lat is None or lon is None:
            continue
        t = d.get('timestamp')
        if t is not None and t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if t is None:
            continue
        pts.append(TrackPoint(
            t=t, lat=lat * SC, lon=lon * SC,
            ele=d.get('enhanced_altitude', d.get('altitude')),
            power=d.get('power'), hr=d.get('heart_rate'),
            cad=d.get('cadence'), temp=d.get('temperature')))
    return pts


def parse_ride(path) -> List[TrackPoint]:
    ext = os.path.splitext(str(path))[1].lower()
    if ext == '.gpx': return parse_gpx(path)
    if ext == '.tcx': return parse_tcx(path)
    if ext == '.fit': return parse_fit(path)
    raise ValueError(f"Неизвестный формат: {ext} (ожидаю .gpx/.tcx/.fit)")


def summary(points: List[TrackPoint]) -> dict:
    if not points:
        return {"points": 0}
    have = lambda a: sum(1 for p in points if getattr(p, a) is not None)
    return {
        "points":     len(points),
        "t_start":    points[0].t.isoformat(),
        "t_end":      points[-1].t.isoformat(),
        "with_power": have('power'),
        "with_ele":   have('ele'),
        "with_hr":    have('hr'),
    }


if __name__ == '__main__':
    import json
    if len(sys.argv) < 2:
        raise SystemExit("usage: python analysis/parse_ride.py <ride.fit|.tcx|.gpx>")
    print(json.dumps(summary(parse_ride(sys.argv[1])), ensure_ascii=False, indent=2))
