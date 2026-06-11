import json, core
from datetime import datetime

cfg = json.load(open("config.json"))
start = datetime.fromisoformat(cfg["start_time"])
print("Start:", start, "UTC")

segs = core.parse_gpx(cfg["gpx_file"])
print(f"Route: {core.total_route_km(segs):.1f} km, {len(segs)} segments")

print("Fetching weather (10 points)...")
weather = core.fetch_weather(segs, n_samples=cfg["weather_samples"], model=cfg["weather_model"])
print(f"Weather OK: {len(weather)} points")

ride = core.simulate(
    segments=segs, start_time=start, weather_points=weather,
    power_w=cfg["power_w"], mass_kg=cfg["mass_kg"],
    cda=cfg["cda"], crr=cfg["crr"],
    time_limit_h=cfg["time_limit_h"],
    overnight_km=cfg["overnight_km"], overnight_h=cfg["overnight_h"],
    rain_threshold=cfg["rain_threshold"], max_rain_wait_h=cfg["max_rain_wait_h"],
)

grid = core.grid_10km(ride)
print()
print(f"{'km':>5}  {'UTC':>5}  {'elapsed':>7}  {'precip':>9}  {'wind':>6}  stop")
print("-" * 65)
for pt in grid:
    stop = f"{pt.stop_here_h:.1f}h {pt.stop_reason}" if pt.stop_here_h > 0 else ""
    precip = f"{pt.precip_mm_h:.1f}mm/h" if pt.precip_mm_h >= 0.1 else "dry"
    print(f"{pt.km:>5.0f}  {str(pt.wall_time)[11:16]:>5}  {pt.elapsed_h:>6.1f}h  {precip:>9}  {pt.wind_ms:>4.1f}ms  {stop}")

last = ride[-1]
print()
print(f"Finish: {str(last.wall_time)[11:16]} UTC  |  Total: {last.elapsed_h:.1f}h of {cfg['time_limit_h']}h limit")
