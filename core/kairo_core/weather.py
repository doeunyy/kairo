"""Forecast from Open-Meteo (no API key; no personal data leaves except the place name)."""

from __future__ import annotations

import urllib.parse

from .jsonhttp import JsonClient, Transport

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog", 51: "drizzle",
       53: "drizzle", 55: "drizzle", 61: "rain", 63: "rain", 65: "heavy rain", 71: "snow", 73: "snow",
       75: "heavy snow", 80: "showers", 81: "showers", 82: "heavy showers", 95: "thunderstorm", 96: "thunderstorm",
       99: "thunderstorm"}


def forecast(place: str, days: int = 3, tz: str = "America/Los_Angeles",
             transport: Transport | None = None) -> dict:
    geo = JsonClient("https://geocoding-api.open-meteo.com/v1", {}, transport=transport)
    found = geo.request("GET", f"/search?count=1&language=en&name={urllib.parse.quote(place)}") or {}
    if not found.get("results"):
        return {"place": place, "error": "place not found"}
    loc = found["results"][0]
    api = JsonClient("https://api.open-meteo.com/v1", {}, transport=transport)
    query = urllib.parse.urlencode({
        "latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": tz,
        "forecast_days": max(1, min(days, 7)),
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
    })
    daily = api.request("GET", f"/forecast?{query}")["daily"]
    return {
        "place": f"{loc['name']}, {loc.get('admin1') or loc.get('country', '')}".strip(", "),
        "days": [{"date": d, "summary": WMO.get(code, f"code {code}"), "high_c": hi, "low_c": lo, "rain_pct": rain}
                 for d, code, hi, lo, rain in zip(daily["time"], daily["weather_code"], daily["temperature_2m_max"],
                                                  daily["temperature_2m_min"], daily["precipitation_probability_max"])],
    }
