"""
analysis/strava_fetch.py — выгрузка заезда из Strava API (streams) в нормализованный JSON.

Зачем: когда «Export Original» (.fit) недоступен, мощность всё равно есть в Strava и
отдаётся через streams API (watts). Токен — секрет, читаем из env / .env, не из кода и не из чата.

Подготовка токена (один раз):
  1) strava.com/settings/api -> создать приложение -> Client ID + Client Secret
  2) открыть в браузере (подставив свой ID):
     https://www.strava.com/oauth/authorize?client_id=ID&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=activity:read_all
     одобрить -> браузер уйдёт на http://localhost/?...&code=CODE — скопировать CODE из адреса
  3) обменять CODE на токен:
     curl -X POST https://www.strava.com/oauth/token \
          -F client_id=ID -F client_secret=SECRET -F code=CODE -F grant_type=authorization_code
  4) положить access_token в .env (в корне репо, он в .gitignore):
     STRAVA_ACCESS_TOKEN=xxxxxxxx
  (access_token живёт ~6 ч; для разовой выгрузки этого хватает.)

Использование:
  python analysis/strava_fetch.py <activity_id>
  -> сохранит analysis/data/strava_<id>.json (нормализованные точки)
  далее: python analysis/validate_physics.py analysis/data/strava_<id>.json --mass 90 --fit
"""
import os
import sys
import json
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_token() -> str:
    tok = os.environ.get("STRAVA_ACCESS_TOKEN")
    if tok:
        return tok.strip()
    env = os.path.join(ROOT, ".env")
    if os.path.exists(env):
        for line in open(env, encoding="utf-8"):
            line = line.strip()
            if line.startswith("STRAVA_ACCESS_TOKEN"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("Нет STRAVA_ACCESS_TOKEN (env или .env в корне). См. шапку файла.")


def fetch_activity(activity_id, token):
    import requests
    h = {"Authorization": f"Bearer {token}"}
    base = f"https://www.strava.com/api/v3/activities/{activity_id}"
    meta = requests.get(base, headers=h, timeout=30)
    if meta.status_code == 401:
        raise SystemExit("401 — токен невалиден/просрочен или нет scope activity:read_all.")
    meta.raise_for_status()
    meta = meta.json()
    start = datetime.fromisoformat(meta["start_date"].replace("Z", "+00:00"))
    st = requests.get(base + "/streams", headers=h, timeout=30, params={
        "keys": "time,latlng,altitude,watts,heartrate,cadence,temp",
        "key_by_type": "true"})
    st.raise_for_status()
    return meta, start, st.json()


def streams_to_points(start, st):
    col = lambda k: (st.get(k, {}) or {}).get("data") or []
    tarr, latlng = col("time"), col("latlng")
    alt, pw = col("altitude"), col("watts")
    hr, cad, temp = col("heartrate"), col("cadence"), col("temperature")
    get = lambda arr, i: (arr[i] if i < len(arr) else None)
    pts = []
    for i in range(len(tarr)):
        ll = get(latlng, i)
        if not ll:
            continue
        pts.append({
            "t": (start + timedelta(seconds=tarr[i])).isoformat(),
            "lat": ll[0], "lon": ll[1],
            "ele": get(alt, i), "power": get(pw, i),
            "hr": get(hr, i), "cad": get(cad, i), "temp": get(temp, i),
        })
    return pts


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python analysis/strava_fetch.py <activity_id>")
    aid = sys.argv[1]
    meta, start, st = fetch_activity(aid, _load_token())
    pts = streams_to_points(start, st)
    out = os.path.join(HERE, "data", f"strava_{aid}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(pts, f, ensure_ascii=False)
    npw = sum(1 for p in pts if p.get("power") is not None)
    print(f"«{meta.get('name')}»  start={start.isoformat()}")
    print(f"точек: {len(pts)}, с мощностью: {npw}  ->  {out}")
    if npw == 0:
        print("⚠ В streams нет watts — у активности нет данных мощности (нет power meter).")


if __name__ == "__main__":
    main()
