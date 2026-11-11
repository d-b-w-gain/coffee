# Offline test of shot detection from power samples, shaped like the real Minibar trace:
# idle 4 W, pump ~220 W on top, heater ~1230 W, the plug averaging over transitions.
import os, sys, tempfile
from zoneinfo import ZoneInfo
sys.path.insert(0, "/app")
from power import Analyzer, TASTES

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


a = Analyzer(tempfile.mkdtemp() + "/m.json", ZoneInfo("Australia/Sydney"), {"heater_w": 700, "pump_w": 120})
t = 1_800_000_000.0


def run(watts, seconds):
    global t
    for _ in range(int(seconds // 2)):
        a.add(t, watts, 236.0, True)
        t += 2


run(1230, 240)   # warm-up from cold: heater only
run(4, 600)      # hot, idle 10 min
run(225, 6)      # purge, 6 s
run(4, 30)       # 30 s gap
run(225, 6)      # shot starts on the pump alone
run(1455, 12)    #   heater kicks in (cold water into the boiler): heater + pump
run(840, 2)      #   one sample caught mid-way as the heater switches off
run(226, 12)     #   pump alone to the end of the shot (30+ s in all)
run(4, 20)       # done
run(1240, 30)    # heater recovers
run(212, 10)     # hot water drawn for the long black: the boiler refills
run(4, 1200)     # sits 20 min
run(606, 4)      # the plug's reading as the heater switches on (between levels)
run(1236, 40)    # heater cycle
run(4, 60)
run(225, 34)     # shot with no purge
run(4, 20)

shots = a.shots
check("two shots found", len(shots) == 2, [(x["dur"], x["purged"]) for x in shots])
if len(shots) == 2:
    s1, s2 = shots
    check("first shot not split by the heater switching mid-shot", 30 <= s1["dur"] <= 36, s1["dur"])
    check("first shot purged (6 s, 30 s before)", s1["purged"] and s1["purge_s"] in (6, 7, 8) and 28 <= s1["purge_gap_s"] <= 34, s1)
    check("idle before the purge counts from switch-on (warm-up + 10 min sit)", 830 <= s1["idle_s"] <= 850 and s1["idle_from"] == "switch-on", s1)
    check("second shot not purged (heater blip isn't a purge)", not s2["purged"], s2)
    check("refill after a shot isn't a shot", s2["dur"] >= 30)
    check("idle before second shot counts from the refill", 1250 <= s2["idle_s"] <= 1400 and s2["idle_from"] == "last draw", s2)
    check("pump watts recorded", 200 <= s2["pump_w"] <= 240, s2["pump_w"])
    x = a.link_order("order-1", t)
    check("made order links to the latest shot", x is s2 and s2["order"] == "order-1")
    a.order_rated("order-1", 2, ["Too hot 🔥"])
    a.set_taste(s2["id"], TASTES[1])
    a.set_taste(s1["id"], TASTES[0])
    v = a.shots_view()
    check("order id never exposed", all("order" not in x for x in v["shots"]) and v["shots"][0]["for_order"], v["shots"][0])
    check("summary splits purged / not", v["summary"]["purged"]["good_pct"] == 100 and v["summary"]["not purged"]["good_pct"] == 0
          and v["summary"]["not purged"]["avg_rating"] == 2.0, v["summary"])
    b = Analyzer(a.path, a.tz, a.settings)
    check("shots survive a restart", len(b.shots) == 2 and b.shots[1]["taste"] == TASTES[1])
    try:
        a.set_taste(s1["id"], "Amazing"); check("bad taste refused", False)
    except ValueError:
        check("bad taste refused", True)
print("\nFAILED: " + ", ".join(fails) if fails else "\nALL PASSED")
sys.exit(1 if fails else 0)
