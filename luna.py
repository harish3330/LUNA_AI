#!/usr/bin/env python3
"""
LUNA — Daily Weather & Motivation Assistant
===========================================

LUNA fetches the live weather for Paris (Open-Meteo), generates a warm, personal
message based on the current conditions, builds a responsive HTML email and sends
it over SMTP.

Usage:
    python luna.py --run-now      Fetch weather, generate the message, send the email now
    python luna.py --test-email   Send a small test email to check your SMTP settings
    python luna.py --daemon       Keep running and send the report every day at LUNA_SEND_TIME
    python luna.py --help         Show help

Architecture:

    Open-Meteo API -> parse_weather() -> validated weather dict
                   -> generate_content() -> build_email_html() -> send_email()
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import smtplib
import ssl
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from datetime import time as dtime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# Settings you may want to change (everything else comes from the .env file)
# ---------------------------------------------------------------------------

APP_NAME = "LUNA"
APP_TAGLINE = "Daily Weather & Motivation Assistant"

DEFAULT_CITY = "Paris"
DEFAULT_COUNTRY = "France"
DEFAULT_TIMEZONE = "Europe/Paris"
DEFAULT_SEND_TIME = "08:00"
DEFAULT_LATITUDE = "48.8566"
DEFAULT_LONGITUDE = "2.3522"

# Open-Meteo endpoint. With the default Paris settings this is exactly the URL from the spec.
WEATHER_URL_TEMPLATE = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={latitude}&longitude={longitude}"
    "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
    "precipitation,weather_code,wind_speed_10m"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    "&timezone={timezone}"
)

HTTP_TIMEOUT_SECONDS = 15
SMTP_TIMEOUT_SECONDS = 30
WEATHER_MAX_ATTEMPTS = 3
SMTP_MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 2  # waits 2s, then 4s, ... between attempts

# Daemon behaviour
DAEMON_POLL_SECONDS = 30
CATCH_UP_WINDOW = timedelta(hours=3)  # still send if the PC woke up/started this late
DAEMON_RETRY_DELAY = timedelta(minutes=15)
DAEMON_MAX_ATTEMPTS_PER_DAY = 4

# Files (pathlib, so this works on Windows, macOS and Linux)
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "luna.log"
STATE_FILE = LOG_DIR / "state.json"  # remembers the last sent date

# ---------------------------------------------------------------------------
# Weather code mapping (Open-Meteo / WMO codes)
# ---------------------------------------------------------------------------

WEATHER_CODES: dict[int, str] = {
    0: "Clear Sky",
    1: "Mainly Clear",
    2: "Partly Cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime Fog",
    51: "Light Drizzle",
    53: "Moderate Drizzle",
    55: "Dense Drizzle",
    61: "Slight Rain",
    63: "Moderate Rain",
    65: "Heavy Rain",
    71: "Slight Snow",
    73: "Moderate Snow",
    75: "Heavy Snow",
    80: "Rain Showers",
    81: "Moderate Rain Showers",
    82: "Heavy Rain Showers",
    95: "Thunderstorm",
    96: "Thunderstorm with Hail",
    99: "Thunderstorm with Heavy Hail",
}
UNKNOWN_WEATHER = "Unknown Weather"

WEATHER_EMOJI: dict[int, str] = {
    0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️", 45: "🌫️", 48: "🌫️",
    51: "🌦️", 53: "🌦️", 55: "🌧️", 61: "🌦️", 63: "🌧️", 65: "🌧️",
    71: "🌨️", 73: "🌨️", 75: "❄️", 80: "🌦️", 81: "🌧️", 82: "🌧️",
    95: "⛈️", 96: "⛈️", 99: "⛈️",
}

# Every value that must be present (and numeric) before LUNA is allowed to send an email.
WEATHER_KEYS = (
    "temperature", "feels_like", "humidity", "precipitation", "weather_code",
    "wind_speed", "min_temperature", "max_temperature", "rain_probability",
)



# ---------------------------------------------------------------------------
# Logging, errors and secret-safety helpers
# ---------------------------------------------------------------------------

log = logging.getLogger("luna")

# Secret values (API key, SMTP password). They are scrubbed from anything we log or print.
_SECRETS: list[str] = []


class LunaError(Exception):
    """An expected failure. The message is written to be safe to show to the user."""


def setup_logging() -> None:
    """Send log messages (with timestamps) to logs/luna.log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    log.propagate = False
    if not log.handlers:
        handler = RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(handler)


def configure_console() -> None:
    """Make printing ✓ ° emoji safe on Windows consoles and when output is redirected."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def scrub(text: str) -> str:
    """Replace any known secret value in text with ***."""
    for secret in _SECRETS:
        if secret:
            text = text.replace(secret, "***")
    return text


def describe_error(exc: BaseException) -> str:
    """A short, secret-free description of an exception for logs and the terminal."""
    return scrub(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def load_config() -> dict[str, Any]:
    """Read settings from the .env file / environment variables. Never prints secrets."""
    load_dotenv(ENV_FILE, encoding="utf-8-sig")  # utf-8-sig also accepts files saved by Notepad with a BOM

    port_text = _env("SMTP_PORT", "587")
    try:
        smtp_port: int | None = int(port_text)
    except ValueError:
        smtp_port = None

    config: dict[str, Any] = {
        "smtp_host": _env("SMTP_HOST"),
        "smtp_port": smtp_port,
        "smtp_port_text": port_text,
        "smtp_username": _env("SMTP_USERNAME"),
        "smtp_password": _env("SMTP_PASSWORD"),
        "email_from": _env("EMAIL_FROM"),
        "email_to": [a.strip() for a in _env("EMAIL_TO").split(",") if a.strip()],
        "city": _env("LUNA_CITY", DEFAULT_CITY),
        "country": _env("LUNA_COUNTRY", DEFAULT_COUNTRY),
        "timezone": _env("LUNA_TIMEZONE", DEFAULT_TIMEZONE),
        "send_time": _env("LUNA_SEND_TIME", DEFAULT_SEND_TIME),
        "latitude": _env("LUNA_LATITUDE", DEFAULT_LATITUDE),
        "longitude": _env("LUNA_LONGITUDE", DEFAULT_LONGITUDE),
    }
    _SECRETS[:] = [s for s in (config["smtp_password"],) if s]
    return config


def _is_placeholder(value: str) -> bool:
    """True for untouched example values copied from .env.example (e.g. your_app_password)."""
    return value.lower().startswith("your_")


def parse_send_time(text: str) -> dtime:
    """Turn '08:00' into a time object (24-hour clock)."""
    try:
        return datetime.strptime(text.strip(), "%H:%M").time()
    except ValueError:
        raise ValueError(
            f"LUNA_SEND_TIME must look like HH:MM on a 24-hour clock, e.g. 08:00 (got {text!r})"
        ) from None


def validate_config(config: dict[str, Any]) -> list[str]:
    """Return a list of problems (empty list = configuration is fine)."""
    problems: list[str] = []

    required = [
        ("SMTP_HOST", "smtp_host"),
        ("SMTP_USERNAME", "smtp_username"),
        ("SMTP_PASSWORD", "smtp_password"),
        ("EMAIL_FROM", "email_from"),
        ("EMAIL_TO", "email_to"),
    ]

    for env_name, key in required:
        value = config.get(key)
        if not value:
            problems.append(f"Missing required environment variable: {env_name}")
            continue
        values = value if isinstance(value, list) else [value]
        if any(_is_placeholder(v) for v in values):
            problems.append(f"{env_name} still contains the example value from .env.example")

    if config.get("smtp_port") is None or not 0 < config["smtp_port"] < 65536:
        problems.append(f"SMTP_PORT must be a number between 1 and 65535 (got {config.get('smtp_port_text')!r})")

    for env_name, address in [("EMAIL_FROM", config.get("email_from"))] + [
        ("EMAIL_TO", a) for a in config.get("email_to", [])
    ]:
        if address:
            bare = parseaddr(address)[1]
            if "@" not in bare or " " in bare:
                problems.append(f"{env_name} contains an invalid email address")
                break

    try:
        ZoneInfo(config["timezone"])
    except (ZoneInfoNotFoundError, ValueError, OSError):
        problems.append(
            f"LUNA_TIMEZONE {config['timezone']!r} was not found "
            "(use a name like Europe/Paris; on Windows also run: pip install tzdata)"
        )

    try:
        parse_send_time(config["send_time"])
    except ValueError as exc:
        problems.append(str(exc))

    for env_name, key, limit in (("LUNA_LATITUDE", "latitude", 90), ("LUNA_LONGITUDE", "longitude", 180)):
        try:
            if abs(float(config[key])) > limit:
                raise ValueError
        except ValueError:
            problems.append(f"{env_name} must be a number between -{limit} and {limit}")

    return problems


# ---------------------------------------------------------------------------
# Weather: fetch -> parse -> validated weather dict
# ---------------------------------------------------------------------------


def build_weather_url(config: dict[str, Any]) -> str:
    """Build the Open-Meteo URL (identical to the spec's URL for the default Paris settings)."""
    return WEATHER_URL_TEMPLATE.format(
        latitude=config["latitude"],
        longitude=config["longitude"],
        timezone=quote(config["timezone"], safe=""),
    )


def fetch_weather(config: dict[str, Any]) -> dict[str, Any]:
    """Call the live Open-Meteo API (with retries and backoff) and return the raw JSON."""
    url = build_weather_url(config)
    for attempt in range(1, WEATHER_MAX_ATTEMPTS + 1):
        log.info("Weather API request (attempt %d/%d): %s", attempt, WEATHER_MAX_ATTEMPTS, url)
        try:
            response = requests.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("the response was not a JSON object")
            log.info("Weather API success")
            return data
        except (requests.RequestException, ValueError) as exc:
            log.warning(
                "Weather API failure (attempt %d/%d): %s",
                attempt, WEATHER_MAX_ATTEMPTS, describe_error(exc),
            )
            if attempt < WEATHER_MAX_ATTEMPTS:
                time.sleep(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
    log.error("Weather API failed after %d attempts", WEATHER_MAX_ATTEMPTS)
    raise LunaError(
        f"Could not retrieve weather data from Open-Meteo after {WEATHER_MAX_ATTEMPTS} attempts. "
        "No email was sent."
    )


def weather_code_to_text(code: Any) -> str:
    """Convert an Open-Meteo weather code into readable text. Unknown codes never get invented."""
    try:
        return WEATHER_CODES.get(int(code), UNKNOWN_WEATHER)
    except (TypeError, ValueError):
        return UNKNOWN_WEATHER


def _number(value: Any) -> float | None:
    """Return value as a float, or None if it is missing / not a real number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return None if number != number else number  # NaN check


def _first(values: Any) -> Any:
    return values[0] if isinstance(values, list) and values else None


def parse_weather(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract and validate the fields LUNA needs. Raises LunaError if anything is missing."""
    current = raw.get("current") if isinstance(raw, dict) else None
    daily = raw.get("daily") if isinstance(raw, dict) else None
    current = current if isinstance(current, dict) else {}
    daily = daily if isinstance(daily, dict) else {}

    fields = {
        "temperature": current.get("temperature_2m"),
        "feels_like": current.get("apparent_temperature"),
        "humidity": current.get("relative_humidity_2m"),
        "precipitation": current.get("precipitation"),
        "weather_code": current.get("weather_code"),
        "wind_speed": current.get("wind_speed_10m"),
        "min_temperature": _first(daily.get("temperature_2m_min")),
        "max_temperature": _first(daily.get("temperature_2m_max")),
        "rain_probability": _first(daily.get("precipitation_probability_max")),
    }

    weather: dict[str, Any] = {}
    missing: list[str] = []
    for name, value in fields.items():
        number = _number(value)
        if number is None:
            missing.append(name)
        else:
            weather[name] = number

    if missing:
        log.error("Weather data incomplete; missing or invalid: %s", ", ".join(missing))
        raise LunaError(
            "The weather data was incomplete (" + ", ".join(missing) + "). No email was sent."
        )

    weather["weather_code"] = int(weather["weather_code"])
    weather["condition"] = weather_code_to_text(weather["weather_code"])
    log.info(
        "Parsed weather: condition=%s temp=%s feels_like=%s humidity=%s wind=%s "
        "precip=%s min=%s max=%s rain_prob=%s",
        weather["condition"], weather["temperature"], weather["feels_like"],
        weather["humidity"], weather["wind_speed"], weather["precipitation"],
        weather["min_temperature"], weather["max_temperature"], weather["rain_probability"],
    )
    return weather


def fmt_num(value: float) -> str:
    """18.0 -> '18', 18.46 -> '18.5'."""
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def fmt_temp(value: float) -> str:
    return f"{fmt_num(value)}°C"


# ---------------------------------------------------------------------------
# State (duplicate-send protection and recently used quotes)
# ---------------------------------------------------------------------------


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Could not read state file (starting fresh): %s", describe_error(exc))
        return {}


def save_state(state: dict[str, Any]) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        tmp_file = STATE_FILE.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp_file.replace(STATE_FILE)  # atomic replace, also works on Windows
    except OSError as exc:
        log.error("Could not save state file: %s", describe_error(exc))


def record_successful_send(sent_on: date) -> None:
    """Remember that today's report was sent (so a restart never sends it twice)."""
    state = load_state()
    state["last_sent_date"] = sent_on.isoformat()
    save_state(state)


# ---------------------------------------------------------------------------
# Content generation (weather-aware, no external AI required)
# ---------------------------------------------------------------------------


def generate_content(config: dict[str, Any], weather: dict[str, Any], now: datetime) -> dict[str, str]:
    """Generate weather-aware email content using the live weather data."""
    city = config["city"]
    condition = weather["condition"]
    temp = round(weather["temperature"])
    feels = round(weather["feels_like"])
    rain_prob = round(weather["rain_probability"])
    wind = round(weather["wind_speed"])
    day_name = now.strftime("%A")

    # Clothing advice (based on feels-like temperature)
    if feels < 5:
        clothing = "It is very cold outside — a heavy winter coat, gloves, and a warm hat are essential."
    elif feels < 10:
        clothing = f"Cold at {feels}\u00b0C — dress in warm layers and do not forget a scarf."
    elif feels < 15:
        clothing = f"Cool today at {feels}\u00b0C. A light jacket or hoodie will keep you comfortable."
    elif feels < 20:
        clothing = f"Mild at {feels}\u00b0C. A light layer such as a cardigan or zip-up is just right."
    elif feels < 25:
        clothing = "Pleasant temperature today. Light clothing is perfectly fine."
    else:
        clothing = f"Warm at {feels}\u00b0C. Light, breathable clothing is the right choice today."

    # Umbrella advice (based on rain probability)
    if rain_prob >= 70:
        umbrella = f"Rain probability is high at {rain_prob}% \u2014 definitely take your umbrella."
    elif rain_prob >= 40:
        umbrella = f"There is a {rain_prob}% chance of rain. It is worth carrying an umbrella."
    elif rain_prob >= 15:
        umbrella = f"Slight chance of rain ({rain_prob}%). A compact umbrella in your bag would not hurt."
    else:
        umbrella = f"Rain is very unlikely today ({rain_prob}% chance). No umbrella needed."

    # Daily recommendation (based on overall conditions)
    if rain_prob >= 60:
        rec = "Consider indoor activities today and save outdoor plans for a sunnier day."
    elif "Thunderstorm" in condition:
        rec = "Thunderstorms are forecast \u2014 stay indoors when possible and avoid open areas."
    elif "Snow" in condition:
        rec = "Snow expected \u2014 allow extra travel time and dress warmly before heading out."
    elif condition in ("Clear Sky", "Mainly Clear") and temp >= 18:
        rec = "Lovely conditions \u2014 a great day to spend some time outside."
    elif condition in ("Partly Cloudy", "Overcast"):
        rec = "Reasonable conditions today. A good day to get things done, indoors or out."
    else:
        rec = "Check the forecast before heading out and dress for the current conditions."

    # Weather summary
    summary = (
        f"Today in {city} the skies are {condition.lower()} with a current temperature of {temp}\u00b0C, "
        f"feeling like {feels}\u00b0C. "
        f"Humidity is at {round(weather['humidity'])}% and wind is at {wind} km/h. "
        f"Rain probability for the day is {rain_prob}%."
    )

    return {
        "headline": f"{day_name}'s Weather in {city} \u2014 {condition}",
        "weather_summary": summary,
        "motivational_quote": "Every day is a fresh start \u2014 make the most of it.",
        "caring_message": "Take care of yourself today. Small acts of kindness to yourself go a long way.",
        "recommendation": rec,
        "clothing_advice": clothing,
        "umbrella_advice": umbrella,
    }



# ---------------------------------------------------------------------------
# Email: HTML / plain-text / message building
# ---------------------------------------------------------------------------

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _section_title(text: str) -> str:
    return (
        '<div style="font-size:12px;font-weight:700;letter-spacing:1.4px;color:#5b5fc7;'
        f'margin-bottom:10px;">{text}</div>'
    )


def _card(inner: str, bg: str = "#ffffff", border: str = "#e3e8f5") -> str:
    return (
        '<tr><td style="padding:0 0 14px 0;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background-color:{bg};border:1px solid {border};border-radius:16px;">'
        f'<tr><td style="padding:20px 22px;font-family:{_FONT};">{inner}</td></tr>'
        "</table></td></tr>"
    )


def _text_card(title: str, text: str, bg: str = "#ffffff", border: str = "#e3e8f5") -> str:
    body = (
        f'<div style="font-size:16px;line-height:1.6;color:#1f2937;">{html.escape(text)}</div>'
    )
    return _card(_section_title(title) + body, bg, border)


def _tile(label: str, value: str) -> str:
    return (
        '<td width="50%" valign="top" style="padding:4px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background-color:#f3f5fc;border-radius:12px;">'
        f'<tr><td align="center" style="padding:14px 8px;font-family:{_FONT};">'
        f'<div style="font-size:12px;color:#6b7280;letter-spacing:0.5px;">{label}</div>'
        f'<div style="font-size:22px;font-weight:600;color:#1f2a5c;margin-top:4px;">{value}</div>'
        "</td></tr></table></td>"
    )


def _tile_row(*tiles: str) -> str:
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        "<tr>" + "".join(tiles) + "</tr></table>"
    )


def _render_page(
    config: dict[str, Any],
    now: datetime,
    title: str,
    headline: str,
    preheader: str,
    rows: str,
    footer_lines: list[str],
) -> str:
    """Wrap card rows in the shared email shell (header, 600px container, footer)."""
    location = html.escape(f"{config['city']}, {config['country']}")
    date_text = html.escape(f"{now:%A, %B} {now.day}, {now.year}")
    footer = "<br>".join(html.escape(line) for line in footer_lines)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>{html.escape(title)}</title>
</head>
<body style="margin:0;padding:0;background-color:#eef1f8;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#eef1f8;">{html.escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#eef1f8" style="background-color:#eef1f8;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;">
<tr><td style="padding:0 0 14px 0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#1f2a5c" style="background-color:#1f2a5c;background-image:linear-gradient(135deg,#1f2a5c,#4b3f9e);border-radius:20px;">
<tr><td align="center" style="padding:30px 24px;font-family:{_FONT};">
<div style="font-size:32px;font-weight:700;letter-spacing:3px;color:#ffffff;">🌙 LUNA</div>
<div style="font-size:15px;color:#c9cffc;margin-top:6px;">Your Daily Weather &amp; Motivation</div>
<div style="font-size:15px;font-weight:600;color:#ffffff;margin-top:18px;">{location}</div>
<div style="font-size:13px;color:#c9cffc;margin-top:2px;">{date_text}</div>
<div style="font-size:16px;font-style:italic;color:#e6e9ff;margin-top:18px;padding-top:16px;border-top:1px solid #4a5596;">{html.escape(headline)}</div>
</td></tr></table>
</td></tr>
{rows}
<tr><td align="center" style="padding:6px 0 0 0;font-family:{_FONT};font-size:12px;line-height:1.8;color:#8a90a6;">{footer}</td></tr>
</table>
</td></tr></table>
</body>
</html>
"""


def build_email_html(
    config: dict[str, Any], weather: dict[str, Any], content: dict[str, str], now: datetime
) -> str:
    """Build the responsive HTML email (inline CSS only, no scripts, no external files)."""
    icon = WEATHER_EMOJI.get(weather["weather_code"], "🌡️")
    condition = html.escape(weather["condition"])

    current = _card(
        _section_title("CURRENT WEATHER")
        + '<div align="center">'
        f'<div style="font-size:64px;font-weight:700;line-height:1.1;color:#1f2a5c;">{fmt_temp(weather["temperature"])}</div>'
        f'<div style="font-size:20px;color:#374151;margin-top:8px;">{icon} {condition}</div>'
        f'<div style="font-size:14px;color:#6b7280;margin-top:6px;">Feels like {fmt_temp(weather["feels_like"])}</div>'
        "</div>"
    )
    details = _card(
        _section_title("WEATHER DETAILS")
        + _tile_row(
            _tile("Humidity", f'{fmt_num(weather["humidity"])}%'),
            _tile("Wind", f'{fmt_num(weather["wind_speed"])} km/h'),
        )
        + _tile_row(
            _tile("Precipitation", f'{fmt_num(weather["precipitation"])} mm'),
            _tile("Rain Probability", f'{fmt_num(weather["rain_probability"])}%'),
        )
    )
    forecast = _card(
        _section_title("TODAY'S FORECAST")
        + _tile_row(
            _tile("Low", fmt_temp(weather["min_temperature"])),
            _tile("High", fmt_temp(weather["max_temperature"])),
        )
    )
    note = _text_card("LUNA'S WEATHER NOTE", content["weather_summary"])
    motivation = _card(
        _section_title("💡 TODAY'S MOTIVATION")
        + '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td style="border-left:4px solid #4f46e5;padding:6px 0 6px 16px;">'
        '<div style="font-size:20px;line-height:1.5;font-style:italic;color:#1f2a5c;">'
        f'“{html.escape(content["motivational_quote"])}”</div></td></tr></table>',
        bg="#eef0ff",
        border="#c9ccf5",
    )
    caring = _text_card("💙 A LITTLE NOTE FROM LUNA", content["caring_message"], bg="#eef6ff", border="#cfe3fb")
    suggestion = _text_card("🌤️ TODAY'S SUGGESTION", content["recommendation"])
    clothing = _text_card("👕 WHAT TO WEAR", content["clothing_advice"])
    umbrella = _text_card("☂️ UMBRELLA CHECK", content["umbrella_advice"])

    preheader = f'{content["headline"]} — {fmt_temp(weather["temperature"])}, {weather["condition"]}'
    return _render_page(
        config, now, "LUNA — Daily Weather & Motivation", content["headline"], preheader,
        current + details + forecast + note + motivation + caring + suggestion + clothing + umbrella,
        ["Weather data: Open-Meteo", "Generated by LUNA"],
    )


def build_email_text(
    config: dict[str, Any], weather: dict[str, Any], content: dict[str, str], now: datetime
) -> str:
    """Plain-text version of the report (shown by email clients that do not render HTML)."""
    return "\n".join([
        "LUNA — Your Daily Weather & Motivation",
        f"{config['city']}, {config['country']} — {now:%A, %B} {now.day}, {now.year}",
        "",
        content["headline"],
        "",
        "CURRENT WEATHER",
        f"{fmt_temp(weather['temperature'])} — {weather['condition']} "
        f"(feels like {fmt_temp(weather['feels_like'])})",
        "",
        "WEATHER DETAILS",
        f"Humidity: {fmt_num(weather['humidity'])}%",
        f"Wind: {fmt_num(weather['wind_speed'])} km/h",
        f"Precipitation: {fmt_num(weather['precipitation'])} mm",
        f"Rain Probability: {fmt_num(weather['rain_probability'])}%",
        "",
        "TODAY'S FORECAST",
        f"Low: {fmt_temp(weather['min_temperature'])}",
        f"High: {fmt_temp(weather['max_temperature'])}",
        "",
        "LUNA'S WEATHER NOTE",
        content["weather_summary"],
        "",
        "TODAY'S MOTIVATION",
        content["motivational_quote"],
        "",
        "A LITTLE NOTE FROM LUNA",
        content["caring_message"],
        "",
        "TODAY'S SUGGESTION",
        content["recommendation"],
        "",
        "WHAT TO WEAR",
        content["clothing_advice"],
        "",
        "UMBRELLA CHECK",
        content["umbrella_advice"],
        "",
        "Weather data: Open-Meteo",
        "Generated by LUNA",
    ])


def build_subject(content: dict[str, str]) -> str:
    """One-line subject (whitespace collapsed so it can never break the email header)."""
    headline = " ".join(content["headline"].split())
    if len(headline) > 100:
        headline = headline[:97].rstrip() + "..."
    return f"🌙 LUNA — {headline}"


def build_test_email(config: dict[str, Any], now: datetime) -> tuple[str, str, str]:
    """Return (subject, html, text) for the SMTP test email. It contains no weather data."""
    headline = "Your test email has arrived"
    message = (
        "If you can read this, LUNA can log in to your mail server and send email. "
        "Your daily weather and motivation report will arrive in the same way."
    )
    rows = _text_card("✅ SMTP TEST", message) + _text_card(
        "WHAT'S NEXT",
        "Run 'python luna.py --run-now' to send a full report, or start the daily schedule.",
    )
    html_body = _render_page(
        config, now, "LUNA — Test email", headline, headline, rows, ["Generated by LUNA"]
    )
    text_body = f"LUNA — Test email\n\n{headline}\n\n{message}\n\nGenerated by LUNA\n"
    return "🌙 LUNA — Test email", html_body, text_body


def validate_email_payload(subject: str, html_body: str, weather: dict[str, Any]) -> None:
    """Final safety check before a production email is sent."""
    if not subject.strip():
        raise LunaError("The email subject is empty. No email was sent.")
    if not html_body.strip():
        raise LunaError("The email body is empty. No email was sent.")
    missing = [key for key in WEATHER_KEYS if key not in weather]
    if missing:
        raise LunaError("Weather data is missing (" + ", ".join(missing) + "). No email was sent.")


def build_message(
    config: dict[str, Any], subject: str, html_body: str, text_body: str
) -> MIMEMultipart:
    """Assemble the MIME message (plain text + HTML). Nothing is sent here."""
    sender_name, sender_address = parseaddr(config["email_from"])
    message = MIMEMultipart("alternative")
    message["Subject"] = Header(subject, "utf-8")
    message["From"] = formataddr((sender_name or APP_NAME, sender_address))
    message["To"] = ", ".join(config["email_to"])
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))
    return message


# ---------------------------------------------------------------------------
# Email: SMTP sending
# ---------------------------------------------------------------------------


def _smtp_deliver(
    config: dict[str, Any], message: MIMEMultipart, sender: str, recipients: list[str]
) -> None:
    """One SMTP attempt: connect, STARTTLS (or SSL on port 465), log in, send."""
    host, port = config["smtp_host"], config["smtp_port"]
    context = ssl.create_default_context()
    if port == 465:
        connection = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECONDS, context=context)
    else:
        connection = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS)
    with connection as server:
        if port != 465:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
        server.login(config["smtp_username"], config["smtp_password"])
        server.send_message(message, from_addr=sender, to_addrs=recipients)


def send_email(
    config: dict[str, Any], subject: str, html_body: str, text_body: str
) -> None:
    """Send the email, retrying temporary problems. Raises LunaError if it cannot be sent."""
    message = build_message(config, subject, html_body, text_body)
    sender = parseaddr(config["email_from"])[1]
    recipients = [parseaddr(address)[1] for address in config["email_to"]]

    for attempt in range(1, SMTP_MAX_ATTEMPTS + 1):
        log.info(
            "SMTP connection to %s:%s (attempt %d/%d)",
            config["smtp_host"], config["smtp_port"], attempt, SMTP_MAX_ATTEMPTS,
        )
        try:
            _smtp_deliver(config, message, sender, recipients)
            log.info("Email successfully sent to %d recipient(s)", len(recipients))
            return
        except smtplib.SMTPAuthenticationError as exc:
            log.error("Email failure: SMTP login rejected: %s", describe_error(exc))
            raise LunaError(
                "The mail server rejected the login. Check SMTP_USERNAME and SMTP_PASSWORD "
                "(Gmail needs an App Password, not your normal password)."
            ) from None
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            log.error("Email failure: address rejected: %s", describe_error(exc))
            raise LunaError(
                "The mail server rejected the sender or recipient address. "
                "Check EMAIL_FROM and EMAIL_TO."
            ) from None
        except (smtplib.SMTPException, OSError) as exc:
            log.warning(
                "Email failure (attempt %d/%d): %s", attempt, SMTP_MAX_ATTEMPTS, describe_error(exc)
            )
            if attempt < SMTP_MAX_ATTEMPTS:
                time.sleep(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))

    log.error("Email failed after %d attempts", SMTP_MAX_ATTEMPTS)
    raise LunaError(
        f"The email could not be sent after {SMTP_MAX_ATTEMPTS} attempts. "
        "Check SMTP_HOST, SMTP_PORT and your internet connection (details are in logs/luna.log)."
    )


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------

_WIDTH = 40


def print_banner(subtitle: str = "Daily Weather Assistant") -> None:
    print("=" * _WIDTH)
    print(APP_NAME.center(_WIDTH).rstrip())
    print(subtitle.center(_WIDTH).rstrip())
    print("=" * _WIDTH)
    print()


def print_weather(config: dict[str, Any], weather: dict[str, Any]) -> None:
    rows = [
        ("Location", f"{config['city']}, {config['country']}"),
        ("Weather", weather["condition"]),
        ("Temperature", fmt_temp(weather["temperature"])),
        ("Feels Like", fmt_temp(weather["feels_like"])),
        ("Humidity", f"{fmt_num(weather['humidity'])}%"),
        ("Wind", f"{fmt_num(weather['wind_speed'])} km/h"),
        ("Rain Probability", f"{fmt_num(weather['rain_probability'])}%"),
    ]
    for label, value in rows:
        print(f"{label}:\n{value}\n")


def _print_failure(steps: list[str], reason: str) -> None:
    print()
    for step in steps:
        print(step)
    print(f"✗ {reason}")
    print("\nLUNA could not complete today's report. Details are in logs/luna.log.")
    print("=" * _WIDTH)


# ---------------------------------------------------------------------------
# Main workflows
# ---------------------------------------------------------------------------


def run_luna(config: dict[str, Any]) -> bool:
    """Run the full daily report once. Returns True if the email was sent."""
    log.info("Starting the daily report run")
    now = datetime.now(ZoneInfo(config["timezone"]))
    steps: list[str] = []
    print_banner()

    try:
        print("Fetching live weather...\n")
        weather = parse_weather(fetch_weather(config))
        steps.append("✓ Weather retrieved")
        print_weather(config, weather)

        state = load_state()
        if state.get("last_sent_date") == now.date().isoformat():
            log.info("A report was already sent today; sending another because this run was requested")
            print("Note: a report was already sent today. Sending another one because you asked for it.\n")

        print("Content:\nGenerating LUNA's daily message...\n")
        content = generate_content(config, weather, now)
        steps.append("\u2713 Content generated")

        log.info("Email generation started")
        subject = build_subject(content)
        html_body = build_email_html(config, weather, content, now)
        text_body = build_email_text(config, weather, content, now)
        validate_email_payload(subject, html_body, weather)
        steps.append("✓ HTML email generated")
        log.info("Email generation finished")

        print("Email:\nSending...\n")
        send_email(config, subject, html_body, text_body)
        steps.append("✓ Email sent successfully")
        record_successful_send(now.date())
    except LunaError as exc:
        log.error("Run failed: %s", exc)
        _print_failure(steps, str(exc))
        return False
    except Exception as exc:  # unexpected bug: log details, show a short message
        log.error("Unexpected error: %s", scrub(traceback.format_exc()))
        _print_failure(steps, f"Unexpected error ({describe_error(exc)})")
        return False

    print("\n".join(steps))
    print("\nLUNA completed today's report.")
    print("=" * _WIDTH)
    log.info("Daily report run completed")
    return True


def run_test_email(config: dict[str, Any]) -> bool:
    """Send a small test email (no weather, no AI) to check the SMTP settings."""
    log.info("Sending test email")
    print_banner("SMTP Test")
    try:
        now = datetime.now(ZoneInfo(config["timezone"]))
        subject, html_body, text_body = build_test_email(config, now)
        print("Email:\nSending test email...\n")
        send_email(config, subject, html_body, text_body)
    except LunaError as exc:
        log.error("Test email failed: %s", exc)
        _print_failure([], str(exc))
        return False
    except Exception as exc:
        log.error("Unexpected error: %s", scrub(traceback.format_exc()))
        _print_failure([], f"Unexpected error ({describe_error(exc)})")
        return False

    print("✓ Test email sent successfully")
    print("=" * _WIDTH)
    return True


def daemon_decision(
    now: datetime, send_time: dtime, last_sent_date: str | None, catch_up: timedelta = CATCH_UP_WINDOW
) -> str:
    """Decide what the daemon should do right now.

    'already_sent' - today's report has been sent
    'wait'         - today's send time has not arrived yet
    'send'         - it is time (or shortly after: the PC may have been asleep)
    'missed'       - the send window has passed; wait for tomorrow
    """
    if last_sent_date == now.date().isoformat():
        return "already_sent"
    scheduled = datetime.combine(now.date(), send_time, tzinfo=now.tzinfo)
    if now < scheduled:
        return "wait"
    if now - scheduled <= catch_up:
        return "send"
    return "missed"


def _describe_daemon_state(now: datetime, send_time: dtime, decision: str) -> str:
    today_at = datetime.combine(now.date(), send_time, tzinfo=now.tzinfo)
    tomorrow_at = datetime.combine(now.date() + timedelta(days=1), send_time, tzinfo=now.tzinfo)
    if decision == "wait":
        return f"Waiting for today's send time ({today_at:%H:%M})."
    if decision == "already_sent":
        return f"Today's report was already sent. Next report: tomorrow at {tomorrow_at:%H:%M}."
    if decision == "missed":
        return f"Today's send window has passed. Next report: tomorrow at {tomorrow_at:%H:%M}."
    return "Giving up for today after repeated failures. LUNA will try again tomorrow."


def run_daemon(config: dict[str, Any]) -> int:
    """Run forever and send the report once a day at LUNA_SEND_TIME (configured timezone)."""
    tz = ZoneInfo(config["timezone"])
    send_time = parse_send_time(config["send_time"])
    log.info("Scheduler started: daily at %s (%s)", send_time.strftime("%H:%M"), config["timezone"])
    print_banner("Daemon Mode")
    print(f"LUNA will send the report every day at {send_time:%H:%M} ({config['timezone']}).")
    print("Keep this window open. Press Ctrl+C to stop.\n")

    attempts_date: date | None = None
    attempts_today = 0
    retry_after: datetime | None = None
    last_announced: tuple[date, str] | None = None

    while True:
        now = datetime.now(tz)
        if attempts_date != now.date():  # a new day: reset the in-memory attempt counter
            attempts_date, attempts_today, retry_after = now.date(), 0, None

        action = daemon_decision(now, send_time, load_state().get("last_sent_date"))
        if action == "send" and attempts_today >= DAEMON_MAX_ATTEMPTS_PER_DAY:
            action = "gave_up"
        elif action == "send" and retry_after is not None and now < retry_after:
            action = "retry_wait"

        if action == "send":
            attempts_today += 1
            log.info(
                "Scheduler: sending today's report (attempt %d/%d)",
                attempts_today, DAEMON_MAX_ATTEMPTS_PER_DAY,
            )
            if run_luna(config):
                log.info("Scheduler: today's report sent")
            else:
                retry_after = datetime.now(tz) + DAEMON_RETRY_DELAY
                log.warning("Scheduler: run failed; next attempt after %s", retry_after.strftime("%H:%M"))
                if attempts_today < DAEMON_MAX_ATTEMPTS_PER_DAY:
                    print(f"\nWill try again at {retry_after:%H:%M}.\n")
        elif action != "retry_wait" and (now.date(), action) != last_announced:
            last_announced = (now.date(), action)  # announce each state once per day
            message = _describe_daemon_state(now, send_time, action)
            log.info("Scheduler: %s", message)
            print(message)

        time.sleep(DAEMON_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="luna.py",
        description=f"{APP_NAME} — {APP_TAGLINE}. Sends a daily weather and motivation email.",
        epilog=(
            "Settings are read from the .env file next to luna.py (see .env.example).\n"
            "For everyday use, run '--run-now' from Windows Task Scheduler (see README.md)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--run-now",
        action="store_true",
        help="fetch today's weather, generate LUNA's message and send the email immediately, then exit",
    )
    mode.add_argument(
        "--test-email",
        action="store_true",
        help="send a small test email right now to check your SMTP settings (no weather, no AI)",
    )
    mode.add_argument(
        "--daemon",
        action="store_true",
        help="keep running and send the report every day at LUNA_SEND_TIME (in LUNA_TIMEZONE); "
        "never sends twice on the same day, even after a restart",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code (0 = success, 1 = run failed, 2 = bad setup)."""
    configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not (args.run_now or args.test_email or args.daemon):
        parser.print_help()
        return 0

    setup_logging()
    mode = "run-now" if args.run_now else "test-email" if args.test_email else "daemon"
    log.info("%s v%s starting (mode: %s)", APP_NAME, __version__, mode)

    config = load_config()
    problems = validate_config(config)
    if problems:
        log.error("Configuration problems: %s", "; ".join(problems))
        print("LUNA cannot start because of configuration problems:\n")
        for problem in problems:
            print(f"  - {problem}")
        if not ENV_FILE.exists():
            print(f"\nNo .env file was found at {ENV_FILE}")
            print("Copy .env.example to .env and fill in your values.")
        return 2

    try:
        if args.daemon:
            return run_daemon(config)
        succeeded = run_luna(config) if args.run_now else run_test_email(config)
    except KeyboardInterrupt:
        log.info("Stopped by the user")
        print("\nLUNA stopped.")
        return 0
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
