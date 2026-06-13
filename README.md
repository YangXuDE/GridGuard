# GridGuard — AI-Powered Grid Security & Contingency Management

> **E.ON Hackathon · Energy × AI Grid Operation Agents Track**

GridGuard is a control-room decision-support platform for distribution-grid operators. It combines a physics-accurate Python backend (pandapower + DeepSeek LLM) with an interactive React dashboard to simulate N-1 contingency events on the IEEE 123-node feeder, rank every contingency by risk score, build a corrective action space, and present the operator with an AI-recommended dispatch plan — all visualised in real time.

---

## Screenshots

### Dashboard — idle (grid secure)
![GridGuard idle state](docs/screenshots/dashboard-idle.png)

### N-1 Violation detected — Risk Screening results
![GridGuard N-1 violation](docs/screenshots/dashboard-result.png)

### Corrective Action Space & Operator Action Plan
![GridGuard action space](docs/screenshots/dashboard-actions.png)

---

## Repository Layout

```
GridGuard/
├── Energy_H/          # Python backend — pandapower physics engine + DeepSeek LLM agent
└── GridGuard AI/      # React/TypeScript frontend — interactive control-room dashboard
```

---

## Energy_H — Python Backend

### Features

| Module | Description |
|--------|-------------|
| **IEEE 123 feeder** | Built from vendored EPRI OpenDSS files; 130 buses, ~3.5 MW, 85 load points at 4.16 kV |
| **Two-stage N-1 screening** | DC power-flow screen across all lines → AC solve on the dangerous shortlist; radial laterals reported as islanding |
| **Renewables** | Rooftop PV, utility PV, wind farms (turbine power curve, 25 m/s cut-out) |
| **Weather** | 12-hour forecast (clear / heatwave / overcast / storm): irradiance → PV, wind speed → wind, temperature → demand |
| **Market** | Day-ahead + balancing prices; redispatch charged at the live balancing price |
| **BESS** | SoC-bounded discharge and charge; DER-sized |
| **SCADA** | Real-time MW / MVAR / kA per line, bus voltage and angle |
| **Asset health** | Per-line health index with thermal derating; lowest-loaded line scheduled for maintenance |
| **DeepSeek LLM agent** | Context-aware corrective loop: issues dispatch commands iteratively until N-1 security is restored |
| **LP baseline** | SCOPF-lite linear program (gen + BESS + renewable spill + load shed) for benchmark comparison |
| **Flask web console** | Live one-line diagram, scenario builder, SSE streaming of LLM reasoning |

### Quick Start

**Requirements:** Python ≥ 3.10

```bash
cd Energy_H
pip install -r requirements.txt

# Set your DeepSeek API key (or add it to .env)
export DEEPSEEK_API_KEY=sk-...

# Interactive web console → http://localhost:8051
python webapp.py

# CLI: LLM agent resolves worst N-1 under a heatwave
python main.py --non-interactive --load-scale 1.3 --weather heatwave --hour 16

# CLI: LLM Agent vs LP Baseline head-to-head benchmark
python main.py --benchmark --load-scale 1.3 --weather heatwave --hour 16
```

### CLI Flags

| Flag | Description |
|------|-------------|
| `--network` | `ieee123` (default) \| `case118` \| `case300` \| … |
| `--load-scale` | Load multiplier (e.g. `1.3` = 130%) |
| `--weather` | `clear` \| `heatwave` \| `overcast` \| `storm` \| `none` |
| `--hour` | Start hour 0–23 |
| `--line N` | Trip a specific line |
| `--no-der` | Disable distributed energy resources |
| `--benchmark` | Run LLM agent and LP baseline side-by-side |
| `--non-interactive` | Headless / CI mode |

### Example Benchmark (IEEE 123, load ×1.3, heatwave @ 16:00)

Worst N-1 (line L13 out) — 12 lines overloaded to 136%, 49 undervoltage buses:

| Metric | LLM Agent | LP Baseline |
|--------|-----------|-------------|
| Base + N-1 secure | ✅ | ✅ |
| Cost (EUR/h) | ~2,700 | ~2,570 |
| Actions taken | 8 | 10 |

### Module Map

```
Energy_H/
├── ieee123.py            OpenDSS → pandapower feeder builder
├── weather.py            Seeded 12-hour forecasts (irradiance / wind / demand)
├── market.py             Day-ahead + balancing price model
├── scenario.py           DER placement + weather + asset-health pipeline
├── grid_environment.py   Physics engine: N-1 screen, action space, SCADA, advance_hour
├── grid_agent.py         DeepSeek LLM corrective-action brain
├── env.py                GridEnv adapter (SCOPF-lite baseline API)
├── baseline.py           LP corrective/preventive dispatch
├── main.py               Two-stage control loop + benchmark mode
├── webapp.py             Flask web console (one-line diagram + SSE)
├── static/index.html     Console frontend (SVG feeder map, streaming log)
├── data/ieee123/         Vendored EPRI OpenDSS test-case files
└── requirements.txt
```

---

## GridGuard AI — React Frontend

### Features

| Panel | Description |
|-------|-------------|
| **Grid Visualization** | Force-directed IEEE 123-node feeder; fault line in red, overloaded lines in amber, restored lines in green |
| **N-1 Risk Screening** | Two-stage DC → AC contingency screen; every line ranked by composite risk score (0 – ∞); islanding → ∞ |
| **Corrective Action Space** | BESS discharge/charge, renewable curtailment, load curtailment — each with sensitivity (pp/MW), cost (€/MWh) and SoC |
| **AI Recommended Action** | Highest-priority option flagged with `★ AI Recommended` — chosen by best sensitivity-to-cost ratio |
| **Operator Action Plan** | AI mode (accept plan) or Manual mode (pick & dispatch any option), plus LP Baseline benchmark tab |
| **Economic Charts** | Per-option cost bar chart (log scale) + max-loading recovery before/after |
| **Corrective Loop** | DeepSeek LLM step-by-step reasoning and actions, cost per iteration |
| **Weather & Market Strip** | 12-hour forecast — temperature, solar/wind MW, balancing price (€/MWh), BESS SoC, derated assets |

### Three Preset Scenarios

| ID | Name | Condition |
|----|------|-----------|
| `n1-line13` | Heatwave Peak — Line 13 trip | 130% load · 16:00 · 34°C |
| `n1-line19` | Solar Midday — Line 19 trip | 100% load · 12:00 · clear |
| `n1-line55` | Storm Evening — Line 55 trip | 120% load · 18:00 · storm |

### Tech Stack

```
GridGuard AI/
├── React 19 + TypeScript
├── TanStack Router / Start    (file-based routing)
├── Vite 8                     (dev server + bundler)
├── Tailwind CSS v4            (design tokens)
├── Radix UI                   (accessible headless primitives)
├── Recharts                   (bar charts)
├── Lucide React               (icons)
└── src/lib/
    ├── grid-data.ts           (TypeScript types + 3 pre-computed scenarios)
    └── ieee123-topology.ts    (IEEE 123 bus/line topology for visualization)
```

The pandapower N-1 simulation results are pre-computed and embedded as structured TypeScript in `grid-data.ts`. No runtime backend is required to run the dashboard.

### Quick Start

**Requirements:** Node.js ≥ 18 or Bun

```bash
cd "GridGuard AI"
bun install          # or: npm install

# Development server → http://localhost:5173
bun run dev          # or: npm run dev

# Production build
bun run build        # or: npm run build
```

---

## Physics & AI Model

1. **Two-stage N-1 screening** — DC power-flow screen across all 123 lines to shortlist risky contingencies; AC solve on the shortlist to compute exact overloads and voltage deviations.
2. **Risk score** = `severity × likelihood` — weighted sum of overloaded-line count, peak loading %, undervoltage depth × count, plus convergence/islanding penalty.
3. **Action space** — for each feasible DER (BESS, PV, wind, flexible load), sensitivity `Δloading_pp / MW` is computed; options sorted by `|sensitivity| / cost`.
4. **DeepSeek LLM corrective loop** — given the action space, network state, and weather context, the agent issues dispatch commands iteratively until N-1 security is restored or all actions are exhausted.
5. **LP baseline** — SCOPF-lite linear program provides a mathematical cost optimum for benchmark comparison.

Dispatch priority: **BESS discharge → curtail renewables → shed load** (cheapest to most expensive per MW of relief).

---

## Team

Mengyu Zhang · Chen Zhao · Cici · Yang Xu · Weiting Liang

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

*Built for the E.ON Hackathon 2025 · Energy × AI Grid Operation Agents Track*
