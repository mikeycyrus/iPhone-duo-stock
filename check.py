#!/usr/bin/env python3
"""Check iPhone Duo pickup stock at five Melbourne Apple Stores.

Stdlib only. Reads Apple's public pickup-message endpoint and writes
docs/data.json for the webpage. Sends an ntfy.sh alert only when a model
becomes available at a store. The topic comes from the NTFY_TOPIC environment
variable (a GitHub Actions secret). It is never written into the repo.

Do not switch this to the fulfillment-messages endpoint. Apple returns HTTP 541
for that URL.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "docs" / "data.json"
STATE_PATH = ROOT / "state" / "availability.json"
HISTORY_PATH = ROOT / "state" / "history.json"
MELBOURNE = ZoneInfo("Australia/Melbourne")
HISTORY_LIMIT = 500
RECENT_LIMIT = 12
WATCH_ROUNDS = 5
WATCH_INTERVAL_SECONDS = 60

# Apple's fulfillment-messages URL is intentionally not used (HTTP 541).
PICKUP_URL = "https://www.apple.com/au/shop/retail/pickup-message"
BUY_URL = "https://www.apple.com/au/shop/buy-iphone/iphone-duo"
LOCATION = "3000"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
REQUEST_PAUSE_SECONDS = 0.4
BLOCK_CODES = {401, 403, 429, 503, 541}

# Models to check. This is the only list to edit.
# Put a part number back to watch that model again.
WATCHED_PARTS = (
    "MK2D4X/A",  # 256GB Star White
    "MK2F4X/A",  # 512GB Star White
)

# Full catalog, Star White then Night Sky in each capacity.
CATALOG = [
    (
        "MK2D4X/A",
        "256GB",
        "Star White",
        "https://www.apple.com/au/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-star-white",
    ),
    (
        "MK2E4X/A",
        "256GB",
        "Night Sky",
        "https://www.apple.com/au/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-night-sky",
    ),
    (
        "MK2F4X/A",
        "512GB",
        "Star White",
        "https://www.apple.com/au/shop/buy-iphone/iphone-duo/7.6-inch-display-512gb-star-white",
    ),
    (
        "MK2G4X/A",
        "512GB",
        "Night Sky",
        "https://www.apple.com/au/shop/buy-iphone/iphone-duo/7.6-inch-display-512gb-night-sky",
    ),
    (
        "MK2H4X/A",
        "1TB",
        "Star White",
        "https://www.apple.com/au/shop/buy-iphone/iphone-duo/7.6-inch-display-1tb-star-white",
    ),
    (
        "MK2J4X/A",
        "1TB",
        "Night Sky",
        "https://www.apple.com/au/shop/buy-iphone/iphone-duo/7.6-inch-display-1tb-night-sky",
    ),
    (
        "MK2K4X/A",
        "2TB",
        "Star White",
        "https://www.apple.com/au/shop/buy-iphone/iphone-duo/7.6-inch-display-2tb-star-white",
    ),
    (
        "MK2L4X/A",
        "2TB",
        "Night Sky",
        "https://www.apple.com/au/shop/buy-iphone/iphone-duo/7.6-inch-display-2tb-night-sky",
    ),
]

_catalog_ids = {item[0] for item in CATALOG}
_unknown_parts = [part for part in WATCHED_PARTS if part not in _catalog_ids]
if _unknown_parts:
    raise SystemExit("Unknown part in WATCHED_PARTS: " + ", ".join(_unknown_parts))

PARTS = [item for item in CATALOG if item[0] in WATCHED_PARTS]

STORES = [
    ("R504", "Highpoint", "https://www.apple.com/au/retail/highpoint/"),
    ("R180", "Chadstone", "https://www.apple.com/au/retail/chadstone/"),
    ("R342", "Doncaster", "https://www.apple.com/au/retail/doncaster/"),
    ("R343", "Southland", "https://www.apple.com/au/retail/southland/"),
    ("R530", "Fountain Gate", "https://www.apple.com/au/retail/fountaingate/"),
]

STORE_IDS = {store_id for store_id, _, _ in STORES}
PART_INDEX = {part: (capacity, color, url) for part, capacity, color, url in PARTS}
STORE_INDEX = {store_id: (name, url) for store_id, name, url in STORES}


def model_name(part: str) -> str:
    capacity, color, _ = PART_INDEX[part]
    return f"iPhone Duo {capacity} {color}"


def is_available(status: str | None) -> bool:
    # "unavailable" must not count. It does not start with "available",
    # and the extra check keeps that obvious.
    if not isinstance(status, str):
        return False
    value = status.lower()
    return value.startswith("available") and not value.startswith("unavailable")


def normalize_topic(raw: str | None) -> tuple[str | None, str | None]:
    """Return (topic, problem). problem is 'missing', 'invalid', or None."""
    if raw is None or not str(raw).strip():
        return None, "missing"
    topic = str(raw).strip()
    lowered = topic.lower()
    for prefix in ("https://ntfy.sh/", "http://ntfy.sh/", "ntfy.sh/"):
        if lowered.startswith(prefix):
            topic = topic[len(prefix) :]
            break
    topic = topic.split("?", 1)[0].strip().strip("/")
    if not topic or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in topic):
        return None, "invalid"
    if len(topic) > 64:
        return None, "invalid"
    return topic, None


def pages_url() -> str:
    explicit = os.environ.get("PAGES_URL", "").strip()
    if explicit:
        return explicit if explicit.endswith("/") else explicit + "/"
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner}.github.io/{name}/"
    return "https://mikeycyrus.github.io/iphone-duo-stock/"


def dump_json(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def load_json(path: Path | str | None) -> dict | None:
    if not path:
        return None
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(f"Ignoring unreadable JSON at {file_path}")
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_json(payload), encoding="utf-8")


def ordered_statuses(raw: dict | None) -> dict:
    raw = raw or {}
    ordered: dict = {}
    for part, _, _, _ in PARTS:
        stores = raw.get(part)
        if not isinstance(stores, dict):
            continue
        ordered[part] = _ordered_stores(stores)
    return ordered


def _ordered_stores(stores: dict) -> dict:
    ordered = {}
    for store_id, _, _ in STORES:
        if store_id in stores:
            ordered[store_id] = stores[store_id]
    for store_id, status in stores.items():
        if store_id not in ordered:
            ordered[store_id] = status
    return ordered


def baseline_rank(baseline: dict | None) -> tuple[str, str]:
    if not baseline:
        return ("", "")
    return (str(baseline.get("status_updated") or ""), str(baseline.get("last_success") or ""))


def choose_baseline(primary: dict | None, secondary: dict | None) -> dict | None:
    """Pick the baseline that last recorded a stock change, then the newer check."""
    if secondary is None:
        return primary
    if primary is None:
        return secondary
    if baseline_rank(secondary) > baseline_rank(primary):
        return secondary
    return primary


def statuses_differ(old: dict | None, new: dict | None) -> bool:
    old_statuses = ordered_statuses((old or {}).get("statuses"))
    new_statuses = ordered_statuses((new or {}).get("statuses"))
    return old_statuses != new_statuses


def looks_like_block_page(text: str) -> bool:
    sample = text.lstrip()[:800].lower()
    if sample.startswith("<") or sample.startswith("<!doctype"):
        return True
    markers = (
        "access denied",
        "captcha",
        "just a moment",
        "cf-browser-verification",
        "errors.edgesuite.net",
        "reference #",
    )
    return any(marker in sample for marker in markers)


def plain_http_message(status: int | None, blocked: bool) -> str:
    if status == 403:
        return (
            "Apple refused this check (HTTP 403). That usually means Apple blocked "
            "the computer running the check, not that the phones are sold out."
        )
    if status == 429:
        return (
            "Apple rate-limited this check (HTTP 429). The next check will try again. "
            "This does not mean the phones are sold out."
        )
    if status == 541:
        return (
            "Apple rejected this check (HTTP 541). That usually means the request was "
            "blocked, not that the phones are sold out."
        )
    if status == 503:
        return (
            "Apple's pickup service returned HTTP 503, so stock could not be read. "
            "This does not mean the phones are sold out."
        )
    if status == 401:
        return (
            "Apple refused this check (HTTP 401). The phones are not necessarily sold out."
        )
    if blocked:
        return (
            "Apple blocked this stock check. The phones are not necessarily sold out."
        )
    if status:
        return (
            f"The stock check failed (HTTP {status}). "
            "This does not mean the phones are sold out."
        )
    return "The stock check could not reach Apple. This does not mean the phones are sold out."


def interpret_response(status: int, body: bytes, part: str) -> dict:
    """Turn one HTTP response into a success or a plain-language failure."""
    text = body.decode("utf-8", "replace")
    if status in BLOCK_CODES or (status >= 400 and looks_like_block_page(text)):
        return {
            "ok": False,
            "blocked": True,
            "status": status,
            "message": plain_http_message(status, True),
        }
    if status >= 400:
        return {
            "ok": False,
            "blocked": False,
            "status": status,
            "message": plain_http_message(status, False),
        }
    if looks_like_block_page(text):
        return {
            "ok": False,
            "blocked": True,
            "status": status,
            "message": plain_http_message(status, True),
        }
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        blocked = looks_like_block_page(text)
        return {
            "ok": False,
            "blocked": blocked,
            "status": status,
            "message": plain_http_message(status, blocked)
            if blocked
            else "Apple's reply was not stock data. This does not mean the phones are sold out.",
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "blocked": False,
            "status": status,
            "message": "Apple's reply was not stock data. This does not mean the phones are sold out.",
        }
    head = str((payload.get("head") or {}).get("status", "200"))
    if head not in {"200", "200 OK"}:
        blocked = head in {str(code) for code in BLOCK_CODES}
        try:
            head_status = int(head)
        except ValueError:
            head_status = status
        return {
            "ok": False,
            "blocked": blocked,
            "status": head_status,
            "message": plain_http_message(head_status, blocked),
        }
    stores = stores_from_payload(payload, part)
    if not stores:
        return {
            "ok": False,
            "blocked": True,
            "status": status,
            "message": (
                "Apple answered, but did not include the Melbourne stores. "
                "That can happen when a request is rejected. "
                "This does not mean the phones are sold out."
            ),
        }
    return {"ok": True, "blocked": False, "status": status, "stores": stores}


def stores_from_payload(payload: dict, part: str) -> dict:
    found = {}
    for store in (payload.get("body") or {}).get("stores") or []:
        if not isinstance(store, dict):
            continue
        store_id = store.get("storeNumber")
        if store_id not in STORE_IDS:
            continue
        availability = (store.get("partsAvailability") or {}).get(part) or {}
        if not isinstance(availability, dict):
            availability = {}
        status = availability.get("pickupDisplay") or "unknown"
        text = availability.get("pickupSearchQuote") or ""
        found[store_id] = {"status": status, "text": text}
    return found


def fetch_part(part: str) -> dict:
    query = urllib.parse.urlencode({"pl": "true", "parts.0": part, "location": LOCATION})
    url = f"{PICKUP_URL}?{query}"
    last: dict | None = None
    for attempt in range(2):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "en-AU,en;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            body = error.read() if error.fp else b""
            status = error.code
        except Exception as error:
            last = {
                "ok": False,
                "blocked": False,
                "status": None,
                "message": (
                    "The stock check could not reach Apple "
                    f"({error.__class__.__name__}). This does not mean the phones are sold out."
                ),
            }
            if attempt == 0:
                time.sleep(1.5)
                continue
            return last
        result = interpret_response(status, body, part)
        last = result
        retryable = (not result["ok"]) and (
            result.get("status") in {429, 500, 502, 504} or result.get("status") is None
        )
        if result["ok"] or not retryable or attempt == 1:
            return result
        time.sleep(1.5)
    return last or {
        "ok": False,
        "blocked": False,
        "status": None,
        "message": "The stock check could not reach Apple. This does not mean the phones are sold out.",
    }


def find_arrivals(old_statuses: dict, observations: dict) -> list[dict]:
    arrivals = []
    for part, capacity, color, buy in PARTS:
        observed = observations.get(part)
        if not observed:
            continue
        previous_part = old_statuses.get(part) or {}
        for store_id, store_name, _store_url in STORES:
            cell = observed.get(store_id)
            if not cell or not is_available(cell.get("status")):
                continue
            previous = previous_part.get(store_id)
            if is_available(previous if isinstance(previous, str) else None):
                continue
            arrivals.append(
                {
                    "part": part,
                    "name": f"iPhone Duo {capacity} {color}",
                    "capacity": capacity,
                    "color": color,
                    "store_id": store_id,
                    "store": store_name,
                    "buy_url": buy,
                    "text": cell.get("text") or "",
                }
            )
    return arrivals


def format_notification(arrivals: list[dict], site_url: str) -> tuple[str, str, str, str]:
    """Return title, markdown body, click URL, and an ntfy Actions header."""
    lines = []
    for item in arrivals:
        lines.append(f"**{item['name']}** is available for pickup at **{item['store']}**.")
        quote = (item.get("text") or "").strip()
        if quote and "unavailable" not in quote.lower():
            lines.append(quote)
        lines.append(f"[Buy this model]({item['buy_url']})")
        lines.append("")
    lines.append(f"[Melbourne stock page]({site_url})")
    body = "\n".join(lines).strip()
    if len(arrivals) == 1:
        item = arrivals[0]
        title = f"{item['name']} in stock at {item['store']}"
        click = item["buy_url"]
        actions = f"view, Stock page, {site_url}"
    else:
        stores = list(dict.fromkeys(item["store"] for item in arrivals))
        if len(stores) == 1:
            title = f"iPhone Duo in stock at {stores[0]}"
        else:
            title = f"iPhone Duo in stock at {len(stores)} stores"
        click = site_url
        actions = f"view, Buy on Apple, {BUY_URL}"
    return title, body, click, actions


def send_ntfy(topic: str, title: str, message: str, click: str, actions: str) -> None:
    url = "https://ntfy.sh/" + urllib.parse.quote(topic)
    request = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "User-Agent": "iphone-duo-stock",
            "Title": title,
            "Click": click,
            "Tags": "iphone",
            "Priority": "4",
            "Markdown": "yes",
            "Actions": actions,
            "Content-Type": "text/markdown; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"ntfy.sh returned HTTP {error.code}") from None
    except Exception as error:
        raise RuntimeError(f"Could not reach ntfy.sh ({error.__class__.__name__})") from None


def merge_statuses(previous: dict | None, observations: dict) -> dict:
    merged = ordered_statuses((previous or {}).get("statuses"))
    for part, stores in observations.items():
        if part not in PART_INDEX:
            continue
        merged.setdefault(part, {})
        for store_id, cell in stores.items():
            if store_id in STORE_IDS:
                merged[part][store_id] = cell["status"]
    return ordered_statuses(merged)


def alert_statuses(previous: dict | None) -> dict:
    if not previous:
        return {}
    stored = previous.get("alerted")
    if isinstance(stored, dict):
        return ordered_statuses(stored)
    return ordered_statuses(previous.get("statuses"))


def seconds_until_next(started: float, now: float, interval: int = WATCH_INTERVAL_SECONDS) -> int:
    remaining = int(interval - (now - started))
    return remaining if remaining > 0 else 0


def short_when(moment: datetime, now: datetime) -> str:
    moment = moment.astimezone(MELBOURNE)
    now = now.astimezone(MELBOURNE)
    if moment.date() == now.date():
        return moment.strftime("%-I:%M %p")
    return moment.strftime("%a %-d %b, %-I:%M %p")


def update_last_seen(previous: dict | None, observations: dict, now: datetime) -> dict:
    last_seen: dict = {}
    old = (previous or {}).get("last_seen") if previous else None
    old = old if isinstance(old, dict) else {}
    for part, _, _, _ in PARTS:
        stored = old.get(part) if isinstance(old.get(part), dict) else {}
        kept = {
            store_id: stored[store_id]
            for store_id, _, _ in STORES
            if isinstance(stored.get(store_id), str) and stored.get(store_id)
        }
        if kept:
            last_seen[part] = kept
    stamp = now.isoformat(timespec="seconds")
    for part, stores in observations.items():
        if part not in PART_INDEX:
            continue
        for store_id, cell in stores.items():
            if store_id in STORE_IDS and is_available(cell.get("status")):
                last_seen.setdefault(part, {})[store_id] = stamp
    return last_seen


def availability_changes(old_statuses: dict, observations: dict, now: datetime) -> list[dict]:
    events = []
    stamp = now.isoformat(timespec="seconds")
    display = _display_time(now)
    for part, capacity, color, _buy in PARTS:
        observed = observations.get(part)
        if not observed:
            continue
        previous_part = old_statuses.get(part) or {}
        for store_id, store_name, _store_url in STORES:
            cell = observed.get(store_id)
            if not cell:
                continue
            previous = previous_part.get(store_id)
            now_in = is_available(cell.get("status"))
            if not isinstance(previous, str):
                if not now_in:
                    continue
                change = "in_stock"
            elif is_available(previous) == now_in:
                continue
            else:
                change = "in_stock" if now_in else "out_of_stock"
            events.append(
                {
                    "at": stamp,
                    "at_display": display,
                    "part": part,
                    "name": f"iPhone Duo {capacity} {color}",
                    "capacity": capacity,
                    "color": color,
                    "store_id": store_id,
                    "store": store_name,
                    "change": change,
                }
            )
    return events


def history_events(payload: dict | None) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    events = payload.get("events")
    if not isinstance(events, list):
        return []
    return [event for event in events if isinstance(event, dict)]


def merge_events(*groups: list[dict]) -> list[dict]:
    merged = []
    seen = set()
    for group in groups:
        for event in group:
            key = (event.get("at"), event.get("part"), event.get("store_id"), event.get("change"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(event)
    merged.sort(key=lambda event: str(event.get("at") or ""))
    if len(merged) > HISTORY_LIMIT:
        merged = merged[-HISTORY_LIMIT:]
    return merged


def _display_time(moment: datetime) -> str:
    # %-d is Linux/macOS. The checker runs on GitHub's Ubuntu runners.
    return moment.strftime("%a %-d %b %Y, %-I:%M %p %Z")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def build_result(previous: dict | None, observations: dict, errors: list[dict], now: datetime) -> dict:
    old_statuses = ordered_statuses((previous or {}).get("statuses")) if previous else {}
    arrivals = find_arrivals(alert_statuses(previous), observations) if previous is not None else []
    merged = merge_statuses(previous, observations)
    transitions = availability_changes(old_statuses, observations, now) if previous is not None else []
    last_seen = update_last_seen(previous, observations, now)
    any_success = bool(observations)
    total_failure = not any_success
    blocked = any(error.get("blocked") for error in errors)
    last_success_dt = _parse_time((previous or {}).get("last_success"))
    if any_success:
        last_success_dt = now
    status_updated = (previous or {}).get("status_updated")
    if previous is None or merged != old_statuses:
        status_updated = now.isoformat(timespec="seconds")
    baseline = {
        "status_updated": status_updated,
        "last_success": last_success_dt.isoformat(timespec="seconds") if last_success_dt else None,
        "statuses": merged,
        "alerted": merged,
        "last_seen": last_seen,
    }

    models = []
    in_stock = []
    for part, capacity, color, buy in PARTS:
        observed = observations.get(part)
        cells = {}
        for store_id, store_name, _store_url in STORES:
            seen_at = (last_seen.get(part) or {}).get(store_id)
            seen_dt = _parse_time(seen_at)
            seen_display = short_when(seen_dt, now) if seen_dt else None
            if observed and store_id in observed:
                cell = observed[store_id]
                cells[store_id] = {
                    "status": cell["status"],
                    "text": cell.get("text") or "",
                    "stale": False,
                    "last_seen": seen_at,
                    "last_seen_display": seen_display,
                }
            else:
                previous_status = old_statuses.get(part, {}).get(store_id)
                if previous_status:
                    cells[store_id] = {
                        "status": previous_status,
                        "text": "Last saved reading",
                        "stale": True,
                        "last_seen": seen_at,
                        "last_seen_display": seen_display,
                    }
                else:
                    cells[store_id] = {
                        "status": "unknown",
                        "text": "No reading yet",
                        "stale": True,
                        "last_seen": seen_at,
                        "last_seen_display": seen_display,
                    }
            if is_available(cells[store_id]["status"]):
                in_stock.append(
                    {
                        "part": part,
                        "name": f"iPhone Duo {capacity} {color}",
                        "capacity": capacity,
                        "color": color,
                        "store_id": store_id,
                        "store": store_name,
                        "buy_url": buy,
                        "text": cells[store_id]["text"],
                        "stale": cells[store_id]["stale"],
                    }
                )
        models.append(
            {
                "part": part,
                "name": f"iPhone Duo {capacity} {color}",
                "capacity": capacity,
                "color": color,
                "buy_url": buy,
                "stores": cells,
            }
        )

    problem = _problem_banner(
        errors=errors,
        blocked=blocked,
        total_failure=total_failure,
        any_success=any_success,
        last_success_dt=last_success_dt if total_failure else None,
        have_previous=bool(old_statuses),
    )
    data = {
        "checked_at": now.isoformat(timespec="seconds"),
        "checked_at_display": _display_time(now),
        "last_success": last_success_dt.isoformat(timespec="seconds") if last_success_dt else None,
        "last_success_display": _display_time(last_success_dt) if last_success_dt else None,
        "buy_url": BUY_URL,
        "location": "Melbourne pickup near postcode 3000",
        "problem": problem,
        "any_in_stock": bool(in_stock),
        "in_stock_count": len(in_stock),
        "in_stock": in_stock,
        "stores": [
            {"id": store_id, "name": name, "url": url} for store_id, name, url in STORES
        ],
        "models": models,
        "recent_history": [],
    }
    return {
        "data": data,
        "baseline": baseline,
        "arrivals": arrivals,
        "transitions": transitions,
        "total_failure": total_failure,
    }


def _problem_banner(
    errors: list[dict],
    blocked: bool,
    total_failure: bool,
    any_success: bool,
    last_success_dt: datetime | None,
    have_previous: bool,
) -> dict | None:
    if not errors:
        return None
    lead = errors[0]["message"]
    statuses = {error.get("status") for error in errors}
    if len(statuses) == 1:
        lead = errors[0]["message"]
    elif blocked:
        lead = "Apple blocked part of this stock check. This does not mean the phones are sold out."
    else:
        lead = "Part of this stock check failed. This does not mean the phones are sold out."
    if any_success:
        names = [model_name(error["part"]) for error in errors if error.get("part") in PART_INDEX]
        if names:
            lead = f"{lead} Not updated: {', '.join(names)}."
        return {"kind": "blocked" if blocked else "error", "text": lead, "stale": False}
    if have_previous and last_success_dt is not None:
        when = _display_time(last_success_dt)
        text = f"{lead} The grid below is the last saved reading from {when}."
        return {"kind": "blocked" if blocked else "error", "text": text, "stale": True}
    text = f"{lead} There is no earlier reading to show yet."
    return {"kind": "blocked" if blocked else "error", "text": text, "stale": False}


def hold_alerted(previous: dict | None, baseline: dict, arrivals: list[dict]) -> dict:
    """Remember real stock, but keep unsent alerts pending for the next check."""
    alerted = ordered_statuses(baseline.get("alerted") or baseline.get("statuses"))
    old = alert_statuses(previous)
    for item in arrivals:
        part = item["part"]
        store_id = item["store_id"]
        previous_status = (old.get(part) or {}).get(store_id)
        alerted.setdefault(part, {})
        if previous_status is None:
            alerted[part].pop(store_id, None)
            if not alerted[part]:
                alerted.pop(part, None)
        else:
            alerted[part][store_id] = previous_status
    updated = dict(baseline)
    updated["alerted"] = ordered_statuses(alerted)
    return updated


def alert_fields(topic_raw: str | None, send_result: str) -> dict:
    topic, problem = normalize_topic(topic_raw)
    if problem == "missing":
        return {
            "alerts": "off",
            "alert_note": "Phone alerts are not set up yet.",
        }
    if problem == "invalid":
        return {
            "alerts": "invalid",
            "alert_note": "Phone alerts are not working because the topic secret does not look like an ntfy topic.",
        }
    if send_result == "error":
        return {
            "alerts": "error",
            "alert_note": "A phone alert could not be sent. It will be tried again on the next check.",
        }
    return {"alerts": "on", "alert_note": "Phone alerts are on."}


def fetch_published_last_success(site_url: str) -> datetime | None:
    url = site_url + "data.json?" + urllib.parse.urlencode({"ts": str(time.time())})
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Cache-Control": "no-cache"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    problem = payload.get("problem") or {}
    if problem.get("stale"):
        return _parse_time(payload.get("last_success"))
    return _parse_time(payload.get("checked_at")) or _parse_time(payload.get("last_success"))


def apply_published_time(data: dict, published: datetime | None) -> None:
    if published is None:
        return
    current = _parse_time(data.get("last_success"))
    if current and published <= current:
        return
    data["last_success"] = published.isoformat(timespec="seconds")
    data["last_success_display"] = _display_time(published)
    problem = data.get("problem")
    if problem and problem.get("stale"):
        lead = problem["text"].split(" The grid below is the last saved reading")[0]
        problem["text"] = (
            f"{lead} The grid below is the last saved reading from {data['last_success_display']}."
        )


def write_summary(data: dict, arrivals: list[dict], notify_note: str) -> None:
    lines = ["## iPhone Duo pickup", "", f"**{data['checked_at_display']}**", ""]
    problem = data.get("problem")
    if problem:
        lines.append(problem["text"])
        lines.append("")
    if data["in_stock"]:
        lines.append("In stock:")
        lines.append("")
        for item in data["in_stock"]:
            stale = " (older reading)" if item.get("stale") else ""
            lines.append(f"- {item['name']} at {item['store']}{stale}")
        lines.append("")
    elif not problem:
        lines.append("Nothing in stock at Highpoint, Chadstone, Doncaster, Southland, or Fountain Gate.")
        lines.append("")
    if arrivals:
        lines.append(f"New since the last saved reading: {len(arrivals)}.")
        lines.append("")
    lines.append(notify_note)
    lines.append("")
    lines.append(f"[Stock page]({pages_url()}) · [Buy on Apple]({BUY_URL})")
    lines.append("")
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)


def annotate_problem(data: dict) -> None:
    problem = data.get("problem")
    if not problem or not os.environ.get("GITHUB_ACTIONS"):
        return
    clean = " ".join(problem["text"].split())
    print(f"::warning title=Apple stock check problem::{clean}")


def load_saved_events() -> list[dict]:
    groups = [history_events(load_json(HISTORY_PATH))]
    git_history = os.environ.get("GIT_HISTORY_FILE")
    if git_history:
        groups.append(history_events(load_json(git_history)))
    return merge_events(*groups)


def remember_check_start() -> None:
    Path("/tmp/stock-check-started").write_text(str(time.time()))


def pause_for_next_check() -> None:
    interval = int(os.environ.get("WATCH_INTERVAL_SECONDS", str(WATCH_INTERVAL_SECONDS)))
    path = Path("/tmp/stock-check-started")
    if not path.exists():
        time.sleep(interval)
        return
    remaining = seconds_until_next(float(path.read_text().strip()), time.time(), interval)
    if remaining > 0:
        print(f"Waiting {remaining}s so the next check stays about a minute apart.")
        time.sleep(remaining)
    else:
        print("The last check used the whole minute. Starting the next one.")


def watch() -> None:
    rounds = int(os.environ.get("WATCH_ROUNDS", str(WATCH_ROUNDS)))
    interval = int(os.environ.get("WATCH_INTERVAL_SECONDS", str(WATCH_INTERVAL_SECONDS)))
    for index in range(rounds):
        print(f"Check {index + 1} of {rounds}")
        started = time.time()
        run()
        if index < rounds - 1:
            remaining = seconds_until_next(started, time.time(), interval)
            if remaining:
                print(f"Waiting {remaining}s so the next check stays about a minute apart.")
                time.sleep(remaining)


def run() -> None:
    remember_check_start()
    now = datetime.now(MELBOURNE)
    disk_baseline = load_json(STATE_PATH)
    git_baseline = load_json(os.environ.get("GIT_BASELINE_FILE"))
    previous = choose_baseline(disk_baseline, git_baseline)

    observations: dict = {}
    errors = []
    for index, (part, _capacity, _color, _url) in enumerate(PARTS):
        if index:
            time.sleep(REQUEST_PAUSE_SECONDS)
        result = fetch_part(part)
        if result.get("ok"):
            observations[part] = result["stores"]
            available = [
                STORE_INDEX[store_id][0]
                for store_id, cell in result["stores"].items()
                if is_available(cell.get("status"))
            ]
            if available:
                print(f"{part} {model_name(part)}: in stock at {', '.join(available)}")
            else:
                print(f"{part} {model_name(part)}: not in stock at these stores")
        else:
            errors.append({"part": part, **result})
            code = result.get("status")
            kind = "BLOCKED" if result.get("blocked") else "ERROR"
            print(f"{part} {model_name(part)}: {kind}" + (f" HTTP {code}" if code else ""))

    built = build_result(previous, observations, errors, now)
    if built["total_failure"]:
        apply_published_time(built["data"], fetch_published_last_success(pages_url()))

    topic_raw = os.environ.get("NTFY_TOPIC")
    send_result = "none"
    arrivals = built["arrivals"]
    save_baseline = not (built["total_failure"] and previous is not None)
    if previous is None and built["total_failure"]:
        save_baseline = False
    if arrivals:
        topic, topic_problem = normalize_topic(topic_raw)
        if topic_problem == "missing":
            print("NTFY_TOPIC is not set. The new stock is saved for an alert once phone alerts are turned on.")
            built["baseline"] = hold_alerted(previous, built["baseline"], arrivals)
        elif topic_problem == "invalid":
            print("NTFY_TOPIC is not a usable ntfy topic name. Skipping phone alerts.")
            built["baseline"] = hold_alerted(previous, built["baseline"], arrivals)
        else:
            title, message, click, actions = format_notification(arrivals, pages_url())
            try:
                send_ntfy(topic, title, message, click, actions)
                send_result = "sent"
                print(f"Sent {len(arrivals)} new-stock alert(s).")
            except RuntimeError as error:
                send_result = "error"
                save_baseline = True
                built["baseline"] = hold_alerted(previous, built["baseline"], arrivals)
                print(f"::warning title=Phone alert failed::{error}")
                print(error)

    events = load_saved_events()
    if not built["total_failure"]:
        events = merge_events(events, built["transitions"])
    data = built["data"]
    data["recent_history"] = list(reversed(events[-RECENT_LIMIT:]))
    data.update(alert_fields(topic_raw, send_result))
    write_json(DATA_PATH, data)
    write_json(HISTORY_PATH, {"events": events})

    if save_baseline and built["baseline"].get("last_success"):
        write_json(STATE_PATH, built["baseline"])
    elif previous is not None:
        # Keep the newer of the git/cache copies so a stale cache is not saved again.
        write_json(STATE_PATH, previous)

    notify_note = data["alert_note"]
    if send_result == "sent":
        notify_note = f"Sent a phone alert for {len(arrivals)} new arrival(s)."
    write_summary(data, arrivals, notify_note)
    annotate_problem(data)


def stock_changed() -> bool:
    new = load_json(STATE_PATH) or {}
    try:
        old_raw = subprocess.check_output(
            ["git", "show", f"HEAD:{STATE_PATH.relative_to(ROOT).as_posix()}"],
            text=True,
            stderr=subprocess.DEVNULL,
            cwd=ROOT,
        )
        old = json.loads(old_raw)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        old = {}
    return statuses_differ(old if isinstance(old, dict) else {}, new)


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "--once"
    if command == "--stock-changed":
        print("yes" if stock_changed() else "no")
        return
    if command == "--pause":
        pause_for_next_check()
        return
    if command == "--watch":
        watch()
        return
    if command in {"--once", "check"}:
        run()
        return
    print("Usage: python3 check.py [--once | --watch | --pause | --stock-changed]", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
