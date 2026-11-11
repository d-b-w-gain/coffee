"""Smart plug drivers: one small interface over the plugs people actually have.

    tapo     TP-Link Tapo and Kasa plugs (P100, P110, P115, HS100/110, KP115 ...)
             through python-kasa. Tapo needs the TP-Link account login
             (TAPO_USERNAME / TAPO_PASSWORD) and "Third-Party Compatibility" on.
    shelly   Shelly plugs and relays, Gen1 (HTTP /relay, /status) and Gen2+
             (RPC: Plus Plug S, Plug S Gen3, 1PM ...). No login support yet.
    tasmota  Anything running Tasmota (Sonoff, Athom, Nous, Gosund ...), HTTP API.
    tuya     Tuya-based plugs on their stock firmware (Arlec Grid Connect, Smart
             Life, Brilliant, Deta ...) over the local network with tinytuya. Each
             needs its device ID and local key, fetched once from Tuya's cloud
             (python -m tinytuya wizard); after that no cloud is involved.
    sim      A pretend plug for testing (only offered when COFFEE_SIM=1).

Every driver reports is_on, and watts when the plug measures power. Each can
also set the plug's own auto-off as a hardware backstop for the grinder, so a
crashed app can't leave the motor running.
"""
import asyncio
import ipaddress
import logging
import os
import random
import time

import aiohttp
import tinytuya
from kasa import Credentials, Device, DeviceConfig, Discover, Module

log = logging.getLogger("coffee.plugs")
TAPO_USER = os.environ.get("TAPO_USERNAME", "")
TAPO_PASS = os.environ.get("TAPO_PASSWORD", "")
SIM = os.environ.get("COFFEE_SIM") == "1"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=5)

DRIVERS = {
    "tapo": "TP-Link Tapo / Kasa",
    "shelly": "Shelly",
    "tasmota": "Tasmota",
    "tuya": "Tuya / Arlec Grid Connect / Smart Life",
}
EXTRA_FIELDS = {"tuya": ("device_id", "local_key", "version", "dps")}  # beyond driver + host
if SIM:
    DRIVERS["sim"] = "Simulated (testing)"


class Plug:
    """Base: is_on/watts/model are filled in by update()."""
    driver = "?"

    def __init__(self, host):
        self.host = host.strip()
        self.is_on: bool | None = None
        self.watts: float | None = None
        self.volts: float | None = None
        self.model = ""
        self.metered: bool | None = None  # unknown until the first reading
        self.on_since: float | None = None  # when the plug says it was switched on, if it knows
        self.updated = 0.0
        self.key = None  # set by make(): the settings this connection was made from
        self._lock = asyncio.Lock()

    async def update(self, max_age=0.0):
        """Read the plug, unless it was read less than max_age seconds ago."""
        async with self._lock:
            if max_age and time.time() - self.updated < max_age:
                return self
            await self._update()
            self.updated = time.time()
            return self

    async def turn(self, on: bool):
        async with self._lock:
            await self._turn(on)
            await self._update()
            self.updated = time.time()

    async def set_backstop(self, seconds: int | None) -> str:
        """Ask the plug to switch itself off this long after it's turned on (None = clear).
        Returns a short description of what was set, or raises if unsupported."""
        async with self._lock:
            return await self._backstop(seconds)

    def info(self):
        return {"driver": self.driver, "host": self.host, "model": self.model, "on": self.is_on,
                "metered": self.metered, "watts": None if self.watts is None else round(self.watts, 1),
                "volts": None if self.volts is None else round(self.volts, 1)}

    async def close(self):
        pass

    # subclasses
    async def _update(self): raise NotImplementedError
    async def _turn(self, on): raise NotImplementedError
    async def _backstop(self, seconds): raise RuntimeError("this plug has no auto-off setting")


# --- TP-Link Tapo / Kasa --------------------------------------------------------
class KasaPlug(Plug):
    driver = "tapo"

    def __init__(self, host):
        super().__init__(host)
        self.dev: Device | None = None

    async def _connect(self):
        creds = Credentials(TAPO_USER, TAPO_PASS) if TAPO_USER and TAPO_PASS else None
        try:
            dev = await Discover.discover_single(self.host, credentials=creds, discovery_timeout=3)
        except Exception as first:
            if not creds:
                raise RuntimeError("TP-Link login not set (secret tapo-credentials)") from first
            # UDP discovery can be filtered; fall back to the Tapo plug's usual transport.
            cfg = DeviceConfig.from_dict({
                "host": self.host,
                "connection_type": {"device_family": "SMART.TAPOPLUG", "encryption_type": "KLAP", "login_version": 2},
            })
            cfg.credentials = creds
            dev = await Device.connect(config=cfg)
        if dev is None:
            raise RuntimeError("no TP-Link plug answered at " + self.host)
        self.dev = dev

    async def _update(self):
        try:
            if self.dev is None:
                await self._connect()
            await self.dev.update()
        except Exception:
            await self.close()
            raise
        d = self.dev
        self.is_on = d.is_on
        self.model = d.model or "TP-Link plug"
        energy = d.modules.get(Module.Energy)
        self.metered = energy is not None
        self.watts = energy.current_consumption if energy else None
        self.volts = getattr(energy, "voltage", None) if energy else None
        since = getattr(d, "on_since", None) if d.is_on else None
        self.on_since = since.timestamp() if since else None

    async def _turn(self, on):
        if self.dev is None:
            await self._connect()
        try:
            await (self.dev.turn_on() if on else self.dev.turn_off())
        except Exception:
            await self.close()
            raise

    async def _backstop(self, seconds):
        if self.dev is None:
            await self._connect()
            await self.dev.update()
        auto = self.dev.modules.get(Module.AutoOff)
        if auto is None:
            raise RuntimeError(f"{self.model or 'this plug'} has no auto-off")
        if seconds is None:
            await auto.set_enabled(False)
            return "auto-off cleared"
        minutes = max(1, -(-int(seconds) // 60))  # Tapo counts whole minutes
        await auto.set_delay(minutes)
        await auto.set_enabled(True)
        return f"plug auto-off after {minutes} min"

    async def close(self):
        if self.dev is not None:
            try:
                await self.dev.disconnect()
            except Exception:
                pass
        self.dev = None


# --- Shelly ---------------------------------------------------------------------
class ShellyPlug(Plug):
    driver = "shelly"

    def __init__(self, host):
        super().__init__(host)
        self.gen: int | None = None

    async def _get(self, path, **params):
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as s:
            async with s.get(f"http://{self.host}{path}", params=params) as r:
                if r.status == 401:
                    raise RuntimeError("Shelly asks for a password; turn its login off or use another plug")
                r.raise_for_status()
                return await r.json(content_type=None)

    async def _detect(self):
        if self.gen is None:
            info = await self._get("/shelly")
            self.gen = int(info.get("gen") or 1)
            self.model = info.get("model") or info.get("type") or "Shelly"

    async def _update(self):
        await self._detect()
        if self.gen >= 2:
            st = await self._get("/rpc/Switch.GetStatus", id=0)
            self.is_on = bool(st.get("output"))
            self.watts = st.get("apower")
            self.volts = st.get("voltage")
        else:
            st = await self._get("/status")
            relay = (st.get("relays") or [{}])[0]
            self.is_on = bool(relay.get("ison"))
            meter = (st.get("meters") or [{}])[0]
            self.watts = meter.get("power")
            self.volts = None
        self.metered = self.watts is not None

    async def _turn(self, on):
        await self._detect()
        if self.gen >= 2:
            await self._get("/rpc/Switch.Set", id=0, on="true" if on else "false")
        else:
            await self._get("/relay/0", turn="on" if on else "off")

    async def _backstop(self, seconds):
        await self._detect()
        if self.gen >= 2:
            cfg = ({"auto_off": False} if seconds is None else {"auto_off": True, "auto_off_delay": float(seconds)})
            import json
            await self._get("/rpc/Switch.SetConfig", id=0, config=json.dumps(cfg))
        else:
            await self._get("/settings/relay/0", auto_off=0 if seconds is None else int(seconds))
        return "auto-off cleared" if seconds is None else f"plug auto-off after {int(seconds)} s"


# --- Tasmota --------------------------------------------------------------------
class TasmotaPlug(Plug):
    driver = "tasmota"

    async def _cmd(self, cmnd):
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as s:
            async with s.get(f"http://{self.host}/cm", params={"cmnd": cmnd}) as r:
                if r.status == 401:
                    raise RuntimeError("Tasmota asks for a password; clear its web password or use another plug")
                r.raise_for_status()
                return await r.json(content_type=None)

    async def _update(self):
        st = await self._cmd("Status 0")
        status = st.get("Status", {})
        self.model = status.get("DeviceName") or status.get("FriendlyName", ["Tasmota"])[0] or "Tasmota"
        power = st.get("StatusSTS", {}).get("POWER", st.get("StatusSTS", {}).get("POWER1"))
        self.is_on = power == "ON" if power is not None else None
        energy = st.get("StatusSNS", {}).get("ENERGY")
        self.metered = energy is not None
        self.watts = energy.get("Power") if energy else None
        self.volts = energy.get("Voltage") if energy else None

    async def _turn(self, on):
        await self._cmd("Power " + ("On" if on else "Off"))

    async def _backstop(self, seconds):
        if seconds is None:
            await self._cmd("PulseTime 0")
            return "auto-off cleared"
        s = int(seconds)
        await self._cmd(f"PulseTime {s * 10 if s <= 11 else s + 100}")  # 1-111 = tenths, 112+ = seconds + 100
        return f"plug auto-off after {s} s"


# --- Tuya (stock firmware, local protocol) ------------------------------------------
TUYA_DPS = {"switch": 1, "countdown": 9, "current_ma": 18, "power_dw": 19, "voltage_dv": 20}  # usual plug layout


class TuyaPlug(Plug):
    """Talks to the plug directly on port 6668, encrypted with its local key."""
    driver = "tuya"
    VERSIONS = (3.3, 3.4, 3.5, 3.1)

    def __init__(self, host, device_id="", local_key="", version="auto", dps=None):
        super().__init__(host)
        self.device_id = str(device_id or "").strip()
        self.local_key = str(local_key or "")
        self.version = str(version or "auto")
        self.dps_map = {**TUYA_DPS, **(dps or {})}
        self.dev = None
        self.proto = None
        self.dps: dict = {}  # last value seen for each data point
        self.countdown_s: int | None = None

    def _make(self, v):
        d = tinytuya.OutletDevice(self.device_id, self.host, self.local_key, version=v, connection_timeout=3,
                                  connection_retry_limit=1, connection_retry_delay=1)
        d.set_socketPersistent(True)
        return d

    @staticmethod
    def _err(reply):
        if not isinstance(reply, dict):
            return None
        if reply.get("Err") == "914" or "decrypt" in str(reply.get("Error", "")).lower():
            return "the plug didn't accept that device ID / local key (re-paired? the key changes)"
        return reply.get("Error")

    async def _status(self):
        if not (self.device_id and self.local_key):
            raise RuntimeError("Tuya plugs need their device ID and local key (Settings)")
        if self.dev is None:
            versions = self.VERSIONS if self.version == "auto" else (float(self.version),)
            last = "no answer"
            for v in versions:
                d = self._make(v)
                reply = await asyncio.to_thread(d.status)
                if isinstance(reply, dict) and "dps" in reply:
                    self.dev, self.proto = d, v
                    return reply
                last = self._err(reply) or "no answer"
                await asyncio.to_thread(d.close)
                if reply and reply.get("Err") == "901":
                    break  # nothing listening: trying other versions won't help
            raise RuntimeError(f"Tuya plug at {self.host}: {last}")
        dp = self.dps_map
        # Ask for fresh power readings first (most plugs only refresh them now and then).
        # The plug answers that on the same socket, possibly after we've asked for status,
        # so a reply can hold only some data points: merge what comes back and keep
        # reading until one includes the switch, or the on/off state would be stale.
        await asyncio.to_thread(self.dev.updatedps, [dp["current_ma"], dp["power_dw"], dp["voltage_dv"]], True)
        merged = {}
        for _ in range(3):
            reply = await asyncio.to_thread(self.dev.status)
            err = None if isinstance(reply, dict) and "dps" in reply else (self._err(reply) or "no answer")
            if err:
                await self.close()
                raise RuntimeError(f"Tuya plug at {self.host}: {err}")
            merged.update({str(k): v for k, v in reply["dps"].items()})
            if str(dp["switch"]) in merged:
                break
        return {"dps": merged}

    async def _update(self):
        reply = await self._status()
        self.dps.update({str(k): v for k, v in reply["dps"].items()})
        dp = {k: str(v) for k, v in self.dps_map.items()}
        self.model = f"Tuya plug (protocol {self.proto})"
        sw = self.dps.get(dp["switch"])
        self.is_on = bool(sw) if sw is not None else None
        power = self.dps.get(dp["power_dw"])
        self.metered = power is not None
        self.watts = power / 10 if isinstance(power, (int, float)) else None
        if self.is_on is False and self.metered:
            self.watts = 0.0  # relay open: the plug's power reading lags a few seconds behind
        volts = self.dps.get(dp["voltage_dv"])
        self.volts = volts / 10 if isinstance(volts, (int, float)) and volts else None

    async def _turn(self, on):
        if self.dev is None:
            await self._update()
        dp = self.dps_map
        sw = str(dp["switch"])
        last = "no answer"
        for attempt in range(2):
            # A fresh connection for every command: on a kept-open one, a reading the plug
            # pushed earlier can be taken as the answer, and an idle connection the plug has
            # dropped swallows the command without an error.
            await asyncio.to_thread(self.dev.close)
            reply = await asyncio.to_thread(self.dev.set_value, dp["switch"], bool(on))
            got = reply.get("dps") if isinstance(reply, dict) and isinstance(reply.get("dps"), dict) else {}
            got = {str(k): v for k, v in got.items()}
            self.dps.update(got)
            if sw in got and bool(got[sw]) == bool(on):
                break  # the plug confirmed the new state
            last = self._err(reply) or f"didn't confirm switching {'on' if on else 'off'}"
        else:
            await self.close()
            raise RuntimeError(f"Tuya plug at {self.host}: {last}")
        if on and self.countdown_s and str(dp["countdown"]) in self.dps:
            # the plug's own countdown: switches itself off if coffee.lan doesn't
            await asyncio.to_thread(self.dev.set_value, dp["countdown"], int(self.countdown_s))

    async def _backstop(self, seconds):
        if self.dev is None:
            await self._update()
        if str(self.dps_map["countdown"]) not in self.dps:
            raise RuntimeError("this Tuya plug has no countdown timer")
        self.countdown_s = int(seconds) if seconds else None
        return "auto-off cleared" if seconds is None else f"plug countdown {int(seconds)} s on every grind"

    async def close(self):
        if self.dev is not None:
            try:
                await asyncio.to_thread(self.dev.close)
            except Exception:
                pass
        self.dev = None


# --- Simulated ------------------------------------------------------------------
class SimPlug(Plug):
    """A pretend plug with a pretend load, for trying the app without hardware.
    host 'machine' draws like an espresso machine (heater cycling, pump when
    asked through sim_pump), anything else like a grinder motor."""
    driver = "sim"
    pump_until = 0.0

    async def _update(self):
        if self.is_on is None:
            self.is_on = False
        self.model = "Simulated " + ("energy plug" if self.host == "machine" else "plug")
        self.metered = self.host == "machine"
        self.volts = 240.0 + random.uniform(-2, 2) if self.metered else None
        if not self.metered:
            self.watts = None
            return
        if not self.is_on:
            self.watts = 0.0
            return
        age = time.time() - (self.on_since or time.time())
        heater = age < 40 or int(age / 15) % 3 == 0  # heats flat out, then cycles
        self.watts = (1150 if heater else 4) + random.uniform(-6, 6) + (165 if time.time() < SimPlug.pump_until else 0)

    async def _turn(self, on):
        if on and not self.is_on:
            self.on_since = time.time()
        if not on:
            self.on_since = None
        self.is_on = on

    async def _backstop(self, seconds):
        return "simulated auto-off " + ("cleared" if seconds is None else f"after {seconds} s")


CLASSES = {"tapo": KasaPlug, "shelly": ShellyPlug, "tasmota": TasmotaPlug, "tuya": TuyaPlug, "sim": SimPlug}


def config_key(cfg):
    """What identifies a plug connection: a change in any of these means reconnect."""
    return tuple([cfg.get("driver"), cfg.get("host")] + [str(cfg.get(f)) for f in EXTRA_FIELDS.get(cfg.get("driver"), ())])


def make(cfg) -> Plug | None:
    driver, host = cfg.get("driver"), str(cfg.get("host") or "")
    if not host or driver not in DRIVERS:
        return None
    extra = {f: cfg[f] for f in EXTRA_FIELDS.get(driver, ()) if cfg.get(f) not in (None, "")}
    p = CLASSES[driver](host, **extra)
    p.key = config_key(cfg)
    return p


# --- Finding plugs on the network -------------------------------------------------
async def _probe_http(session, ip):
    found = []
    try:
        async with session.get(f"http://{ip}/shelly") as r:
            if r.status == 200:
                j = await r.json(content_type=None)
                if "type" in j or "gen" in j or "model" in j:
                    found.append({"driver": "shelly", "host": ip, "model": j.get("model") or j.get("type") or "Shelly",
                                  "name": j.get("name") or ""})
    except Exception:
        pass
    try:
        async with session.get(f"http://{ip}/cm", params={"cmnd": "Status"}) as r:
            if r.status == 200:
                j = await r.json(content_type=None)
                st = j.get("Status") if isinstance(j, dict) else None
                if st:
                    found.append({"driver": "tasmota", "host": ip, "model": st.get("DeviceName") or "Tasmota",
                                  "name": (st.get("FriendlyName") or [""])[0]})
    except Exception:
        pass
    return found


async def _probe_tuya(ip):
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, 6668), 0.8)
        w.close()
        return [{"driver": "tuya", "host": ip, "model": "Tuya / Grid Connect device", "name": "needs device ID + local key"}]
    except Exception:
        return []


async def _probe_kasa(ip):
    raw = {}

    def got(info):
        raw.update(info.get("discovery_response") or info)

    try:
        await Discover.discover(target=ip, discovery_timeout=1, discovery_packets=2, on_discovered_raw=got)
    except Exception:
        return []
    if not raw:
        return []
    r = raw.get("result") or raw.get("system", {}).get("get_sysinfo") or raw
    model = r.get("device_model") or r.get("model") or "TP-Link"
    if "PLUG" not in str(r.get("device_type", "PLUG")).upper() and "plug" not in str(r.get("type", "plug")).lower():
        return []
    return [{"driver": "tapo", "host": ip, "model": model, "name": r.get("alias") or r.get("mac") or ""}]


async def scan(subnet: str, skip=()):
    """Look for Tapo/Kasa, Shelly and Tasmota plugs on a /24 (a few seconds)."""
    net = ipaddress.ip_network(subnet, strict=False)
    if net.num_addresses > 256:
        raise ValueError("scan at most a /24")
    ips = [str(h) for h in net.hosts() if str(h) not in skip]
    sem = asyncio.Semaphore(64)
    out = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.5)) as s:
        async def one(ip):
            async with sem:
                res = await asyncio.gather(_probe_kasa(ip), _probe_http(s, ip), _probe_tuya(ip))
                for r in res:
                    out.extend(r)
        await asyncio.gather(*(one(ip) for ip in ips))
    if SIM:
        out += [{"driver": "sim", "host": "machine", "model": "Simulated energy plug", "name": ""},
                {"driver": "sim", "host": "grinder", "model": "Simulated plug", "name": ""}]
    return sorted(out, key=lambda d: ipaddress.ip_address(d["host"]) if d["host"][0].isdigit() else ipaddress.ip_address("0.0.0.0"))
