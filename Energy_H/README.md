# Grid Self-Healing Agent — IEEE 123 + GridGuard feature parity

An LLM-in-the-loop grid operations agent (DeepSeek + pandapower) that
detects N-1 contingency overloads and autonomously issues corrective
actions to restore security. Originally an E.ON Hackathon agent on
`case118`, now running on the **IEEE 123-node distribution feeder** with
the full operational-data feature set ported from GridGuard.

## Quick start

```bash
pip install -r requirements.txt
export DEEPSEEK_API_KEY=sk-...        # (or put it in ./.env)

# Web console — interactive one-line diagram + agents
python webapp.py                      # -> http://localhost:8051

# CLI: LLM agent resolves the worst N-1 under a heatwave
python main.py --non-interactive --load-scale 1.3 --weather heatwave --hour 16

# CLI: LLM Agent vs LP Baseline (SCOPF-lite) head-to-head
python main.py --benchmark --load-scale 1.3 --weather heatwave --hour 16

# build / inspect the feeder alone
python ieee123.py
```

## Web console

`python webapp.py` serves an interactive control room at
**http://localhost:8051** (Flask). Set `DEEPSEEK_API_KEY` (shell or `.env`)
before starting for the LLM button.

- **Scenario builder** — load multiplier, weather profile, start hour.
- **Live feeder map** — the IEEE 123 one-line diagram (real bus
  coordinates): lines coloured by loading (green / amber / red), open
  lines dashed, faulted ⚡, maintenance 🔧, derated ⚠, substation ★,
  rooftop/utility PV ☀, wind 🌀, batteries 🔋 with live state of charge.
  Hover any element for SCADA flow (MW/MVAR), health and ratings.
- **Weather strip** — the 12-hour forecast (temperature, wind, solar/wind
  MW, demand factor, balancing price) with the current hour highlighted,
  plus the asset-health summary.
- **Operate** — run the two-stage N-1 screen, trip the worst line, step
  the clock `⏩ +1 h`, run the **LP baseline**, run the **DeepSeek agent**
  (its reasoning streams live into the control-room log over SSE), or
  apply a manual action (discharge/charge BESS, curtail renewable/load).

The backend exposes the same operations as JSON: `POST /api/scenario`,
`/api/screen/<sid>`, `/api/fault/<sid>`, `/api/advance/<sid>`,
`/api/apply/<sid>`, `/api/baseline/<sid>`, `GET /api/scada/<sid>` and the
SSE stream `GET /api/llm/<sid>`.

CLI flags: `--network ieee123|case118|case300|…`, `--load-scale`,
`--weather clear|heatwave|overcast|storm|none`, `--hour 0-23`,
`--no-der`, `--line N` (trip a specific line), `--benchmark`,
`--non-interactive`.

## The IEEE 123 grid

`ieee123.py` builds the feeder from the official EPRI OpenDSS test-case
files (vendored in `data/ieee123/`): 130 buses, ~3.5 MW over 85 load
points at 4.16 kV. Phase impedance matrices are reduced to positive
sequence (balanced approximation); single-phase laterals keep their
per-phase rating; the four LDC voltage regulators and the service
transformer become near-ideal links with the substation held at 1.045 pu
to stand in for their boost. The normally-open 3-phase tie (Sw7,
151–300) is closed at startup to mesh the backbone, so backbone N-1
outages become survivable rather than islanding the whole feeder.

## What was added (GridGuard feature parity)

| Feature | Before | Now |
|---------|--------|-----|
| **Grid** | `case118` transmission | **IEEE 123** distribution feeder (+ any pandapower case) |
| **N-1 screening** | brute-force AC on every line (~130 solves) | **two-stage**: DC screen all, AC only the dangerous shortlist; radial laterals reported as **islanding** (unserved kW) not solved |
| **Renewables** | none | rooftop PV, utility PV, **wind farms** (turbine power curve, 25 m/s **cut-out**) — sized to the feeder load |
| **Weather** | none | 12-hour forecast (clear / heatwave / overcast / storm): irradiance→PV, wind speed→wind, temperature→demand |
| **Market** | random EUR/MWh draw | **day-ahead + balancing prices**; redispatch charged at the live balancing price |
| **Storage** | fixed 2 MW VPP BESS, discharge only | DER-sized BESS with **state of charge**, discharge **and charge**, SoC-bounded |
| **SCADA** | none | `get_scada_measurements`: real-time MW/MVAR/kA per line, bus V/angle |
| **Forecasts** | none | hourly load / solar / wind forecasts in MW |
| **Asset health** | none | per-line health index with thermal **derating**; lowest-loaded line scheduled for maintenance |
| **Voltage** | thermal only | bus voltage-violation checks against a per-grid band |
| **Time** | static snapshot | `advance_hour()` steps the forecast; BESS integrates SoC |
| **Actions** | redispatch, curtail_load, discharge_battery | + **charge_battery**, **curtail_renewable** (solar/wind spill) |
| **LP baseline** | gen + load only (no levers on a gen-less feeder) | gen + **BESS** + **renewable spill** + load, thresholds scaled to feeder size |

All feeds are surfaced to the LLM through `GridEnvironment.context_block()`
(weather, forecast, prices, BESS state, asset health) and the expanded
action space, so the agent plans against what is *coming* (sunset, storm
cut-out, rising demand/prices) rather than only the instantaneous state.

## Module map

```
ieee123.py            OpenDSS -> pandapower builder for the IEEE 123 feeder
weather.py            seeded forecasts; irradiance/wind/turbine-curve/demand
market.py             day-ahead + balancing prices
scenario.py           DER placement + weather + asset-health pipeline
grid_environment.py   physics engine: two-stage N-1 screen, action space,
                      execution, SCADA / asset-health / forecast feeds,
                      advance_hour, context_block
grid_agent.py         DeepSeek LLM brain (context-aware prompt)
env.py                GridEnv adapter exposing the SCOPF-lite baseline API
baseline.py           LP corrective/preventive dispatch (gen+BESS+spill+shed)
main.py               two-stage control loop + benchmark mode
webapp.py             Flask web console (one-line diagram + agents)
static/index.html     console front-end (SVG feeder map, SSE log)
data/ieee123/         vendored EPRI OpenDSS test-case files
```

## Example benchmark (IEEE 123, load ×1.3, heatwave @ 16:00)

Worst N-1 (line L13 out) leaves 12 lines overloaded to 136% and 49
undervoltage buses. Both agents restore base + N-1 security:

| Metric | LLM Agent | LP Baseline |
|--------|-----------|-------------|
| Base + N-1 secure | ✅ | ✅ |
| Cost (EUR/h) | ~2,700 | ~2,570 |
| Actions | 8 | 10 |

The LLM lands within a few percent of the LP optimum while choosing
batteries first (cheapest MW) and shedding load only where needed —
exactly the doctrine the context block encodes.
