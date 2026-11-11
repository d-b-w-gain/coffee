# Offline test: grinder + orders with simulated plugs and a fake Signal / Spruik.
import asyncio, os, sys, time, tempfile
d = tempfile.mkdtemp()
os.environ.update(ORDERS_FILE=f"{d}/orders.json", STOCK_FILE=f"{d}/stock.json", CASEFILE=f"{d}/case.json",
                  SIGNAL_API_URL="http://127.0.0.1:9998", SIGNAL_ACCOUNT="+61400000000", SIGNAL_RECIPIENT="+61400000001",
                  SCHEDULE_FILE=f"{d}/schedule.json", HISTORY_FILE=f"{d}/history.json", SETTINGS_FILE=f"{d}/settings.json",
                  GRINDER_FILE=f"{d}/grinder.json", MAINTENANCE_FILE=f"{d}/maint.json",
                  SPRUIK_ANNOUNCE_URL="http://127.0.0.1:9998/announce", COFFEE_SIM="1")
sys.path.insert(0, "/app"); import app as A
from power import Analyzer
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
sent, calls = [], []
async def fake_send(req):
    sent.append(await req.json()); return web.json_response({"timestamp": 1})
async def fake_call(req):
    calls.append(await req.json()); return web.Response(text="called")
order = {"drink": "Flat white", "size": "Regular", "milk": "Oat", "shots": "Single", "sugar": "No sugar",
         "beans": "Regular", "cup": "Book mug", "name": "Hayley"}

async def main():
    fs = web.Application(); fs.router.add_post("/v2/send", fake_send); fs.router.add_post("/announce", fake_call)
    r = web.AppRunner(fs); await r.setup(); await web.TCPSite(r, "127.0.0.1", 9998).start()
    A.app.on_startup.clear()
    A.analyzer = Analyzer(f"{d}/maint.json", A.TZ, A.settings["machine"])
    c = TestClient(TestServer(A.app)); await c.start_server()
    resp = await c.post("/api/settings", json={"mode": "machine+grinder",
        "plugs": {"machine": {"driver": "sim", "host": "machine"}, "grinder": {"driver": "sim", "host": "grinder"}},
        "grinder": {"grind_s": {"Regular": 2}, "extra_s": 1, "on_match": "notify", "call_on_match": False}})
    assert resp.status == 200, await resp.text()

    # 1. nothing loaded
    j = await (await c.post("/api/request", json=order)).json(); assert j.get("ok"), j
    msg = sent[-1]["message"]; print("empty:", msg.replace("\n", " | ")[:160])
    assert "No dose loaded" in msg and "READY TO GO" not in msg
    A.last_request["at"] = 0

    # 2. mismatched dose
    await c.post("/api/grinder/load", json={"beans": "Decaf"})
    j = await (await c.post("/api/request", json=order)).json(); assert j.get("ok"), j
    msg = sent[-1]["message"]; print("mismatch:", msg.replace("\n", " | ")[:160])
    assert "decaf dose (18 g) loaded, but this order is regular" in msg and "READY TO GO" not in msg
    A.last_request["at"] = 0

    # 3. matching dose, notify only: flagged, no grind
    await c.post("/api/grinder/load", json={"beans": "Regular"})
    j = await (await c.post("/api/request", json=order)).json(); oid = j["id"]
    msg = sent[-1]["message"]; print("match/notify:", msg.replace("\n", " | ")[:200])
    assert msg.startswith("🚨 READY TO GO") and "#grinder" in msg and not A.grinder["run"]
    assert A.orders[oid]["dose_match"] and not A.orders[oid]["call_now"]
    A.last_request["at"] = 0

    # 4. matching dose, grind automatically + call straight away
    await c.post("/api/settings", json={"grinder": {"on_match": "grind", "call_on_match": True}})
    j = await (await c.post("/api/request", json=order)).json(); oid = j["id"]
    msg = sent[-1]["message"]; print("match/grind:", msg.replace("\n", " | ")[:200])
    assert "Grinding it now (3 s)" in msg and A.grinder["run"] and A.grinder["run"]["why"] == "grind"
    assert A.orders[oid]["call_now"]
    A.check_nudges(time.time()); await asyncio.sleep(0.5)
    assert calls and "Hayley just ordered a coffee, and a matching dose is loaded" in calls[-1]["text"], calls
    print("call:", calls[-1]["text"][:100])
    await asyncio.sleep(3.5)
    assert not A.grinder["run"] and A.grinder["dose"]["status"] == "ground", A.grinder
    # marking it made uses the dose up
    await c.post(f"/api/order/{oid}/made")
    assert A.grinder["dose"] is None, A.grinder["dose"]

    # 5. safety: grinder switched on outside a run is cut after max_run_s
    await c.post("/api/settings", json={"grinder": {"max_run_s": 5}})
    A.grinder["rest_until"] = 0
    g = A.plug("grinder"); await g.turn(True)
    task = asyncio.create_task(A.grinder_watch())
    t0 = time.time()
    while time.time() - t0 < 15 and g.is_on:
        await asyncio.sleep(0.5)
    task.cancel()
    took = time.time() - t0
    print(f"safety cut after {took:.1f} s; last={A.grinder['last']}")
    assert not g.is_on and 5 <= took <= 10, took
    assert A.grinder["last"]["why"] == "safety" and A.grinder["rest_until"] > time.time()

    # 6. status works with the machine on and no metering samples yet
    await c.post("/api/on?minutes=5")
    s = await (await c.get("/api/status")).json()
    assert s["on"] and s["warm_seconds_left"] is not None and s["grinder"]["enabled"], s
    await c.close(); await r.cleanup()
    print("ALL PASSED")

asyncio.run(main())
