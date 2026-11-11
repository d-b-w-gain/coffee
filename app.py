"""Coffee machine (and grinder) control: a tiny web page + API over smart plugs.

Every "on" comes with an auto-off timer (default 30 min, max MAX_ON_MINUTES),
and if the plug is found on with no timer running (pod restart, someone pressed
the button) it gets switched off after MAX_ON_MINUTES as well.

The weekly schedule (/data/schedule.json, local time in TZ) is the one place the
machine is switched on and off on a timetable: at a window's start the machine is
switched on until the window's end. Inside a window, timers only ever get longer
(a warm-up or "on" never cuts the window short); pressing Off still wins until
the next window. The Tapo app's own schedules should stay disabled.

Settings (/data/settings.json) pick the mode (machine only, or machine + grinder)
and which plug does what (Tapo/Kasa, Shelly or Tasmota; see plugs.py). The grinder
is run for timed single-dose grinds that never break its duty cycle (Mazzer Mini:
50 s on / 50 s off). A plug that measures power feeds power.py, which measures
warm-ups and keeps daily numbers for spotting wear.
"""
import asyncio
import base64
import json
import logging
import os
import random
import re
import secrets
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from urllib.parse import quote

import plugs
from power import Analyzer

HOST = os.environ.get("TAPO_HOST", "192.168.88.10")  # the machine's plug until settings say otherwise
DEFAULT_MIN = int(os.environ.get("DEFAULT_ON_MINUTES", "30"))
MAX_MIN = int(os.environ.get("MAX_ON_MINUTES", "60"))
POLL_S = 30

TZ = ZoneInfo(os.environ.get("TZ") or "Australia/Sydney")
SCHEDULE_FILE = os.environ.get("SCHEDULE_FILE", "/data/schedule.json")
MAX_WINDOW_MIN = int(os.environ.get("MAX_SCHEDULE_MINUTES", "720"))
HISTORY_FILE = os.environ.get("HISTORY_FILE", "/data/power-log.json")
HISTORY_KEEP_S = 7 * 86400  # on/off changes only, so a week is a few hundred bytes
START_GRACE_S = 15 * 60  # if the plug can't be reached at the start, keep trying this long
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# Seeded from the Tapo app rules that were in use (on 05:45, off 11:00, every day).
DEFAULT_SCHEDULE = {"enabled": True, "rules": [{"days": list(range(7)), "on": "05:45", "off": "11:00"}]}

SIGNAL_API = os.environ.get("SIGNAL_API_URL", "http://signal-cli.cnc-controller.svc:8080")
SIGNAL_ACCOUNT = os.environ.get("SIGNAL_ACCOUNT", "")
SIGNAL_RECIPIENT = os.environ.get("SIGNAL_RECIPIENT", "")
BARISTA_NAME = os.environ.get("BARISTA_NAME", "Dan")
BARISTA_NUMBER = os.environ.get("BARISTA_NUMBER") or SIGNAL_RECIPIENT
REQUEST_GAP_S = 60  # one coffee request a minute, so a double tap isn't two messages

# Spruik phones the barista (Signal voice, Kokoro speech) if an order isn't started:
# not marked made, and the machine not switched on since the order came in.
CALL_URL = os.environ.get("SPRUIK_ANNOUNCE_URL", "http://signal-call-bridge.cnc-controller.svc:9091/announce")
CALL_VOICE = os.environ.get("SPRUIK_ANNOUNCE_VOICE", "af_heart")
NUDGE_AFTER_MIN = [int(m) for m in os.environ.get("NUDGE_AFTER_MINUTES", "5,15").split(",") if m.strip()]
calling: set[str] = set()  # order ids with a call in progress

MENU = {
    "drink": ["Flat white", "Latte", "Cappuccino", "Piccolo", "Macchiato", "Long black", "Espresso"],
    "milk": ["Full cream", "Oat", "No milk"],
    "beans": ["Regular", "Decaf", "Half & half"],
    "shots": ["Single", "Double"],
    "sugar": ["No sugar", "1 sugar", "2 sugars"],
    "size": ["Small", "Regular", "Large"],
    "cup": ["Dan's choice", "Hayley's cup", "Book mug", "White dimple cup", "Mustard mug", "Green drip mug", "Speckled cup",
            "Art mug", "Glass keep cup", "Red travel mug", "Blue tumbler", "Black tumbler"],
}
BLACK = ("Long black", "Espresso")

# --- Stock -------------------------------------------------------------------
BASKET_G = int(os.environ.get("BASKET_GRAMS", "18"))  # every coffee is one full basket
# Milk steamed per drink, including what's left in the jug.
MILK_ML = {"Small": 180, "Regular": 240, "Large": 320}
MILK_FIXED_ML = {"Piccolo": 100, "Macchiato": 40}
STOCK_FILE = os.environ.get("STOCK_FILE", "/data/stock.json")
STOCK_ITEMS = {
    "beans": {"name": "Urban Coffee Culture Strong", "unit": "g", "pack": 1000, "pack_name": "bag", "low": 4 * BASKET_G},
    "decaf": {"name": "Swiss Water Decaf", "unit": "g", "pack": 250, "pack_name": "bag", "low": 3 * BASKET_G},
    "milk": {"name": "Full cream milk", "unit": "ml", "pack": 2000, "pack_name": "2 L bottle", "low": 500},
    "oat": {"name": "Oat milk (made up)", "unit": "ml", "pack": 5000, "pack_name": "5 L pack", "low": 500},
}
stock_levels: dict[str, float | None] = {k: None for k in STOCK_ITEMS}


def load_stock():
    try:
        with open(STOCK_FILE) as f:
            saved = json.load(f)
        for k in STOCK_ITEMS:
            if isinstance(saved.get(k), (int, float)):
                stock_levels[k] = saved[k]
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("couldn't load stock: %s", e)


def save_stock():
    try:
        tmp = STOCK_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(stock_levels, f)
        os.replace(tmp, STOCK_FILE)
    except Exception as e:
        log.warning("couldn't save stock: %s", e)


def usage(o):
    """What one coffee takes out of stock."""
    use = {}
    beans = o.get("beans", "Regular")
    if beans == "Decaf":
        use["decaf"] = BASKET_G
    elif beans == "Half & half":
        use["beans"] = use["decaf"] = BASKET_G / 2
    else:
        use["beans"] = BASKET_G
    if o["drink"] not in BLACK and o.get("milk") in ("Full cream", "Oat"):
        ml = MILK_FIXED_ML.get(o["drink"], MILK_ML.get(o.get("size"), 240))
        use["milk" if o["milk"] == "Full cream" else "oat"] = ml
    return use


def per_serve(key):
    return BASKET_G if STOCK_ITEMS[key]["unit"] == "g" else MILK_ML["Regular"]


def stock_view():
    out = []
    for k, item in STOCK_ITEMS.items():
        lvl = stock_levels[k]
        out.append({"id": k, **item, "level": None if lvl is None else round(lvl),
                    "serves": None if lvl is None else int(max(lvl, 0) // per_serve(k)),
                    "is_low": lvl is not None and lvl < item["low"]})
    return out


def use_stock(o):
    """Take one coffee out of stock; returns the items that just ran low."""
    newly_low = []
    for k, amount in usage(o).items():
        lvl = stock_levels[k]
        if lvl is None:
            continue  # not being tracked until someone sets a level
        after = max(lvl - amount, 0)
        if lvl >= STOCK_ITEMS[k]["low"] > after:
            newly_low.append(k)
        stock_levels[k] = after
    save_stock()
    return newly_low


async def warn_low(keys):
    if not keys or not (SIGNAL_ACCOUNT and SIGNAL_RECIPIENT):
        return
    parts = []
    for k in keys:
        n = int(stock_levels[k] // per_serve(k))
        parts.append(f"{STOCK_ITEMS[k]['name']} (about {n} coffee{'s' if n != 1 else ''} left)")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            await signal_send(s, SIGNAL_RECIPIENT, "🛒 Running low: " + ", ".join(parts) + f"\nRestock at {BASE_URL}")
    except Exception as e:
        log.warning("low-stock message failed: %s", e)


last_request = {"at": 0.0}

GIPHY_KEY = os.environ.get("GIPHY_API_KEY", "")
GIF_MAX_BYTES = 4_000_000
BASE_URL = os.environ.get("BASE_URL", "http://coffee.lan").rstrip("/")
ORDERS_FILE = os.environ.get("ORDERS_FILE", "/data/active-orders.json")
ORDER_TTL_S = 6 * 3600  # an order not made within 6 h is forgotten
RATE_WINDOW_S = 24 * 3600  # a made/delivered order stays this long so both sides can rate
STATUSES = ("waiting", "made", "delivered")

# Only orders still in progress (or waiting to be rated) are kept, keyed by an
# unguessable id. History lives in each person's own browser, never here.
orders: dict[str, dict] = {}


def finished(o):
    """Delivered and rated both ways: nothing left to do."""
    return o["status"] == "delivered" and o.get("rating") and o.get("review")


def prune():
    now = time.time()
    for oid in [k for k, o in orders.items()
                if finished(o)
                or o["created"] < now - RATE_WINDOW_S
                or (o["status"] == "waiting" and o["created"] < now - ORDER_TTL_S)]:
        del orders[oid]


# --- Ratings ("Coffee Detective" case file) ---------------------------------------
# The drinker rates the coffee (1-5 beans, tags, optional comment); the barista
# rates the customer with tags only. Only anonymous totals are kept.
RATE_TAGS = ["Perfect crema", "Latte art 🎨", "Worth the wait", "Just right", "Smooth",
             "Too hot 🔥", "Too weak", "Needs another shot", "Wrong cup?!"]
REVIEW_TAGS = ["Said thanks 🙏", "Brought the mug back ☕", "Tipped in biscuits 🍪",
               "Complicated order 😅", "Perfect timing ⏰", "Patient customer 🧘",
               "Good chat 💬", "Customer of the day 🏆"]
CASEFILE = os.environ.get("CASEFILE", "/data/casefile.json")
casefile = {"rated": 0, "beans": [0, 0, 0, 0, 0], "by_drink": {}, "by_cup": {}, "tags": {},
            "reviews": 0, "review_tags": {}, "streak": 0, "best_streak": 0}


def load_casefile():
    try:
        with open(CASEFILE) as f:
            casefile.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("couldn't load case file: %s", e)


def save_casefile():
    try:
        tmp = CASEFILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(casefile, f)
        os.replace(tmp, CASEFILE)
    except Exception as e:
        log.warning("couldn't save case file: %s", e)


def tally(table, key, beans=None):
    if beans is None:
        table[key] = table.get(key, 0) + 1
    else:
        n, total = table.get(key, [0, 0])
        table[key] = [n + 1, total + beans]


def casefile_view():
    c = casefile
    rated = c["rated"]
    avg = round(sum((i + 1) * n for i, n in enumerate(c["beans"])) / rated, 1) if rated else None

    def best(table):
        if not table:
            return None
        name, (n, total) = max(table.items(), key=lambda kv: (kv[1][1] / kv[1][0], kv[1][0]))
        return {"name": name, "avg": round(total / n, 1), "count": n}

    def top(table):
        return max(table.items(), key=lambda kv: kv[1]) if table else None

    return {"rated": rated, "average": avg, "beans": c["beans"],
            "best_drink": best(c["by_drink"]), "best_cup": best(c["by_cup"]),
            "top_tag": top(c["tags"]), "reviews": c["reviews"], "top_review_tag": top(c["review_tags"]),
            "streak": c["streak"], "best_streak": c["best_streak"],
            "rate_tags": RATE_TAGS, "review_tags": REVIEW_TAGS}


def save_orders():
    prune()
    try:
        tmp = ORDERS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(orders, f)
        os.replace(tmp, ORDERS_FILE)
    except Exception as e:
        log.warning("couldn't save active orders: %s", e)


def load_orders():
    try:
        with open(ORDERS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            orders.update(data)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("couldn't load active orders: %s", e)
    old = "/data/orders.json"  # the shared history list from the first version
    if os.path.exists(old):
        os.remove(old)
    save_orders()


def describe(o):
    milk_choice = o.get("milk", "No milk")
    if milk_choice == "No milk":
        milk = "" if o["drink"] in BLACK else ", no milk"
    else:
        milk = f", {milk_choice.lower()} milk"
    beans = {"Decaf": ", decaf", "Half & half": ", half & half (½ decaf)"}.get(o.get("beans", "Regular"), "")
    cup = o.get("cup") or "Dan's choice"
    cup = "" if cup == "Dan's choice" else (f", in {cup}" if "'s " in cup else f", in the {cup.lower()}")
    return f"{o['size']} {o['drink'].lower()}{milk}, {o['shots'].lower()} shot{beans}, {o['sugar'].lower()}{cup}"


def public(o):
    return {"status": o["status"], "who": o["who"], "drink": o["drink"], "summary": describe(o),
            "note": o["note"], "created": int(o["created"]), "updated": int(o["updated"]),
            "rating": o.get("rating"), "review": o.get("review"), "has_phone": bool(o.get("phone"))}


async def find_gif(session, drink, query=None):
    """A random G-rated Giphy GIF for the drink (or `query`), as a data: URI for signal-cli, or None."""
    if not GIPHY_KEY:
        return None
    query = query or f"{drink.lower()} coffee"
    for q in (query, "coffee"):
        try:
            async with session.get("https://api.giphy.com/v1/gifs/search", params={
                    "api_key": GIPHY_KEY, "q": q, "limit": "25", "rating": "g"}) as r:
                results = (await r.json()).get("data", []) if r.status == 200 else []
            random.shuffle(results)
            for g in results[:5]:
                img = g.get("images", {})
                url = (img.get("downsized_medium") or img.get("downsized") or img.get("original") or {}).get("url")
                if not url:
                    continue
                async with session.get(url) as r:
                    data = await r.read() if r.status == 200 else b""
                if data[:3] == b"GIF" and len(data) <= GIF_MAX_BYTES:
                    return "data:image/gif;filename=coffee.gif;base64," + base64.b64encode(data).decode()
        except Exception as e:
            log.warning("giphy lookup failed for %r: %s", q, e)
    return None

log = logging.getLogger("coffee")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --- Schedule ------------------------------------------------------------------
schedule = {"enabled": True, "rules": [], "started": None}  # started = key of the last window switched on


def hm(text):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(text).strip())
    if not m:
        raise ValueError(f"time must be HH:MM, got {text!r}")
    return int(m[1]) * 60 + int(m[2])


def clean_rules(rules):
    if not isinstance(rules, list) or len(rules) > 8:
        raise ValueError("up to 8 schedule rows")
    out = []
    for r in rules:
        days = sorted({int(d) for d in r.get("days", [])})
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("pick at least one day")
        on, off = hm(r.get("on")), hm(r.get("off"))
        if off <= on:
            raise ValueError("the off time must be later the same day than the on time")
        if off - on > MAX_WINDOW_MIN:
            raise ValueError(f"a window can be at most {MAX_WINDOW_MIN // 60} hours")
        out.append({"days": days, "on": f"{on // 60:02d}:{on % 60:02d}", "off": f"{off // 60:02d}:{off % 60:02d}"})
    return out


def load_schedule():
    try:
        with open(SCHEDULE_FILE) as f:
            data = json.load(f)
        schedule.update(enabled=bool(data.get("enabled", True)), rules=clean_rules(data.get("rules", [])),
                        started=data.get("started"))
    except FileNotFoundError:
        schedule.update(enabled=DEFAULT_SCHEDULE["enabled"], rules=clean_rules(DEFAULT_SCHEDULE["rules"]))
        win = current_window()
        schedule["started"] = win[2] if win else None  # don't switch on just because we started up
        save_schedule()
    except Exception as e:
        log.warning("schedule unreadable, using none: %s", e)
        schedule.update(enabled=False, rules=[])


def save_schedule():
    os.makedirs(os.path.dirname(SCHEDULE_FILE), exist_ok=True)
    tmp = SCHEDULE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(schedule, f, indent=1)
    os.replace(tmp, SCHEDULE_FILE)


def windows_on(day):
    """(start_ts, end_ts, key) for each scheduled window on a local date."""
    if not schedule["enabled"]:
        return []
    out = []
    for i, r in enumerate(schedule["rules"]):
        if day.weekday() not in r["days"]:
            continue
        midnight = datetime(day.year, day.month, day.day, tzinfo=TZ)
        on, off = hm(r["on"]), hm(r["off"])
        out.append(((midnight + timedelta(minutes=on)).timestamp(),
                    (midnight + timedelta(minutes=off)).timestamp(), f"{day.isoformat()}#{i}"))
    return sorted(out)


def current_window(now=None):
    now = now or time.time()
    hits = [w for w in windows_on(datetime.fromtimestamp(now, TZ).date()) if w[0] <= now < w[1]]
    return max(hits, key=lambda w: w[1]) if hits else None


def next_window(now=None):
    now = now or time.time()
    today = datetime.fromtimestamp(now, TZ).date()
    for d in range(8):
        for w in windows_on(today + timedelta(days=d)):
            if w[0] > now:
                return w
    return None


def when(ts):
    t = datetime.fromtimestamp(ts, TZ)
    days = (t.date() - datetime.fromtimestamp(time.time(), TZ).date()).days
    day = "today" if days == 0 else "tomorrow" if days == 1 else DAY_NAMES[t.weekday()]
    return f"{day} {t:%H:%M}"


def off_time(minutes):
    """Auto-off time for an 'on': never earlier than the scheduled window's end, or a timer already running."""
    now = time.time()
    until = now + minutes * 60
    win = current_window(now)
    if win:
        until = max(until, win[1])
    if state["is_on"] and state["off_at"]:
        until = max(until, state["off_at"])
    return until


# --- Power history -------------------------------------------------------------
# Only changes are stored ({"t", "on": true/false/null, "why"}); the watchdog already
# polls the plug every POLL_S for its timers, so this adds no extra polling.
history: list[dict] = []


def load_history():
    try:
        with open(HISTORY_FILE) as f:
            history[:] = [e for e in json.load(f) if "t" in e and "on" in e]
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("power log unreadable, starting fresh: %s", e)


def record(on, why):
    if history and history[-1]["on"] == on:
        return
    history.append({"t": round(time.time()), "on": on, "why": why})
    cutoff = time.time() - HISTORY_KEEP_S
    while len(history) > 1 and history[1]["t"] < cutoff:
        history.pop(0)  # keep one event from before the cutoff, so the state at the cutoff is known
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(history, f)
        os.replace(tmp, HISTORY_FILE)
    except OSError as e:
        log.warning("couldn't save power log: %s", e)


state = {"off_at": None, "error": None, "is_on": None, "on_since": None}
WARMUP_MIN = int(os.environ.get("WARMUP_MINUTES", "20"))  # Minibar, 2 L boiler (estimate until measured)
lock = asyncio.Lock()

# --- Settings ----------------------------------------------------------------------
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "/data/settings.json")
MODES = {"machine": "Machine only", "machine+grinder": "Machine and grinder"}
GRINDER_PLATE_S = 50  # Mazzer Mini MAN rating plate: 50 s on / 50 s off
SCAN_SUBNET = os.environ.get("SCAN_SUBNET", "")
settings = {
    "mode": "machine",
    "plugs": {"machine": {"driver": "tapo", "host": HOST}, "grinder": {"driver": "tapo", "host": ""}},
    "machine": {"warmup_min": WARMUP_MIN, "heater_w": 600, "pump_w": 60, "sample_s": 2},
    "grinder": {"dose_g": 18, "grind_s": {b: None for b in ("Regular", "Decaf", "Half & half")}, "extra_s": 3.0,
                "max_run_s": GRINDER_PLATE_S, "on_match": "notify", "call_on_match": False, "backstop": True},
}


def num(value, lo, hi, what, digits=1):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number")
    if not lo <= v <= hi:
        raise ValueError(f"{what} must be between {lo:g} and {hi:g}")
    return round(v, digits)


def merge_settings(body, base):
    """Validated copy of base with body's changes applied; raises ValueError."""
    out = json.loads(json.dumps(base))
    if "mode" in body:
        if body["mode"] not in MODES:
            raise ValueError("unknown mode")
        out["mode"] = body["mode"]
    for role, p in (body.get("plugs") or {}).items():
        if role not in out["plugs"] or not isinstance(p, dict):
            raise ValueError("plugs are 'machine' and 'grinder'")
        out["plugs"][role] = clean_plug(p, out["plugs"][role])
    m = body.get("machine") or {}
    if "warmup_min" in m:
        out["machine"]["warmup_min"] = int(num(m["warmup_min"], 1, 90, "warm-up minutes", 0))
    if "heater_w" in m:
        out["machine"]["heater_w"] = num(m["heater_w"], 100, 3000, "heater threshold", 0)
    if "pump_w" in m:
        out["machine"]["pump_w"] = num(m["pump_w"], 10, 1000, "pump threshold", 0)
    if "sample_s" in m:
        out["machine"]["sample_s"] = num(m["sample_s"], 1, 30, "sample interval", 0)
    g = body.get("grinder") or {}
    if "dose_g" in g:
        out["grinder"]["dose_g"] = num(g["dose_g"], 5, 30, "dose")
    if "extra_s" in g:
        out["grinder"]["extra_s"] = num(g["extra_s"], 0, 15, "extra seconds")
    if "max_run_s" in g:
        out["grinder"]["max_run_s"] = num(g["max_run_s"], 5, GRINDER_PLATE_S, "longest run", 0)
    for beans, s in (g.get("grind_s") or {}).items():
        if beans not in out["grinder"]["grind_s"]:
            raise ValueError(f"unknown beans {beans!r}")
        out["grinder"]["grind_s"][beans] = None if s in (None, "") else num(s, 1, GRINDER_PLATE_S, "grind time")
    if "on_match" in g:
        if g["on_match"] not in ("notify", "grind"):
            raise ValueError("on_match is notify or grind")
        out["grinder"]["on_match"] = g["on_match"]
    for flag in ("call_on_match", "backstop"):
        if flag in g:
            out["grinder"][flag] = bool(g[flag])
    return out


def clean_plug(p, old):
    """One plug's settings, validated. A blank local key keeps the saved one (the page never sees it)."""
    driver, host = p.get("driver", old.get("driver")), str(p.get("host", old.get("host", ""))).strip()
    if driver not in plugs.DRIVERS:
        raise ValueError(f"unknown plug type {driver!r}")
    if host and not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
        raise ValueError("the plug address should be an IP address or host name")
    out = {"driver": driver, "host": host}
    if driver == "tuya":
        dev_id = str(p.get("device_id", old.get("device_id", "")) or "").strip()
        if dev_id and not re.fullmatch(r"[A-Za-z0-9]{10,32}", dev_id):
            raise ValueError("the Tuya device ID is 10-32 letters and digits")
        key = str(p.get("local_key") or "")
        if not key and old.get("driver") == "tuya" and old.get("device_id") == dev_id:
            key = old.get("local_key", "")
        if key and not re.fullmatch(r"[\x21-\x7e]{8,64}", key):
            raise ValueError("the Tuya local key looks wrong (it's usually 16 characters)")
        version = str(p.get("version", old.get("version", "auto")) or "auto")
        if version not in ("auto", "3.1", "3.3", "3.4", "3.5"):
            raise ValueError("Tuya protocol version is auto, 3.1, 3.3, 3.4 or 3.5")
        dps = p.get("dps", old.get("dps")) or {}
        if not isinstance(dps, dict) or any(k not in plugs.TUYA_DPS or not str(v).isdigit() for k, v in dps.items()):
            raise ValueError("Tuya data points: " + ", ".join(plugs.TUYA_DPS) + " (numbers)")
        out.update(device_id=dev_id, local_key=key, version=version, dps={k: int(v) for k, v in dps.items()})
    return out


def public_plug(cfg):
    """A plug's settings for the page: the local key is never sent back."""
    out = {k: v for k, v in cfg.items() if k != "local_key"}
    if cfg.get("driver") == "tuya":
        out["local_key_set"] = bool(cfg.get("local_key"))
    return out


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            settings.update(merge_settings(json.load(f), settings))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("settings unreadable, using defaults: %s", e)


def save_settings():
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(settings, f, indent=1)
        os.replace(tmp, SETTINGS_FILE)
    except OSError as e:
        log.warning("couldn't save settings: %s", e)


# --- Plugs -------------------------------------------------------------------------
live_plugs: dict[str, plugs.Plug | None] = {"machine": None, "grinder": None}


def plug(role) -> plugs.Plug | None:
    want = settings["plugs"][role]
    p = live_plugs[role]
    if p is None or p.key != plugs.config_key(want):
        if p is not None:
            asyncio.create_task(p.close())
        p = live_plugs[role] = plugs.make(want)
    return p


def machine_plug():
    p = plug("machine")
    if p is None:
        raise RuntimeError("No plug set for the machine (Settings)")
    return p


async def refresh():
    try:
        p = await machine_plug().update(max_age=3)
        state["is_on"] = p.is_on
        state["error"] = None
        if lock.locked():
            pass  # a switch() is mid-flight and records its own reason
        elif not history or history[-1]["on"] is None and history[-1]["why"] == "plug unreachable":
            record(p.is_on, "plug reachable again" if history else "first reading")
        else:
            record(p.is_on, "plug button or plug app")  # no-op unless it changed behind our back
        if p.on_since is not None:
            state["on_since"] = p.on_since
        elif not p.is_on:
            state["on_since"] = None
        elif state["on_since"] is None:
            state["on_since"] = time.time()
    except Exception as e:  # the plug drops its session and reconnects next time
        state["is_on"], state["error"] = None, str(e) or type(e).__name__
        log.warning("plug unreachable: %s", state["error"])
        record(None, "plug unreachable")


async def switch(on: bool, minutes: int | None = None, why: str = "coffee.lan"):
    async with lock:
        p = machine_plug()
        was_on = state["is_on"]
        await p.turn(on)
        state["is_on"], state["error"] = p.is_on, None
        state["off_at"] = off_time(minutes) if on else None
        record(p.is_on, why)
        wake_sampler.set()
        if not on:
            state["on_since"] = None
        elif not was_on:
            state["on_since"] = p.on_since or time.time()
        log.info("turned %s%s", "on" if on else "off",
                 f" until {datetime.fromtimestamp(state['off_at'], TZ):%H:%M}" if on else "")


# --- Power samples (plugs that measure watts) --------------------------------------
MAINT_FILE = os.environ.get("MAINTENANCE_FILE", "/data/maintenance.json")
analyzer: Analyzer | None = None  # created at start-up, once settings are loaded
wake_sampler = asyncio.Event()  # set on switch-on / new settings, so sampling starts at once


async def sampler():
    """Read the machine's watts every sample_s while it's on (every POLL_S while off)."""
    while True:
        delay = POLL_S
        try:
            p = plug("machine")
            if p is not None and p.metered is not False:
                await p.update(max_age=0.5)
                if p.metered:
                    analyzer.add(time.time(), p.watts, p.volts, bool(p.is_on))
                    if p.is_on:
                        delay = settings["machine"]["sample_s"]
        except Exception:
            pass  # refresh() reports the plug being unreachable
        wake_sampler.clear()
        try:
            await asyncio.wait_for(wake_sampler.wait(), delay)
        except asyncio.TimeoutError:
            pass


def warm_seconds_left():
    if not (state["is_on"] and state["on_since"]):
        return None
    p = live_plugs["machine"]
    measured = analyzer.warm() if analyzer and p is not None and p.metered else None
    if measured and measured["ready"]:
        return 0
    est = (analyzer.typical_warmup_s() if analyzer else None) or settings["machine"]["warmup_min"] * 60
    left = max(0, int(est - (time.time() - state["on_since"])))
    if measured and left < 30:
        left = 30  # the plug says the heater hasn't cut out yet: almost, not quite
    return left


def warmup_minutes():
    typical = analyzer.typical_warmup_s() if analyzer else None
    return round(typical / 60) if typical else settings["machine"]["warmup_min"]


# --- Grinder -----------------------------------------------------------------------
# Single dosing: load one dose in the hopper with the cup under the chute and tell
# coffee.lan (which switches the grinder's plug off, so its own switch can be left
# on), then each grind is a timed run: the measured grind time for those beans plus
# a little extra, never longer than max_run_s, followed by an equal rest.
GRINDER_FILE = os.environ.get("GRINDER_FILE", "/data/grinder.json")
grinder = {"dose": None, "rest_until": 0.0, "totals": {"runs": 0, "motor_s": 0.0, "grams": 0.0},
           "run": None, "on": None, "error": None, "backstop": None, "found_on_at": None, "last": None}
grind_task: dict[str, asyncio.Task | None] = {"t": None}


def load_grinder():
    try:
        with open(GRINDER_FILE) as f:
            data = json.load(f)
        for k in ("dose", "rest_until", "totals"):
            if k in data:
                grinder[k] = data[k]
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("grinder state unreadable: %s", e)


def save_grinder():
    try:
        os.makedirs(os.path.dirname(GRINDER_FILE), exist_ok=True)
        tmp = GRINDER_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: grinder[k] for k in ("dose", "rest_until", "totals")}, f)
        os.replace(tmp, GRINDER_FILE)
    except OSError as e:
        log.warning("couldn't save grinder state: %s", e)


def grinder_enabled():
    return settings["mode"] == "machine+grinder" and bool(settings["plugs"]["grinder"]["host"])


def grind_seconds(beans):
    """How long to run for one dose of these beans, or None if it hasn't been timed yet."""
    g = settings["grinder"]
    base = g["grind_s"].get(beans) or g["grind_s"].get("Regular")
    if not base:
        return None
    return round(min(base + g["extra_s"], g["max_run_s"]), 1)


def grinder_view():
    now = time.time()
    run = grinder["run"]
    dose = grinder["dose"]
    return {"enabled": grinder_enabled(), "on": grinder["on"], "error": grinder["error"],
            "dose": dose, "grind_s": grind_seconds(dose["beans"]) if dose else None,
            "run": None if not run else {"why": run["why"], "seconds": round(now - run["start"], 1),
                                         "of": round(run["until"] - run["start"], 1), "beans": run.get("beans")},
            "rest_s": max(0, round(grinder["rest_until"] - now)), "totals": grinder["totals"],
            "backstop": grinder["backstop"], "max_run_s": settings["grinder"]["max_run_s"],
            "timed": {b: s for b, s in settings["grinder"]["grind_s"].items()}}


class GrinderBusy(Exception):
    pass


async def grind(seconds, why, beans=None):
    """Start a timed run. Raises GrinderBusy (running, resting) or the plug's error."""
    p = plug("grinder")
    if p is None or not grinder_enabled():
        raise GrinderBusy("The grinder isn't set up (Settings)")
    if grinder["run"]:
        raise GrinderBusy("Already grinding")
    rest = grinder["rest_until"] - time.time()
    if rest > 0:
        raise GrinderBusy(f"The grinder is resting for {int(rest) + 1} s more (50 s on / 50 s off)")
    seconds = min(float(seconds), settings["grinder"]["max_run_s"])
    grinder["run"] = {"start": time.time(), "until": time.time() + seconds, "why": why, "beans": beans, "watts": []}
    try:
        await p.turn(True)
    except Exception as e:
        grinder["run"] = None
        grinder["error"] = str(e) or type(e).__name__
        raise
    run = grinder["run"]
    run["start"], run["until"] = time.time(), time.time() + seconds  # time the motor, not the network
    grinder["on"], grinder["error"] = True, None
    grind_task["t"] = asyncio.create_task(_grind_timer(run))
    log.info("grinder on for %.1f s (%s)", seconds, why)


async def _grind_timer(run):
    try:
        while time.time() < run["until"]:
            await asyncio.sleep(min(1.0, max(0.05, run["until"] - time.time())))
            p = live_plugs["grinder"]
            if p is not None and p.metered and time.time() < run["until"] - 0.5:
                try:
                    await p.update()
                    if p.watts:
                        run["watts"].append(p.watts)
                except Exception:
                    pass
    except asyncio.CancelledError:
        pass
    finally:
        await grinder_off("timer" if time.time() >= run["until"] else "stopped")


async def grinder_off(how):
    """Switch the grinder off (retrying) and close out the run, if any."""
    p = plug("grinder")
    for attempt in range(6):
        try:
            if p is not None:
                await p.turn(False)
            grinder["on"], grinder["error"] = False if p else None, None
            break
        except Exception as e:
            grinder["error"] = f"Couldn't switch the grinder off: {e}"
            log.warning("grinder off failed (try %d): %s", attempt + 1, e)
            await asyncio.sleep(1)
    run, grinder["run"] = grinder["run"], None
    grind_task["t"] = None
    if not run:
        return None
    ran = round(time.time() - run["start"], 1)
    grinder["rest_until"] = time.time() + ran  # rest as long as it ran (plate: 50 s on / 50 s off)
    t = grinder["totals"]
    t["runs"] += 1
    t["motor_s"] = round(t["motor_s"] + ran, 1)
    result = {"ran": ran, "why": run["why"], "how": how}
    dose = grinder["dose"]
    if run["why"] == "grind" and dose and dose["status"] == "loaded":
        dose["status"], dose["ground_at"] = "ground", time.time()
        t["grams"] = round(t["grams"] + dose["grams"], 1)
    if run["why"] == "teach":
        if how == "stopped" and run.get("beans"):
            settings["grinder"]["grind_s"][run["beans"]] = ran
            save_settings()
            result["timed"] = ran
            if dose and dose["status"] == "loaded":
                dose["status"], dose["ground_at"] = "ground", time.time()
                t["grams"] = round(t["grams"] + dose["grams"], 1)
        else:
            result["error"] = f"Hit the {run['until'] - run['start']:.0f} s limit, so that wasn't saved as a grind time"
    mean_w = sum(run["watts"]) / len(run["watts"]) if run["watts"] else None
    if analyzer:
        analyzer.grinder_run(time.time(), ran, dose["grams"] if run["why"] != "teach" and dose else 0, mean_w)
    grinder["last"] = {**result, "at": time.time()}
    save_grinder()
    log.info("grinder off after %.1f s (%s, %s)", ran, run["why"], how)
    return result


async def set_backstop():
    """Have the grinder's plug switch itself off shortly after the longest run, if it can."""
    p = plug("grinder")
    if p is None or not grinder_enabled():
        grinder["backstop"] = None
        return
    try:
        if settings["grinder"]["backstop"]:
            grinder["backstop"] = await p.set_backstop(int(settings["grinder"]["max_run_s"]) + 5)
        else:
            grinder["backstop"] = await p.set_backstop(None)
    except Exception as e:
        grinder["backstop"] = f"not set: {e}"
    log.info("grinder backstop: %s", grinder["backstop"])


async def grinder_watch():
    """Every few seconds: is the grinder on when it shouldn't be? Cut it at max_run_s."""
    backstop_for = None
    while True:
        await asyncio.sleep(3)
        if not grinder_enabled():
            grinder["on"] = grinder["found_on_at"] = None
            continue
        p = plug("grinder")
        try:
            want = (p.key, settings["grinder"]["backstop"], settings["grinder"]["max_run_s"])
            if backstop_for != want:
                await set_backstop()
                backstop_for = want
            if grinder["run"]:
                continue  # the run's own timer handles it
            await p.update(max_age=1)
            grinder["on"], grinder["error"] = p.is_on, None
            if p.is_on:
                grinder["found_on_at"] = grinder["found_on_at"] or time.time()
                if time.time() - grinder["found_on_at"] >= settings["grinder"]["max_run_s"]:
                    log.warning("grinder found on outside a run; switching it off (duty cycle)")
                    grinder["run"] = {"start": grinder["found_on_at"], "until": time.time(), "why": "safety", "watts": []}
                    await grinder_off("safety")
            else:
                grinder["found_on_at"] = None
        except Exception as e:
            grinder["on"], grinder["error"] = None, str(e) or type(e).__name__
            backstop_for = None


async def watchdog(app):
    while True:
        await refresh()
        now = time.time()
        win = current_window(now)
        if win and schedule["started"] != win[2] and now - win[0] < START_GRACE_S and state["is_on"] is not None:
            try:  # a scheduled window just began
                if state["is_on"]:
                    state["off_at"] = off_time(0)
                else:
                    await switch(True, 0, "schedule")
                schedule["started"] = win[2]
                save_schedule()
                log.info("schedule: on until %s", datetime.fromtimestamp(win[1], TZ).strftime("%H:%M"))
            except Exception as e:
                log.warning("scheduled on failed: %s", e)
        if state["is_on"] and state["off_at"] is None:
            state["off_at"] = win[1] if win else now + MAX_MIN * 60  # found on without a timer
            log.info("plug was on with no timer; auto-off at %s",
                     datetime.fromtimestamp(state["off_at"], TZ).strftime("%H:%M"))
        elif state["is_on"] and win and state["off_at"] < win[1]:
            state["off_at"] = win[1]  # on during a window: stay on until it ends
        elif state["is_on"] is False:
            state["off_at"] = None
        if state["is_on"] and state["off_at"] and now >= state["off_at"]:
            try:
                ends = {w[1] for w in windows_on(datetime.fromtimestamp(now, TZ).date())}
                await switch(False, why="schedule ended" if state["off_at"] in ends else "auto-off timer")
                log.info("auto-off")
            except Exception as e:
                log.warning("auto-off failed: %s", e)
        if any(o["created"] < now - ORDER_TTL_S for o in orders.values()):
            save_orders()  # drop orders older than 6 h
        check_nudges(now)
        await asyncio.sleep(POLL_S)


def started(o):
    """Made, or the machine switched on after the order (not by the order's own warm-up)."""
    if o["status"] != "waiting":
        return True
    since = state.get("on_since")
    return bool(state.get("is_on") and since and since > o["created"] and not o.get("warmed"))


def spoken(o):
    """The order summary, worded for speech."""
    return describe(o).replace(" (½ decaf)", "").replace("&", "and")


def check_nudges(now):
    for oid, o in list(orders.items()):
        done = o.get("nudges", 0)
        if oid in calling or done >= len(NUDGE_AFTER_MIN) or started(o):
            continue
        if now - o["created"] >= NUDGE_AFTER_MIN[done] * 60 or (done == 0 and o.get("call_now")):
            calling.add(oid)
            asyncio.create_task(nudge(oid))


async def nudge(oid):
    """Ring the barista through Spruik and say what's waiting."""
    try:
        o = orders.get(oid)
        if not o or started(o):
            return
        mins = max(1, int((time.time() - o["created"]) // 60))
        who = "Someone" if o["who"] == "someone" else o["who"]
        if o.get("call_now") and mins < 2:
            text = (f"Hi {BARISTA_NAME}, it's Spruik. {who} just ordered a coffee, "
                    f"and a matching dose is loaded in the grinder. {spoken(o)}.")
        else:
            text = (f"Hi {BARISTA_NAME}, it's Spruik. {who} ordered a coffee {mins} minutes ago, "
                    f"and it hasn't been started. {spoken(o)}.")
        if o.get("note"):
            text += f" Their note says: {o['note']}."
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=150)) as s:
            async with s.post(CALL_URL, json={"text": text[:400], "voice": CALL_VOICE}) as r:
                result = r.status, (await r.text()).strip()
        if result[0] == 409:
            log.info("coffee nudge waiting: Spruik is on another call")
            return  # not counted; tried again on the next check
        o["nudges"] = o.get("nudges", 0) + 1
        save_orders()
        log.info("coffee nudge call %d: %s", o["nudges"], result[1])
    except Exception as e:
        o = orders.get(oid)
        if o:
            o["nudges"] = o.get("nudges", 0) + 1  # don't retry a broken call path in a loop
            save_orders()
        log.warning("coffee nudge call failed: %s", e)
    finally:
        calling.discard(oid)


def status():
    left = None
    if state["is_on"] and state["off_at"]:
        left = max(0, int(state["off_at"] - time.time()))
    win, nxt = current_window(), next_window()
    p = live_plugs["machine"]
    return {"on": state["is_on"], "seconds_left": left, "error": state["error"],
            "warm_seconds_left": warm_seconds_left(), "warmup_minutes": warmup_minutes(),
            "warm_measured": bool(p is not None and p.metered),
            "watts": None if p is None or p.watts is None or not p.metered else round(p.watts),
            "default_minutes": DEFAULT_MIN, "max_minutes": MAX_MIN,
            "scheduled_until": datetime.fromtimestamp(win[1], TZ).strftime("%H:%M") if win else None,
            "next_scheduled": when(nxt[0]) if nxt else None,
            "mode": settings["mode"], "plug": public_plug(settings["plugs"]["machine"]),
            "plug_model": p.model if p is not None else "", "grinder": grinder_view(),
            "last_shot": last_shot()}


def last_shot():
    """The latest shot from the last 3 hours, for the 'how was it?' card."""
    if not analyzer or not analyzer.shots:
        return None
    x = analyzer.shots[-1]
    return analyzer.public_shot(x) if time.time() - x["t"] < 3 * 3600 else None


def schedule_view():
    nxt = next_window()
    return {"enabled": schedule["enabled"], "rules": schedule["rules"], "days": DAY_NAMES,
            "timezone": str(TZ), "max_hours": MAX_WINDOW_MIN // 60,
            "next": when(nxt[0]) if nxt else None}


async def api_history(req):
    try:
        hours = max(1, min(int(req.query.get("hours", 24)), HISTORY_KEEP_S // 3600))
    except ValueError:
        hours = 24
    now = time.time()
    start = now - hours * 3600
    before = [e for e in history if e["t"] <= start]
    events = ([before[-1]] if before else []) + [e for e in history if e["t"] > start]
    return web.json_response({"now": round(now), "start": round(start), "hours": hours,
                              "timezone": str(TZ), "events": events})


async def api_schedule(_):
    return web.json_response(schedule_view())


async def api_schedule_set(req):
    try:
        body = await req.json()
        rules = clean_rules(body.get("rules", []))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as e:
        return web.json_response({"error": str(e) or "bad schedule"}, status=400)
    schedule.update(enabled=bool(body.get("enabled", True)), rules=rules)
    win = current_window()
    schedule["started"] = win[2] if win else None  # a new schedule never switches on mid-window
    if win and state["is_on"] and state["off_at"] and state["off_at"] < win[1]:
        state["off_at"] = win[1]
    save_schedule()
    log.info("schedule saved: %s", json.dumps(schedule))
    return web.json_response(schedule_view())


async def api_status(_):
    return web.json_response(status())


async def api_on(req):
    try:
        minutes = int(req.query.get("minutes", DEFAULT_MIN))
    except ValueError:
        return web.json_response({"error": "minutes must be a number"}, status=400)
    minutes = max(1, min(minutes, MAX_MIN))
    try:
        await switch(True, minutes, "on button")
    except Exception as e:
        return web.json_response({**status(), "error": str(e)}, status=502)
    return web.json_response(status())


async def api_off(_):
    try:
        await switch(False, why="off button")
    except Exception as e:
        return web.json_response({**status(), "error": str(e)}, status=502)
    return web.json_response(status())


async def api_menu(_):
    return web.json_response({**MENU, "signal_ready": bool(SIGNAL_ACCOUNT and SIGNAL_RECIPIENT)})


def clean(text, limit):
    return " ".join(str(text or "").split())[:limit]


def normalize_phone(raw):
    """Australian-friendly: 0412 345 678 / 61412345678 / +61 412 345 678 -> +61412345678."""
    s = re.sub(r"[\s\-().]", "", str(raw or ""))
    if s.startswith("00"):
        s = "+" + s[2:]
    if re.fullmatch(r"0\d{9}", s):
        s = "+61" + s[1:]
    elif re.fullmatch(r"61\d{9}", s):
        s = "+" + s
    return s if re.fullmatch(r"\+\d{8,15}", s) else None


async def on_signal(session, number):
    url = f"{SIGNAL_API}/v1/search/{quote(SIGNAL_ACCOUNT)}"
    async with session.get(url, params={"numbers": number}) as r:
        if r.status != 200:
            raise RuntimeError(f"lookup HTTP {r.status}")
        return any(x.get("registered") for x in await r.json())


async def signal_send(session, to, text, gif=None):
    payload = {"number": SIGNAL_ACCOUNT, "recipients": [to], "message": text}
    if gif:
        payload["base64_attachments"] = [gif]
    async with session.post(f"{SIGNAL_API}/v2/send", json=payload) as r:
        if r.status >= 300:
            raise RuntimeError(f"HTTP {r.status}: {(await r.text())[:200]}")


def call_lines():
    return [f"Call {BARISTA_NAME} on Signal: https://signal.me/#p/{BARISTA_NUMBER}",
            f"Or phone: {BARISTA_NUMBER}"]


async def api_request(req):
    if not (SIGNAL_ACCOUNT and SIGNAL_RECIPIENT):
        return web.json_response({"error": "Spruik not set up (secret coffee-signal)"}, status=503)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    order, bad = menu_choice(body)
    if not order:
        return web.json_response({"error": f"pick a {bad}"}, status=400)
    who = clean(body.get("name"), 40) or "someone"
    note = clean(body.get("note"), 200)
    warm = bool(body.get("warm_up"))
    phone = None
    if str(body.get("phone") or "").strip():
        phone = normalize_phone(body["phone"])
        if not phone:
            return web.json_response({"error": "That phone number doesn't look right"}, status=400)

    wait = REQUEST_GAP_S - (time.time() - last_request["at"])
    if wait > 0:
        return web.json_response({"error": f"Already asked. Try again in {int(wait) + 1}s"}, status=429)
    last_request["at"] = time.time()

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        if phone:
            try:
                registered = await on_signal(s, phone)
            except Exception as e:
                log.warning("signal lookup failed: %s", e)
                registered = True  # can't check; try sending anyway
            if not registered:
                last_request["at"] = 0.0
                return web.json_response({"error": "That number isn't on Signal"}, status=400)

        warmed = None
        if warm:
            try:
                if not state["is_on"]:
                    await switch(True, DEFAULT_MIN, "coffee order warm-up")
                warmed = True
            except Exception as e:
                warmed = False
                log.warning("warm-up failed: %s", e)

        dose_lines, match = await dose_for(order)

        oid = secrets.token_urlsafe(12)
        now = time.time()
        entry = {**order, "who": who, "note": note, "phone": phone,
                 "status": "waiting", "created": now, "updated": now,
                 "warmed": warmed is True, "nudges": 0, "dose_match": match,
                 "call_now": bool(match and settings["grinder"]["call_on_match"])}

        lines = (["🚨 READY TO GO: matching dose loaded"] if match else []) + [f"☕ Coffee request from {who}", describe(order)]
        if note:
            lines.append(f"Note: {note}")
        if warmed is True:
            lines.append("The machine is on and warming up.")
        elif warmed is False:
            lines.append("(Couldn't switch the machine on.)")
        lines += dose_lines
        if phone:
            lines.append("They've been sent a confirmation on Signal.")
        lines.append(f"Mark it made / delivered: {BASE_URL}/o/{oid}")

        gif = await find_gif(s, order["drink"])
        try:
            await signal_send(s, SIGNAL_RECIPIENT, "\n".join(lines), gif)
        except Exception as e:
            last_request["at"] = 0.0
            log.warning("signal send failed: %s", e)
            return web.json_response({"error": f"Couldn't send: {e}"}, status=502)

        confirmed = None
        if phone:
            msg = [f"☕ Order confirmed{'' if who == 'someone' else ', ' + who}!", describe(order)]
            if note:
                msg.append(f"Note: {note}")
            msg += [f"{BARISTA_NAME} has it, and you'll get a message here when it's ready.", "", *call_lines()]
            try:
                await signal_send(s, phone, "\n".join(msg), gif)
                confirmed = True
            except Exception as e:
                confirmed = False
                log.warning("confirmation send failed: %s", e)

    orders[oid] = entry
    save_orders()
    log.info("coffee request sent (%s, gif=%s, confirmation=%s)", order["drink"], bool(gif), confirmed)
    return web.json_response({"ok": True, "id": oid, "warmed": warmed, "gif": bool(gif),
                              "confirmed": confirmed, **status()})


async def dose_for(order):
    """Signal lines about the grinder for a new order, and whether the loaded dose matches it.
    A match is the next level of alarm: the message is flagged, and (if set) it grinds now
    and Spruik calls straight away instead of after a few minutes."""
    if not grinder_enabled():
        return [], False
    dose, beans = grinder["dose"], order["beans"]
    if not dose:
        return ["🫙 No dose loaded in the grinder."], False
    what = f"{dose['beans'].lower()} dose ({dose['grams']:g} g)"
    if dose["beans"] != beans:
        return [f"⚠️ The grinder has a {what} loaded, but this order is {beans.lower()}."], False
    if dose["status"] == "ground":
        return [f"🟢 A {what} is already ground and waiting in the cup."], True
    lines = [f"🟢 A {what} is loaded and matches."]
    if settings["grinder"]["on_match"] == "grind":
        secs = grind_seconds(beans)
        try:
            if not secs:
                raise GrinderBusy("no grind time set yet")
            await grind(secs, "grind", beans)
            lines.append(f"Grinding it now ({secs:g} s); it'll be in the cup.")
        except Exception as e:
            lines.append(f"Couldn't grind it automatically: {e}.")
    else:
        lines.append(f"Grind it: {BASE_URL}/#grinder")
    return lines, True


def find_order(req):
    prune()
    return orders.get(req.match_info["oid"])


async def api_order(req):
    o = find_order(req)
    if not o:
        return web.json_response({"status": "gone"}, status=404)
    return web.json_response(public(o))


async def api_order_set(req):
    o = find_order(req)
    new = req.match_info["status"]
    if not o:
        return web.json_response({"status": "gone"}, status=404)
    if new not in STATUSES:
        return web.json_response({"error": "unknown status"}, status=400)
    was = o["status"]
    o["status"], o["updated"] = new, time.time()
    result = public(o)
    if new == "made" and was == "waiting" and analyzer:
        analyzer.link_order(req.match_info["oid"], time.time())  # that shot was this coffee
    if new in ("made", "delivered") and not o.get("stock_used"):
        o["stock_used"] = True
        await warn_low(use_stock(o))
        dose = grinder["dose"]
        if dose and dose["beans"] == o.get("beans", "Regular") and not grinder["run"]:
            grinder["dose"] = None  # that dose went into this coffee
            save_grinder()
    if new == "made" and was == "waiting" and o.get("phone"):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                await signal_send(s, o["phone"], f"☕ Your {o['drink'].lower()} is ready!\n\n"
                                  + "\n".join(call_lines())
                                  + f"\n\n🕵️ Once you've tried it, rate it for the case file: "
                                  + f"{BASE_URL}/r/{req.match_info['oid']}")
        except Exception as e:
            log.warning("ready message failed: %s", e)
    save_orders()  # kept for rating until both sides have rated (or 24 h)
    log.info("order marked %s", new)
    return web.json_response(result)


async def api_order_rate(req):
    """The drinker rates their coffee: 1-5 beans, up to 4 tags, an optional short comment."""
    o = find_order(req)
    if not o:
        return web.json_response({"status": "gone"}, status=404)
    if o["status"] == "waiting":
        return web.json_response({"error": "It hasn't been made yet"}, status=409)
    if o.get("rating"):
        return web.json_response({"error": "Already rated", **public(o)}, status=409)
    try:
        body = await req.json()
        beans = int(body.get("beans"))
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    if not 1 <= beans <= 5:
        return web.json_response({"error": "pick 1 to 5 beans"}, status=400)
    tags = [t for t in RATE_TAGS if t in (body.get("tags") or [])][:4]
    comment = clean(body.get("comment"), 120)
    o["rating"] = {"beans": beans, "tags": tags, "comment": comment, "at": int(time.time())}
    if analyzer:
        analyzer.order_rated(req.match_info["oid"], beans, tags)
    result = public(o)
    oid = req.match_info["oid"]
    reviewed = bool(o.get("review"))
    save_orders()  # dropped here if it's delivered and rated both ways

    c = casefile
    c["rated"] += 1
    c["beans"][beans - 1] += 1
    tally(c["by_drink"], o["drink"], beans)
    if o.get("cup") and o["cup"] != "Dan's choice":
        tally(c["by_cup"], o["cup"], beans)
    for t in tags:
        tally(c["tags"], t)
    c["streak"] = c["streak"] + 1 if beans == 5 else 0
    c["best_streak"] = max(c["best_streak"], c["streak"])
    save_casefile()

    if SIGNAL_ACCOUNT and SIGNAL_RECIPIENT:
        who = "Someone" if o["who"] == "someone" else o["who"]
        lines = [f"🕵️ Case report: {who} rated their {o['drink'].lower()}",
                 "🫘" * beans + "·" * (5 - beans) + f"  {beans}/5"]
        if tags:
            lines.append(" · ".join(tags))
        if comment:
            lines.append(f"“{comment}”")
        if c["streak"] >= 2:
            lines.append(f"🔥 {c['streak']} five-bean coffees in a row!")
        if not reviewed:
            lines.append(f"Rate the customer back: {BASE_URL}/o/{oid}")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                await signal_send(s, SIGNAL_RECIPIENT, "\n".join(lines))
        except Exception as e:
            log.warning("rating message failed: %s", e)
    log.info("order rated %d beans", beans)
    return web.json_response(result)


async def api_order_review(req):
    """The barista rates the customer, tags only (no typing)."""
    o = find_order(req)
    if not o:
        return web.json_response({"status": "gone"}, status=404)
    if o["status"] == "waiting":
        return web.json_response({"error": "Make it first"}, status=409)
    if o.get("review"):
        return web.json_response({"error": "Already reviewed", **public(o)}, status=409)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    tags = [t for t in REVIEW_TAGS if t in (body.get("tags") or [])][:4]
    if not tags:
        return web.json_response({"error": "pick at least one"}, status=400)
    o["review"] = {"tags": tags, "at": int(time.time())}
    oid = req.match_info["oid"]
    sent = None
    if o.get("phone"):
        name = "" if o["who"] == "someone" else f", {o['who']}"
        text = (f"🕵️ Case closed{name}! {BARISTA_NAME}'s review of you as a customer:\n"
                + "\n".join(f"• {t}" for t in tags))
        if not o.get("rating"):
            text += f"\n\nYour turn: rate the coffee at {BASE_URL}/r/{oid}"
        query = "cookies" if "Tipped in biscuits 🍪" in tags else (
            "trophy" if "Customer of the day 🏆" in tags else "thank you")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                await signal_send(s, o["phone"], text, await find_gif(s, o["drink"], query))
            sent = True
        except Exception as e:
            sent = False
            log.warning("review message failed: %s", e)
    result = public(o)
    save_orders()  # dropped here if it's delivered and rated both ways
    casefile["reviews"] += 1
    for t in tags:
        tally(casefile["review_tags"], t)
    save_casefile()
    log.info("customer reviewed (%d tags, sent=%s)", len(tags), sent)
    return web.json_response({**result, "sent": sent})


async def api_casefile(_):
    return web.json_response(casefile_view())


async def rate_page(_):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), "rate.html"))


def menu_choice(body):
    order = {}
    body = {"cup": "Dan's choice", "beans": "Regular", **body}
    for key, options in MENU.items():
        if body.get(key) not in options:
            return None, key
        order[key] = body[key]
    return order, None


async def api_stock(_):
    return web.json_response({"items": stock_view(), "basket_g": BASKET_G})


async def api_stock_change(req):
    key = req.match_info["item"]
    if key not in STOCK_ITEMS:
        return web.json_response({"error": "unknown item"}, status=404)
    try:
        body = await req.json()
    except Exception:
        body = {}
    if body.get("action") == "add":
        stock_levels[key] = (stock_levels[key] or 0) + STOCK_ITEMS[key]["pack"]
    elif body.get("action") == "set":
        try:
            stock_levels[key] = max(0.0, float(body.get("level")))
        except (TypeError, ValueError):
            return web.json_response({"error": "level must be a number"}, status=400)
    elif body.get("action") == "untrack":
        stock_levels[key] = None
    else:
        return web.json_response({"error": "action must be add, set or untrack"}, status=400)
    save_stock()
    log.info("stock %s -> %s", key, stock_levels[key])
    return web.json_response({"items": stock_view(), "basket_g": BASKET_G})


async def api_stock_log(req):
    """A coffee made without an order through the app."""
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    order, bad = menu_choice(body)
    if not order:
        return web.json_response({"error": f"pick a {bad}"}, status=400)
    await warn_low(use_stock(order))
    log.info("logged a coffee by hand (%s)", order["drink"])
    return web.json_response({"items": stock_view(), "basket_g": BASKET_G, "logged": describe(order)})


async def order_page(_):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), "order.html"))


async def settings_page(_):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), "settings.html"))


# --- Settings, plugs and grinder API -------------------------------------------------
def settings_view():
    return {**settings, "plugs": {r: public_plug(c) for r, c in settings["plugs"].items()}, "modes": MODES, "drivers": plugs.DRIVERS, "beans": MENU["beans"],
            "plate_s": GRINDER_PLATE_S, "scan_subnet": scan_subnet(),
            "live": {r: (p.info() if (p := live_plugs[r]) is not None and p.updated else None) for r in live_plugs}}


def scan_subnet():
    if SCAN_SUBNET:
        return SCAN_SUBNET
    for role in ("machine", "grinder"):
        h = settings["plugs"][role]["host"]
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", h):
            return h.rsplit(".", 1)[0] + ".0/24"
    return "192.168.88.0/24"


async def api_settings(_):
    return web.json_response(settings_view())


async def api_settings_set(req):
    try:
        new = merge_settings(await req.json(), settings)
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as e:
        return web.json_response({"error": str(e) or "bad settings"}, status=400)
    if new["plugs"]["machine"]["host"] and new["plugs"]["machine"] == new["plugs"]["grinder"]:
        return web.json_response({"error": "The machine and the grinder need different plugs"}, status=400)
    if grinder["run"] and new["plugs"]["grinder"] != settings["plugs"]["grinder"]:
        return web.json_response({"error": "Wait for the grinder to finish first"}, status=409)
    old_machine = dict(settings["plugs"]["machine"])
    settings.update(new)
    save_settings()
    analyzer.settings = settings["machine"]  # thresholds apply from the next sample
    wake_sampler.set()
    if settings["plugs"]["machine"] != old_machine:
        record(None, "machine plug changed")
        state.update(is_on=None, off_at=None, on_since=None)
        await refresh()
    log.info("settings saved: mode=%s plugs=%s", settings["mode"], {r: public_plug(c) for r, c in settings["plugs"].items()})
    return web.json_response(settings_view())


async def api_plug_test(req):
    """Try a plug without saving it: model, on/off, watts."""
    try:
        body = await req.json()
        role = body.get("role") if body.get("role") in settings["plugs"] else "machine"
        cfg = clean_plug(body, settings["plugs"][role])  # a blank Tuya key means the saved one
        p = plugs.make(cfg)
        if p is None:
            raise ValueError("pick a plug type and address")
    except (ValueError, json.JSONDecodeError, AttributeError) as e:
        return web.json_response({"error": str(e) or "bad request"}, status=400)
    current = [r for r in live_plugs if live_plugs[r] is not None and live_plugs[r].key == p.key]
    if current:
        p = live_plugs[current[0]]  # same plug already connected: reuse its session
    try:
        await asyncio.wait_for(p.update(), 15)
        return web.json_response({"ok": True, **p.info()})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e) or type(e).__name__}, status=502)
    finally:
        if not current:
            await p.close()


async def api_plug_scan(req):
    try:
        found = await asyncio.wait_for(plugs.scan(req.query.get("subnet") or scan_subnet()), 30)
    except (ValueError, asyncio.TimeoutError) as e:
        return web.json_response({"error": str(e) or "scan took too long"}, status=400)
    for f in found:
        f["role"] = next((r for r in live_plugs if settings["plugs"][r]["host"] == f["host"]), None)
    return web.json_response({"subnet": req.query.get("subnet") or scan_subnet(), "found": found})


async def api_grinder(_):
    return web.json_response(grinder_view())


def grinder_error(e, status=409):
    return web.json_response({**grinder_view(), "error": str(e)}, status=status)


async def api_grinder_load(req):
    """A dose is in the hopper: switch the plug off so the grinder's own switch can be turned on."""
    if not grinder_enabled():
        return grinder_error("The grinder isn't set up (Settings)")
    if grinder["run"]:
        return grinder_error("Wait for the grinder to finish")
    try:
        body = await req.json()
    except Exception:
        body = {}
    beans = body.get("beans", "Regular")
    if beans not in MENU["beans"]:
        return grinder_error("pick the beans", 400)
    try:
        grams = num(body.get("grams", settings["grinder"]["dose_g"]), 5, 30, "dose")
        await plug("grinder").turn(False)
        grinder["on"], grinder["error"], grinder["found_on_at"] = False, None, None
    except ValueError as e:
        return grinder_error(e, 400)
    except Exception as e:
        grinder["error"] = str(e)
        return grinder_error(f"Couldn't switch the grinder's plug off ({e}), so don't turn its switch on yet", 502)
    grinder["dose"] = {"beans": beans, "grams": grams, "status": "loaded", "at": time.time()}
    save_grinder()
    log.info("dose loaded: %s %.1f g", beans, grams)
    return web.json_response(grinder_view())


async def api_grinder_grind(req):
    try:
        body = await req.json()
    except Exception:
        body = {}
    dose = grinder["dose"]
    if not (dose and dose["status"] == "loaded") and not body.get("anyway"):
        return grinder_error("Load a dose first")
    beans = dose["beans"] if dose else "Regular"
    secs = grind_seconds(beans)
    if not secs:
        return grinder_error("Time a grind first (Settings → Grinder)")
    try:
        await grind(secs, "grind", beans)
    except GrinderBusy as e:
        return grinder_error(e)
    except Exception as e:
        return grinder_error(f"Couldn't switch the grinder on: {e}", 502)
    return web.json_response(grinder_view())


async def api_grinder_teach(req):
    """Start a timed grind; stop it when the beans are gone and that time is saved for these beans."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    beans = body.get("beans") or (grinder["dose"] or {}).get("beans") or "Regular"
    if beans not in MENU["beans"]:
        return grinder_error("pick the beans", 400)
    try:
        await grind(settings["grinder"]["max_run_s"], "teach", beans)
    except GrinderBusy as e:
        return grinder_error(e)
    except Exception as e:
        return grinder_error(f"Couldn't switch the grinder on: {e}", 502)
    return web.json_response(grinder_view())


async def api_grinder_stop(_):
    task = grind_task["t"]
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return web.json_response({**grinder_view(), "result": grinder["last"]})
    if grinder_enabled():
        try:
            await plug("grinder").turn(False)
            grinder["on"] = False
        except Exception as e:
            return grinder_error(f"Couldn't switch the grinder off: {e}", 502)
    return web.json_response(grinder_view())


async def api_grinder_clear(_):
    if grinder["run"]:
        return grinder_error("Wait for the grinder to finish")
    grinder["dose"] = None
    save_grinder()
    return web.json_response(grinder_view())


async def api_power(req):
    try:
        minutes = max(1, min(int(req.query.get("minutes", 10)), 30))
    except ValueError:
        minutes = 10
    p = live_plugs["machine"]
    return web.json_response({"metered": bool(p is not None and p.metered), "now": time.time(),
                              "samples": analyzer.live_view(minutes), "heater_w": settings["machine"]["heater_w"],
                              "pump_w": settings["machine"]["pump_w"],
                              "heater_on": analyzer.heater_on, "pumping": analyzer.pump is not None})


async def api_maintenance(_):
    return web.json_response({"days": analyzer.daily_view()[-60:], "trends": analyzer.trends(),
                              "typical_warmup_min": warmup_minutes(), "grinder_totals": grinder["totals"]})


async def api_shots(_):
    return web.json_response(analyzer.shots_view())


async def api_shot_taste(req):
    try:
        body = await req.json()
        x = analyzer.set_taste(req.match_info["sid"], body.get("taste"))
    except KeyError:
        return web.json_response({"error": "no such shot"}, status=404)
    except (ValueError, json.JSONDecodeError, AttributeError) as e:
        return web.json_response({"error": str(e) or "bad request"}, status=400)
    log.info("shot tasted: %s (purged=%s, idle %d s)", x["taste"], x["purged"], x["idle_s"])
    return web.json_response(analyzer.public_shot(x))


async def api_sim_pump(req):
    """Testing only (COFFEE_SIM=1): pretend the pump runs for a few seconds."""
    plugs.SimPlug.pump_until = time.time() + float(req.query.get("s", 25))
    return web.json_response({"ok": True})


async def healthz(_):
    return web.Response(text="ok")


async def index(_):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


async def start_bg(app):
    global analyzer
    load_settings()
    load_orders()
    load_stock()
    load_casefile()
    load_schedule()
    load_history()
    load_grinder()
    analyzer = Analyzer(MAINT_FILE, TZ, settings["machine"])
    app["wd"] = asyncio.create_task(watchdog(app))
    app["sampler"] = asyncio.create_task(sampler())
    app["grinder"] = asyncio.create_task(grinder_watch())


async def stop_bg(app):
    if grinder["run"]:
        await grinder_off("shutting down")  # never leave the grinder running
    if analyzer:
        analyzer.save()


app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/healthz", healthz)
app.router.add_get("/api/status", api_status)
app.router.add_post("/api/on", api_on)
app.router.add_post("/api/off", api_off)
app.router.add_get("/api/menu", api_menu)
app.router.add_post("/api/request", api_request)
app.router.add_get("/api/order/{oid}", api_order)
app.router.add_post("/api/order/{oid}/rate", api_order_rate)
app.router.add_post("/api/order/{oid}/review", api_order_review)
app.router.add_post("/api/order/{oid}/{status}", api_order_set)
app.router.add_get("/o/{oid}", order_page)
app.router.add_get("/r/{oid}", rate_page)
app.router.add_get("/api/casefile", api_casefile)
app.router.add_get("/api/history", api_history)
app.router.add_get("/api/schedule", api_schedule)
app.router.add_post("/api/schedule", api_schedule_set)
app.router.add_get("/api/stock", api_stock)
app.router.add_post("/api/stock/log", api_stock_log)
app.router.add_post("/api/stock/{item}", api_stock_change)
app.router.add_get("/settings", settings_page)
app.router.add_get("/api/settings", api_settings)
app.router.add_post("/api/settings", api_settings_set)
app.router.add_post("/api/plugs/test", api_plug_test)
app.router.add_post("/api/plugs/scan", api_plug_scan)
app.router.add_get("/api/grinder", api_grinder)
app.router.add_post("/api/grinder/load", api_grinder_load)
app.router.add_post("/api/grinder/grind", api_grinder_grind)
app.router.add_post("/api/grinder/teach", api_grinder_teach)
app.router.add_post("/api/grinder/stop", api_grinder_stop)
app.router.add_post("/api/grinder/clear", api_grinder_clear)
app.router.add_get("/api/power", api_power)
app.router.add_get("/api/maintenance", api_maintenance)
app.router.add_get("/api/shots", api_shots)
app.router.add_post("/api/shots/{sid}/taste", api_shot_taste)
if plugs.SIM:
    app.router.add_post("/api/sim/pump", api_sim_pump)
app.on_startup.append(start_bg)
app.on_shutdown.append(stop_bg)

if __name__ == "__main__":
    web.run_app(app, port=8080, access_log=None)
