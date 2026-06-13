import { ShieldCheck, AlertTriangle } from "lucide-react";
import type { Scenario } from "@/lib/grid-data";
import { FEEDER_NODES, FEEDER_EDGES, NODE_POS, FEEDER_VIEWBOX } from "@/lib/ieee123-topology";

/**
 * IEEE 123-node feeder one-line diagram. Lines are coloured on a continuous
 * loading spectrum, energised corridors carry animated flow particles, faults
 * pulse, and DER assets (PV / wind / BESS / substation) are clearly labelled.
 * When the operator accepts the plan, faults and overloads are cleared and the
 * whole feeder renders healthy. Pure presentation.
 */

function loadColor(loadPct: number): string {
  const l = Math.max(0, Math.min(120, loadPct));
  if (l < 50) return "oklch(0.78 0.14 175)";
  if (l < 75) return "oklch(0.82 0.15 135)";
  if (l < 90) return "oklch(0.84 0.16 95)";
  if (l < 100) return "oklch(0.78 0.18 60)";
  if (l < 110) return "oklch(0.70 0.20 35)";
  return "oklch(0.62 0.24 25)";
}

// Asset palette — keep distinct, high-contrast hues so each DER type reads at a glance.
const ASSET = {
  sub: { color: "oklch(0.92 0.02 230)", glyph: "⌂", name: "SUB" },
  bess: { color: "oklch(0.80 0.16 150)", glyph: "🔋", name: "BESS" },
  pv: { color: "oklch(0.85 0.17 90)", glyph: "☀", name: "PV" },
  wind: { color: "oklch(0.80 0.13 230)", glyph: "🌀", name: "WIND" },
} as const;

export function GridVisualization({
  scenario,
  running,
  accepted = false,
}: {
  scenario: Scenario | null;
  running: boolean;
  accepted?: boolean;
}) {
  const { w: W, h: H } = FEEDER_VIEWBOX;

  // Once the operator accepts the plan the disturbance is cleared.
  const showFault = !!scenario && !accepted;

  const overload = new Map<string, number>();
  if (showFault) {
    scenario!.postFault.overloaded.forEach((o) => {
      overload.set(`${o.from}->${o.to}`, o.loading);
      overload.set(`${o.to}->${o.from}`, o.loading);
    });
  }
  const faultKey = showFault ? `${scenario!.faultLine.from}->${scenario!.faultLine.to}` : "";
  const faultKeyR = showFault ? `${scenario!.faultLine.to}->${scenario!.faultLine.from}` : "";

  // DER assets always come from scenario data (independent of fault state).
  const bessBuses = new Set(scenario?.bess.map((b) => b.bus));
  const pvBuses = new Set(
    scenario?.actionSpace
      .filter((a) => a.actionType === "curtail_renewable" && !a.label.includes("Wind"))
      .map((a) => a.bus),
  );
  const windBuses = new Set(
    scenario?.actionSpace.filter((a) => a.label.includes("Wind")).map((a) => a.bus),
  );
  const overBuses = new Set<string>();
  if (showFault) {
    scenario!.postFault.overloaded.forEach((o) => {
      overBuses.add(o.from);
      overBuses.add(o.to);
    });
  }
  const substation = "150r";

  const P = (id: string) => NODE_POS[id] ?? { x: W / 2, y: H / 2 };

  function assetKind(id: string): keyof typeof ASSET | null {
    if (id === substation) return "sub";
    if (bessBuses.has(id)) return "bess";
    if (windBuses.has(id)) return "wind";
    if (pvBuses.has(id)) return "pv";
    return null;
  }

  return (
    <div className="relative h-full overflow-hidden rounded-xl border border-border bg-[radial-gradient(ellipse_at_30%_20%,_color-mix(in_oklab,var(--primary)_16%,transparent),transparent_60%)] bg-[#070d18]">
      <div
        className="pointer-events-none absolute inset-0 opacity-[0.18]"
        style={{
          backgroundImage:
            "linear-gradient(color-mix(in oklab, var(--primary) 40%, transparent) 1px, transparent 1px), linear-gradient(90deg, color-mix(in oklab, var(--primary) 40%, transparent) 1px, transparent 1px)",
          backgroundSize: "26px 26px",
          maskImage: "radial-gradient(ellipse at center, black 55%, transparent 100%)",
        }}
      />

      <div className="absolute left-4 top-4 z-10">
        <p className="text-[11px] font-medium uppercase tracking-widest text-muted-foreground">
          Live Feeder Topology
        </p>
        <p className="font-mono text-sm font-bold text-foreground">
          {scenario ? scenario.network : "IEEE 123-node feeder · standby"}
        </p>
      </div>
      <div className="absolute right-4 top-4 z-10">
        <StatusChip scenario={scenario} running={running} accepted={accepted} />
      </div>

      <svg viewBox={`0 0 ${W} ${H}`} className="relative h-full w-full" preserveAspectRatio="xMidYMid meet">
        <defs>
          <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="3" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
          <filter id="softglow" x="-80%" y="-80%" width="260%" height="260%">
            <feGaussianBlur stdDeviation="6" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        {/* feeder edges */}
        {FEEDER_EDGES.map((e) => {
          const a = P(e.from);
          const b = P(e.to);
          const key = `${e.from}->${e.to}`;
          const ol = overload.get(key);
          const isFault = key === faultKey || key === faultKeyR;

          if (isFault) {
            return (
              <g key={e.name}>
                <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="var(--warning)" strokeWidth={7} strokeLinecap="round" opacity={0.15} filter="url(#softglow)" />
                <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="var(--warning)" strokeWidth={2.5} strokeDasharray="5 7" opacity={0.9} />
              </g>
            );
          }

          if (ol) {
            const c = loadColor(ol);
            return (
              <g key={e.name}>
                <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={c} strokeWidth={10} strokeLinecap="round" opacity={0.16} filter="url(#softglow)" />
                <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={c} strokeWidth={3.2} strokeLinecap="round" />
                <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="oklch(0.98 0.02 95)" strokeWidth={1.6} strokeLinecap="round" className="grid-flow" opacity={0.9} />
              </g>
            );
          }

          return (
            <g key={e.name}>
              <line
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                stroke={scenario ? "oklch(0.72 0.10 185)" : "var(--border)"}
                strokeWidth={scenario ? 1.8 : 1.4}
                strokeLinecap="round"
                opacity={scenario ? 0.5 : 0.45}
              />
              {scenario && (
                <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="oklch(0.9 0.08 185)" strokeWidth={1.4} strokeLinecap="round" className="grid-flow-slow" opacity={0.55} />
              )}
            </g>
          );
        })}

        {/* overloaded corridor labels */}
        {showFault &&
          scenario!.postFault.overloaded.slice(0, 3).map((o) => {
            const a = P(o.from);
            const b = P(o.to);
            const mx = (a.x + b.x) / 2;
            const my = (a.y + b.y) / 2;
            const text = `${o.name} ${o.loading.toFixed(0)}%`;
            const c = loadColor(o.loading);
            return (
              <g key={`lbl-${o.lineIndex}`}>
                <rect x={mx - text.length * 3.4} y={my - 9} width={text.length * 6.8} height={18} rx={6} fill="#0a1424" stroke={c} strokeWidth={1.2} />
                <text x={mx} y={my + 3.5} textAnchor="middle" className="font-mono" fontSize={10} fontWeight={700} fill={c}>
                  {text}
                </text>
              </g>
            );
          })}

        {/* fault marker */}
        {showFault && <FaultMarker a={P(scenario!.faultLine.from)} c={P(scenario!.faultLine.to)} />}

        {/* nodes */}
        {FEEDER_NODES.map((n) => {
          const isOver = overBuses.has(n.id);
          const kind = assetKind(n.id);
          let r = 2.4;
          let fill = scenario ? "oklch(0.75 0.10 185)" : "oklch(0.6 0.10 230)";
          if (isOver) {
            fill = loadColor(110);
            r = 4.2;
          } else if (kind) {
            r = 3.2;
          }
          return (
            <g key={n.id}>
              {isOver && <circle cx={n.x} cy={n.y} r={11} fill={loadColor(110)} opacity={0.22} className="grid-pulse" />}
              <circle cx={n.x} cy={n.y} r={r} fill={fill} filter={kind || isOver ? "url(#glow)" : undefined} />
            </g>
          );
        })}

        {/* asset glyphs + labels drawn last so they sit on top */}
        {scenario &&
          FEEDER_NODES.map((n) => {
            const kind = assetKind(n.id);
            if (!kind) return null;
            return <AssetMarker key={`as-${n.id}`} x={n.x} y={n.y} kind={kind} bus={n.id} />;
          })}
      </svg>

      <Legend active={!!scenario} />

      <style>{`
        .grid-flow { stroke-dasharray: 9 14; animation: gridflow .7s linear infinite; }
        .grid-flow-slow { stroke-dasharray: 4 22; animation: gridflow 2.4s linear infinite; }
        @keyframes gridflow { to { stroke-dashoffset: -23; } }
        .grid-pulse { animation: gridpulse 1.5s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }
        @keyframes gridpulse { 0%,100% { opacity: .12; transform: scale(1);} 50% { opacity: .35; transform: scale(1.35);} }
      `}</style>
    </div>
  );
}

function AssetMarker({
  x,
  y,
  kind,
  bus,
}: {
  x: number;
  y: number;
  kind: keyof typeof ASSET;
  bus: string;
}) {
  const { color, glyph, name } = ASSET[kind];
  const label = `${name} ${bus}`;
  const w = label.length * 5.4 + 8;
  return (
    <g filter="url(#glow)">
      {/* connector tick */}
      <line x1={x} y1={y} x2={x} y2={y - 13} stroke={color} strokeWidth={1} opacity={0.6} />
      {/* glyph badge */}
      <circle cx={x} cy={y - 17} r={8.5} fill="#0a1424" stroke={color} strokeWidth={1.6} />
      <text x={x} y={y - 13.2} textAnchor="middle" fontSize={9.5}>
        {glyph}
      </text>
      {/* type + bus label */}
      <rect x={x - w / 2} y={y - 35} width={w} height={12} rx={3} fill="#0a1424" stroke={color} strokeWidth={0.9} opacity={0.95} />
      <text x={x} y={y - 26.3} textAnchor="middle" className="font-mono" fontSize={7.5} fontWeight={700} fill={color}>
        {label}
      </text>
    </g>
  );
}

function StatusChip({
  scenario,
  running,
  accepted,
}: {
  scenario: Scenario | null;
  running: boolean;
  accepted: boolean;
}) {
  if (running)
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-primary/15 px-3 py-1 text-xs font-semibold text-primary ring-1 ring-primary/30">
        <span className="h-2 w-2 animate-pulse rounded-full bg-primary" /> Solving…
      </span>
    );
  if (!scenario || accepted)
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-success/15 px-3 py-1 text-xs font-semibold text-success ring-1 ring-success/30">
        <ShieldCheck className="h-3.5 w-3.5" /> {accepted ? "N-1 Restored" : "Grid Secure"}
      </span>
    );
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-destructive/15 px-3 py-1 text-xs font-semibold text-destructive ring-1 ring-destructive/40">
      <AlertTriangle className="h-3.5 w-3.5" /> N-1 Violation
    </span>
  );
}

function FaultMarker({ a, c }: { a: { x: number; y: number }; c: { x: number; y: number } }) {
  const mx = (a.x + c.x) / 2;
  const my = (a.y + c.y) / 2;
  return (
    <g filter="url(#glow)">
      <circle cx={mx} cy={my} r={13} fill="var(--warning)" opacity={0.18} className="grid-pulse" />
      <circle cx={mx} cy={my} r={9} fill="#0a1424" stroke="var(--warning)" strokeWidth={1.6} />
      <text x={mx} y={my + 3.5} textAnchor="middle" fontSize={10} fontWeight={700}>
        ⚡
      </text>
    </g>
  );
}

function Legend({ active }: { active: boolean }) {
  if (!active)
    return (
      <div className="absolute bottom-3 left-4 flex flex-wrap items-center gap-x-4 gap-y-1.5 pr-4">
        <span className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <span className="h-2.5 w-2.5 rounded-full" style={{ background: "var(--primary)", boxShadow: "0 0 6px var(--primary)" }} />
          Feeder de-energised · standby
        </span>
      </div>
    );
  const loads = [
    { c: "oklch(0.78 0.14 175)", label: "<75% load" },
    { c: "oklch(0.78 0.18 60)", label: "75–100%" },
    { c: "oklch(0.62 0.24 25)", label: "Overloaded" },
    { c: "var(--warning)", label: "Tripped ⚡" },
  ];
  const assets = [
    { c: ASSET.pv.color, label: "☀ Solar PV" },
    { c: ASSET.wind.color, label: "🌀 Wind" },
    { c: ASSET.bess.color, label: "🔋 BESS" },
    { c: ASSET.sub.color, label: "⌂ Substation" },
  ];
  return (
    <div className="absolute bottom-3 left-4 right-4 flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        {loads.map((it) => (
          <span key={it.label} className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <span className="h-2.5 w-2.5 rounded-full" style={{ background: it.c, boxShadow: `0 0 6px ${it.c}` }} />
            {it.label}
          </span>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        {assets.map((it) => (
          <span key={it.label} className="inline-flex items-center gap-1.5 text-[11px] font-medium text-foreground/80">
            <span className="h-2.5 w-2.5 rounded-sm" style={{ background: it.c, boxShadow: `0 0 6px ${it.c}` }} />
            {it.label}
          </span>
        ))}
      </div>
    </div>
  );
}
