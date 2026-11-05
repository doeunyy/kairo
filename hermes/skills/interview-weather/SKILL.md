---
name: interview-weather
description: Weather for interview days and commutes, with practical advice on timing and what to bring.
version: 0.1.0
author: Kairo
license: MIT
metadata:
  hermes:
    tags: [Weather, Job Search]
    related_skills: [interview-scheduling]
    requires_toolsets: [mcp-kairo]
---

# Interview Weather

## When to Use

- "What's the weather?" (or "날씨 어때?"), an on-site interview is coming up, or the Morning Briefing needs weather.

## Quick Reference

`mcp_kairo_weather(place, days)` — Open-Meteo daily forecast: summary, high/low °C, rain chance.
Default place: Los Angeles, unless the user or the interview location says otherwise.

## Procedure

1. Use the city the user named, or the interview's location from the calendar event.
2. Call `weather(place, days)` covering the interview day.
3. Answer in one or two lines: the day's weather, then advice only if it matters
   (rain ≥ 40% → leave 15 minutes early and bring an umbrella; ≥ 32°C → carry water;
   ≤ 5°C → coat).

## Pitfalls

- If the place is not found, ask for a nearby city instead of guessing.
- Forecasts beyond 7 days are not available; say so.

## Verification

The answer names the place and date the forecast is for.
