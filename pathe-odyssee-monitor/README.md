# Pathé Odysseum — L'Odyssée IMAX 70mm seat monitor

Watches **Pathé Odysseum (Montpellier)** for free seats for:

- **Film:** L'Odyssée : Projection IMAX 70mm  
- **Date:** 5 August 2026  
- **Time:** 21:00 French time (`Europe/Paris`)

When a place appears, it prints an alert and can notify you on **Telegram** or **Discord**.

> Alert-only: the script does **not** book tickets. Open the booking link yourself quickly.

## What you should do

### 1) Get the code on your computer

Clone/download this repo, then open a terminal in `pathe-odyssee-monitor/`.

### 2) Install (once)

Needs **Python 3.10+**.

```bash
cd pathe-odyssee-monitor
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # includes tzdata (needed on Windows)
playwright install chromium
```

### 3) Configure alerts (recommended)

Copy and edit config if you want:

```bash
cp config.example.yaml config.yaml   # already present with good defaults
```

#### Option A — Telegram (simple)

1. In Telegram, talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the **bot token**
2. Talk to your bot once, then get your chat id via [@userinfobot](https://t.me/userinfobot) (or any chat-id bot)
3. In `config.yaml`:

```yaml
alerts:
  telegram:
    enabled: true
    bot_token: "123456:ABC..."
    chat_id: "123456789"
```

Or export env vars instead of putting secrets in the file:

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="123456789"
```

#### Option B — Discord

1. Server settings → Integrations → Webhooks → New webhook → copy URL
2. In `config.yaml`:

```yaml
alerts:
  discord:
    enabled: true
    webhook_url: "https://discord.com/api/webhooks/..."
```

### 4) Important: run it from your home network

Pathé blocks many datacenter / cloud IPs. Run this on your **laptop or home PC** (ideally in France, without a blocked VPN).

### 5) First test — list the day’s showtimes

```bash
python monitor.py list
```

You should see the 21:00 show (and others). If you get a **403**, leave the VPN / try another network.

### 6) Verify Telegram works

Make sure `config.yaml` has:

```yaml
alerts:
  telegram:
    enabled: true
    bot_token: "YOUR_TOKEN"
    chat_id: "YOUR_CHAT_ID"
```

Then:

```bash
python monitor.py test-alert
```

You should get a Telegram message and see `Telegram OK` in the terminal.

### 7) One-shot check

```bash
python monitor.py once
```

### 8) Start continuous monitoring

```bash
python monitor.py loop
```

Leave this terminal open. Default poll interval is **120 seconds**.

Optional: keep it running in the background with `tmux` / `screen`, or a local cron every few minutes calling `python monitor.py once`.

## How it works

1. Calls Pathé’s showtimes API for Odysseum on `2026-08-05`
2. Selects the **21:00** screening
3. In `auto` mode, opens the booking page (`s.pathe.fr`) with Chromium and tries to count free seats
4. Sends an alert when at least `min_free_seats` are free (default: 1)

## Useful config knobs

| Key | Meaning |
|---|---|
| `interval_seconds` | Poll period in seconds (min 30; default `60`. Use `30` only for short tests — more 403 risk) |
| `min_free_seats` | Alert threshold |
| `check_mode` | `auto` / `showtimes` / `seats` |
| `stop_on_alert` | Stop after first alert |
| `required_version` | e.g. `vost` if several 21:00 shows exist |
| `required_tags_any` | e.g. `[imax]` |

Event page: https://www.pathe.fr/evenements/l-odyssee-projection-imax-70mm-54413

## Troubleshooting

| Problem | Fix |
|---|---|
| `403` / Akamai error | Run from home IP, disable VPN, retry later |
| No 21:00 show in `list` | Pathé may have changed the schedule — pick another time in `config.yaml` |
| Seat count always `unknown` / `could not parse seat map` | Run `python monitor.py debug-seats` (writes `debug-seats-output/`). Also try `headless: false`. Status-change alerts still work even if the seat map can’t be parsed. |
| Cancelled a seat but no Telegram / still shows Complet | Normal: Pathé often keeps session `soldout` while 1 seat is free for only seconds. Status-only checks miss that. Use latest monitor (HTTP booking probe) on PC, react instantly, don’t rely on phone-only. |
| Keeps alerting while show looks full again | Pathé often leaves `status=available` after a seat was taken. Use latest monitor (`alert_on_transition_only: true`, default) — alerts only on new availability transitions. |
| Too many alerts | Raise `alert_cooldown_seconds` or set `stop_on_alert: true` |
| `No time zone found with key Europe/Paris` (Windows) | `pip install tzdata` then retry |
| YAML parse error on Termux but same file works on laptop | Phone editor inserted invisible spaces/smart quotes. Update `monitor.py` (auto-sanitizes) or recreate with `nano config.yaml` inside Termux. |

Be polite with polling (2+ minutes). This tool is for personal monitoring only.
