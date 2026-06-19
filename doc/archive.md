# Архив статистики заезда (#26)

Цель — офлайн-отладка: совпадение прогнозов после обновления погоды, точность прогноза скорости,
точность перерасчёта мощности. Пишется на VPS, стягивается `scp` как логи.

**Папка:** `archive/<ride_id>/`, где `ride_id` = `start_time[:16]` с заменой `:`→`-`
(напр. `2026-06-20T05-00`). Управляется конфигом: `archive_enabled` (default true), `archive_dir`.

| Файл | Когда пишется | Содержимое |
|------|---------------|-----------|
| `meta.json` | один раз на заезд | config, GPX (имя+sha1+total_km), git-commit, модели — для реплея |
| `forecast.jsonl` | каждый `simulate_only()` | `t, trigger, current_km, eff_power`; по модели: `run_at, finish, finish_no_rain, grid[]` (компактный 10км-грид) |
| `position.jsonl` | каждый `update_position()` | факт `km/время`, `actual_speed_kmh, riding_h, calibrated_power, forecast_age_h, live_pred` (прогноз на этом км ДО пересчёта) |
| `power_log.jsonl` | калибровка (расширен) | `from/to_km, riding_h, elapsed_h, stop_h, prev_power, default_power, delta_w` |

**Триггеры `forecast`:** `startup` / `weather` (новый прогон) / `position` / `power` / `manual`.

**Защита:** все записи в try/except — архив не может уронить `simulate_only`/`update_position`.

## Стоп-гап: текущая погода (#30)

`weather_now.jsonl` — раз в 30 мин (`_bg_loop` → `_snapshot_current_weather`) снимок ТЕКУЩЕЙ
погоды по точкам маршрута: `{t, model, points:[{lat,lon,wind_ms,wind_dir,precip_mm_h,temp_c}]}`.
Значения берутся из актуального кеша прогноза через `core._interp(wp, now)` — **нулевой расход API**
(свежайший доступный nowcast).

Зачем: `forecast.jsonl` покрывает только окно заезда, а здесь копится непрерывный ряд погоды для
местности → погода-«истина» для сверки прогнозов и отладки физики.
Решение временное; «честный» вариант — отдельный фетч `current=` с Open-Meteo.

## Как анализировать (офлайн)

- **Совпадение прогнозов после обновления погоды:** diff соседних снимков `forecast.jsonl` с
  `trigger=weather` → Δ-время/скорость/осадки по каждому км.
- **Точность прогноза скорости:** `position.actual_speed_kmh` vs `forecast.grid[].speed_kmh` на том
  же км при разных lead'ах (join по `km`).
- **Точность перерасчёта мощности:** стабильность `calibrated_power` по отрезкам (ровный гонщик →
  малая дисперсия) + ошибка ETA при калиброванной vs заложенной мощности.
- **Погода-«истина»:** прогноз с минимальным lead из `forecast.jsonl` либо ряд `weather_now.jsonl`.
- **Сходимость ETA:** серия `finish` по времени → сходится ли к факт. финишу.

Стянуть архив заезда:
```bash
scp -r vps_griha3212:/opt/velo_weather/archive/<ride_id> .
```
