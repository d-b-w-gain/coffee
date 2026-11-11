"""Power analysis for a plug that measures watts (Tapo P110, Shelly PM, Tasmota).

Samples come in every couple of seconds while the machine is on. From them:

  heater   watts at or above heater_w (the element is a big, flat step)
  pump     a step of at least pump_w on top of whatever else is running: over the
           idle baseline when the heater is off, over the heater's own level when
           it's on. A rotary vane pump is a few hundred watts at most, so it sits
           well below the heater step.
  warm-up  from switch-on (cold) to the heater's first cut-out: the thermostat
           opening means the boiler has reached temperature.

Each day gets one summary row (energy, time on, heater duty and watts, cycles,
pump runs and watts, warm-ups), kept for a year. Slow drifts in those numbers
are the predictive-maintenance signal: a heater drawing less (element ageing),
warm-ups getting longer (scale), the pump drawing more or running longer per
shot (wear, scale, a choked jet).

Thresholds are guesses until there's real data; the settings page shows the
live trace so they can be set from what the machine actually does.
"""
import json
import logging
import os
import statistics
import time
from collections import deque
from datetime import datetime

log = logging.getLogger("coffee.power")
LIVE_KEEP_S = 30 * 60
DAYS_KEEP = 400
COLD_AFTER_S = 60 * 60  # off this long before switch-on counts as a cold start


class Analyzer:
    def __init__(self, path, tz, settings):
        self.path = path
        self.tz = tz
        self.settings = settings  # dict with heater_w, pump_w (read live, so edits apply at once)
        self.live = deque()  # (t, watts)
        self.days: dict[str, dict] = {}
        self.session = None  # current on-period
        self.last_off = 0.0
        self.heater_level = None  # recent watts with only the heater on
        self.base_level = None  # recent watts with nothing running
        self.pump = None  # current pump run
        self.heater_on = False
        self.last = None
        self.load()

    # -- persistence --
    def load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.days = data.get("days", {})
            self.last_off = data.get("last_off", 0.0)
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("maintenance log unreadable, starting fresh: %s", e)

    def save(self):
        keep = sorted(self.days)[-DAYS_KEEP:]
        self.days = {k: self.days[k] for k in keep}
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"days": self.days, "last_off": self.last_off}, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("couldn't save maintenance log: %s", e)

    def day(self, t):
        key = datetime.fromtimestamp(t, self.tz).date().isoformat()
        return self.days.setdefault(key, {
            "on_s": 0.0, "wh": 0.0, "heater_s": 0.0, "heater_wsum": 0.0, "heater_n": 0, "cycles": 0,
            "pump_runs": 0, "pump_s": 0.0, "pump_wsum": 0.0, "pump_n": 0, "warmups": [], "peak_w": 0.0,
            "volts_sum": 0.0, "volts_n": 0, "grinds": 0, "grind_s": 0.0, "grind_g": 0.0, "grind_wsum": 0.0, "grind_n": 0})

    # -- input --
    def add(self, t, watts, volts=None, is_on=True):
        """One reading from the machine's plug."""
        heater_w = float(self.settings.get("heater_w", 600))
        pump_w = float(self.settings.get("pump_w", 60))
        self.live.append((round(t, 1), None if watts is None else round(watts, 1)))
        while self.live and self.live[0][0] < t - LIVE_KEEP_S:
            self.live.popleft()
        if watts is None:
            return
        d = self.day(t)
        if self.last is not None:
            dt = min(t - self.last[0], 30.0)  # don't integrate across a gap in sampling
            d["wh"] += self.last[1] * dt / 3600
            if self.last[2]:
                d["on_s"] += dt
                if self.heater_on:
                    d["heater_s"] += dt
        self.last = (t, watts, is_on)
        d["peak_w"] = max(d["peak_w"], watts)
        if volts:
            d["volts_sum"] += volts
            d["volts_n"] += 1

        if not is_on:
            if self.session:
                self.end_session(t)
            return
        if not self.session:
            self.session = {"start": t, "cold": t - self.last_off >= COLD_AFTER_S if self.last_off else True,
                            "warm_at": None, "heater_seen": False}

        heater = watts >= heater_w
        if heater != self.heater_on:
            self.heater_on = heater
            if heater:
                d["cycles"] += 1
                self.session["heater_seen"] = True
            elif self.session and self.session["warm_at"] is None:
                self.session["warm_at"] = t
                if self.session["cold"]:
                    d["warmups"].append(round(t - self.session["start"]))
                    log.info("warm-up measured: %d s", t - self.session["start"])
        # levels to measure the pump against
        ref = self.heater_level if heater else self.base_level
        pumping = ref is not None and watts - ref >= pump_w
        if heater and not pumping:
            self.heater_level = watts if self.heater_level is None else 0.8 * self.heater_level + 0.2 * watts
            d["heater_wsum"] += watts
            d["heater_n"] += 1
        elif not heater and not pumping:
            self.base_level = watts if self.base_level is None else 0.8 * self.base_level + 0.2 * watts
        if pumping:
            extra = watts - ref
            if self.pump is None:
                self.pump = {"start": t, "wsum": 0.0, "n": 0}
            self.pump["wsum"] += extra
            self.pump["n"] += 1
            self.pump["last"] = t
        elif self.pump is not None:
            self.end_pump(d)

    def end_pump(self, d):
        p, self.pump = self.pump, None
        dur = p["last"] - p["start"] + 2  # one sample period
        if dur >= 3:  # ignore blips
            d["pump_runs"] += 1
            d["pump_s"] += dur
            d["pump_wsum"] += p["wsum"]
            d["pump_n"] += p["n"]

    def end_session(self, t):
        if self.pump is not None:
            self.end_pump(self.day(t))
        self.session = None
        self.heater_on = False
        self.last_off = t
        self.save()

    def grinder_run(self, t, seconds, grams, mean_w=None):
        d = self.day(t)
        d["grinds"] += 1
        d["grind_s"] += seconds
        d["grind_g"] += grams
        if mean_w:
            d["grind_wsum"] += mean_w
            d["grind_n"] += 1
        self.save()

    # -- output --
    def warm(self):
        """(ready, seconds since switch-on) for the current session, if measured."""
        if not self.session:
            return None
        s = self.session
        # Already hot: switched on and the heater hasn't needed to come on at all.
        hot = not s["heater_seen"] and time.time() - s["start"] > 120
        return {"ready": s["warm_at"] is not None or hot, "since": s["start"]}

    def typical_warmup_s(self):
        w = [x for k in sorted(self.days)[-60:] for x in self.days[k]["warmups"]]
        return statistics.median(w[-10:]) if len(w) >= 3 else None

    def live_view(self, minutes):
        cut = time.time() - minutes * 60
        return [[t, w] for t, w in self.live if t >= cut]

    def daily_view(self):
        rows = []
        for k in sorted(self.days):
            d = self.days[k]
            rows.append({
                "date": k, "on_min": round(d["on_s"] / 60), "kwh": round(d["wh"] / 1000, 3),
                "heater_duty": round(100 * d["heater_s"] / d["on_s"]) if d["on_s"] > 60 else None,
                "heater_w": round(d["heater_wsum"] / d["heater_n"]) if d["heater_n"] else None,
                "cycles": d["cycles"], "pump_runs": d["pump_runs"],
                "pump_s_avg": round(d["pump_s"] / d["pump_runs"], 1) if d["pump_runs"] else None,
                "pump_w": round(d["pump_wsum"] / d["pump_n"]) if d["pump_n"] else None,
                "warmup_min": round(statistics.median(d["warmups"]) / 60, 1) if d["warmups"] else None,
                "volts": round(d["volts_sum"] / d["volts_n"], 1) if d["volts_n"] else None,
                "peak_w": round(d["peak_w"]),
                "grinds": d["grinds"], "grind_s": round(d["grind_s"]), "grind_g": round(d["grind_g"]),
                "grind_w": round(d["grind_wsum"] / d["grind_n"]) if d["grind_n"] else None,
            })
        return rows

    def trends(self):
        """Last 7 days against the 30 days before them, for the numbers that drift with wear."""
        rows = self.daily_view()
        recent, base = rows[-7:], rows[-37:-7]
        out = []
        checks = [("heater_w", "Heater power", "W", -0.04, "lower heater draw can mean an ageing element"),
                  ("warmup_min", "Warm-up from cold", "min", 0.15, "longer warm-ups often mean scale in the boiler"),
                  ("pump_w", "Pump power", "W", 0.12, "a pump working harder can mean scale, a choked jet or vane wear"),
                  ("pump_s_avg", "Pump time per run", "s", 0.2, "longer pump runs per shot can mean a slower flow"),
                  ("grind_w", "Grinder power", "W", 0.12, "a grinder working harder can mean dull burrs")]
        for key, name, unit, limit, why in checks:
            r = [x[key] for x in recent if x[key] is not None]
            b = [x[key] for x in base if x[key] is not None]
            if len(r) < 3 or len(b) < 7:
                continue
            now, then = statistics.median(r), statistics.median(b)
            change = (now - then) / then if then else 0
            flag = change <= limit if limit < 0 else change >= limit
            out.append({"key": key, "name": name, "unit": unit, "now": now, "before": then,
                        "change_pct": round(100 * change, 1), "flag": flag, "why": why if flag else ""})
        return out
