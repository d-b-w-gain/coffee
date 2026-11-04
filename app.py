"""Coffee machine control: a tiny web page + API over a Tapo P100 smart plug.

Every "on" comes with an auto-off timer (default 30 min, max MAX_ON_MINUTES),
and if the plug is found on with no timer running (pod restart, someone pressed
the button) it gets switched off after MAX_ON_MINUTES as well.
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

import aiohttp
from aiohttp import web
from urllib.parse import quote
from kasa import Credentials, Device, DeviceConfig, Discover

HOST = os.environ.get("TAPO_HOST", "192.168.88.10")
USER = os.environ.get("TAPO_USERNAME", "")
PASS = os.environ.get("TAPO_PASSWORD", "")
DEFAULT_MIN = int(os.environ.get("DEFAULT_ON_MINUTES", "30"))
MAX_MIN = int(os.environ.get("MAX_ON_MINUTES", "60"))
POLL_S = 30

SIGNAL_API = os.environ.get("SIGNAL_API_URL", "http://signal-cli.cnc-controller.svc:8080")
SIGNAL_ACCOUNT = os.environ.get("SIGNAL_ACCOUNT", "")
SIGNAL_RECIPIENT = os.environ.get("SIGNAL_RECIPIENT", "")
BARISTA_NAME = os.environ.get("BARISTA_NAME", "Dan")
BARISTA_NUMBER = os.environ.get("BARISTA_NUMBER") or SIGNAL_RECIPIENT
REQUEST_GAP_S = 60  # one coffee request a minute, so a double tap isn't two messages

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
ORDER_TTL_S = 6 * 3600  # an order not delivered within 6 h is forgotten
STATUSES = ("waiting", "made", "delivered")

# Only orders still in progress are kept, keyed by an unguessable id. History
# lives in each person's own browser, never here.
orders: dict[str, dict] = {}


def prune():
    cutoff = time.time() - ORDER_TTL_S
    for oid in [k for k, o in orders.items() if o["created"] < cutoff or o["status"] == "delivered"]:
        del orders[oid]


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
    return {"status": o["status"], "who": o["who"], "summary": describe(o), "note": o["note"],
            "created": int(o["created"]), "updated": int(o["updated"])}


async def find_gif(session, drink):
    """A random G-rated Giphy GIF for the drink, as a data: URI for signal-cli, or None."""
    if not GIPHY_KEY:
        return None
    query = f"{drink.lower()} coffee"
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

state = {"dev": None, "off_at": None, "error": None, "is_on": None, "on_since": None}
WARMUP_MIN = int(os.environ.get("WARMUP_MINUTES", "20"))  # Minibar, 2 L boiler, 1500 W
lock = asyncio.Lock()


async def device() -> Device:
    if state["dev"] is None:
        if not (USER and PASS):
            raise RuntimeError("TP-Link login not set (secret tapo-credentials)")
        creds = Credentials(USER, PASS)
        try:
            dev = await Discover.discover_single(HOST, credentials=creds, timeout=5)
        except Exception:
            # UDP discovery can be filtered; fall back to the P100's usual transport.
            cfg = DeviceConfig.from_dict({
                "host": HOST,
                "connection_type": {"device_family": "SMART.TAPOPLUG",
                                    "encryption_type": "KLAP", "login_version": 2},
            })
            cfg.credentials = creds
            dev = await Device.connect(config=cfg)
        state["dev"] = dev
    await state["dev"].update()
    return state["dev"]


async def refresh():
    try:
        dev = await device()
        state["is_on"] = dev.is_on
        state["error"] = None
        since = getattr(dev, "on_since", None) if dev.is_on else None
        if since is not None:
            state["on_since"] = since.timestamp()
        elif not dev.is_on:
            state["on_since"] = None
        elif state["on_since"] is None:
            state["on_since"] = time.time()
    except Exception as e:  # drop the session and reconnect next time
        state["dev"], state["is_on"], state["error"] = None, None, str(e) or type(e).__name__
        log.warning("plug unreachable: %s", state["error"])


async def switch(on: bool, minutes: int | None = None):
    async with lock:
        dev = await device()
        await (dev.turn_on() if on else dev.turn_off())
        await dev.update()
        was_on = state["is_on"]
        state["is_on"], state["error"] = dev.is_on, None
        state["off_at"] = time.time() + minutes * 60 if on else None
        if not on:
            state["on_since"] = None
        elif not was_on:
            since = getattr(dev, "on_since", None)
            state["on_since"] = since.timestamp() if since else time.time()
        log.info("turned %s%s", "on" if on else "off", f" for {minutes} min" if on else "")


async def watchdog(app):
    while True:
        await refresh()
        now = time.time()
        if state["is_on"] and state["off_at"] is None:
            state["off_at"] = now + MAX_MIN * 60  # found on without a timer
            log.info("plug was on with no timer; auto-off in %d min", MAX_MIN)
        elif state["is_on"] is False:
            state["off_at"] = None
        if state["is_on"] and state["off_at"] and now >= state["off_at"]:
            try:
                await switch(False)
                log.info("auto-off")
            except Exception as e:
                log.warning("auto-off failed: %s", e)
        if any(o["created"] < now - ORDER_TTL_S for o in orders.values()):
            save_orders()  # drop orders older than 6 h
        await asyncio.sleep(POLL_S)


def status():
    left = None
    if state["is_on"] and state["off_at"]:
        left = max(0, int(state["off_at"] - time.time()))
    warm_left = None
    if state["is_on"] and state["on_since"]:
        warm_left = max(0, int(WARMUP_MIN * 60 - (time.time() - state["on_since"])))
    return {"on": state["is_on"], "seconds_left": left, "error": state["error"],
            "warm_seconds_left": warm_left, "warmup_minutes": WARMUP_MIN,
            "default_minutes": DEFAULT_MIN, "max_minutes": MAX_MIN}


async def api_status(_):
    return web.json_response(status())


async def api_on(req):
    try:
        minutes = int(req.query.get("minutes", DEFAULT_MIN))
    except ValueError:
        return web.json_response({"error": "minutes must be a number"}, status=400)
    minutes = max(1, min(minutes, MAX_MIN))
    try:
        await switch(True, minutes)
    except Exception as e:
        state["dev"] = None
        return web.json_response({**status(), "error": str(e)}, status=502)
    return web.json_response(status())


async def api_off(_):
    try:
        await switch(False)
    except Exception as e:
        state["dev"] = None
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
                    await switch(True, DEFAULT_MIN)
                warmed = True
            except Exception as e:
                state["dev"] = None
                warmed = False
                log.warning("warm-up failed: %s", e)

        oid = secrets.token_urlsafe(12)
        now = time.time()
        entry = {**order, "who": who, "note": note, "phone": phone,
                 "status": "waiting", "created": now, "updated": now}

        lines = [f"☕ Coffee request from {who}", describe(order)]
        if note:
            lines.append(f"Note: {note}")
        if warmed is True:
            lines.append("The machine is on and warming up.")
        elif warmed is False:
            lines.append("(Couldn't switch the machine on.)")
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
    if new in ("made", "delivered") and not o.get("stock_used"):
        o["stock_used"] = True
        await warn_low(use_stock(o))
    if new == "made" and was == "waiting" and o.get("phone"):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                await signal_send(s, o["phone"], f"☕ Your {o['drink'].lower()} is ready!\n\n" + "\n".join(call_lines()))
        except Exception as e:
            log.warning("ready message failed: %s", e)
    save_orders()  # a delivered order is dropped here
    log.info("order marked %s", new)
    return web.json_response(result)


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


async def healthz(_):
    return web.Response(text="ok")


async def index(_):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


async def start_bg(app):
    load_orders()
    load_stock()
    app["wd"] = asyncio.create_task(watchdog(app))


app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/healthz", healthz)
app.router.add_get("/api/status", api_status)
app.router.add_post("/api/on", api_on)
app.router.add_post("/api/off", api_off)
app.router.add_get("/api/menu", api_menu)
app.router.add_post("/api/request", api_request)
app.router.add_get("/api/order/{oid}", api_order)
app.router.add_post("/api/order/{oid}/{status}", api_order_set)
app.router.add_get("/o/{oid}", order_page)
app.router.add_get("/api/stock", api_stock)
app.router.add_post("/api/stock/log", api_stock_log)
app.router.add_post("/api/stock/{item}", api_stock_change)
app.on_startup.append(start_bg)

if __name__ == "__main__":
    web.run_app(app, port=8080, access_log=None)
