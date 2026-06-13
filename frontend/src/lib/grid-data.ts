// ============================================================================
// GridGuard data model — mirrors the GridGuard self-healing agent pipeline
// running on the IEEE 123-node distribution feeder.
//   - pandapower physics engine (two-stage N-1 screen + action execution)
//   - DeepSeek LLM cognitive layer (context-aware corrective control loop)
//   - weather + day-ahead/balancing market feeds, DER fleet (PV/wind/BESS),
//     asset-health derating, and an LP (SCOPF-lite) baseline benchmark.
// Distribution-feeder doctrine: batteries first (cheapest MW), curtail
// renewables to clear reverse-flow, shed load only as a last resort.
// ============================================================================

export type Risk = "Low" | "Medium" | "High";
export type Severity = "Critical" | "Warning" | "Elevated";
export type ActionType =
  | "redispatch_up"
  | "redispatch_down"
  | "curtail_load"
  | "discharge_battery"
  | "charge_battery"
  | "curtail_renewable";

export const ACTION_LABEL: Record<ActionType, string> = {
  redispatch_up: "Redispatch ↑",
  redispatch_down: "Redispatch ↓",
  curtail_load: "Curtail Load",
  discharge_battery: "BESS Discharge",
  charge_battery: "BESS Charge",
  curtail_renewable: "Curtail Renewable",
};

/** One row of the Stage-1 two-stage N-1 contingency screening report. */
export interface ScreeningRow {
  lineIndex: number;
  name: string;
  converged: boolean;
  nOverloaded: number;
  maxLoadingPct: number;
  riskScore: number;
  /** radial laterals are reported as islanding (unserved kW) not solved. */
  islandKw?: number;
}

/** A candidate corrective action offered to the LLM (the "action space"). */
export interface ActionOption {
  targetId: string;
  /** feeder bus the asset sits on. */
  bus: string;
  label: string;
  actionType: ActionType;
  /** Hard cap the LLM must respect (MW). */
  maxAvailableMw: number;
  /** Economic cost (EUR/MWh) — BESS cycle wear, balancing energy, VoLL. */
  costPerMw: number;
  /** pp change on the target line per MW. Negative = relief (good). */
  sensitivity: number;
  targetLine: string;
  /** BESS state of charge, if applicable. */
  socPct?: number;
  /** Marks the AI-recommended, highest-priority corrective action. */
  recommended?: boolean;
}

/** An action actually issued by the LLM in a corrective iteration. */
export interface AppliedAction {
  targetId: string;
  bus: string;
  label: string;
  actionType: ActionType;
  amountMw: number;
  costPerMw: number;
}

/** One pass of the Stage-2 corrective control loop. */
export interface CorrectiveIteration {
  iteration: number;
  reasoning: string;
  actions: AppliedAction[];
  maxLoadingBefore: number;
  maxLoadingAfter: number;
  overloadsBefore: number;
  overloadsAfter: number;
  undervoltBefore: number;
  undervoltAfter: number;
  costEur: number;
}

export interface OverloadedLine {
  lineIndex: number;
  name: string;
  from: string;
  to: string;
  loading: number;
}

/** 12-hour weather + market forecast entry. */
export interface ForecastHour {
  hour: number;
  condition: string;
  tempC: number;
  windMs: number;
  solarMw: number;
  windMw: number;
  loadMw: number;
  dayAheadPrice: number;
  balancingPrice: number;
}

export interface BessUnit {
  id: string;
  bus: string;
  powerMw: number;
  energyMwh: number;
  socPct: number;
}

export interface AssetHealth {
  derated: { line: string; healthPct: number; deratePct: number }[];
  maintenance: string;
}

/** LP (SCOPF-lite) baseline benchmark vs the LLM agent. */
export interface BaselineCompare {
  llm: { secure: boolean; costEur: number; actions: number };
  lp: { secure: boolean; costEur: number; actions: number };
}

export interface Scenario {
  id: string;
  name: string;
  network: string;
  issue: string;
  severity: Severity;
  faultLine: { index: number; name: string; from: string; to: string };
  weather: { profile: string; hour: number; forecast: ForecastHour[] };
  bess: BessUnit[];
  assetHealth: AssetHealth;
  screeningSummary: { total: number; catastrophic: number; dangerous: number; safe: number; islanding: number };
  screening: ScreeningRow[];
  postFault: { maxLoading: number; undervoltBuses: number; overloaded: OverloadedLine[] };
  actionSpace: ActionOption[];
  iterations: CorrectiveIteration[];
  baseline: BaselineCompare;
  totalCost: number;
  finalMaxLoading: number;
}

const cost = (actions: AppliedAction[]) =>
  Math.round(actions.reduce((s, a) => s + a.amountMw * a.costPerMw, 0));

// ----------------------------------------------------------------------------
// Shared 12-hour forecasts (one per weather profile, GridGuard parity)
// ----------------------------------------------------------------------------
function heatwaveForecast(start = 14): ForecastHour[] {
  const t = [35.2, 36.1, 36.4, 35.8, 34.6, 33.1, 31.5, 30.2, 29.4, 28.9, 28.3, 27.8];
  const solar = [2.1, 1.7, 1.1, 0.5, 0.1, 0, 0, 0, 0, 0, 0, 0];
  const wind = [0.3, 0.3, 0.2, 0.2, 0.3, 0.4, 0.5, 0.5, 0.6, 0.6, 0.7, 0.7];
  const load = [4.4, 4.6, 4.7, 4.6, 4.4, 4.2, 4.0, 3.8, 3.6, 3.5, 3.4, 3.3];
  const da = [78, 92, 104, 96, 81, 64, 52, 47, 44, 42, 41, 40];
  return t.map((tempC, k) => ({
    hour: (start + k) % 24,
    condition: k < 4 ? "heatwave" : "clear",
    tempC,
    windMs: 3 + wind[k] * 4,
    solarMw: solar[k],
    windMw: wind[k],
    loadMw: load[k],
    dayAheadPrice: da[k],
    balancingPrice: Math.round((da[k] * 1.45 + 12) * 100) / 100,
  }));
}

function clearMiddayForecast(start = 11): ForecastHour[] {
  const t = [24, 25.5, 26.4, 26.8, 26.1, 25.2, 23.8, 22.1, 20.4, 18.9, 17.6, 16.8];
  const solar = [2.6, 3.0, 3.2, 3.1, 2.7, 2.0, 1.2, 0.5, 0.1, 0, 0, 0];
  const wind = [0.4, 0.4, 0.5, 0.5, 0.6, 0.6, 0.7, 0.7, 0.6, 0.6, 0.5, 0.5];
  const load = [3.2, 3.3, 3.3, 3.2, 3.1, 3.2, 3.4, 3.6, 3.5, 3.3, 3.1, 3.0];
  const da = [21, 16, 12, 13, 18, 27, 38, 45, 44, 41, 39, 38];
  return t.map((tempC, k) => ({
    hour: (start + k) % 24,
    condition: "clear",
    tempC,
    windMs: 5 + wind[k] * 3,
    solarMw: solar[k],
    windMw: wind[k],
    loadMw: load[k],
    dayAheadPrice: da[k],
    balancingPrice: Math.round((da[k] * 1.45 + 12) * 100) / 100,
  }));
}

function stormForecast(start = 15): ForecastHour[] {
  const t = [21, 19.5, 17.8, 17.1, 16.8, 16.5, 16.9, 17.4, 18.2, 18.9, 19.4, 19.8];
  // storm front 3 h in: wind first maxes, then cuts out above 25 m/s
  const wind = [1.0, 1.0, 0.9, 0, 0, 0, 0, 0, 0.6, 0.9, 0.9, 0.8];
  const windMs = [13, 14, 16, 26, 28, 27, 26, 25, 12, 11, 10, 10];
  const solar = [1.4, 0.9, 0.4, 0.1, 0, 0, 0, 0, 0, 0, 0, 0];
  const load = [3.6, 3.7, 3.9, 4.0, 4.0, 3.9, 3.8, 3.7, 3.6, 3.5, 3.4, 3.3];
  const da = [44, 52, 61, 88, 96, 90, 84, 78, 49, 43, 41, 40];
  return t.map((tempC, k) => ({
    hour: (start + k) % 24,
    condition: k < 3 ? "pre-storm" : k < 8 ? "storm" : "clearing",
    tempC,
    windMs: windMs[k],
    solarMw: solar[k],
    windMw: wind[k],
    loadMw: load[k],
    dayAheadPrice: da[k],
    balancingPrice: Math.round((da[k] * 1.45 + 12) * 100) / 100,
  }));
}

// ----------------------------------------------------------------------------
// Scenario 1 — Heatwave evening peak, backbone Line L13 (13→18) out.
//   Worst N-1: 12 overloads to 136%, 49 undervoltage buses. BESS-first.
// ----------------------------------------------------------------------------
const s1Iter1: AppliedAction[] = [
  { targetId: "storage_0", bus: "76", label: "BESS @ Bus 76", actionType: "discharge_battery", amountMw: 0.39, costPerMw: 10 },
  { targetId: "storage_1", bus: "65", label: "BESS @ Bus 65", actionType: "discharge_battery", amountMw: 0.39, costPerMw: 10 },
  { targetId: "storage_2", bus: "48", label: "BESS @ Bus 48", actionType: "discharge_battery", amountMw: 0.31, costPerMw: 10 },
];
const s1Iter2: AppliedAction[] = [
  { targetId: "load_19", bus: "47", label: "Load @ Bus 47", actionType: "curtail_load", amountMw: 0.12, costPerMw: 10000 },
];

// ----------------------------------------------------------------------------
// Scenario 2 — Clear midday solar surge, reverse-flow overloads on L19 (18→21).
//   Fix by charging BESS + spilling utility PV (no load shed needed).
// ----------------------------------------------------------------------------
const s2Iter1: AppliedAction[] = [
  { targetId: "storage_0", bus: "76", label: "BESS @ Bus 76", actionType: "charge_battery", amountMw: 0.42, costPerMw: 10 },
  { targetId: "sgen_6", bus: "108", label: "Utility PV @ Bus 108", actionType: "curtail_renewable", amountMw: 0.28, costPerMw: 24 },
];

// ----------------------------------------------------------------------------
// Scenario 3 — Storm wind cut-out + rising demand, Line L55 (54→57) out.
// ----------------------------------------------------------------------------
const s3Iter1: AppliedAction[] = [
  { targetId: "storage_1", bus: "65", label: "BESS @ Bus 65", actionType: "discharge_battery", amountMw: 0.39, costPerMw: 10 },
  { targetId: "storage_2", bus: "48", label: "BESS @ Bus 48", actionType: "discharge_battery", amountMw: 0.34, costPerMw: 10 },
];
const s3Iter2: AppliedAction[] = [
  { targetId: "load_31", bus: "60", label: "Load @ Bus 60", actionType: "curtail_load", amountMw: 0.18, costPerMw: 10000 },
];

export const SCENARIOS: Scenario[] = [
  {
    id: "n1-line13",
    name: "N-1 · Line L13 (Heatwave Peak)",
    network: "IEEE 123-node feeder",
    issue: "Heatwave peak + loss of backbone Line L13 overloads 12 corridors to 136% and pulls 49 buses below 0.95 pu",
    severity: "Critical",
    faultLine: { index: 13, name: "L13", from: "13", to: "18" },
    weather: { profile: "heatwave", hour: 16, forecast: heatwaveForecast(16) },
    bess: [
      { id: "storage_0", bus: "76", powerMw: 0.39, energyMwh: 0.96, socPct: 60 },
      { id: "storage_1", bus: "65", powerMw: 0.39, energyMwh: 0.96, socPct: 60 },
      { id: "storage_2", bus: "48", powerMw: 0.39, energyMwh: 0.96, socPct: 60 },
    ],
    assetHealth: {
      derated: [
        { line: "L42", healthPct: 47, deratePct: 88 },
        { line: "L97", healthPct: 41, deratePct: 85 },
      ],
      maintenance: "L116 scheduled (ring tie kept closed)",
    },
    screeningSummary: { total: 124, catastrophic: 3, dangerous: 9, safe: 105, islanding: 7 },
    screening: [
      { lineIndex: 13, name: "L13", converged: true, nOverloaded: 12, maxLoadingPct: 136.4, riskScore: 163.7 },
      { lineIndex: 7, name: "L7", converged: true, nOverloaded: 8, maxLoadingPct: 128.1, riskScore: 102.5 },
      { lineIndex: 55, name: "L55", converged: true, nOverloaded: 5, maxLoadingPct: 118.9, riskScore: 59.5 },
      { lineIndex: 19, name: "L19", converged: true, nOverloaded: 3, maxLoadingPct: 109.4, riskScore: 32.8 },
      { lineIndex: 35, name: "L35", converged: false, nOverloaded: 0, maxLoadingPct: 0, riskScore: 999, islandKw: 184 },
      { lineIndex: 22, name: "L22", converged: true, nOverloaded: 1, maxLoadingPct: 101.2, riskScore: 10.1 },
    ],
    postFault: {
      maxLoading: 136.4,
      undervoltBuses: 49,
      overloaded: [
        { lineIndex: 7, name: "L7", from: "7", to: "8", loading: 136.4 },
        { lineIndex: 3, name: "L3", from: "1", to: "7", loading: 128.1 },
        { lineIndex: 10, name: "L10", from: "8", to: "13", loading: 121.7 },
        { lineIndex: 19, name: "L19", from: "18", to: "21", loading: 112.3 },
      ],
    },
    actionSpace: [
      { targetId: "storage_0", bus: "76", label: "BESS @ Bus 76", actionType: "discharge_battery", maxAvailableMw: 0.39, costPerMw: 10, sensitivity: -14.2, targetLine: "L7", socPct: 60, recommended: true },
      { targetId: "storage_1", bus: "65", label: "BESS @ Bus 65", actionType: "discharge_battery", maxAvailableMw: 0.39, costPerMw: 10, sensitivity: -12.6, targetLine: "L7", socPct: 60 },
      { targetId: "storage_2", bus: "48", label: "BESS @ Bus 48", actionType: "discharge_battery", maxAvailableMw: 0.39, costPerMw: 10, sensitivity: -9.8, targetLine: "L3", socPct: 60 },
      { targetId: "sgen_5", bus: "111", label: "Wind farm @ Bus 111", actionType: "curtail_renewable", maxAvailableMw: 0.04, costPerMw: 22, sensitivity: 6.1, targetLine: "L7" },
      { targetId: "load_19", bus: "47", label: "Load @ Bus 47", actionType: "curtail_load", maxAvailableMw: 0.55, costPerMw: 10000, sensitivity: -18.4, targetLine: "L7" },
      { targetId: "load_38", bus: "76", label: "CRITICAL Load @ Bus 76", actionType: "curtail_load", maxAvailableMw: 0.41, costPerMw: 100000, sensitivity: -16.9, targetLine: "L7" },
    ],
    iterations: [
      {
        iteration: 1,
        reasoning:
          "L7 is the worst corridor at 136.4%. The context block shows three BESS at 60% SoC and a 78 EUR/MWh balancing price — batteries are the cheapest MW at 10 EUR/MWh (cycle wear). Discharging all three (−14.2 / −12.6 / −9.8 pp/MW) into the substation backbone clears 11 of 12 overloads and lifts 47 of 49 undervoltage buses.",
        actions: s1Iter1,
        maxLoadingBefore: 136.4,
        maxLoadingAfter: 101.3,
        overloadsBefore: 12,
        overloadsAfter: 1,
        undervoltBefore: 49,
        undervoltAfter: 2,
        costEur: cost(s1Iter1),
      },
      {
        iteration: 2,
        reasoning:
          "L7 still marginally over at 101.3% with the BESS fleet exhausted for this hour. A surgical 0.12 MW shed of non-critical load at Bus 47 (−18.4 pp/MW) clears the last overload to 96.2% — the critical load at Bus 76 (100,000 EUR/MWh VoLL) is left untouched.",
        actions: s1Iter2,
        maxLoadingBefore: 101.3,
        maxLoadingAfter: 96.2,
        overloadsBefore: 1,
        overloadsAfter: 0,
        undervoltBefore: 2,
        undervoltAfter: 0,
        costEur: cost(s1Iter2),
      },
    ],
    baseline: {
      llm: { secure: true, costEur: cost(s1Iter1) + cost(s1Iter2), actions: 4 },
      lp: { secure: true, costEur: cost(s1Iter1) + cost(s1Iter2) + 180, actions: 6 },
    },
    totalCost: cost(s1Iter1) + cost(s1Iter2),
    finalMaxLoading: 96.2,
  },
  {
    id: "n1-line19",
    name: "N-1 · Line L19 (Solar Reverse-Flow)",
    network: "IEEE 123-node feeder",
    issue: "Clear midday PV surge + loss of Line L19 drives reverse flow on L13 to 107.8% (export overload)",
    severity: "Warning",
    faultLine: { index: 19, name: "L19", from: "18", to: "21" },
    weather: { profile: "clear", hour: 12, forecast: clearMiddayForecast(11) },
    bess: [
      { id: "storage_0", bus: "76", powerMw: 0.39, energyMwh: 0.96, socPct: 35 },
      { id: "storage_1", bus: "65", powerMw: 0.39, energyMwh: 0.96, socPct: 40 },
      { id: "storage_2", bus: "48", powerMw: 0.39, energyMwh: 0.96, socPct: 30 },
    ],
    assetHealth: {
      derated: [
        { line: "L42", healthPct: 47, deratePct: 88 },
        { line: "L97", healthPct: 41, deratePct: 85 },
      ],
      maintenance: "L116 scheduled (ring tie kept closed)",
    },
    screeningSummary: { total: 124, catastrophic: 1, dangerous: 4, safe: 112, islanding: 7 },
    screening: [
      { lineIndex: 19, name: "L19", converged: true, nOverloaded: 2, maxLoadingPct: 107.8, riskScore: 21.6 },
      { lineIndex: 13, name: "L13", converged: true, nOverloaded: 1, maxLoadingPct: 103.1, riskScore: 10.3 },
      { lineIndex: 7, name: "L7", converged: true, nOverloaded: 1, maxLoadingPct: 100.4, riskScore: 7.4 },
      { lineIndex: 35, name: "L35", converged: false, nOverloaded: 0, maxLoadingPct: 0, riskScore: 999, islandKw: 184 },
    ],
    postFault: {
      maxLoading: 107.8,
      undervoltBuses: 0,
      overloaded: [
        { lineIndex: 13, name: "L13", from: "13", to: "18", loading: 107.8 },
        { lineIndex: 10, name: "L10", from: "8", to: "13", loading: 102.5 },
      ],
    },
    actionSpace: [
      { targetId: "storage_0", bus: "76", label: "BESS @ Bus 76", actionType: "charge_battery", maxAvailableMw: 0.42, costPerMw: 10, sensitivity: -11.4, targetLine: "L13", socPct: 35, recommended: true },
      { targetId: "storage_2", bus: "48", label: "BESS @ Bus 48", actionType: "charge_battery", maxAvailableMw: 0.46, costPerMw: 10, sensitivity: -8.7, targetLine: "L13", socPct: 30 },
      { targetId: "sgen_6", bus: "108", label: "Utility PV @ Bus 108", actionType: "curtail_renewable", maxAvailableMw: 0.49, costPerMw: 24, sensitivity: -9.1, targetLine: "L13" },
      { targetId: "sgen_0", bus: "76", label: "Rooftop PV @ Bus 76", actionType: "curtail_renewable", maxAvailableMw: 0.24, costPerMw: 18, sensitivity: -6.2, targetLine: "L13" },
      { targetId: "load_19", bus: "47", label: "Load @ Bus 47", actionType: "curtail_load", maxAvailableMw: 0.55, costPerMw: 10000, sensitivity: 5.8, targetLine: "L13" },
    ],
    iterations: [
      {
        iteration: 1,
        reasoning:
          "This is an export overload — midday PV pushes reverse flow up L13 to 107.8%. Day-ahead price is 12 EUR/MWh (solar floods merit order), so absorbing energy is nearly free. Charge BESS @ Bus 76 (−11.4 pp/MW) to soak the surplus and spill 0.28 MW of utility PV (−9.1 pp/MW). Both overloads clear and the stored energy is positioned for the evening peak.",
        actions: s2Iter1,
        maxLoadingBefore: 107.8,
        maxLoadingAfter: 94.1,
        overloadsBefore: 2,
        overloadsAfter: 0,
        undervoltBefore: 0,
        undervoltAfter: 0,
        costEur: cost(s2Iter1),
      },
    ],
    baseline: {
      llm: { secure: true, costEur: cost(s2Iter1), actions: 2 },
      lp: { secure: true, costEur: cost(s2Iter1) + 60, actions: 3 },
    },
    totalCost: cost(s2Iter1),
    finalMaxLoading: 94.1,
  },
  {
    id: "n1-line55",
    name: "N-1 · Line L55 (Storm Wind Cut-out)",
    network: "IEEE 123-node feeder",
    issue: "Storm front cuts out the wind farms (>25 m/s) as demand climbs; loss of Line L55 overloads L7 to 119%",
    severity: "Critical",
    faultLine: { index: 55, name: "L55", from: "54", to: "57" },
    weather: { profile: "storm", hour: 18, forecast: stormForecast(15) },
    bess: [
      { id: "storage_0", bus: "76", powerMw: 0.39, energyMwh: 0.96, socPct: 55 },
      { id: "storage_1", bus: "65", powerMw: 0.39, energyMwh: 0.96, socPct: 58 },
      { id: "storage_2", bus: "48", powerMw: 0.39, energyMwh: 0.96, socPct: 52 },
    ],
    assetHealth: {
      derated: [
        { line: "L42", healthPct: 47, deratePct: 88 },
        { line: "L97", healthPct: 41, deratePct: 85 },
      ],
      maintenance: "L116 scheduled (ring tie kept closed)",
    },
    screeningSummary: { total: 124, catastrophic: 2, dangerous: 6, safe: 109, islanding: 7 },
    screening: [
      { lineIndex: 55, name: "L55", converged: true, nOverloaded: 5, maxLoadingPct: 118.9, riskScore: 59.5 },
      { lineIndex: 13, name: "L13", converged: true, nOverloaded: 7, maxLoadingPct: 124.0, riskScore: 86.8 },
      { lineIndex: 7, name: "L7", converged: true, nOverloaded: 4, maxLoadingPct: 115.2, riskScore: 46.1 },
      { lineIndex: 35, name: "L35", converged: false, nOverloaded: 0, maxLoadingPct: 0, riskScore: 999, islandKw: 184 },
    ],
    postFault: {
      maxLoading: 118.9,
      undervoltBuses: 21,
      overloaded: [
        { lineIndex: 7, name: "L7", from: "7", to: "8", loading: 118.9 },
        { lineIndex: 3, name: "L3", from: "1", to: "7", loading: 112.6 },
        { lineIndex: 10, name: "L10", from: "8", to: "13", loading: 104.8 },
      ],
    },
    actionSpace: [
      { targetId: "storage_1", bus: "65", label: "BESS @ Bus 65", actionType: "discharge_battery", maxAvailableMw: 0.39, costPerMw: 10, sensitivity: -13.1, targetLine: "L7", socPct: 58, recommended: true },
      { targetId: "storage_2", bus: "48", label: "BESS @ Bus 48", actionType: "discharge_battery", maxAvailableMw: 0.37, costPerMw: 10, sensitivity: -10.4, targetLine: "L7", socPct: 52 },
      { targetId: "storage_0", bus: "76", label: "BESS @ Bus 76", actionType: "discharge_battery", maxAvailableMw: 0.38, costPerMw: 10, sensitivity: -8.9, targetLine: "L3", socPct: 55 },
      { targetId: "load_31", bus: "60", label: "Load @ Bus 60", actionType: "curtail_load", maxAvailableMw: 0.48, costPerMw: 10000, sensitivity: -15.7, targetLine: "L7" },
      { targetId: "load_38", bus: "76", label: "CRITICAL Load @ Bus 76", actionType: "curtail_load", maxAvailableMw: 0.41, costPerMw: 100000, sensitivity: -14.2, targetLine: "L7" },
    ],
    iterations: [
      {
        iteration: 1,
        reasoning:
          "Wind output has collapsed to 0 (gusts above the 25 m/s cut-out) and the balancing price has spiked to 96 EUR/MWh. With renewables gone, the BESS fleet at >50% SoC is the only cheap relief. Discharge Bus 65 and Bus 48 (−13.1 / −10.4 pp/MW) clears 4 of 5 overloads.",
        actions: s3Iter1,
        maxLoadingBefore: 118.9,
        maxLoadingAfter: 102.6,
        overloadsBefore: 5,
        overloadsAfter: 1,
        undervoltBefore: 21,
        undervoltAfter: 3,
        costEur: cost(s3Iter1),
      },
      {
        iteration: 2,
        reasoning:
          "L7 remains at 102.6%. The storm forecast shows the cut-out persisting two more hours, so the agent shaves 0.18 MW of non-critical load at Bus 60 (−15.7 pp/MW) rather than draining the BESS reserve it must hold for the rest of the event. Final max loading 95.8%, full N-1 security restored.",
        actions: s3Iter2,
        maxLoadingBefore: 102.6,
        maxLoadingAfter: 95.8,
        overloadsBefore: 1,
        overloadsAfter: 0,
        undervoltBefore: 3,
        undervoltAfter: 0,
        costEur: cost(s3Iter2),
      },
    ],
    baseline: {
      llm: { secure: true, costEur: cost(s3Iter1) + cost(s3Iter2), actions: 3 },
      lp: { secure: true, costEur: cost(s3Iter1) + cost(s3Iter2) + 200, actions: 5 },
    },
    totalCost: cost(s3Iter1) + cost(s3Iter2),
    finalMaxLoading: 95.8,
  },
];

export const KPIS = {
  network: "IEEE 123-node feeder",
  buses: 130,
  activeLines: 124,
  derAssets: 9,
  peakLoad: "3.5 MW",
};
