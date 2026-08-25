from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

from opus.logutil import get_logger
from opus.net_policy import guarded_urlopen

if TYPE_CHECKING:
    from opus.settings import Settings

log = get_logger()

WEATHER_CODES = {
    0: "clear skies",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "foggy",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "severe thunderstorms",
}


def _fetch_json(url: str, timeout: float = 8.0) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "Opus/1.0"})
    with guarded_urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


def _windows_location() -> tuple[float, float, str] | None:
    script = """
    try {
        Add-Type -AssemblyName System.Runtime.WindowsRuntime
        [Windows.Devices.Geolocation.Geolocator,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
        $geo = New-Object Windows.Devices.Geolocation.Geolocator
        $geo.DesiredAccuracy = [Windows.Devices.Geolocation.PositionAccuracy]::High
        $task = $geo.GetGeopositionAsync()
        $task.AsTask().Wait(2500) | Out-Null
        if (-not $task.IsCompleted) { exit 1 }
        $point = $task.GetResults().Coordinate.Point
        Write-Output ("{0}|{1}" -f $point.Latitude, $point.Longitude)
    } catch { exit 1 }
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    line = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    if "|" not in line:
        return None
    lat_text, lon_text = line.split("|", 1)
    try:
        lat = float(lat_text)
        lon = float(lon_text)
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon, "your PC"


def _ip_location() -> tuple[float, float, str] | None:
    try:
        data = _fetch_json("http://ip-api.com/json/?fields=status,lat,lon,city,regionName,country")
    except Exception:
        log.exception("ip location failed")
        return None
    if data.get("status") != "success":
        return None
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    parts = [str(data.get("city") or "").strip(), str(data.get("regionName") or "").strip()]
    label = ", ".join(part for part in parts if part) or str(data.get("country") or "near you")
    return lat, lon, label


def _geocode_city(city: str) -> tuple[float, float, str] | None:
    query = urllib.parse.quote((city or "").strip())
    if not query:
        return None
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={query}&count=1&language=en&format=json"
    try:
        data = _fetch_json(url)
    except Exception:
        log.exception("geocode failed")
        return None
    results = data.get("results") or []
    if not results:
        return None
    hit = results[0]
    try:
        lat = float(hit["latitude"])
        lon = float(hit["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    label = ", ".join(
        part
        for part in (
            str(hit.get("name") or "").strip(),
            str(hit.get("admin1") or "").strip(),
            str(hit.get("country") or "").strip(),
        )
        if part
    )
    return lat, lon, label or city


def resolve_location(settings: Settings, city: str = "") -> tuple[float, float, str]:
    override = (city or settings.get("location_city") or "").strip()
    if override:
        geocoded = _geocode_city(override)
        if geocoded:
            return geocoded

    lat = settings.get("location_lat")
    lon = settings.get("location_lon")
    label = (settings.get("location_label") or "").strip()
    if lat is not None and lon is not None:
        try:
            return float(lat), float(lon), label or "your area"
        except (TypeError, ValueError):
            pass

    for resolver in (_windows_location, _ip_location):
        found = resolver()
        if not found:
            continue
        lat, lon, label = found
        try:
            settings.update(
                {
                    "location_lat": lat,
                    "location_lon": lon,
                    "location_label": label,
                }
            )
        except Exception:
            pass
        return lat, lon, label

    raise RuntimeError("I couldn't figure out where you are. Set your city in the Opus panel.")


def location_summary(settings: Settings) -> str:
    try:
        _lat, _lon, label = resolve_location(settings)
    except RuntimeError as exc:
        return str(exc)
    return f"You are near {label}."


def get_weather(settings: Settings, city: str = "") -> str:
    lat, lon, label = resolve_location(settings, city=city)
    params = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "forecast_days": 1,
        }
    )
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    try:
        data = _fetch_json(url)
    except Exception:
        log.exception("weather fetch failed")
        return "I couldn't reach the weather service."
    current = data.get("current") or {}
    daily = data.get("daily") or {}
    code = int(current.get("weather_code") or daily.get("weather_code", [0])[0] or 0)
    summary = WEATHER_CODES.get(code, "mixed conditions")
    temp = current.get("temperature_2m")
    feels = current.get("apparent_temperature")
    humidity = current.get("relative_humidity_2m")
    wind = current.get("wind_speed_10m")
    hi = (daily.get("temperature_2m_max") or [None])[0]
    lo = (daily.get("temperature_2m_min") or [None])[0]
    rain = (daily.get("precipitation_probability_max") or [None])[0]

    parts = [f"In {label}, it's {summary}"]
    if temp is not None:
        parts.append(f"about {round(temp)} degrees")
    if feels is not None and temp is not None and abs(feels - temp) >= 3:
        parts.append(f"feels like {round(feels)}")
    if hi is not None and lo is not None:
        parts.append(f"high {round(hi)}, low {round(lo)}")
    if rain is not None and rain >= 35:
        parts.append(f"{round(rain)} percent chance of rain")
    if humidity is not None:
        parts.append(f"humidity {round(humidity)} percent")
    if wind is not None:
        parts.append(f"wind {round(wind)} miles an hour")
    return ", ".join(parts) + "."
