# VPS и деплой

- **Адрес:** `84.252.135.130:5000`, SSH-алиас `vps_griha3212`, пользователь `ivan`, Ubuntu, Python 3.12.
- **Рабочая директория:** `/opt/velo_weather`, venv в `/opt/velo_weather/venv`.
- **Nginx** обслуживает другие сайты; велопогода идёт напрямую на `:5000` (открыт в ufw).

## Сервис

⚠️ **Активный сервис — `velo_weather`** (держит :5000). Был дубликат `velo.service` (тот же
`ExecStart=.../app.py`), висел в `activating` и флапал на занятом порту — **погашен** 19.06.2026
(`sudo systemctl disable --now velo.service`). Рестарт/деплой — только через `velo_weather`.

```bash
sudo systemctl restart velo_weather
sudo systemctl is-active velo_weather
sudo journalctl -u velo_weather -n 50 --no-pager     # логи
```

## Деплой с локальной машины

```bash
scp core.py app.py vps_griha3212:/opt/velo_weather/
scp templates/index.html vps_griha3212:/opt/velo_weather/templates/
ssh vps_griha3212 "sudo systemctl restart velo_weather"
```

Хорошая практика: сверить sha1 после `scp` (`sha1sum` локально и на VPS), затем рестарт и
проверить журнал на старт без traceback. Рестарт восстанавливает состояние заезда из `state.json`
(позиция/стоянки/мощность), `_model_sched` сбрасывается → первый фетч после старта полный.

## Стянуть данные для анализа

```bash
scp vps_griha3212:/opt/velo_weather/weather_log.jsonl .
scp vps_griha3212:/opt/velo_weather/weather_now.jsonl .
scp vps_griha3212:/opt/velo_weather/power_log.jsonl .
scp -r vps_griha3212:/opt/velo_weather/archive/<ride_id> .
```

## Карта портов (проверено 19.06.2026, `ss -tlnp` + `ufw` + `nginx -T`)

| Порт | Слушает | Наружу | Что |
|------|---------|--------|-----|
| 22 | sshd | 🌍 | SSH |
| 80 / 443 | nginx | 🌍 | firstglance.ru (+ SSL) |
| 47291 | nginx | 🌍 | → `127.0.0.1:8000` co2monitor |
| 8080 | nginx | 🌍 | → `127.0.0.1:8001` ambrosia |
| **5000** | **python** | **🌍** | **velo_weather напрямую (Werkzeug), без nginx** |
| 8000 | uvicorn | 🔒 | co2monitor backend |
| 8001 | dockerd | 🔒 | ambrosia backend (Docker) |
| 5984 | couchdb (beam.smp) | 🔒 | данные firstglance (наружу только через nginx) |
| 4369 / 41179 | epmd / beam.smp | 🔒 | служебное CouchDB/Erlang |

ufw открыт наружу: 22, 80, 443, **5000**, 8080, 47291. velo_weather — единственное приложение,
торчащее в интернет напрямую (и слушает `0.0.0.0:5000`, и открыт в ufw). Принцип сервера —
«наружу только nginx (+SSH)»; velo нарушает. План спрятать за nginx — см. TODO #33.
