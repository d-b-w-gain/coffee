"""Drive the coffee app with simulated plugs (COFFEE_SIM=1) through its API."""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
fails = []


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


for _ in range(30):
    try:
        if call("GET", "/healthz")[0] == 200:
            break
    except Exception:
        time.sleep(0.5)

code, s = call("GET", "/api/settings")
check("settings default mode machine", s["mode"] == "machine", s["mode"])
check("sim driver offered", "sim" in s["drivers"])
code, s = call("POST", "/api/settings", {"plugs": {"machine": {"driver": "sim", "host": "machine"},
                                                    "grinder": {"driver": "sim", "host": "machine"}},
                                          "mode": "machine+grinder"})
check("same plug for both rejected", code == 400, s.get("error"))
code, s = call("POST", "/api/settings", {"mode": "bogus"})
check("bad mode rejected", code == 400)
code, s = call("POST", "/api/settings", {"grinder": {"max_run_s": 80}})
check("max run above plate rejected", code == 400, s.get("error"))
code, s = call("POST", "/api/settings", {"plugs": {"machine": {"driver": "sim", "host": "machine"},
                                                    "grinder": {"driver": "sim", "host": "grinder"}},
                                          "mode": "machine+grinder", "machine": {"sample_s": 1, "heater_w": 600, "pump_w": 60}})
check("settings saved", code == 200 and s["mode"] == "machine+grinder", s.get("error"))

code, t = call("POST", "/api/plugs/test", {"driver": "sim", "host": "machine"})
check("plug test reports metering", code == 200 and t["metered"] is True, t)
code, sc = call("POST", "/api/plugs/scan?subnet=127.0.0.0/30")
check("scan returns sim plugs", code == 200 and any(f["driver"] == "sim" for f in sc["found"]), sc)

code, st = call("POST", "/api/on?minutes=10")
check("machine on", code == 200 and st["on"] is True, st.get("error"))
check("status has grinder", st["grinder"]["enabled"] is True)
time.sleep(5)
code, pw = call("GET", "/api/power?minutes=5")
check("power samples arriving", pw["metered"] and len(pw["samples"]) >= 3, len(pw.get("samples", [])))
check("heater detected", pw["heater_on"] is True, pw["samples"][-1])
call("POST", "/api/sim/pump?s=6")
time.sleep(3)
code, pw = call("GET", "/api/power?minutes=5")
check("pump detected on top of heater", pw["pumping"] is True, pw["samples"][-3:])
time.sleep(5)

# grinder
code, g = call("POST", "/api/grinder/grind")
check("grind without dose refused", code == 409, g.get("error"))
code, g = call("POST", "/api/grinder/teach", {"beans": "Regular"})
check("teach starts", code == 200 and g["run"] and g["run"]["why"] == "teach", g.get("error"))
time.sleep(3)
code, g = call("POST", "/api/grinder/stop")
timed = (g.get("result") or {}).get("timed")
check("teach saved a grind time", timed and 2.5 <= timed <= 4.0, g.get("result"))
check("resting after run", g["rest_s"] >= 2, g["rest_s"])
code, g = call("POST", "/api/grinder/teach", {"beans": "Regular"})
check("run during rest refused", code == 409 and "resting" in g["error"], g.get("error"))
code, g = call("POST", "/api/grinder/load", {"beans": "Decaf", "grams": 18})
check("dose loaded, plug off", code == 200 and g["dose"]["status"] == "loaded" and g["on"] is False, g.get("error"))
check("decaf falls back to regular time + extra", g["grind_s"] == round(timed + 3, 1), g["grind_s"])
time.sleep(max(g["rest_s"], 0) + 1)
code, g = call("POST", "/api/grinder/grind")
check("grind starts", code == 200 and g["run"]["why"] == "grind", g.get("error"))
check("grinder on while grinding", g["on"] is True)
time.sleep(g["run"]["of"] + 1.5)
code, g = call("GET", "/api/grinder")
check("grind finished by timer", g["run"] is None and g["on"] is False, g)
check("dose now ground", g["dose"] and g["dose"]["status"] == "ground", g["dose"])
check("totals counted", g["totals"]["runs"] == 2 and g["totals"]["grams"] == 18, g["totals"])
code, g = call("POST", "/api/grinder/clear")
check("dose cleared", g["dose"] is None)
code, m = call("GET", "/api/maintenance")
check("maintenance has today", m["days"] and m["days"][-1]["grinds"] == 2, m["days"][-1] if m["days"] else None)
check("pump run counted", m["days"][-1]["pump_runs"] >= 1, m["days"][-1])
code, st = call("POST", "/api/off")
check("machine off", st["on"] is False)
code, st = call("POST", "/api/settings", {"mode": "machine"})
code, st = call("GET", "/api/status")
check("grinder hidden in machine-only mode", st["grinder"]["enabled"] is False)
print("\nFAILED: " + ", ".join(fails) if fails else "\nALL PASSED")
sys.exit(1 if fails else 0)
