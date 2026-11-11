# ☕ Coffee Detective

A small web app that runs a home espresso machine (a Bezzera Minibar) through a
**Tapo P100** smart plug. People on the home network can warm the machine up,
order a coffee that reaches the barista on Signal, and rate it afterwards.

It's one Python file ([aiohttp](https://docs.aiohttp.org/) +
[python-kasa](https://github.com/python-kasa/python-kasa)) plus three plain HTML
pages. It runs as a single pod on k3s.

## What it does

- **Power control with safety timers.** Every "on" comes with an auto-off
  (30 min by default, at most 60). If the plug is found on with no timer
  running, for example after someone presses the plug's button, the app
  switches it off after 60 minutes.
- **Weekly schedule.** On/off windows in local time (`Australia/Sydney`).
  Inside a window, timers only ever get longer. A manual Off holds until the
  next window. A 24-hour power history records every on/off change and why
  it happened.
- **Warm-up countdown.** The page draws the machine as an inline SVG. The
  power lamp and pressure gauge follow the plug state and an estimated
  20-minute warm-up.
- **Coffee orders over Signal.** Pick a drink, size, milk, beans, shots, sugar
  and cup. The order goes to the barista on Signal (via a
  [signal-cli REST API](https://github.com/bbernhard/signal-cli-rest-api))
  with a random G-rated Giphy GIF, and can warm the machine up too. Each order
  has its own page (`/o/<id>`) where it is marked Made or Delivered. If an
  order hasn't been started after 5 and 15 minutes, the barista gets a phone
  call (optional).
- **Customer confirmations.** Customers can give their Signal number to get a
  confirmation and a "ready" message. The number is kept only in that
  browser and in the active order.
- **Stock tracking.** Beans and milk go down by one 18 g basket plus the milk
  for the drink size each time a coffee is marked made. The barista gets a
  "running low" alert on Signal.
- **Ratings.** Drinkers rate each coffee 1–5 🫘 with tags (`/r/<id>`). The
  barista reviews customers by tapping badges. The server keeps only
  anonymous totals (the "case file").

Privacy: order history lives in each browser (`localStorage`). The server keeps
only active orders (up to 24 h) and anonymous totals.

- **Grinder (optional).** In "Machine and grinder" mode a second plug runs the
  grinder (a single-dose Mazzer Mini here). Load a dose and tap "I've loaded a
  dose": the plug switches off so the grinder's own switch can be left on. Each
  grind is then a timed run, using the grind time measured with "Time it" plus a
  few extra seconds. No run is longer than the motor's rating (50 s on / 50 s
  off), each run is followed by an equal rest, and a grinder found running
  outside a run is switched off. The plug's own auto-off is set as a backstop.
- **Order ↔ dose matching.** A new order is checked against the loaded dose.
  A match flags the Signal message "🚨 READY TO GO", and can grind straight away
  and have Spruik phone at once (both optional); a mismatch is warned about.
- **Any common smart plug.** Tapo/Kasa (python-kasa), Shelly (Gen1 and Gen2+)
  and Tasmota, each with Test and a network scan on the settings page.
- **Power and maintenance.** With a plug that measures power on the machine
  (Tapo P110, Shelly PM, Tasmota energy), watts are read every couple of
  seconds while it's on. The heater (big step) and pump (smaller step on top)
  are picked out, "ready" comes from the heater's first cut-out, and daily
  numbers (energy, warm-up, heater watts and duty, pump runs and watts, grinder
  runs and grams) are kept for a year. Drifts are flagged after a few weeks.

## Files

| File | What |
|---|---|
| `app.py` | Server: machine and grinder control, settings, schedule, orders, Signal, stock, ratings |
| `plugs.py` | Smart plug drivers (Tapo/Kasa, Shelly, Tasmota, simulated) and the network scan |
| `power.py` | Power analysis: heater/pump detection, measured warm-up, daily maintenance numbers |
| `settings.html` | Settings: mode, plugs, grinder tuning, live power trace, maintenance |
| `tests/` | Offline tests with simulated plugs (see Testing) |
| `index.html` | Main page: machine, order form, schedule, stock, case file |
| `order.html` | Barista's page for one order (`/o/<id>`) |
| `rate.html` | Drinker's rating page (`/r/<id>`) |
| `Dockerfile` | `python:3.12-slim` + `python-kasa==0.10.2`, runs as nobody |
| `k8s.yaml` | PVC, Deployment, Service and Ingress (namespace `home-automation`) |
| `deploy.sh` | Builds the image, imports it into k3s and rolls the deployment |

## Running it

### Locally

```sh
pip install aiohttp python-kasa==0.10.2
mkdir -p data
TAPO_HOST=192.168.x.x TAPO_USERNAME=you@example.com TAPO_PASSWORD=... \
SCHEDULE_FILE=data/schedule.json HISTORY_FILE=data/power-log.json \
ORDERS_FILE=data/active-orders.json STOCK_FILE=data/stock.json \
CASEFILE=data/casefile.json BASE_URL=http://localhost:8080 \
python app.py
```

Then open <http://localhost:8080>. Without the Signal settings, power control
and the schedule work, but ordering reports that Signal isn't set up.

### On k3s

Create the secrets in the `home-automation` namespace (all are optional except
the Tapo login), then deploy:

```sh
kubectl -n home-automation create secret generic tapo-credentials \
  --from-literal=username=you@example.com --from-literal=password=...
kubectl -n home-automation create secret generic coffee-signal \
  --from-literal=account=+61... --from-literal=recipient=+61...
kubectl -n home-automation create secret generic coffee-giphy \
  --from-literal=api-key=...
./deploy.sh
```

The Tapo login is the TP-Link account the plug is paired to. Turn on
**Third-Party Compatibility** in the Tapo app so local control works.
Disable any schedules in the Tapo app, because this app is the only scheduler.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TAPO_HOST` | `192.168.88.10` | Plug IP |
| `TAPO_USERNAME` / `TAPO_PASSWORD` | — | TP-Link account (secret `tapo-credentials`) |
| `DEFAULT_ON_MINUTES` / `MAX_ON_MINUTES` | 30 / 60 | Auto-off timer |
| `WARMUP_MINUTES` | 20 | Warm-up estimate shown on the page |
| `TZ` | `Australia/Sydney` | Schedule time zone |
| `MAX_SCHEDULE_MINUTES` | 720 | Longest schedule window |
| `SIGNAL_API_URL` | `http://signal-cli.cnc-controller.svc:8080` | signal-cli REST API |
| `SIGNAL_ACCOUNT` / `SIGNAL_RECIPIENT` | — | Sending number and barista's number (secret `coffee-signal`) |
| `BARISTA_NAME` / `BARISTA_NUMBER` | `Dan` / recipient | Shown to customers |
| `SPRUIK_ANNOUNCE_URL` | `http://signal-call-bridge…/announce` | Phone-call nudges (optional) |
| `NUDGE_AFTER_MINUTES` | `5,15` | When to call about unstarted orders |
| `GIPHY_API_KEY` | — | GIFs in messages (secret `coffee-giphy`, optional) |
| `BASKET_GRAMS` | 18 | Beans used per coffee |
| `BASE_URL` | `http://coffee.lan` | Used in links sent over Signal |
| `SETTINGS_FILE`, `GRINDER_FILE`, `MAINTENANCE_FILE` | `/data/…json` | Settings, grinder state, daily power numbers |
| `SCAN_SUBNET` | from the plug addresses | /24 to search for plugs |
| `COFFEE_SIM` | — | `1` offers simulated plugs (testing only) |

`TAPO_HOST` only seeds the machine's plug; after that the settings page decides
which plug does what.
| `SCHEDULE_FILE`, `HISTORY_FILE`, `ORDERS_FILE`, `STOCK_FILE`, `CASEFILE` | `/data/…json` | State files (on the PVC) |

## API

| Method | Path | |
|---|---|---|
| GET | `/api/status` | Plug state, timer, warm-up, next scheduled window |
| POST | `/api/on?minutes=N` | Switch on (1–60 min, default 30) |
| POST | `/api/off` | Switch off |
| GET / POST | `/api/schedule` | Read / replace the weekly schedule |
| GET | `/api/history?hours=24` | On/off changes with reasons |
| GET | `/api/menu` | Drinks, sizes, milks, beans, cups |
| POST | `/api/request` | Place an order (sends to Signal) |
| GET | `/api/order/{id}` | One active order |
| POST | `/api/order/{id}/made`, `/delivered` | Barista updates |
| POST | `/api/order/{id}/rate`, `/review` | Drinker rating / barista review |
| GET | `/api/stock` | Stock levels |
| POST | `/api/stock/{item}`, `/api/stock/log` | Set stock / log a coffee made off-app |
| GET | `/api/casefile` | Anonymous rating totals |
| GET / POST | `/api/settings` | Mode, plugs, grinder and machine settings |
| POST | `/api/plugs/test`, `/api/plugs/scan` | Try a plug; look for plugs on the network |
| GET | `/api/grinder` | Grinder state: dose, run, rest, totals |
| POST | `/api/grinder/load`, `/grind`, `/stop`, `/clear` | Single-dose workflow |
| POST | `/api/grinder/teach` | Time a grind (stop it when empty) |
| GET | `/api/power?minutes=10` | Live watts from a power-measuring plug |
| GET | `/api/maintenance` | Daily numbers and trend flags |
| GET | `/healthz` | Liveness |

## Testing

The tests run the app against simulated plugs and a fake Signal, in the image:

```sh
docker build -t coffee-dev .
docker run --rm -v "$PWD:/app:ro" -v "$PWD/tests:/t:ro" -w /tmp coffee-dev python /t/grinder_order_test.py
# the API walk-through needs a running server with COFFEE_SIM=1:
docker run -d --name coffee-sim -e COFFEE_SIM=1 -e SETTINGS_FILE=/tmp/s.json -e GRINDER_FILE=/tmp/g.json \
  -e MAINTENANCE_FILE=/tmp/m.json -e SCHEDULE_FILE=/tmp/sc.json -e HISTORY_FILE=/tmp/h.json \
  -e ORDERS_FILE=/tmp/o.json -e STOCK_FILE=/tmp/st.json -e CASEFILE=/tmp/c.json \
  -v "$PWD:/app:ro" -v "$PWD/tests:/t:ro" coffee-dev
docker exec coffee-sim python /t/sim_api_test.py
```

## Safety note

The machine's old plug showed signs of heat discolouration, which is why the
app never leaves the machine on without a timer. If you adapt this for your
own machine, keep the auto-off.
