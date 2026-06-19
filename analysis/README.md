# analysis/ — сверка физики по реальному заезду

Офлайн-инструменты: проверить `core.solve_speed` по реальному прохождению трека
(факт. скорость + измеренная мощность + уклон + ветер). Подробно — в [../doc/analysis.md](../doc/analysis.md).

- `parse_ride.py` — парсер `.fit/.tcx/.gpx` → нормализованные точки.
- `validate_physics.py` — предсказание скорости vs факт + подбор `CdA/Crr`.
- `data/` — сюда класть файлы заездов (в `.gitignore`, не коммитятся).

```bash
python analysis/parse_ride.py analysis/data/ride.fit
python analysis/validate_physics.py analysis/data/ride.fit --mass 90 --fit
```

Зависимости: `requests` (ERA5), `fitparse` для `.fit` (`pip install fitparse`).
Переиспользует физику из `core.py` (импорт из корня репозитория).
