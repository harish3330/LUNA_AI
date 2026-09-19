---
name: LUNA Daily Weather
command: /luna-daily-weather
description: >
  Inspect, verify, test, and maintain the LUNA Daily Weather & Motivation
  Assistant (luna.py). Runs a full end-to-end check: project structure →
  syntax → live weather fetch → content generation → HTML email → SMTP delivery.
  Never fabricates weather data. Never exposes credentials in output.
---

# LUNA Daily Weather — Agent Workflow

## Overview

This workflow is an **agent maintenance tool** for the LUNA Python application.
It does **not** replace LUNA's own built-in daily scheduler (`--daemon` mode).
LUNA itself is responsible for sending the weather email every day.

This workflow exists so you can:
- Verify that `luna.py` is intact and well-formed.
- Run an immediate end-to-end test (`--run-now`).
- Diagnose and fix errors without touching working functionality.

---

## Step 1 — Inspect the Project Structure

List every file in the project root so we know exactly what is present.

```
list the files in the current workspace directory
```

Expected files:
- `luna.py` — the main application
- `requirements.txt` — Python dependencies
- `README.md` — usage documentation
- `logs/` — created at runtime by LUNA (may not exist yet)

> **Important:** Do NOT modify, move, or delete any file unless a specific
> error requires it.

---

## Step 2 — Confirm luna.py Exists

Verify that `luna.py` is present in the project root.

If `luna.py` is **missing**, stop and report:
> "luna.py was not found. The LUNA application cannot be tested."

Do not proceed past this step if the file is absent.

---

## Step 3 — Inspect luna.py Before Making Any Changes

Read `luna.py` in full before touching anything. Understand its current state.

Confirm the following components are present:

| Component | What to look for |
|---|---|
| Paris weather configuration | `DEFAULT_LATITUDE = "48.8566"`, `DEFAULT_LONGITUDE = "2.3522"`, `DEFAULT_TIMEZONE = "Europe/Paris"` |
| Open-Meteo API request | `fetch_weather()` function calling `api.open-meteo.com/v1/forecast` |
| Weather data parsing | `parse_weather()` function extracting temperature, humidity, wind, etc. |
| Weather code conversion | `weather_code_to_text()` and `WEATHER_CODES` dictionary |
| Weather-aware content generation | `generate_content()` generating summary, clothing advice, and recommendations |
| HTML email generation | `build_email_html()` producing a responsive inline-CSS HTML email |
| Gmail SMTP sending | `send_email()` and `_smtp_deliver()` using `smtplib` with STARTTLS / SSL |
| Error handling | `LunaError` class, try/except in `run_luna()`, retry loops |
| Logging | `setup_logging()` writing to `logs/luna.log` via `RotatingFileHandler` |

If any component is missing or appears broken, note it — but **do not
rewrite working functionality**.

---

## Step 4 — Run a Syntax Check

Run Python's built-in compiler against `luna.py`:

```
python -m py_compile luna.py
```

- **No output** = syntax is valid. Proceed to Step 5.
- **Error output** = a syntax error was found.

### If a Syntax Error Is Found

1. Read the error message carefully and locate the offending line.
2. Open `luna.py` and apply the minimal fix needed.
3. Re-run `python -m py_compile luna.py`.
4. Repeat until syntax is clean.
5. Log what was fixed.

Do not change logic, variable names, or structure beyond what is strictly
necessary to fix the syntax error.

---

## Step 5 — Run LUNA in Immediate Mode

Execute the application once and capture the result:

```
python luna.py --run-now
```

This triggers the full pipeline:
1. Fetches live Paris weather from Open-Meteo.
2. Parses the weather data.
3. Generates weather-aware message, recommendations, and clothing advice.
4. Builds the HTML email.
5. Sends the email via Gmail SMTP.

### Expected Successful Output

The terminal output should include these checkmarks (in order):

```
✓ Weather retrieved
✓ Content generated
✓ HTML email generated
✓ Email sent successfully

LUNA completed today's report.
```

### Verify Each Stage Individually

After the run, confirm:

| Stage | Pass condition |
|---|---|
| Weather API | No `LunaError` about Open-Meteo; weather values printed |
| Paris data received | Location shown as "Paris, France" in terminal output |
| Weather values parsed | Temperature, feels-like, humidity, wind, precipitation all printed |
| Content generated | `✓ Content generated` printed |
| HTML email generated | No error about empty body or missing weather keys |
| SMTP email sent | `✓ Email sent successfully` printed; no `SMTPAuthenticationError` |

---

## Step 6 — Credential Safety

> **NEVER expose credentials in the agent response, terminal output,
> email preview, or logs.**

Credentials in this project include:
- Anthropic API key
- SMTP password (Gmail App Password)
- SMTP username / email address

The `scrub()` function in `luna.py` automatically redacts these values in logs.

If you need to confirm a credential is set, only report:
- Whether the value is present (non-empty).
- Whether it appears to be a placeholder (starts with `your_`).

**Do not print, echo, or quote the actual credential value.**

Do not move credentials into `.env` files, environment variables, or any
other configuration system. The credential architecture is intentional and
must not be changed.

---

## Step 7 — Error Handling and Self-Repair

If `python luna.py --run-now` fails, follow this procedure:

### 7a — Identify the Error Type

| Error type | Typical message | Action |
|---|---|---|
| Missing dependency | `ModuleNotFoundError: No module named 'requests'` | Run `pip install -r requirements.txt` |
| SMTP auth failure | `SMTP login rejected` | Check credential values are set and not placeholders |
| Weather API failure | `Could not retrieve weather data from Open-Meteo` | Check internet connectivity; retry once |
| Syntax error | `SyntaxError` | Fix syntax (see Step 4) |
| Logic error / exception | Unexpected traceback | Inspect `logs/luna.log` for details |

### 7b — Apply the Fix

Apply the smallest possible fix. Do not rewrite working code.

### 7c — Re-Run and Verify

After applying a fix:

```
python luna.py --run-now
```

Repeat Step 5's verification checklist. If the run now succeeds, the
workflow is complete.

### 7d — Continue Until Resolved or Blocked

Continue the fix → re-run → verify loop until one of these is true:

- ✅ The run completes successfully.
- 🔴 A genuine external dependency prevents completion (e.g., Gmail is
  down, network is unreachable). In that case, report the
  external blocker clearly and stop.

---

## Step 8 — Check the Daily Scheduler

After a successful `--run-now`, report the status of LUNA's built-in
daily scheduler.

LUNA supports three operating modes. Check which one is appropriate for
the user's setup:

### `--daemon` mode (built-in scheduler)

```
python luna.py --daemon
```

This keeps LUNA running indefinitely and sends the daily report at the
configured time (`DEFAULT_SEND_TIME = "08:00"`, Paris timezone).

**This is the recommended always-on scheduler.**

If the user wants LUNA to run in the background automatically:
- On **Windows**: set up a Windows Task Scheduler task that runs
  `python luna.py --daemon` at user logon.
- On **Linux / macOS**: set up a `systemd` service or `launchd` plist.

### `--run-now` mode (on-demand / Task Scheduler trigger)

```
python luna.py --run-now
```

Runs once and exits. Can be scheduled via:
- **Windows**: Task Scheduler (daily trigger at 08:00).
- **Linux / macOS**: `cron` job.
- **GitHub Actions**: Automated scheduled workflow (`.github/workflows/luna-daily.yml`).

### `--test-email` mode (SMTP check only)

```
python luna.py --test-email
```

Sends a minimal test email with no weather data. Use this to
confirm SMTP credentials are working without triggering a full run.

---

## Step 9 — Final Report

After the workflow completes, provide a concise summary:

```
LUNA Daily Weather Workflow — Result
=====================================
Syntax check:       PASS
Weather API:        PASS  (Paris, France — live Open-Meteo data)
Weather parsing:    PASS  (temperature, humidity, wind, precipitation)
Content generation: PASS  (Weather-aware advice & summary)
HTML email:         PASS
SMTP delivery:      PASS
Daily scheduler:    --daemon mode / GitHub Actions available
=====================================
LUNA completed today's report successfully.
```

If any stage failed and could not be fixed, replace that line with:
`FAIL  — <brief reason>`

---

## Constraints and Guardrails

These rules apply throughout the entire workflow execution:

1. **Do not fabricate weather data.** All weather values must come from the
   live Open-Meteo API response. If the API is unavailable, report it and
   stop — do not substitute made-up numbers.

2. **Do not expose credentials.** Never print, echo, or include API keys,
   SMTP passwords, or email addresses in the agent response or logs.

3. **Do not rewrite working code.** Only change `luna.py` if a specific,
   confirmed error requires it. Apply the smallest possible fix.

4. **Do not move credentials.** The credential configuration inside
   `luna.py` (or its `.env` file) is intentional. Do not introduce
   `python-dotenv`, secret managers, or any other credential system.

5. **Do not confuse this workflow with the daily scheduler.** This workflow
   is a maintenance and testing tool. The Python application (`--daemon`
   or a Task Scheduler job) is what sends the daily email automatically.

6. **Preserve all existing functionality.** Do not remove, rename, or
   restructure any function, class, or module unless fixing a confirmed bug.
