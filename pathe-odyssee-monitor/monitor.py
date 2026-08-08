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


def sanitize_yaml_text(text: str) -> str:
    """Normalize text that phone editors often corrupt.

    Android/Samsung editors and copy-paste can inject non-breaking spaces,
    smart quotes, or a UTF-8 BOM. Those still "look" identical on a laptop
    preview but break PyYAML on Termux.
    """
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    # Unicode spaces -> ASCII space (keeps newlines/tabs out of this set)
    text = re.sub(r"[\u00a0\u1680\u180e\u2000-\u200b\u202f\u205f\u3000\ufeff]", " ", text)
    # Smart quotes -> plain quotes
    text = (
        text.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )
    # Normalize newlines
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        if EXAMPLE_CONFIG.exists():
            raise SystemExit(
                f"Missing {path}. Copy config.example.yaml to config.yaml first:\n"
                f"  cp {EXAMPLE_CONFIG.name} {path.name}"
            )
        raise SystemExit(f"Missing config file: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        cfg = yaml.safe_load(sanitize_yaml_text(raw)) or {}
    except yaml.YAMLError as e:
        raise SystemExit(
            f"Invalid YAML in {path}:\n{e}\n\n"
            "If this file works on your laptop but fails on Android/Termux,\n"
            "recreate it inside Termux (phone editors often insert invisible spaces):\n"
            "  nano config.yaml\n\n"
            "Also quote Telegram values, e.g.\n"
            '  bot_token: "123456:ABC..."\n'
            '  chat_id: "108457361"'
        ) from e
    if not isinstance(cfg, dict):
        raise SystemExit(f"Config root must be a mapping/object in {path}")
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


FREE_SEAT_STATES = {
    "available",
    "free",
    "vacant",
    "open",
    "empty",
    "liberated",
    "a",
    "av",
    "1",
    "true",
    "0",  # some engines use 0 = available
}
TAKEN_SEAT_STATES = {
    "sold",
    "taken",
    "occupied",
    "reserved",
    "blocked",
    "house",
    "unavailable",
    "disabled",
    "broken",
    "complet",
    "so",
    "b",
    "2",
    "3",
}


def _looks_like_seat(obj: dict[str, Any]) -> bool:
    keys = {k.lower() for k in obj}
    return bool(
        keys
        & {
            "status",
            "seatstatus",
            "seatstatusid",
            "availability",
            "isavailable",
            "available",
            "seatnumber",
            "seatsnumber",
            "position",
            "colindex",
            "columnindex",
            "seatid",
            "idseat",
        }
    )


def _seat_state(seat: dict[str, Any]) -> str:
    for key in (
        "status",
        "Status",
        "state",
        "State",
        "availability",
        "Availability",
        "seatStatus",
        "SeatStatus",
        "seatStatusId",
        "SeatStatusId",
    ):
        if key in seat and seat[key] is not None:
            return str(seat[key]).strip().lower()
    return ""


def _seat_is_free(seat: dict[str, Any]) -> bool | None:
    seat_type = str(
        seat.get("type")
        or seat.get("seatType")
        or seat.get("SeatType")
        or seat.get("description")
        or ""
    ).lower()
    if any(x in seat_type for x in ("wheelchair", "pmr", "handicap")):
        return False
    if seat.get("available") is True or seat.get("isAvailable") is True:
        return True
    if seat.get("available") is False or seat.get("isAvailable") is False:
        return False
    state = _seat_state(seat)
    if not state:
        return None
    if state in FREE_SEAT_STATES:
        return True
    if state in TAKEN_SEAT_STATES:
        return False
    if "avail" in state and "unavail" not in state:
        return True
    if any(x in state for x in ("sold", "taken", "occup", "reserv", "block")):
        return False
    return None


def _extract_free_seats_from_json(payload: Any) -> int | None:
    """Best-effort parse of booking/seat JSON shapes (Pathé / Vista-like)."""
    if payload is None:
        return None

    found_counts: list[int] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(node, dict):
            for key in (
                "availableSeats",
                "available_seats",
                "freeSeats",
                "free_seats",
                "nbAvailableSeats",
                "seatsAvailable",
                "AvailableSeats",
            ):
                val = node.get(key)
                if isinstance(val, int) and val >= 0:
                    found_counts.append(val)

            # Direct seat collections
            for key in (
                "seats",
                "Seats",
                "seatList",
                "SeatList",
                "items",
                "Places",
                "places",
            ):
                seats = node.get(key)
                if isinstance(seats, list) and seats and all(
                    isinstance(x, dict) for x in seats
                ):
                    free = 0
                    seen = 0
                    for seat in seats:
                        if not _looks_like_seat(seat) and not any(
                            k in seat for k in ("status", "Status", "available", "isAvailable")
                        ):
                            continue
                        flag = _seat_is_free(seat)
                        if flag is None:
                            continue
                        seen += 1
                        if flag:
                            free += 1
                    if seen:
                        found_counts.append(free)

            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            # list of seat-like dicts
            if node and all(isinstance(x, dict) for x in node):
                seatish = [x for x in node if _looks_like_seat(x)]
                if len(seatish) >= 5:
                    free = 0
                    seen = 0
                    for seat in seatish:
                        flag = _seat_is_free(seat)
                        if flag is None:
                            continue
                        seen += 1
                        if flag:
                            free += 1
                    if seen:
                        found_counts.append(free)
            for item in node:
                walk(item, depth + 1)

    walk(payload)
    if not found_counts:
        return None
    # Prefer the largest seat-collection interpretation (full map), not tiny nested counts
    return max(found_counts)


def count_free_seats_playwright(
    booking_url: str,
    headless: bool = True,
    timeout_ms: int = 45000,
    debug_dir: Path | None = None,
) -> tuple[int | None, str]:
    if sync_playwright is None:
        return None, "Playwright is not installed. Run: pip install -r requirements.txt && playwright install chromium"

    if not booking_url:
        return None, "No booking URL (refCmd) on this showtime"

    json_counts: list[int] = []
    notes: list[str] = []
    json_meta: list[dict[str, Any]] = []
    body_text = ""
    final_url = booking_url
    title = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            locale="fr-FR",
            timezone_id="Europe/Paris",
            user_agent=USER_AGENT,
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()

        def on_response(resp: Any) -> None:
            try:
                ctype = (resp.headers or {}).get("content-type", "")
                url = resp.url
                if resp.status != 200:
                    return
                if "application/json" not in ctype and "text/json" not in ctype:
                    # still try obvious seat endpoints returning other types
                    if not any(
                        k in url.lower()
                        for k in (
                            "seat",
                            "placement",
                            "availability",
                            "vista",
                            "booking",
                        )
                    ):
                        return
                try:
                    data = resp.json()
                except Exception:
                    return
                got = _extract_free_seats_from_json(data)
                path = urlparse(url).path
                json_meta.append(
                    {
                        "url": url,
                        "path": path,
                        "parsed_free_seats": got,
                        "top_keys": list(data.keys())[:40]
                        if isinstance(data, dict)
                        else [f"list[{len(data)}]"]
                        if isinstance(data, list)
                        else [type(data).__name__],
                    }
                )
                if got is not None:
                    json_counts.append(got)
                    notes.append(f"json:{path}={got}")
                    if debug_dir is not None:
                        debug_dir.mkdir(parents=True, exist_ok=True)
                        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", path.strip("/"))[:80]
                        (debug_dir / f"json_{safe or 'root'}.json").write_text(
                            json.dumps(data, ensure_ascii=False, indent=2)[:2_000_000],
                            encoding="utf-8",
                        )
            except Exception:
                return

        page.on("response", on_response)
        page.goto(booking_url, wait_until="domcontentloaded", timeout=timeout_ms)

        # Cookie / consent banners
        for selector in (
            "button:has-text('Tout accepter')",
            "button:has-text('Tout Accepter')",
            "button:has-text('Accepter & Fermer')",
            "button:has-text('Accepter')",
            "button:has-text('Accept all')",
            "button:has-text('Accept')",
            "#didomi-notice-agree-button",
            "button#onetrust-accept-btn-handler",
        ):
            try:
                loc = page.locator(selector).first
                if loc.is_visible(timeout=1500):
                    loc.click(timeout=2000)
                    page.wait_for_timeout(500)
            except Exception:
                pass

        # Booking flows sometimes need an explicit continue / choose seats step
        for selector in (
            "button:has-text('Choisir')",
            "button:has-text('Continuer')",
            "button:has-text('Sélectionner')",
            "a:has-text('Choisir mes places')",
            "button:has-text('places')",
        ):
            try:
                loc = page.locator(selector).first
                if loc.is_visible(timeout=1500):
                    loc.click(timeout=2000)
                    page.wait_for_timeout(1000)
            except Exception:
                pass

        try:
            page.wait_for_load_state("networkidle", timeout=min(15000, timeout_ms))
        except Exception:
            pass
        page.wait_for_timeout(5000)

        # Also scan same-origin frames
        frames = page.frames
        dom_count = None
        dom_selectors = [
            "[data-status='available']",
            "[data-seat-status='available']",
            "[data-seat-status='Available']",
            ".seat.available",
            ".seat--available",
            ".seat-available",
            ".seat.is-available",
            "button.seat:not([disabled]):not(.disabled):not(.sold)",
            ".placement-seat.available",
            "[class*='seat'][class*='available']",
            "svg [class*='available']",
            "[aria-label*='disponible' i]",
            "[title*='disponible' i]",
        ]
        for frame in frames:
            for sel in dom_selectors:
                try:
                    n = frame.locator(sel).count()
                    if n > 0:
                        dom_count = n
                        notes.append(f"dom:{sel}={n}")
                        break
                except Exception:
                    continue
            if dom_count is not None:
                break

        try:
            body_text = page.inner_text("body")
            final_url = page.url
            title = page.title()
        except Exception:
            pass

        # Pathé shows a clear counter: "0 place libre" / "2 places libres"
        explicit = parse_places_libres(body_text)
        if explicit is not None:
            notes.append(f"ui:places_libres={explicit}")
            browser.close()
            return explicit, "; ".join(notes) or "ui places libres"

        if debug_dir is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            try:
                page.screenshot(path=str(debug_dir / "booking.png"), full_page=True)
            except Exception:
                pass
            try:
                (debug_dir / "booking.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            (debug_dir / "network_json_index.json").write_text(
                json.dumps(json_meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (debug_dir / "page_meta.json").write_text(
                json.dumps(
                    {
                        "booking_url": booking_url,
                        "final_url": final_url,
                        "title": title,
                        "notes": notes,
                        "json_counts": json_counts,
                        "dom_count": dom_count,
                        "body_excerpt": body_text[:4000],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        browser.close()

    if json_counts:
        return max(json_counts), "; ".join(notes) or "seat json"
    if dom_count is not None:
        return dom_count, "; ".join(notes) or "seat dom"
    explicit = parse_places_libres(body_text)
    if explicit is not None:
        return explicit, "; ".join(notes + [f"ui:places_libres={explicit}"])
    if re.search(r"\bcomplet\b", body_text, re.I):
        return 0, "booking page shows Complet"
    if re.search(r"plus de places? disponibles?|aucune place", body_text, re.I):
        return 0, "booking page says no seats left"
    hint = "could not parse seat map"
    if debug_dir is not None:
        hint += f" — debug saved in {debug_dir}"
    else:
        hint += " — run: python monitor.py debug-seats"
    hint += " (no alert unless free seats are confirmed)"
    return None, hint


def parse_places_libres(text: str) -> int | None:
    """Parse Pathé UI strings like '0 place libre' / '3 places libres'."""
    if not text:
        return None
    # Prefer the explicit counter shown above the seat map
    matches = re.findall(
        r"(\d+)\s*places?\s*libres?",
        text,
        flags=re.IGNORECASE,
    )
    if matches:
        # If several counters appear, take the minimum non-negative (map header)
        values = [int(m) for m in matches]
        return min(values)
    if re.search(
        r"\bcomplet\b|plus de places?\s+disponibles?|aucune place(?:\s+disponible)?|"
        r"0\s*place\s*disponible|sold.?out",
        text,
        flags=re.IGNORECASE,
    ):
        return 0
    return None


def probe_booking_http(
    session: requests.Session, booking_url: str
) -> tuple[int | None, str]:
    """Lightweight booking-page probe (works on Termux; no Playwright).

    Pathé often keeps showtimes status='available' even with 0 seats left.
    The booking page text 'N place(s) libre(s)' is the reliable signal.
    """
    if not booking_url:
        return None, "no booking url"

    # Keep original URL (may include token); also try clean booking path
    candidates = [booking_url]
    parsed = urlparse(booking_url)
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if clean not in candidates:
        candidates.append(clean)

    base = clean.rstrip("/")
    if base.endswith("/booking"):
        root = base[: -len("/booking")]
        candidates.extend(
            [
                f"{root}/seats",
                f"{root}/placement",
                f"{root}/seat-map",
                f"{root}/api/seats",
            ]
        )

    notes: list[str] = []
    best_free: int | None = None
    saw_explicit_counter = False

    for url in candidates:
        try:
            r = session.get(url, timeout=25, allow_redirects=True)
        except Exception as e:
            notes.append(f"http-err:{urlparse(url).path}:{e.__class__.__name__}")
            continue

        ctype = (r.headers.get("content-type") or "").lower()
        path = urlparse(url).path or "/"

        if r.status_code == 403:
            notes.append(f"http:{path}:403")
            continue
        if r.status_code >= 400:
            notes.append(f"http:{path}:{r.status_code}")
            continue

        # JSON seat payloads
        if "json" in ctype or r.text.lstrip().startswith(("{", "[")):
            try:
                data = r.json()
                got = _extract_free_seats_from_json(data)
                if got is not None:
                    best_free = got if best_free is None else min(best_free, got)
                    notes.append(f"http-json:{path}={got}")
                else:
                    notes.append(f"http-json:{path}:unparsed")
                continue
            except Exception:
                pass

        text = r.text or ""
        explicit = parse_places_libres(text)
        if explicit is not None:
            saw_explicit_counter = True
            best_free = explicit if best_free is None else min(best_free, explicit)
            notes.append(f"http-html:{path}:places_libres={explicit}")
            continue

        # Available-seat markers in HTML/JS (weaker signal)
        avail_hits = len(
            re.findall(
                r"data-status=[\"']available[\"']|"
                r"seat--available|seat-available|seat\.available|"
                r"[\"']status[\"']\s*:\s*[\"']available[\"']|"
                r"[\"']SeatStatus[\"']\s*:\s*[\"']Available[\"']",
                text,
                re.I,
            )
        )
        if avail_hits > 0 and not saw_explicit_counter:
            estimate = max(1, min(avail_hits, 50))
            best_free = estimate if best_free is None else max(best_free, estimate)
            notes.append(f"http-html:{path}:avail~{estimate}")
        else:
            notes.append(f"http-html:{path}:no-avail-marker")

    if best_free is not None:
        return best_free, "; ".join(notes) or "http probe"
    return None, "; ".join(notes) or "http probe failed"


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

    # Pathé can keep status='soldout' while one cancelled seat is briefly free.
    # Always probe the booking link when present (HTTP on phone; Playwright on PC).
    if booking_url and mode in {"auto", "seats", "showtimes"}:
        if mode in {"auto", "seats"} and sync_playwright is not None:
            free_seats, seat_detail = count_free_seats_playwright(
                booking_url,
                headless=bool(cfg.get("headless", True)),
                timeout_ms=int(cfg.get("browser_timeout_ms", 45000)),
                debug_dir=Path(cfg["debug_dir"]) if cfg.get("debug_dir") else None,
            )
            detail = f"{detail} | seats: {seat_detail}"
            # If Playwright cannot parse, fall back to HTTP markers
            if free_seats is None:
                http_free, http_detail = probe_booking_http(session, booking_url)
                if http_free is not None:
                    free_seats = http_free
                detail = f"{detail} | http: {http_detail}"
        else:
            free_seats, http_detail = probe_booking_http(session, booking_url)
            detail = f"{detail} | http: {http_detail}"
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


def is_alertable(
    result: CheckResult,
    min_free: int,
    require_confirmed_free_seats: bool = True,
) -> bool:
    if not result.matched or not result.showtime:
        return False
    if result.free_seats is not None:
        return result.free_seats >= min_free
    # Pathé often keeps showtimes status='available' while the seat map says
    # "0 place libre". Never trust status alone unless explicitly allowed.
    if require_confirmed_free_seats:
        return False
    return result.session_bookable is True


def watch_key(cfg: dict[str, Any]) -> str:
    return (
        f"{cfg.get('film_slug')}|{cfg.get('cinema_slug')}|"
        f"{cfg.get('date')}|{cfg.get('time')}"
    )


def state_path_for(cfg: dict[str, Any]) -> Path:
    custom = cfg.get("state_file")
    if custom:
        return Path(custom)
    return Path(__file__).with_name(".monitor_state.json")


def load_monitor_state(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        LOG.warning("Could not read state file %s: %s", path, e)
    return {}


def save_monitor_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        LOG.warning("Could not write state file %s: %s", path, e)


def should_send_alert(
    result: CheckResult,
    min_free: int,
    prev: dict[str, Any] | None,
    transition_only: bool,
    require_confirmed_free_seats: bool = True,
) -> tuple[bool, str]:
    """Decide whether to notify.

    Pathé often leaves status='available' after a brief free seat was taken.
    Default behavior alerts only on a rising edge (not-available -> available),
    or when the estimated free-seat count increases.
    """
    alertable = is_alertable(
        result, min_free, require_confirmed_free_seats=require_confirmed_free_seats
    )
    prev = prev or {}
    prev_alertable = bool(prev.get("alertable"))
    prev_free = prev.get("free_seats")

    if not alertable:
        if result.session_bookable and result.free_seats == 0:
            return False, "status available but 0 place libre"
        if result.session_bookable and result.free_seats is None:
            return False, "status available but free seats not confirmed"
        return False, "not alertable"

    if not transition_only:
        return True, "alertable"

    # Rising edge: was not alertable, now is
    if not prev_alertable:
        return True, "transition to available"

    # Free-seat count increased (another cancellation while still 'available')
    if (
        result.free_seats is not None
        and isinstance(prev_free, int)
        and result.free_seats > prev_free
    ):
        return True, f"free_seats increased {prev_free}->{result.free_seats}"

    if (
        result.free_seats is not None
        and prev_free is None
        and result.free_seats >= min_free
        and prev_alertable
    ):
        # We newly learned a concrete free-seat count
        return True, f"free_seats confirmed={result.free_seats}"

    return False, "already alerted for current available period"


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
    require_confirmed = bool(cfg.get("require_confirmed_free_seats", True))
    # `once` always notifies if currently alertable (manual check).
    if is_alertable(
        result, min_free, require_confirmed_free_seats=require_confirmed
    ):
        notify(cfg, result)
        return 0
    LOG.info(
        "No alert — free_seats=%s session_bookable=%s require_confirmed=%s",
        result.free_seats,
        result.session_bookable,
        require_confirmed,
    )
    return 1


def loop(cfg: dict[str, Any]) -> int:
    session = build_session()
    interval = max(30, int(cfg.get("interval_seconds", 60)))
    min_free = int(cfg.get("min_free_seats", 1))
    cooldown = int(cfg.get("alert_cooldown_seconds", 300))
    stop_on_alert = bool(cfg.get("stop_on_alert", False))
    transition_only = bool(cfg.get("alert_on_transition_only", True))
    require_confirmed = bool(cfg.get("require_confirmed_free_seats", True))
    last_alert_at = 0.0

    state_file = state_path_for(cfg)
    all_state = load_monitor_state(state_file)
    key = watch_key(cfg)
    prev = dict(all_state.get(key) or {})
    first_sample = key not in all_state

    tz = get_timezone(cfg.get("timezone"))
    if interval < 60:
        LOG.warning(
            "Polling every %ss is aggressive — higher chance of Pathé/Akamai 403. "
            "Prefer 60s; use 30s only for short tests.",
            interval,
        )
    LOG.info(
        "Watching %s @ %s %s %s (every %ss, mode=%s, transition_only=%s, "
        "require_confirmed_free_seats=%s)",
        cfg["film_slug"],
        cfg["cinema_slug"],
        cfg["date"],
        cfg["time"],
        interval,
        cfg.get("check_mode", "auto"),
        transition_only,
        require_confirmed,
    )
    if prev:
        LOG.info(
            "Loaded prior state: alertable=%s free_seats=%s status=%s",
            prev.get("alertable"),
            prev.get("free_seats"),
            prev.get("status"),
        )

    while True:
        now = datetime.now(tz)
        try:
            result = check_once(session, cfg)
            LOG.info(result.detail)
            alertable = is_alertable(
                result, min_free, require_confirmed_free_seats=require_confirmed
            )
            if first_sample and transition_only:
                # Remember current Pathé state without replaying a past availability.
                send, reason = False, "bootstrap current state (no alert)"
                first_sample = False
            else:
                send, reason = should_send_alert(
                    result,
                    min_free,
                    prev,
                    transition_only=transition_only,
                    require_confirmed_free_seats=require_confirmed,
                )

            # Persist current observation (even when not alerting)
            prev = {
                "alertable": alertable,
                "free_seats": result.free_seats,
                "status": result.showtime.status if result.showtime else None,
                "bookable": result.session_bookable,
                "updated_at": now.isoformat(),
            }
            all_state[key] = prev
            save_monitor_state(state_file, all_state)

            if send:
                if time.time() - last_alert_at >= cooldown:
                    LOG.info("Sending alert (%s)", reason)
                    notify(cfg, result)
                    last_alert_at = time.time()
                    prev["last_alert_at"] = last_alert_at
                    all_state[key] = prev
                    save_monitor_state(state_file, all_state)
                    if stop_on_alert:
                        LOG.info("Stopping after alert (stop_on_alert=true)")
                        return 0
                else:
                    LOG.info("Alert suppressed (cooldown) — %s", reason)
            elif alertable:
                LOG.info(
                    "Still available, no new alert — %s (free_seats=%s bookable=%s)",
                    reason,
                    result.free_seats,
                    result.session_bookable,
                )
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


def debug_seats(cfg: dict[str, Any]) -> int:
    """Open the booking page once and dump HTML/JSON/screenshot for inspection."""
    session = build_session()
    showtimes = fetch_showtimes(
        session, cfg["film_slug"], cfg["cinema_slug"], cfg["date"]
    )
    show = match_showtime(showtimes, cfg)
    if not show:
        LOG.error("No matching showtime to debug")
        return 2
    if not show.ref_cmd:
        LOG.error("Matched showtime has no booking URL (refCmd)")
        return 2

    out = Path(__file__).with_name("debug-seats-output")
    LOG.info("Debugging seats for %s", show.time)
    LOG.info("Booking URL: %s", show.ref_cmd)
    free, detail = count_free_seats_playwright(
        show.ref_cmd,
        headless=bool(cfg.get("headless", True)),
        timeout_ms=int(cfg.get("browser_timeout_ms", 60000)),
        debug_dir=out,
    )
    LOG.info("Result free_seats=%s (%s)", free, detail)
    LOG.info("Debug files written to %s", out.resolve())
    return 0 if free is not None else 1


def test_alert(cfg: dict[str, Any]) -> int:
    """Send a Telegram/Discord/webhook test message using current config."""
    alerts = cfg.get("alerts") or {}
    tg = alerts.get("telegram") or {}
    dc = alerts.get("discord") or {}
    wh = alerts.get("webhook") or {}
    any_enabled = False
    ok = True

    text = (
        "✅ Test alerte Pathé monitor\n"
        f"Cible: {cfg.get('film_slug')} @ {cfg.get('cinema_slug')}\n"
        f"Séance: {cfg.get('date')} {cfg.get('time')} ({cfg.get('timezone')})\n"
        "Si tu vois ce message, Telegram/Discord est bien configuré."
    )

    if tg.get("enabled"):
        any_enabled = True
        token = (tg.get("bot_token") or "").strip()
        chat_id = str(tg.get("chat_id") or "").strip()
        if not token or not chat_id:
            LOG.error(
                "Telegram enabled but bot_token/chat_id missing in config.yaml "
                "(or TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars)."
            )
            ok = False
        else:
            try:
                send_telegram(token, chat_id, text)
                LOG.info("Telegram OK — message sent to chat_id=%s", chat_id)
            except Exception as e:
                LOG.error("Telegram FAILED: %s", e)
                ok = False
    else:
        LOG.warning("Telegram alerts.enabled is false in config.yaml")

    if dc.get("enabled"):
        any_enabled = True
        url = (dc.get("webhook_url") or "").strip()
        if not url:
            LOG.error("Discord enabled but webhook_url is empty")
            ok = False
        else:
            try:
                send_discord(url, text)
                LOG.info("Discord OK — webhook accepted the message")
            except Exception as e:
                LOG.error("Discord FAILED: %s", e)
                ok = False

    if wh.get("enabled"):
        any_enabled = True
        url = (wh.get("url") or "").strip()
        if not url:
            LOG.error("Webhook enabled but url is empty")
            ok = False
        else:
            try:
                send_webhook(url, {"message": text, "test": True})
                LOG.info("Webhook OK")
            except Exception as e:
                LOG.error("Webhook FAILED: %s", e)
                ok = False

    if not any_enabled:
        LOG.error(
            "No alert channel enabled. In config.yaml set:\n"
            "  alerts:\n"
            "    telegram:\n"
            "      enabled: true\n"
            "      bot_token: \"...\"\n"
            "      chat_id: \"...\""
        )
        return 2
    return 0 if ok else 1


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
    sub.add_parser("test-alert", help="Send a test Telegram/Discord alert")
    sub.add_parser(
        "debug-seats",
        help="Open booking page once and dump seat-map debug files",
    )

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    command = args.command or "once"

    if command == "list":
        return list_showtimes(cfg)
    if command == "loop":
        return loop(cfg)
    if command == "test-alert":
        return test_alert(cfg)
    if command == "debug-seats":
        return debug_seats(cfg)
    return once_and_print(cfg)


if __name__ == "__main__":
    sys.exit(main())
