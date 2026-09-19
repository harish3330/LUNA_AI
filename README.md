# 🌙 LUNA — Daily Weather & Motivation Assistant

LUNA is a small personal assistant written in a single Python file (`luna.py`).
Every morning it:

1. Fetches **live weather** for your city from [Open-Meteo](https://open-meteo.com/) (free, no account needed)
2. Generates a **personalised weather summary**, clothing advice, umbrella tip, and daily recommendation — all from the real weather data
3. Builds a **beautiful HTML email**
4. **Sends it to you** through your Gmail (or any SMTP provider)

No website. No frontend. No third-party automation service. No AI API key required.

---

## Features

- 🌤️ Live weather from Open-Meteo (temperature, humidity, wind, rain probability)
- 👗 Clothing advice based on the real feels-like temperature
- ☂️ Umbrella tip based on the actual rain probability
- 📋 Daily recommendation based on conditions
- 📧 Responsive HTML email with plain-text fallback
- 🔁 Automatic daily delivery via **GitHub Actions** (8 AM Paris time)
- 🛡️ Credentials stored in `.env` — never committed to the repo

---

## Quick Start (Local)

### 1. Clone the repository

```bash
git clone https://github.com/your-username/luna-agent.git
cd luna-agent
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file

```bash
copy .env.example .env   # Windows
cp .env.example .env     # macOS / Linux
```

Edit `.env` and fill in your real values:

```dotenv
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=your_gmail_app_password   # Gmail → Security → App Passwords
EMAIL_FROM=you@gmail.com
EMAIL_TO=you@gmail.com
```

> **Gmail tip:** Go to [myaccount.google.com → Security → 2-Step Verification → App Passwords](https://myaccount.google.com/apppasswords) and generate a password for "Mail".

### 4. Send a test email

```bash
python luna.py --test-email
```

### 5. Send today's weather report now

```bash
python luna.py --run-now
```

---

## Automated Daily Email (GitHub Actions)

The workflow in [`.github/workflows/workflow.yml`](.github/workflows/workflow.yml) runs every day at **8:00 AM IST** (02:30 UTC).

### Set up GitHub Secrets

Go to your repository → **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name     | Value                          |
|-----------------|--------------------------------|
| `SMTP_HOST`     | `smtp.gmail.com`               |
| `SMTP_PORT`     | `587`                          |
| `SMTP_USERNAME` | your Gmail address             |
| `SMTP_PASSWORD` | your Gmail App Password        |
| `EMAIL_FROM`    | your Gmail address             |
| `EMAIL_TO`      | recipient address(es)          |

That's it — push to `main` and GitHub Actions will handle the rest.

---

## Command Reference

| Command | Description |
|---|---|
| `python luna.py --run-now` | Fetch weather, generate message, send email immediately |
| `python luna.py --test-email` | Send a small test email to verify SMTP settings |
| `python luna.py --daemon` | Keep running and auto-send every day at `LUNA_SEND_TIME` |
| `python luna.py --help` | Show all options |

---

## Configuration Reference

All settings live in `.env` (copy from `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `SMTP_HOST` | — | SMTP server hostname |
| `SMTP_PORT` | `587` | SMTP port (587 = STARTTLS) |
| `SMTP_USERNAME` | — | Your email login |
| `SMTP_PASSWORD` | — | Your email password / App Password |
| `EMAIL_FROM` | — | Sender address |
| `EMAIL_TO` | — | Recipient address(es), comma-separated |
| `LUNA_CITY` | `Paris` | City name shown in the email |
| `LUNA_COUNTRY` | `France` | Country name shown in the email |
| `LUNA_TIMEZONE` | `Europe/Paris` | Timezone for scheduling |
| `LUNA_SEND_TIME` | `08:00` | Time to send (daemon mode, 24-hour) |
| `LUNA_LATITUDE` | `48.8566` | Latitude for Open-Meteo |
| `LUNA_LONGITUDE` | `2.3522` | Longitude for Open-Meteo |

---

## Project Structure

```
luna-agent/
├── luna.py                          # The entire application
├── requirements.txt                 # Python dependencies
├── .env.example                     # Template — copy to .env and fill in values
├── .gitignore                       # Keeps .env and logs out of git
├── .github/
│   └── workflows/
│       └── workflow.yml             # GitHub Actions: send email every morning
└── .agents/
    └── workflows/
        └── luna-daily-weather.md    # Antigravity IDE workflow for local testing
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `requests` | Fetch live weather from Open-Meteo |
| `python-dotenv` | Load credentials from `.env` |
| `tzdata` | Timezone database (required on Windows) |

No AI API. No paid services. Just Python.
