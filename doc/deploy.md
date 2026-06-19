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
