import { createFileRoute } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import {
  Activity,
  Zap,
  Gauge,
  AlertTriangle,
  ShieldCheck,
  ShieldAlert,
  Cpu,
  Play,
  Check,
  ListFilter,
  FileText,
  TrendingUp,
  Brain,
  Network,
  Search,
  Sparkles,
  ArrowUpRight,
  ArrowDownRight,
  Power,
  BatteryCharging,
  BatteryLow,
  Sun,
  Star,
  Wand2,
  SlidersHorizontal,
  CloudSun,
  Scale,
  Database,
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  SCENARIOS,
  KPIS,
  ACTION_LABEL,
  type Scenario,
  type ActionOption,
  type ActionType,
  type CorrectiveIteration,
} from "@/lib/grid-data";
import { GridVisualization } from "@/components/GridVisualization";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "E.ON GridGuard — AI Grid Security & Contingency Management" },
      {
        name: "description",
        content:
          "AI-powered decision support for power grid operators. Screen N-1 contingencies, evaluate the action space and restore N-1 security at the lowest cost.",
      },
      { property: "og:title", content: "E.ON GridGuard" },
      {
        property: "og:description",
        content: "AI-powered Grid Security & Contingency Management for control room operators.",
      },
    ],
  }),
  component: GridCopilot,
});

const eur = (n: number) => "€" + Math.round(n).toLocaleString("en-US");
const tag = (i: number) => String.fromCharCode(65 + i); // 0 -> A

const actionTone: Record<ActionType, { text: string; bg: string; icon: typeof ArrowUpRight }> = {
  redispatch_up: { text: "text-primary", bg: "bg-primary/15 ring-primary/30", icon: ArrowUpRight },
  redispatch_down: { text: "text-success", bg: "bg-success/15 ring-success/30", icon: ArrowDownRight },
  curtail_load: { text: "text-warning", bg: "bg-warning/15 ring-warning/30", icon: Power },
  discharge_battery: { text: "text-accent", bg: "bg-accent/15 ring-accent/30", icon: BatteryCharging },
  charge_battery: { text: "text-primary", bg: "bg-primary/15 ring-primary/30", icon: BatteryLow },
  curtail_renewable: { text: "text-warning", bg: "bg-warning/15 ring-warning/30", icon: Sun },
};

function GridCopilot() {
  const [scenarioId, setScenarioId] = useState(SCENARIOS[0].id);
  const [stage, setStage] = useState<"idle" | "screening" | "result">("idle");
  const [accepted, setAccepted] = useState(false);

  const selected = SCENARIOS.find((s) => s.id === scenarioId)!;
  const sc = stage === "result" ? selected : null;

  const runSimulation = () => {
    setStage("screening");
    setAccepted(false);
    setTimeout(() => setStage("result"), 1200);
  };

  return (
    <div className="min-h-screen bg-background">
      <Header scenario={sc} />

      <main className="mx-auto max-w-7xl space-y-8 px-4 py-6 sm:px-6 lg:px-8">
        {/* ===== HERO: half visual grid + control deck ===== */}
        <section className="grid gap-5 lg:grid-cols-12">
          <div className="lg:col-span-7">
            <div className="h-[440px] sm:h-[480px]">
              <GridVisualization scenario={sc} running={stage === "screening"} accepted={accepted} />
            </div>
          </div>
          <div className="lg:col-span-5">
            <ControlDeck
              scenarioId={scenarioId}
              onChange={(id) => {
                setScenarioId(id);
                setStage("idle");
                setAccepted(false);
              }}
              onRun={runSimulation}
              running={stage === "screening"}
              scenario={sc}
            />
          </div>
        </section>

        {sc && (
          <>
            <ScreeningReport scenario={sc} />
            <ActionSpace scenario={sc} />
            <OperatorActionPlan scenario={sc} accepted={accepted} onAccept={() => setAccepted(true)} />
            
            <EconomicCharts scenario={sc} />
            <CorrectiveLoop scenario={sc} />
            <WeatherStrip scenario={sc} />
          </>
        )}

      </main>

      <footer className="border-t border-border py-6 text-center text-xs text-muted-foreground">
        E.ON GridGuard · pandapower physics engine + DeepSeek LLM · Decision support — operator approval required
      </footer>
    </div>
  );
}

/* ---------- Header ---------- */
function Header({ scenario }: { scenario: Scenario | null }) {
  const violation = !!scenario;
  const secure = scenario && scenario.finalMaxLoading < 100;
  return (
    <header className="sticky top-0 z-30 border-b border-border bg-card/80 backdrop-blur-md">
      <div className="mx-auto flex max-w-7xl flex-col gap-4 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6 lg:px-8">
        <div className="flex items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-lg bg-primary/15 ring-1 ring-primary/30">
            <Zap className="h-6 w-6 text-primary" />
          </div>
          <div>
            <h1 className="text-lg font-bold tracking-tight text-foreground sm:text-xl">E.ON GridGuard</h1>
            <p className="text-xs text-muted-foreground sm:text-sm">
              AI-powered Grid Security &amp; Contingency Management
            </p>
          </div>
        </div>
        <StatusPill violation={violation} resolved={!!secure} />
      </div>
    </header>
  );
}

function StatusPill({ violation, resolved }: { violation: boolean; resolved: boolean }) {
  if (violation && resolved) {
    return (
      <div className="inline-flex items-center gap-2 self-start rounded-full bg-success/15 px-4 py-2 text-sm font-semibold text-success ring-1 ring-success/40">
        <ShieldCheck className="h-4 w-4" /> N-1 Security Restored
      </div>
    );
  }
  return (
    <div
      className={`inline-flex items-center gap-2 self-start rounded-full px-4 py-2 text-sm font-semibold ring-1 ${
        violation
          ? "bg-destructive/15 text-destructive ring-destructive/40"
          : "bg-success/15 text-success ring-success/40"
      }`}
    >
      {violation ? <ShieldAlert className="h-4 w-4" /> : <ShieldCheck className="h-4 w-4" />}
      <span className={`h-2 w-2 rounded-full ${violation ? "bg-destructive" : "bg-success"} animate-pulse`} />
      {violation ? "N-1 Violation Detected" : "Grid Secure"}
    </div>
  );
}

/* ---------- Control Deck (right of hero) ---------- */
function ControlDeck({
  scenarioId,
  onChange,
  onRun,
  running,
  scenario,
}: {
  scenarioId: string;
  onChange: (id: string) => void;
  onRun: () => void;
  running: boolean;
  scenario: Scenario | null;
}) {
  const overloaded = scenario?.postFault.overloaded.length ?? 0;
  const secure = !scenario || scenario.finalMaxLoading < 100;
  return (
    <div className="flex h-full flex-col gap-4 rounded-xl border border-border bg-card p-5">
      <div>
        <p className="text-[11px] font-medium uppercase tracking-widest text-muted-foreground">Control Deck</p>
        <h2 className="text-base font-bold text-foreground">Contingency Simulator</h2>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <Kpi icon={Network} label="Buses" value={String(KPIS.buses)} tone="primary" />
        <Kpi icon={Activity} label="Lines" value={String(KPIS.activeLines)} tone="primary" />
        <Kpi icon={Database} label="DER Assets" value={String(KPIS.derAssets)} tone="primary" />
        <Kpi
          icon={secure ? ShieldCheck : ShieldAlert}
          label={scenario ? "Overloads" : "Security"}
          value={scenario ? String(overloaded) : "Secure"}
          tone={secure ? "success" : "critical"}
        />
      </div>

      <div>
        <label className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Select N-1 Scenario
        </label>
        <select
          value={scenarioId}
          onChange={(e) => onChange(e.target.value)}
          className="w-full rounded-lg border border-input bg-background px-3 py-2.5 text-sm text-foreground outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/30"
        >
          {SCENARIOS.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </div>

      <button
        onClick={onRun}
        disabled={running}
        className="inline-flex items-center justify-center gap-2 rounded-lg bg-primary px-5 py-2.5 text-sm font-semibold text-primary-foreground transition-all hover:brightness-110 disabled:opacity-60"
      >
        {running ? (
          <>
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-primary-foreground/40 border-t-primary-foreground" />
            Running…
          </>
        ) : (
          <>
            <Play className="h-4 w-4" /> Run Self-Healing
          </>
        )}
      </button>

      <div className="mt-auto">
        {running && (
          <div className="space-y-1.5 text-xs text-muted-foreground">
            <p className="animate-pulse">● Stage 1 — N-1 screening (DC→AC)…</p>
            <p>● Stage 2 — building corrective action space…</p>
            <p>● DeepSeek LLM agent reasoning…</p>
          </div>
        )}
        {!running && scenario && (
          <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-3">
            <div className="flex items-start gap-2">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
              <div>
                <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                  Current Issue · Severity {scenario.severity}
                </p>
                <p className="text-sm font-semibold text-foreground">{scenario.issue}</p>
              </div>
            </div>
          </div>
        )}
        {!running && !scenario && (
          <div className="rounded-lg border border-success/30 bg-success/10 p-3 text-sm text-success">
            <ShieldCheck className="mr-1.5 inline h-4 w-4" />
            Grid is N-1 secure. Trip an element to start a self-healing run.
          </div>
        )}
      </div>
    </div>
  );
}

type Tone = "primary" | "success" | "warning" | "critical";
const toneRing: Record<Tone, string> = {
  primary: "ring-primary/25",
  success: "ring-success/40",
  warning: "ring-warning/40",
  critical: "ring-destructive/40",
};
const toneText: Record<Tone, string> = {
  primary: "text-primary",
  success: "text-success",
  warning: "text-warning",
  critical: "text-destructive",
};

function Kpi({ icon: Icon, label, value, tone }: { icon: typeof Activity; label: string; value: string; tone: Tone }) {
  return (
    <div className={`rounded-lg border border-border bg-background/40 p-3 ring-1 ${toneRing[tone]}`}>
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{label}</span>
        <Icon className={`h-3.5 w-3.5 ${toneText[tone]}`} />
      </div>
      <p className={`mt-1 font-mono text-xl font-bold ${tone === "primary" ? "text-foreground" : toneText[tone]}`}>
        {value}
      </p>
    </div>
  );
}

/* ---------- Weather & Market Strip ---------- */
function WeatherStrip({ scenario }: { scenario: Scenario }) {
  const { forecast, hour, profile } = scenario.weather;
  return (
    <section>
      <SectionTitle
        icon={CloudSun}
        title="Weather & Market Outlook"
        subtitle={`12-hour ${profile} forecast — drives solar, wind and temperature-dependent demand & balancing price`}
      />
      <div className="rounded-xl border border-border bg-card p-4">
        <div className="flex gap-2 overflow-x-auto pb-1">
          {forecast.map((f) => {
            const now = f.hour === hour;
            return (
              <div
                key={f.hour}
                className={`flex min-w-[88px] flex-1 flex-col gap-1 rounded-lg border p-2.5 text-center ${
                  now ? "border-primary bg-primary/10 ring-1 ring-primary/40" : "border-border bg-background/40"
                }`}
              >
                <span className="text-[11px] font-semibold text-foreground">{String(f.hour).padStart(2, "0")}:00</span>
                <span className="text-[10px] capitalize text-muted-foreground">{f.condition}</span>
                <span className="font-mono text-sm font-bold text-foreground">{f.tempC.toFixed(0)}°C</span>
                <div className="mt-1 space-y-0.5 text-[10px] text-muted-foreground">
                  <p>☀ {f.solarMw.toFixed(1)} · 🌀 {f.windMw.toFixed(1)} MW</p>
                  <p className="font-mono text-warning">€{f.balancingPrice.toFixed(0)}/MWh</p>
                </div>
              </div>
            );
          })}
        </div>
        <div className="mt-3 flex flex-wrap gap-2 border-t border-border pt-3 text-[11px] text-muted-foreground">
          {scenario.bess.map((b) => (
            <span key={b.id} className="inline-flex items-center gap-1.5 rounded-full bg-accent/10 px-2.5 py-1 ring-1 ring-accent/30">
              <BatteryCharging className="h-3.5 w-3.5 text-accent" />
              BESS @ Bus {b.bus} · {b.powerMw} MW · SoC {b.socPct}%
            </span>
          ))}
          {scenario.assetHealth.derated.map((d) => (
            <span key={d.line} className="inline-flex items-center gap-1.5 rounded-full bg-warning/10 px-2.5 py-1 ring-1 ring-warning/30">
              <AlertTriangle className="h-3.5 w-3.5 text-warning" />
              {d.line} derated {d.deratePct}% · health {d.healthPct}%
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ---------- LP Baseline vs LLM Agent benchmark ---------- */
function BaselineCompare({ scenario }: { scenario: Scenario }) {
  const { llm, lp } = scenario.baseline;
  const rows = [
    { k: "Base + N-1 secure", a: llm.secure ? "✓" : "✗", b: lp.secure ? "✓" : "✗", ok: true },
    { k: "Corrective cost (€/h)", a: eur(llm.costEur), b: eur(lp.costEur), ok: false },
    { k: "Actions issued", a: String(llm.actions), b: String(lp.actions), ok: false },
  ];
  return (
    <div className="mb-4">
      <p className="mb-3 text-xs text-muted-foreground">
        DeepSeek LLM agent benchmarked against the SCOPF-lite LP optimum.
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        <BenchCard title="DeepSeek LLM Agent" highlight icon={Brain} secure={llm.secure} cost={llm.costEur} actions={llm.actions} note="Batteries first, load shed only where needed" />
        <BenchCard title="LP Baseline (SCOPF-lite)" icon={Scale} secure={lp.secure} cost={lp.costEur} actions={lp.actions} note="Mathematical optimum — reference cost" />
      </div>
      <div className="mt-4 overflow-x-auto rounded-xl border border-border bg-background/40">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th className="px-4 py-2.5 font-medium">Metric</th>
              <th className="px-4 py-2.5 font-medium text-accent">LLM Agent</th>
              <th className="px-4 py-2.5 font-medium">LP Baseline</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.k} className="border-b border-border/60 last:border-0">
                <td className="px-4 py-2.5 text-muted-foreground">{r.k}</td>
                <td className="px-4 py-2.5 font-mono font-semibold text-foreground">{r.a}</td>
                <td className="px-4 py-2.5 font-mono font-semibold text-foreground">{r.b}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function BenchCard({
  title,
  icon: Icon,
  secure,
  cost,
  actions,
  note,
  highlight,
}: {
  title: string;
  icon: typeof Brain;
  secure: boolean;
  cost: number;
  actions: number;
  note: string;
  highlight?: boolean;
}) {
  return (
    <div className={`rounded-xl border p-5 ${highlight ? "border-accent bg-accent/5 ring-1 ring-accent/30" : "border-border bg-card"}`}>
      <div className="mb-3 flex items-center gap-2">
        <Icon className={`h-5 w-5 ${highlight ? "text-accent" : "text-primary"}`} />
        <h3 className="text-sm font-bold text-foreground">{title}</h3>
        {secure && (
          <span className="ml-auto inline-flex items-center gap-1 rounded-full bg-success/15 px-2 py-0.5 text-[11px] font-semibold text-success ring-1 ring-success/30">
            <ShieldCheck className="h-3 w-3" /> Secure
          </span>
        )}
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <p className="text-[11px] uppercase tracking-wide text-muted-foreground">Cost</p>
          <p className={`font-mono text-2xl font-bold ${highlight ? "text-accent" : "text-foreground"}`}>{eur(cost)}<span className="text-sm font-normal text-muted-foreground">/h</span></p>
        </div>
        <div>
          <p className="text-[11px] uppercase tracking-wide text-muted-foreground">Actions</p>
          <p className="font-mono text-2xl font-bold text-foreground">{actions}</p>
        </div>
      </div>
      <p className="mt-3 text-[11px] text-muted-foreground">{note}</p>
    </div>
  );
}

function ActionSpace({ scenario }: { scenario: Scenario }) {
  const ordered = useMemo(
    () => scenario.actionSpace.map((a, i) => ({ a, label: tag(i) })),
    [scenario],
  );
  return (
    <section>
      <SectionTitle
        icon={ListFilter}
        title="Corrective Action Options"
        subtitle="Feasible actions ranked for the operator · highest-priority option is flagged"
      />
      <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
        {ordered.map(({ a, label }) => (
          <ActionCard key={`${a.targetId}-${a.actionType}`} a={a} optTag={label} />
        ))}
      </div>
    </section>
  );
}

function ActionCard({ a, optTag }: { a: ActionOption; optTag: string }) {
  const tone = actionTone[a.actionType];
  const Icon = tone.icon;
  const relief = a.sensitivity < 0;
  return (
    <div
      className={`relative flex flex-col rounded-xl border bg-card p-4 transition-shadow ${
        a.recommended
          ? "border-accent ring-2 ring-accent/40 shadow-[0_0_24px_-6px_color-mix(in_oklab,var(--accent)_55%,transparent)]"
          : "border-border"
      }`}
    >
      {a.recommended && (
        <div className="absolute -top-2.5 left-3 inline-flex items-center gap-1 rounded-full bg-accent px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-accent-foreground">
          <Star className="h-3 w-3" /> AI Recommended
        </div>
      )}
      <div className="mb-2 flex items-center justify-between">
        <span className="inline-flex h-6 w-6 items-center justify-center rounded-md bg-secondary font-mono text-xs font-bold text-foreground ring-1 ring-border">
          {optTag}
        </span>
        <span className={`inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] font-semibold ring-1 ${tone.bg} ${tone.text}`}>
          <Icon className="h-3.5 w-3.5" /> {ACTION_LABEL[a.actionType]}
        </span>
      </div>
      <h3 className="text-sm font-bold text-foreground">{a.label}</h3>
      <dl className="mt-3 space-y-1.5 text-xs">
        <Row label="Max available" value={`${a.maxAvailableMw} MW`} />
        <Row label="Cost" value={`${a.costPerMw.toLocaleString()} €/MWh`} />
        <div className="flex items-center justify-between">
          <dt className="text-muted-foreground">Sensitivity</dt>
          <dd className={`font-mono font-semibold ${relief ? "text-success" : "text-destructive"}`}>
            {a.sensitivity > 0 ? "+" : ""}
            {a.sensitivity.toFixed(1)} pp/MW
          </dd>
        </div>
        {a.socPct !== undefined && <Row label="State of charge" value={`${a.socPct}%`} />}
      </dl>
      <p className="mt-2 text-[11px] text-muted-foreground">
        on <span className="font-mono">{a.targetLine}</span> · {relief ? "relieves" : "worsens"} loading
      </p>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-mono font-semibold text-foreground">{value}</dd>
    </div>
  );
}

/* ---------- Operator Action Plan (AI vs Manual mode) ---------- */
const TYPE_FILTERS: { key: ActionType; label: string }[] = [
  { key: "discharge_battery", label: "BESS Discharge" },
  { key: "charge_battery", label: "BESS Charge" },
  { key: "curtail_renewable", label: "Curtail RES" },
  { key: "curtail_load", label: "Curtail Load" },
];

function OperatorActionPlan({
  scenario,
  accepted,
  onAccept,
}: {
  scenario: Scenario;
  accepted: boolean;
  onAccept: () => void;
}) {
  const [mode, setMode] = useState<"ai" | "manual" | "baseline">("ai");
  const [typeFilter, setTypeFilter] = useState<ActionType | "all">("all");
  const recommendedIndex = Math.max(0, scenario.actionSpace.findIndex((a) => a.recommended));
  const [selectedIdx, setSelectedIdx] = useState(recommendedIndex);

  const filtered = scenario.actionSpace
    .map((a, i) => ({ a, i }))
    .filter(({ a }) => typeFilter === "all" || a.actionType === typeFilter);

  const manualPick = scenario.actionSpace[selectedIdx];
  // Manual estimate: MW needed to bring max loading to 94% target via sensitivity.
  const ppNeeded = Math.max(0, scenario.postFault.maxLoading - 94);
  const mwNeeded = manualPick.sensitivity < 0 ? ppNeeded / Math.abs(manualPick.sensitivity) : 0;
  const manualMw = Math.min(manualPick.maxAvailableMw, Math.ceil(mwNeeded));
  const manualCost = manualMw * manualPick.costPerMw;

  return (
    <section>
      <SectionTitle icon={Check} title="Operator Action Plan" subtitle="Approve the AI plan or build one manually — control room approval required" />
      <div className="rounded-xl border border-border bg-card p-5">
        {/* mode toggle */}
        <div className="mb-4 inline-flex flex-wrap rounded-lg border border-border bg-background/60 p-1">
          <ModeBtn active={mode === "ai"} onClick={() => setMode("ai")} icon={Wand2} label="AI Recommended" />
          <ModeBtn active={mode === "manual"} onClick={() => setMode("manual")} icon={SlidersHorizontal} label="Manual Mode" />
          <ModeBtn active={mode === "baseline"} onClick={() => setMode("baseline")} icon={Scale} label="Agent vs LP Baseline" />
        </div>

        {mode === "ai" ? (
          <>
            <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-3">
              <MiniStat label="Total Cost" value={`${eur(scenario.totalCost)}/h`} tone="primary" />
              <MiniStat label="Final Max Load" value={`${scenario.finalMaxLoading.toFixed(1)}%`} tone="success" />
              <MiniStat label="Iterations" value={String(scenario.iterations.length)} tone="primary" />
            </div>
            {accepted && (
              <div className="mb-4 flex items-center gap-2 rounded-lg border border-success/40 bg-success/10 px-4 py-3 text-sm font-medium text-success">
                <ShieldCheck className="h-4 w-4" />
                Action plan accepted — dispatch issued. N-1 security restored at {eur(scenario.totalCost)}/h.
              </div>
            )}
          </>
        ) : mode === "baseline" ? (
          <BaselineCompare scenario={scenario} />
        ) : (
          <div className="mb-4">
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Action mode</p>
            <div className="mb-4 flex flex-wrap gap-2">
              <FilterChip active={typeFilter === "all"} onClick={() => setTypeFilter("all")} label="All" />
              {TYPE_FILTERS.map((f) => (
                <FilterChip key={f.key} active={typeFilter === f.key} onClick={() => setTypeFilter(f.key)} label={f.label} />
              ))}
            </div>

            <div className="grid gap-2 sm:grid-cols-2">
              {filtered.map(({ a, i }) => {
                const tone = actionTone[a.actionType];
                const Icon = tone.icon;
                const active = selectedIdx === i;
                return (
                  <button
                    key={`${a.targetId}-${a.actionType}`}
                    onClick={() => setSelectedIdx(i)}
                    className={`flex items-center justify-between rounded-lg border p-3 text-left transition-colors ${
                      active ? "border-primary bg-primary/10 ring-1 ring-primary/40" : "border-border bg-background/40 hover:bg-secondary/50"
                    }`}
                  >
                    <span className="flex items-center gap-2">
                      <span className="inline-flex h-6 w-6 items-center justify-center rounded-md bg-secondary font-mono text-xs font-bold text-foreground ring-1 ring-border">
                        {tag(i)}
                      </span>
                      <span className={`inline-flex items-center gap-1.5 text-sm font-medium ${tone.text}`}>
                        <Icon className="h-4 w-4" /> {a.label}
                      </span>
                    </span>
                    {active && <Check className="h-4 w-4 text-primary" />}
                  </button>
                );
              })}
            </div>

            <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
              <MiniStat label="Selected" value={`Option ${tag(selectedIdx)}`} tone="primary" />
              <MiniStat label="Dispatch MW" value={`${manualMw} MW`} tone="primary" />
              <MiniStat label="Est. Cost" value={`${eur(manualCost)}/h`} tone="warning" />
              <MiniStat
                label="Relief"
                value={manualPick.sensitivity < 0 ? "Yes" : "No"}
                tone={manualPick.sensitivity < 0 ? "success" : "critical"}
              />
            </div>
            {manualPick.sensitivity >= 0 && (
              <p className="mt-3 text-xs text-destructive">
                ⚠ Option {tag(selectedIdx)} has positive sensitivity — it would worsen loading on {manualPick.targetLine}.
              </p>
            )}
          </div>
        )}

        <div className="flex flex-wrap gap-3 border-t border-border pt-4">
          {mode !== "baseline" && (
            <button
              onClick={onAccept}
              className="inline-flex items-center gap-2 rounded-lg bg-success px-5 py-2.5 text-sm font-semibold text-success-foreground transition-all hover:brightness-110"
            >
              <Check className="h-4 w-4" /> {mode === "ai" ? "Accept AI Plan" : `Dispatch Option ${tag(selectedIdx)}`}
            </button>
          )}
          <button className="inline-flex items-center gap-2 rounded-lg border border-border bg-secondary px-5 py-2.5 text-sm font-semibold text-secondary-foreground transition-colors hover:bg-secondary/70">
            <FileText className="h-4 w-4" /> Generate Report
          </button>
        </div>
      </div>
    </section>
  );
}

function ModeBtn({ active, onClick, icon: Icon, label }: { active: boolean; onClick: () => void; icon: typeof Wand2; label: string }) {
  return (
    <button
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-semibold transition-colors ${
        active ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"
      }`}
    >
      <Icon className="h-4 w-4" /> {label}
    </button>
  );
}

function FilterChip({ active, onClick, label }: { active: boolean; onClick: () => void; label: string }) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full px-3 py-1.5 text-xs font-semibold ring-1 transition-colors ${
        active ? "bg-primary text-primary-foreground ring-primary" : "bg-background/60 text-muted-foreground ring-border hover:text-foreground"
      }`}
    >
      {label}
    </button>
  );
}

/* ---------- Economic Charts (Option A/B/C tags) ---------- */
function EconomicCharts({ scenario }: { scenario: Scenario }) {
  const costData = scenario.actionSpace.map((a, i) => ({
    name: `Option ${tag(i)}`,
    cost: a.costPerMw,
    type: a.actionType,
    recommended: !!a.recommended,
  }));
  const loadingData = [
    { name: "Before", loading: scenario.postFault.maxLoading },
    { name: "AI Recommendation", loading: scenario.finalMaxLoading },
  ];

  return (
    <section>
      <SectionTitle icon={TrendingUp} title="Cost & Loading Analysis" subtitle="Per-option cost comparison and max-loading recovery trajectory" />
      <div className="grid gap-4 lg:grid-cols-2">
        <ChartCard title="Action Cost per Option (€/MWh, log scale)">
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={costData} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
              <XAxis dataKey="name" tick={axisTick} interval={0} />
              <YAxis scale="log" domain={[1, 100000]} tick={axisTick} />
              <Tooltip {...tooltipProps} />
              <Bar dataKey="cost" radius={[4, 4, 0, 0]}>
                {costData.map((d, i) => (
                  <Cell
                    key={i}
                    fill={
                      d.recommended
                        ? "var(--accent)"
                        : d.type === "curtail_load"
                          ? "var(--warning)"
                          : d.type === "curtail_renewable"
                            ? "var(--warning)"
                            : "var(--primary)"
                    }
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
          <div className="mt-2 flex flex-wrap gap-3 text-[11px] text-muted-foreground">
            <LegendDot c="var(--accent)" label="Recommended (BESS)" />
            <LegendDot c="var(--primary)" label="BESS / charge" />
            <LegendDot c="var(--warning)" label="Curtail load / RES" />
          </div>
        </ChartCard>

        <ChartCard title="Max Line Loading Recovery (%)">
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={loadingData} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
              <XAxis dataKey="name" tick={axisTick} interval={0} />
              <YAxis domain={[0, 120]} tick={axisTick} />
              <Tooltip {...tooltipProps} />
              <ReferenceLine y={100} stroke="var(--destructive)" strokeDasharray="4 4" />
              <Bar dataKey="loading" radius={[4, 4, 0, 0]}>
                {loadingData.map((d, i) => (
                  <Cell key={i} fill={d.loading >= 100 ? "var(--destructive)" : "var(--success)"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>
    </section>
  );
}

function LegendDot({ c, label }: { c: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="h-2.5 w-2.5 rounded-sm" style={{ background: c }} /> {label}
    </span>
  );
}

const axisTick = { fill: "var(--muted-foreground)", fontSize: 11 };
const tooltipProps = {
  contentStyle: {
    background: "var(--popover)",
    border: "1px solid var(--border)",
    borderRadius: "8px",
    color: "var(--popover-foreground)",
    fontSize: "12px",
  },
  cursor: { fill: "color-mix(in oklab, var(--muted) 40%, transparent)" },
};

function ChartCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <h3 className="mb-3 text-sm font-semibold text-foreground">{title}</h3>
      {children}
    </div>
  );
}

/* ---------- Corrective Loop (AI Reasoning) ---------- */
function CorrectiveLoop({ scenario }: { scenario: Scenario }) {
  return (
    <section>
      <SectionTitle icon={Brain} title="AI Corrective Control Loop" subtitle="Explainable, cost-optimal decisions issued by the GridAgent" />
      <div className="space-y-4">
        {scenario.iterations.map((it) => (
          <IterationCard key={it.iteration} it={it} total={scenario.iterations.length} />
        ))}
      </div>
    </section>
  );
}

function IterationCard({ it, total }: { it: CorrectiveIteration; total: number }) {
  const resolved = it.overloadsAfter === 0;
  const showDelta = it.iteration > 1; // first pass delta is redundant with the chart
  return (
    <div className="rounded-xl border border-primary/30 bg-card p-4 ring-1 ring-primary/15">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="flex h-7 w-7 items-center justify-center rounded-full bg-accent/15 ring-1 ring-accent/30">
            <Sparkles className="h-3.5 w-3.5 text-accent" />
          </span>
          <span className="text-sm font-semibold text-foreground">
            Reasoning{total > 1 ? ` · step ${it.iteration}/${total}` : ""}
          </span>
        </div>
        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold ${
            resolved ? "bg-success/15 text-success ring-1 ring-success/30" : "bg-warning/15 text-warning ring-1 ring-warning/30"
          }`}
        >
          {resolved ? <ShieldCheck className="h-3.5 w-3.5" /> : <ShieldAlert className="h-3.5 w-3.5" />}
          {resolved ? "Secure" : "In progress"}
          {showDelta && ` · ${it.maxLoadingBefore.toFixed(1)}% → ${it.maxLoadingAfter.toFixed(1)}%`}
        </span>
      </div>

      <div className="mb-4 rounded-lg border border-border bg-background/40 p-3">
        <p className="text-sm leading-relaxed text-foreground/90">{it.reasoning}</p>
      </div>

      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Actions executed</p>
      <div className="grid gap-2 sm:grid-cols-2">
        {it.actions.map((act) => {
          const tone = actionTone[act.actionType];
          const Icon = tone.icon;
          return (
            <div key={`${act.targetId}-${act.actionType}`} className="flex items-center justify-between rounded-lg border border-border bg-card p-3">
              <span className={`inline-flex items-center gap-1.5 text-sm font-medium ${tone.text}`}>
                <Icon className="h-4 w-4" /> {act.label}
              </span>
              <div className="text-right">
                <p className="font-mono text-sm font-semibold text-foreground">{act.amountMw} MW</p>
                <p className="font-mono text-[11px] text-muted-foreground">{eur(act.amountMw * act.costPerMw)}/h</p>
              </div>
            </div>
          );
        })}
      </div>
      <div className="mt-3 flex items-center justify-between border-t border-border pt-3 text-sm">
        <span className="text-muted-foreground">Iteration cost</span>
        <span className="font-mono font-bold text-accent">{eur(it.costEur)}/h</span>
      </div>
    </div>
  );
}

/* ---------- Stage 1 Screening Report ---------- */
function ScreeningReport({ scenario }: { scenario: Scenario }) {
  const { screeningSummary: ss, screening } = scenario;
  const visible = screening.filter((r) => r.riskScore <= 200);
  const max = Math.max(...visible.filter((r) => r.riskScore < 999).map((r) => r.riskScore));
  return (
    <section>
      <SectionTitle icon={Search} title="Risk Screening & Prioritization" subtitle="Fast physics filter ranks every contingency by risk score" />
      <div className="rounded-xl border border-border bg-card p-5">
        <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <MiniStat label="Lines Evaluated" value={String(visible.length)} tone="primary" />
          <MiniStat label="Catastrophic" value={String(ss.catastrophic)} tone="critical" />
          <MiniStat label="Dangerous" value={String(ss.dangerous)} tone="warning" />
          <MiniStat label="N-1 Secure" value={String(ss.safe)} tone="success" />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th className="px-2 py-2 font-medium">Rank</th>
                <th className="px-2 py-2 font-medium">Line</th>
                <th className="px-2 py-2 font-medium">Overloads</th>
                <th className="px-2 py-2 font-medium">Max Load</th>
                <th className="px-2 py-2 font-medium">Risk Score <span className="font-normal normal-case text-muted-foreground/70">(0–∞)</span></th>
              </tr>
            </thead>
            <tbody>
              {visible.map((r, i) => {
                const escalated = r.lineIndex === scenario.faultLine.index;
                return (
                  <tr key={r.lineIndex} className={`border-b border-border/60 ${escalated ? "bg-accent/10" : ""}`}>
                    <td className="px-2 py-2 font-mono text-muted-foreground">#{i + 1}</td>
                    <td className="px-2 py-2 font-mono font-semibold text-foreground">
                      {r.name}
                      {escalated && (
                        <span className="ml-2 rounded bg-accent px-1.5 py-0.5 text-[10px] font-bold uppercase text-accent-foreground">
                          Escalated
                        </span>
                      )}
                    </td>
                    <td className="px-2 py-2">
                      {r.converged ? r.nOverloaded : "—"}
                    </td>
                    <td className="px-2 py-2 font-mono">
                      {r.converged ? (
                        <span className={r.maxLoadingPct >= 100 ? "text-destructive" : "text-foreground"}>
                          {r.maxLoadingPct.toFixed(1)}%
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-2 py-2">
                      <div className="flex items-center gap-2">
                        <div className="h-1.5 w-20 overflow-hidden rounded-full bg-secondary">
                          <div
                            className={`h-full rounded-full ${
                              r.riskScore >= 999 || r.riskScore > 100
                                ? "bg-destructive"
                                : r.riskScore >= 30
                                  ? "bg-accent"
                                  : "bg-primary"
                            }`}
                            style={{ width: r.riskScore >= 999 ? "100%" : `${(r.riskScore / max) * 100}%` }}
                          />
                        </div>
                        <span
                          className={`font-mono text-xs font-semibold ${
                            r.riskScore >= 999 || r.riskScore > 100
                              ? "text-destructive"
                              : r.riskScore >= 30
                                ? "text-accent"
                                : "text-primary"
                          }`}
                        >
                          {r.riskScore >= 999 ? "∞" : r.riskScore.toFixed(1)}
                        </span>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="mt-4 rounded-lg border border-border bg-background/40 p-3 text-[11px] leading-relaxed text-muted-foreground">
          <p className="mb-1.5 font-semibold uppercase tracking-wide text-foreground/80">Risk score scale</p>
          <div className="mb-2 flex flex-wrap items-center gap-3">
            <span className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-primary" /> 0–30 Low</span>
            <span className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-accent" /> 30–100 Elevated</span>
            <span className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-destructive" /> &gt;100 Dangerous · ∞ non-convergent (islanding)</span>
          </div>
          <p>
            Score = severity × likelihood, computed from: number of overloaded lines, peak line loading (% of rating),
            depth &amp; count of undervoltage buses, and convergence / islanding penalty. Higher = more urgent to mitigate.
          </p>
          <p className="mt-1.5 text-[10px] text-muted-foreground/70">
            Catastrophic = highest composite risk (typically &gt;150 points or &gt;10 simultaneous overloads / severe undervoltage),
            indicating potential cascading failure requiring immediate operator intervention.
          </p>
        </div>
      </div>
    </section>
  );
}

function MiniStat({ label, value, tone }: { label: string; value: string; tone: Tone }) {
  return (
    <div className={`rounded-lg border border-border bg-background/40 p-3 ring-1 ${toneRing[tone]}`}>
      <p className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className={`mt-0.5 font-mono text-xl font-bold ${toneText[tone]}`}>{value}</p>
    </div>
  );
}

/* ---------- shared ---------- */
function SectionTitle({ icon: Icon, title, subtitle }: { icon: typeof Activity; title: string; subtitle: string }) {
  return (
    <div className="mb-4 flex items-center gap-3">
      <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-secondary ring-1 ring-border">
        <Icon className="h-5 w-5 text-primary" />
      </div>
      <div>
        <h2 className="text-base font-bold tracking-tight text-foreground sm:text-lg">{title}</h2>
        <p className="text-xs text-muted-foreground">{subtitle}</p>
      </div>
    </div>
  );
}
