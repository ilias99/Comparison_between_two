#!/usr/bin/env python3
"""Monitor Pathé Odysseum seat availability for a specific screening.

Default target:
  L'Odyssée : Projection IMAX 70mm
  Pathé Odysseum (Montpellier)
  2026-08-05 at 21:00 Europe/Paris
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
import yaml

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # optional until seat mode is used
    sync_playwright = None  # type: ignore


LOG = logging.getLogger("pathe-monitor")

DEFAULT_CONFIG = Path(__file__).with_name("config.yaml")
EXAMPLE_CONFIG = Path(__file__).with_name("config.example.yaml")

PATHE_SHOWTIMES_URL = (
    "https://www.pathe.fr/api/show/{film}/showtimes/{cinema}/{date}?language=fr"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

AVAILABLE_STATUSES = {
    "available",
    "open",
    "reservable",
    "ok",
}
UNAVAILABLE_STATUSES = {
    "soldout",
    "sold_out",
    "full",
    "complet",
    "unavailable",
    "closed",
    "disabled",
    "not_available",
    "notavailable",
}


@dataclass
class Showtime:
    time: str
    status: str
    version: str
    tags: list[str]
    ref_cmd: str
    auditorium_name: str
    auditorium_capacity: str | int | None
    raw: dict[str, Any]

    @property
    def hhmm(self) -> str:
        # "2026-08-05 21:00:00" -> "21:00"
        m = re.search(r"(\d{2}:\d{2})", self.time or "")
        return m.group(1) if m else ""


@dataclass
class CheckResult:
    matched: bool
    showtime: Showtime | None
    session_bookable: bool | None
    free_seats: int | None
    booking_url: str | None
    detail: str
    all_showtimes: list[Showtime]


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        if EXAMPLE_CONFIG.exists():
            raise SystemExit(
                f"Missing {path}. Copy config.example.yaml to config.yaml first:\n"
                f"  cp {EXAMPLE_CONFIG.name} {path.name}"
            )
        raise SystemExit(f"Missing config file: {path}")
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # Env overrides for secrets
    alerts = cfg.setdefault("alerts", {})
    tg = alerts.setdefault("telegram", {})
    dc = alerts.setdefault("discord", {})
    wh = alerts.setdefault("webhook", {})
    tg["bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN", tg.get("bot_token") or "")
    tg["chat_id"] = os.getenv("TELEGRAM_CHAT_ID", tg.get("chat_id") or "")
    dc["webhook_url"] = os.getenv("DISCORD_WEBHOOK_URL", dc.get("webhook_url") or "")
    wh["url"] = os.getenv("GENERIC_WEBHOOK_URL", wh.get("url") or "")
    return cfg


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Referer": "https://www.pathe.fr/",
            "Origin": "https://www.pathe.fr",
        }
    )
    return s


def fetch_showtimes(
    session: requests.Session, film: str, cinema: str, date: str
) -> list[Showtime]:
    url = PATHE_SHOWTIMES_URL.format(film=film, cinema=cinema, date=date)
    r = session.get(url, timeout=30)
    if r.status_code == 403:
        raise RuntimeError(
            "Pathé blocked this IP (Akamai 403). Run the script from your home/laptop "
            "network in France, not a datacenter/VPN exit node."
        )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected showtimes payload: {type(data)}")
    out: list[Showtime] = []
    for item in data:
        out.append(
            Showtime(
                time=str(item.get("time") or ""),
                status=str(item.get("status") or "").lower(),
                version=str(item.get("version") or "").lower(),
                tags=[str(t).lower() for t in (item.get("tags") or [])],
                ref_cmd=str(item.get("refCmd") or ""),
                auditorium_name=str(item.get("auditoriumName") or ""),
                auditorium_capacity=item.get("auditoriumCapacity"),
                raw=item,
            )
        )
    return out


def match_showtime(showtimes: list[Showtime], cfg: dict[str, Any]) -> Showtime | None:
    want_time = cfg["time"]
    want_version = (cfg.get("required_version") or "").strip().lower()
    any_tags = [t.lower() for t in (cfg.get("required_tags_any") or [])]
    all_tags = [t.lower() for t in (cfg.get("required_tags_all") or [])]

    candidates = [s for s in showtimes if s.hhmm == want_time]
    filtered: list[Showtime] = []
    for s in candidates:
        if want_version and s.version != want_version:
            continue
        if any_tags and not any(t in s.tags for t in any_tags):
            continue
        if all_tags and not all(t in s.tags for t in all_tags):
            continue
        filtered.append(s)

    if not filtered:
        return None
    # Prefer IMAX-ish tags if several remain
    def score(s: Showtime) -> tuple[int, int]:
        imax = 1 if any("imax" in t for t in s.tags) else 0
        return (imax, len(s.tags))

    filtered.sort(key=score, reverse=True)
    return filtered[0]


def session_is_bookable(status: str) -> bool | None:
    s = (status or "").lower()
    if s in AVAILABLE_STATUSES:
        return True
    if s in UNAVAILABLE_STATUSES:
        return False
    if not s:
        return None
    # Unknown value: treat non-empty unknown as maybe-bookable so seat check can run
    return True


def _extract_free_seats_from_json(payload: Any) -> int | None:
    """Best-effort parse of booking/seat JSON shapes."""
    if payload is None:
        return None

    if isinstance(payload, dict):
        for key in (
            "availableSeats",
            "available_seats",
            "freeSeats",
            "free_seats",
            "nbAvailableSeats",
            "seatsAvailable",
        ):
            if key in payload and isinstance(payload[key], int):
                return payload[key]

        seats = payload.get("seats") or payload.get("seatList") or payload.get("items")
        if isinstance(seats, list):
            free = 0
            seen = False
            for seat in seats:
                if not isinstance(seat, dict):
                    continue
                seen = True
                state = str(
                    seat.get("status")
                    or seat.get("state")
                    or seat.get("availability")
                    or ""
                ).lower()
                seat_type = str(seat.get("type") or seat.get("seatType") or "").lower()
                if state in {"available", "free", "vacant", "open", "a", "1", "true"}:
                    if "wheelchair" in seat_type or "pmr" in seat_type:
                        continue
                    free += 1
                elif seat.get("available") is True or seat.get("isAvailable") is True:
                    free += 1
            if seen:
                return free

        rows = payload.get("rows") or payload.get("Rows")
        if isinstance(rows, list):
            free = 0
            seen = False
            for row in rows:
                row_seats = []
                if isinstance(row, dict):
                    row_seats = row.get("seats") or row.get("Seats") or []
                if not isinstance(row_seats, list):
                    continue
                for seat in row_seats:
                    if not isinstance(seat, dict):
                        continue
                    seen = True
                    seat_type = str(
                        seat.get("type") or seat.get("seatType") or ""
                    ).lower()
                    if "wheelchair" in seat_type or "pmr" in seat_type:
                        continue
                    state = str(
                        seat.get("status")
                        or seat.get("state")
                        or seat.get("Status")
                        or ""
                    ).lower()
                    if state in {"available", "free", "vacant", "open", "a"}:
                        free += 1
                    elif seat.get("available") is True:
                        free += 1
            if seen:
                return free

        # Recurse into nested objects commonly used by booking engines
        for key in ("data", "result", "payload", "seatMap", "seatmap", "auditorium"):
            if key in payload:
                got = _extract_free_seats_from_json(payload[key])
                if got is not None:
                    return got

    if isinstance(payload, list):
        # list of seats
        if payload and all(isinstance(x, dict) for x in payload):
            return _extract_free_seats_from_json({"seats": payload})

    return None


def count_free_seats_playwright(
    booking_url: str, headless: bool = True, timeout_ms: int = 45000
) -> tuple[int | None, str]:
    if sync_playwright is None:
        return None, "Playwright is not installed. Run: pip install -r requirements.txt && playwright install chromium"

    if not booking_url:
        return None, "No booking URL (refCmd) on this showtime"

    json_counts: list[int] = []
    notes: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            locale="fr-FR",
            timezone_id="Europe/Paris",
            user_agent=USER_AGENT,
        )
        page = context.new_page()

        def on_response(resp: Any) -> None:
            try:
                ctype = (resp.headers or {}).get("content-type", "")
                url = resp.url
                if resp.status != 200:
                    return
                interesting = any(
                    k in url.lower()
                    for k in ("seat", "booking", "placement", "availability", "session")
                )
                if "application/json" not in ctype and not interesting:
                    return
                if "application/json" not in ctype:
                    return
                data = resp.json()
                got = _extract_free_seats_from_json(data)
                if got is not None:
                    json_counts.append(got)
                    notes.append(f"json:{urlparse(url).path}={got}")
            except Exception:
                return

        page.on("response", on_response)
        page.goto(booking_url, wait_until="domcontentloaded", timeout=timeout_ms)
        # Cookie banners / consent — best effort
        for selector in (
            "button:has-text('Tout accepter')",
            "button:has-text('Accepter')",
            "button:has-text('Accept all')",
            "#didomi-notice-agree-button",
        ):
            try:
                page.locator(selector).first.click(timeout=2000)
            except Exception:
                pass

        page.wait_for_timeout(4000)

        # DOM heuristics for free seats
        dom_selectors = [
            "[data-status='available']",
            "[data-seat-status='available']",
            ".seat.available",
            ".seat--available",
            ".seat-available",
            "button.seat:not([disabled])",
            ".placement-seat.available",
            "[class*='seat'][class*='available']",
        ]
        dom_count = None
        for sel in dom_selectors:
            try:
                n = page.locator(sel).count()
                if n > 0:
                    dom_count = n
                    notes.append(f"dom:{sel}={n}")
                    break
            except Exception:
                continue

        # Text fallback: visible "Complet" with no selectable seats
        body_text = ""
        try:
            body_text = page.inner_text("body")
        except Exception:
            pass

        browser.close()

    if json_counts:
        # Prefer the smallest positive interpretation from seat map payloads
        return max(json_counts), "; ".join(notes) or "seat json"
    if dom_count is not None:
        return dom_count, "; ".join(notes) or "seat dom"
    if re.search(r"\bcomplet\b", body_text, re.I):
        return 0, "booking page shows Complet"
    return None, "could not parse seat map (session may still be bookable — open the link)"


def check_once(session: requests.Session, cfg: dict[str, Any]) -> CheckResult:
    showtimes = fetch_showtimes(
        session, cfg["film_slug"], cfg["cinema_slug"], cfg["date"]
    )
    show = match_showtime(showtimes, cfg)
    if not show:
        times = ", ".join(sorted({s.hhmm for s in showtimes})) or "(none)"
        return CheckResult(
            matched=False,
            showtime=None,
            session_bookable=None,
            free_seats=None,
            booking_url=None,
            detail=f"No matching showtime at {cfg['time']}. Times found: {times}",
            all_showtimes=showtimes,
        )

    bookable = session_is_bookable(show.status)
    mode = (cfg.get("check_mode") or "auto").lower()
    free_seats: int | None = None
    booking_url = show.ref_cmd or None
    detail = (
        f"Matched {show.time} status={show.status!r} version={show.version!r} "
        f"tags={show.tags} room={show.auditorium_name} capacity={show.auditorium_capacity}"
    )
    if booking_url:
        detail = f"{detail} booking={booking_url}"
    else:
        detail = f"{detail} booking=(none)"

    # In auto mode, always try the seat map when a booking URL exists — including
    # sold-out shows, because cancellations can free seats before status flips.
    need_seats = mode == "seats" or (mode == "auto" and bool(booking_url))
    if mode == "showtimes":
        need_seats = False

    if need_seats and booking_url:
        free_seats, seat_detail = count_free_seats_playwright(
            booking_url,
            headless=bool(cfg.get("headless", True)),
            timeout_ms=int(cfg.get("browser_timeout_ms", 45000)),
        )
        detail = f"{detail} | seats: {seat_detail}"
    elif mode == "auto" and not booking_url and bookable is False:
        detail = (
            f"{detail} | no booking link while sold out — "
            "will alert when Pathé status becomes available"
        )

    return CheckResult(
        matched=True,
        showtime=show,
        session_bookable=bookable,
        free_seats=free_seats,
        booking_url=booking_url,
        detail=detail,
        all_showtimes=showtimes,
    )


def is_alertable(result: CheckResult, min_free: int) -> bool:
    if not result.matched or not result.showtime:
        return False
    if result.free_seats is not None:
        return result.free_seats >= min_free
    # Fallback when seat map could not be parsed: alert if session status is bookable
    return result.session_bookable is True


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=30,
    )
    r.raise_for_status()


def send_discord(webhook_url: str, text: str) -> None:
    r = requests.post(webhook_url, json={"content": text[:1900]}, timeout=30)
    r.raise_for_status()


def send_webhook(url: str, payload: dict[str, Any]) -> None:
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()


def notify(cfg: dict[str, Any], result: CheckResult) -> None:
    show = result.showtime
    assert show is not None
    free = result.free_seats
    free_txt = str(free) if free is not None else "unknown (session bookable)"
    text = (
        "🎟️ Place(s) dispo — L'Odyssée IMAX 70mm\n"
        f"Pathé Odysseum — {cfg['date']} {cfg['time']} (heure française)\n"
        f"Statut séance: {show.status}\n"
        f"Fauteuils libres (estim.): {free_txt}\n"
        f"Salle: {show.auditorium_name or '?'} / capacité {show.auditorium_capacity or '?'}\n"
        f"Réserver: {result.booking_url or 'https://www.pathe.fr/evenements/l-odyssee-projection-imax-70mm-54413'}"
    )
    LOG.info("ALERT\n%s", text)
    # Terminal bell
    print("\a", end="", flush=True)

    alerts = cfg.get("alerts") or {}
    tg = alerts.get("telegram") or {}
    if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id"):
        try:
            send_telegram(tg["bot_token"], tg["chat_id"], text)
            LOG.info("Telegram alert sent")
        except Exception as e:
            LOG.error("Telegram failed: %s", e)

    dc = alerts.get("discord") or {}
    if dc.get("enabled") and dc.get("webhook_url"):
        try:
            send_discord(dc["webhook_url"], text)
            LOG.info("Discord alert sent")
        except Exception as e:
            LOG.error("Discord failed: %s", e)

    wh = alerts.get("webhook") or {}
    if wh.get("enabled") and wh.get("url"):
        try:
            send_webhook(
                wh["url"],
                {
                    "message": text,
                    "date": cfg["date"],
                    "time": cfg["time"],
                    "status": show.status,
                    "free_seats": result.free_seats,
                    "booking_url": result.booking_url,
                },
            )
            LOG.info("Webhook alert sent")
        except Exception as e:
            LOG.error("Webhook failed: %s", e)


def get_timezone(name: str | None) -> ZoneInfo:
    """Resolve IANA timezone; on Windows the tzdata package is required."""
    key = name or "Europe/Paris"
    try:
        return ZoneInfo(key)
    except Exception as e:
        raise SystemExit(
            f"Unknown/unavailable timezone {key!r}: {e}\n"
            "On Windows, install timezone data then retry:\n"
            "  pip install tzdata"
        ) from e


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def once_and_print(cfg: dict[str, Any]) -> int:
    session = build_session()
    result = check_once(session, cfg)
    LOG.info(result.detail)
    if not result.matched:
        return 2
    min_free = int(cfg.get("min_free_seats", 1))
    if is_alertable(result, min_free):
        notify(cfg, result)
        return 0
    LOG.info(
        "No alert — free_seats=%s session_bookable=%s",
        result.free_seats,
        result.session_bookable,
    )
    return 1


def loop(cfg: dict[str, Any]) -> int:
    session = build_session()
    interval = max(60, int(cfg.get("interval_seconds", 120)))
    min_free = int(cfg.get("min_free_seats", 1))
    cooldown = int(cfg.get("alert_cooldown_seconds", 300))
    stop_on_alert = bool(cfg.get("stop_on_alert", False))
    last_alert_at = 0.0

    tz = get_timezone(cfg.get("timezone"))
    LOG.info(
        "Watching %s @ %s %s %s (every %ss, mode=%s)",
        cfg["film_slug"],
        cfg["cinema_slug"],
        cfg["date"],
        cfg["time"],
        interval,
        cfg.get("check_mode", "auto"),
    )

    while True:
        now = datetime.now(tz)
        try:
            result = check_once(session, cfg)
            LOG.info(result.detail)
            if is_alertable(result, min_free):
                if time.time() - last_alert_at >= cooldown:
                    notify(cfg, result)
                    last_alert_at = time.time()
                    if stop_on_alert:
                        LOG.info("Stopping after alert (stop_on_alert=true)")
                        return 0
                else:
                    LOG.info("Alert suppressed (cooldown)")
            else:
                LOG.info(
                    "Still waiting — free_seats=%s bookable=%s (%s)",
                    result.free_seats,
                    result.session_bookable,
                    now.strftime("%Y-%m-%d %H:%M:%S %Z"),
                )
        except Exception as e:
            LOG.error("Check failed: %s", e)

        time.sleep(interval)


def list_showtimes(cfg: dict[str, Any]) -> int:
    session = build_session()
    showtimes = fetch_showtimes(
        session, cfg["film_slug"], cfg["cinema_slug"], cfg["date"]
    )
    if not showtimes:
        print("No showtimes returned for that date.")
        return 2
    print(f"{len(showtimes)} showtimes on {cfg['date']}:")
    for s in sorted(showtimes, key=lambda x: x.time):
        print(
            f"  {s.hhmm:>5}  status={s.status:<12} version={s.version:<5} "
            f"tags={','.join(s.tags) or '-'}  room={s.auditorium_name}  "
            f"cap={s.auditorium_capacity}"
        )
        if s.ref_cmd:
            print(f"         {s.ref_cmd}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Monitor Pathé seat availability for L'Odyssée IMAX 70mm"
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to config.yaml",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("once", help="Run a single check (default)")
    sub.add_parser("loop", help="Poll until interrupted")
    sub.add_parser("list", help="List all showtimes for the configured date")

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    command = args.command or "once"

    if command == "list":
        return list_showtimes(cfg)
    if command == "loop":
        return loop(cfg)
    return once_and_print(cfg)


if __name__ == "__main__":
    sys.exit(main())
