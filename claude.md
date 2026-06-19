# Проект: велосипедный оптимизатор маршрута

Flask-сервер + симулятор: прогноз времени и осадков по 10км-отрезкам маршрута с учётом старта,
мощности педалирования, рельефа/направления из GPX и прогноза погоды Open-Meteo.

## Документация — в [`doc/`](doc/README.md)

| Документ | О чём |
|----------|-------|
| [doc/overview.md](doc/overview.md) | Цель, режимы, файлы проекта, быстрый старт, `config.json` |
| [doc/architecture.md](doc/architecture.md) | Физика `solve_speed`, ветер, планировщик остановок, API |
| [doc/weather.md](doc/weather.md) | Open-Meteo, сетка точек, расписание фетча, дешёвая проба, логи |
| [doc/archive.md](doc/archive.md) | Архив статистики заезда + стоп-гап текущей погоды |
| [doc/deploy.md](doc/deploy.md) | VPS, сервис `velo_weather`, команды |
| [doc/decisions.md](doc/decisions.md) | Журнал решений (почему так) + разборы логов |
| [doc/todo.md](doc/todo.md) | Задачи (активные/сделано) |

**Документацию ведём по ходу дела** — правим профильный файл в `doc/`, новые плодим только при
необходимости. Правила — в [doc/README.md](doc/README.md).

## Памятка (самое частое)
- **Деплой:** `scp core.py app.py vps_griha3212:/opt/velo_weather/` → `sudo systemctl restart velo_weather` (активный сервис — `velo_weather`, НЕ `velo`). Подробно — [doc/deploy.md](doc/deploy.md).
- **Время** везде UTC; старт не сдвигать при обновлении позиции.
- **Логи/архив/`state.json`** в репозиторий не коммитим (`.gitignore`).
- Длину/файл живого маршрута брать из VPS `state.json` (`config.gpx_file`, `total_km`), не из локального `route.gpx`.
