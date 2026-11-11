# Offline test of the Tuya driver: tinytuya's device class is replaced by a fake
# that only speaks protocol 3.4 with the right key, like a real plug would.
import asyncio, io, logging, os, sys, tempfile
d = tempfile.mkdtemp()
os.environ.update(ORDERS_FILE=f"{d}/orders.json", STOCK_FILE=f"{d}/stock.json", CASEFILE=f"{d}/case.json",
                  SCHEDULE_FILE=f"{d}/schedule.json", HISTORY_FILE=f"{d}/history.json", SETTINGS_FILE=f"{d}/settings.json",
                  GRINDER_FILE=f"{d}/grinder.json", MAINTENANCE_FILE=f"{d}/maint.json")
sys.path.insert(0, "/app")
import plugs
import app as A
from power import Analyzer
from aiohttp.test_utils import TestClient, TestServer

KEY, DEV = "a1b2c3d4e5f6g7h8", "bf0123456789abcdefgh"
plug_state = {"1": False, "9": 0, "18": 0, "19": 0, "20": 2405}
sets = []


class FakeDevice:
    def __init__(self, dev_id, address, local_key, version=3.1, **kw):
        self.dev_id, self.address, self.key, self.version = dev_id, address, local_key, version

    def set_socketPersistent(self, p): pass
    def close(self): pass

    def _ok(self):
        if self.address == "10.9.9.9":
            return {"Error": "Network Error: Unable to Connect", "Err": "901", "Payload": None}
        if self.key != KEY or self.version != 3.4 or self.dev_id != DEV:
            return {"Error": "Check device key or version", "Err": "914", "Payload": None}

    def status(self):
        return self._ok() or {"dps": dict(plug_state)}

    def updatedps(self, index=None, nowait=False):
        if plug_state["1"]:
            plug_state["19"], plug_state["18"] = 11873, 4950  # 1187.3 W, 4.95 A

    def set_value(self, index, value, nowait=False):
        err = self._ok()
        if err:
            return err
        sets.append((index, value))
        plug_state[str(index)] = value
        if index == 1 and not value:
            plug_state["19"] = plug_state["18"] = 0
        return {"dps": {str(index): value}}


plugs.tinytuya.OutletDevice = FakeDevice
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


async def main():
    # driver on its own
    p = plugs.make({"driver": "tuya", "host": "192.168.88.50", "device_id": DEV, "local_key": KEY})
    await p.update()
    check("auto-detects protocol 3.4", p.proto == 3.4, p.proto)
    check("reads off, metered, volts", p.is_on is False and p.metered and p.volts == 240.5, p.info())
    await p.turn(True)
    check("turns on and reads watts", p.is_on and p.watts == 1187.3, p.info())
    msg = await p.set_backstop(55)
    sets.clear()
    await p.turn(False); await p.turn(True)
    check("countdown backstop set on switch-on", (9, 55) in sets and msg.startswith("plug countdown"), (sets, msg))
    await p.turn(False)

    bad = plugs.make({"driver": "tuya", "host": "192.168.88.50", "device_id": DEV, "local_key": "wrongwrongwrong1"})
    try:
        await bad.update(); check("wrong key refused", False)
    except RuntimeError as e:
        check("wrong key gives a clear error", "local key" in str(e), e)
    dead = plugs.make({"driver": "tuya", "host": "10.9.9.9", "device_id": DEV, "local_key": KEY})
    try:
        await dead.update(); check("dead address refused", False)
    except RuntimeError as e:
        check("unreachable plug says so", "Unable to Connect" in str(e), e)
    nokey = plugs.make({"driver": "tuya", "host": "192.168.88.50"})
    try:
        await nokey.update(); check("missing key refused", False)
    except RuntimeError as e:
        check("missing key/ID explained", "device ID and local key" in str(e), e)

    # through the app: the key is stored but never shown or logged
    logbuf = io.StringIO()
    logging.getLogger().addHandler(logging.StreamHandler(logbuf))
    A.app.on_startup.clear()
    A.analyzer = Analyzer(f"{d}/maint.json", A.TZ, A.settings["machine"])
    c = TestClient(TestServer(A.app)); await c.start_server()
    r = await c.post("/api/settings", json={"plugs": {"machine": {"driver": "tuya", "host": "192.168.88.50",
                                                                     "device_id": DEV, "local_key": KEY}}})
    j = await r.json()
    check("tuya plug saved", r.status == 200 and j["plugs"]["machine"]["local_key_set"], j.get("error"))
    check("key not in the settings reply", KEY not in await (await c.get("/api/settings")).text())
    check("key not in status", KEY not in await (await c.get("/api/status")).text())
    check("key not logged", KEY not in logbuf.getvalue())
    check("key stored on disk for the app", A.settings["plugs"]["machine"]["local_key"] == KEY)
    r = await c.post("/api/settings", json={"plugs": {"machine": {"driver": "tuya", "host": "192.168.88.51",
                                                                     "device_id": DEV, "local_key": ""}}})
    check("blank key keeps the saved one", A.settings["plugs"]["machine"]["local_key"] == KEY)
    r = await c.post("/api/plugs/test", json={"role": "machine", "driver": "tuya", "host": "192.168.88.50", "device_id": DEV})
    j = await r.json()
    check("test uses the saved key", j.get("ok") and j["metered"], j)
    r = await c.post("/api/settings", json={"plugs": {"machine": {"driver": "tuya", "host": "192.168.88.50",
                                                                     "device_id": "bf9999999999999999zz", "local_key": ""}}})
    check("new device ID drops the old key", A.settings["plugs"]["machine"]["local_key"] == "")
    r = await c.post("/api/settings", json={"plugs": {"machine": {"driver": "tuya", "host": "192.168.88.50",
                                                                     "device_id": DEV, "local_key": "has space in it!"}}})
    check("bad key rejected", r.status == 400)
    await c.post("/api/settings", json={"plugs": {"machine": {"driver": "tuya", "host": "192.168.88.50",
                                                                 "device_id": DEV, "local_key": KEY}}})
    r = await c.post("/api/on?minutes=5")
    j = await r.json()
    check("machine on through the app", r.status == 200 and j["on"] is True, j.get("error"))
    await A.live_plugs["machine"].update()
    check("watts through the app", A.live_plugs["machine"].watts == 1187.3)
    await c.post("/api/off")
    await c.close()

    # scan finds anything listening on Tuya's port
    srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 6668)
    found = await plugs.scan("127.0.0.0/30")
    srv.close()
    check("scan finds a Tuya device on port 6668", any(f["driver"] == "tuya" and f["host"] == "127.0.0.1" for f in found), found)
    print("\nFAILED: " + ", ".join(fails) if fails else "\nALL PASSED")
    sys.exit(1 if fails else 0)

asyncio.run(main())
