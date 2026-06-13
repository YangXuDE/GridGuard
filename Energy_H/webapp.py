#!/usr/bin/env python3
"""Energy_H web console — IEEE 123 self-healing grid agent.

    python webapp.py            # http://localhost:8051

Build a stressed IEEE 123 distribution-feeder scenario in the browser,
watch the live one-line diagram, run the two-stage N-1 screen, trip a
line, then fix the grid by hand, with the LP baseline, or with the
DeepSeek LLM operator (its reasoning streams into the log over SSE).
Set DEEPSEEK_API_KEY before starting for the LLM button to work.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import uuid

from flask import Flask, Response, jsonify, request, send_from_directory

from grid_environment import GridEnvironment
from grid_agent import GridAgent


def load_dotenv(path: str = ".env") -> None:
    """Read KEY=VALUE lines from ./.env into os.environ (keeps the API key
    out of the shell profile and out of source)."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


load_dotenv()
app = Flask(__name__, static_folder="static")

SESSIONS: dict[str, "Session"] = {}
LOCKS: dict[str, threading.Lock] = {}


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------

class Session:
    """A live GridEnvironment plus cumulative cost and action log."""

    def __init__(self, load_scale: float, weather: str | None, hour: int):
        self.env = GridEnvironment(
            "ieee123", load_scale=load_scale, weather=weather,
            weather_hour=hour, with_der=True, with_asset_health=True,
        )
        self.env.run_powerflow()
        self.cost = 0.0
        self.action_log: list[dict] = []
        self.faulted: int | None = None

    # ---- cost of a list of GridEnvironment-format actions --------------
    def action_cost(self, actions: list[dict]) -> float:
        state = self.env.get_grid_state()
        space = self.env.get_action_space(grid_state=state)
        cost_map = {(a.target_id, a.action_type): a.cost_per_mw for a in space}
        total = 0.0
        for act in actions:
            key = (act.get("target_id"), act.get("action_type"))
            total += float(act.get("amount_mw", 0.0)) * cost_map.get(key, 0.0)
        return total


# ---------------------------------------------------------------------------
# serialization for the front-end
# ---------------------------------------------------------------------------

def _topology(env: GridEnvironment) -> dict:
    net = env.net
    health = net.get("asset_health") or {}
    line_health = health.get("lines", {})
    maint = set(health.get("maintenance_lines", []))
    sched = set(health.get("maintenance_scheduled", []))

    buses = []
    for idx, row in net.bus.iterrows():
        try:
            x, y = json.loads(row.geo)["coordinates"]
        except (TypeError, KeyError, ValueError):
            x, y = 0.0, 0.0
        buses.append({"bus": int(idx), "name": str(row["name"]),
                      "x": float(x), "y": float(y)})

    lines = []
    for idx, row in net.line.iterrows():
        in_serv = bool(row.in_service)
        loading = p = q = None
        if in_serv and idx in net.res_line.index:
            v = net.res_line.at[idx, "loading_percent"]
            if v == v:  # NaN guard
                loading = round(float(v), 1)
                p = round(float(net.res_line.at[idx, "p_from_mw"]), 3)
                q = round(float(net.res_line.at[idx, "q_from_mvar"]), 3)
        rec = line_health.get(int(idx), {})
        lines.append({
            "line": int(idx), "name": str(row["name"]),
            "from_bus": int(row.from_bus), "to_bus": int(row.to_bus),
            "in_service": in_serv,
            "faulted": int(idx) == env_faulted(env),
            "maintenance": int(idx) in maint or int(idx) in sched,
            "loading_pct": loading, "p_mw": p, "q_mvar": q,
            "health": rec.get("health"), "derate": rec.get("derate"),
        })

    sgens = [
        {"sgen": int(i), "bus": int(s.bus), "type": str(s.type or "PV"),
         "p_mw": round(float(s.p_mw), 3), "capacity_mw": round(float(s.max_p_mw), 3)}
        for i, s in net.sgen.iterrows()
    ]
    storages = [
        {"storage": int(i), "bus": int(s.bus),
         "output_mw": round(-float(s.p_mw), 3),
         "p_max_mw": round(float(s.max_p_mw), 3),
         "soc_percent": round(float(s.soc_percent), 1)}
        for i, s in net.storage.iterrows()
    ]
    loads = [
        {"load": int(i), "bus": int(l.bus), "p_mw": round(float(l.p_mw), 3)}
        for i, l in net.load.iterrows()
    ]
    slack = [int(b) for b in net.ext_grid.bus]
    return {"buses": buses, "lines": lines, "sgens": sgens,
            "storages": storages, "loads": loads, "slack_buses": slack}


def env_faulted(env: GridEnvironment) -> int:
    return getattr(env, "_web_faulted", -1)


def _state(sess: Session) -> dict:
    env = sess.env
    setattr(env, "_web_faulted", sess.faulted if sess.faulted is not None else -1)
    state = env.get_grid_state()
    wx = env.net.get("weather")
    overloads = [
        {"line": ol.line_index, "name": ol.name,
         "loading_percent": ol.loading_percent}
        for ol in state.overloaded_lines
    ]
    total_load = round(float(env.net.load.p_mw.sum()), 3)
    pv = round(float(env.net.sgen.loc[env.net.sgen.type != "WT", "p_mw"].sum()), 3) \
        if len(env.net.sgen) else 0.0
    wt = round(float(env.net.sgen.loc[env.net.sgen.type == "WT", "p_mw"].sum()), 3) \
        if len(env.net.sgen) else 0.0
    return {
        "topology": _topology(env),
        "cost": round(sess.cost, 0),
        "faulted": sess.faulted,
        "converged": state.converged,
        "max_loading_pct": state.max_loading_percent,
        "overloads": overloads,
        "voltage_violations": state.voltage_violations,
        "base_secure": state.converged and not overloads
                       and not state.voltage_violations,
        "total_load_mw": total_load,
        "solar_mw": pv, "wind_mw": wt,
        "weather": {
            "profile": wx["profile"], "hour": wx["hour"],
            "now": wx.get("now"), "forecast": wx["forecast"],
        } if wx else None,
        "asset_health": env.get_asset_health(),
    }


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/scenario")
def new_scenario():
    body = request.get_json(force=True) or {}
    load_scale = float(body.get("load_scale", 1.3))
    weather = body.get("weather", "heatwave")
    if weather == "none":
        weather = None
    hour = int(body.get("hour", 16))
    try:
        sess = Session(load_scale, weather, hour)
    except Exception as exc:
        return jsonify({"error": f"could not build scenario: {exc}"}), 400
    sid = uuid.uuid4().hex[:12]
    SESSIONS[sid] = sess
    LOCKS[sid] = threading.Lock()
    return jsonify({"sid": sid, **_state(sess)})


@app.get("/api/state/<sid>")
def get_state(sid):
    sess = SESSIONS.get(sid)
    if sess is None:
        return jsonify({"error": "unknown session"}), 404
    return jsonify(_state(sess))


@app.post("/api/screen/<sid>")
def screen(sid):
    sess = SESSIONS.get(sid)
    if sess is None:
        return jsonify({"error": "unknown session"}), 404
    with LOCKS[sid]:
        risks = sess.env.screen_all_contingencies()
    dangerous, islanding = [], []
    for r in risks:
        if r.get("islands"):
            islanding.append({"line": r["line_index"], "name": r["name"],
                              "unserved_kw": r["unserved_kw"],
                              "buses": r["islands"]})
        elif r["n_overloaded"] > 0 or not r["converged"]:
            dangerous.append({"line": r["line_index"], "name": r["name"],
                              "n_overloaded": r["n_overloaded"],
                              "max_loading_pct": r["max_loading_pct"],
                              "risk_score": r["risk_score"],
                              "converged": r["converged"]})
    n_ac = sum(1 for r in risks if r.get("stage") == "ac")
    return jsonify({
        "n_screened": len(risks), "n_ac_solved": n_ac,
        "n_dangerous": len(dangerous), "n_islanding": len(islanding),
        "secure": not dangerous,
        "dangerous": dangerous[:25],
        "islanding": sorted(islanding, key=lambda d: -d["unserved_kw"])[:5],
    })


@app.post("/api/fault/<sid>")
def trigger_fault(sid):
    sess = SESSIONS.get(sid)
    if sess is None:
        return jsonify({"error": "unknown session"}), 404
    body = request.get_json(force=True) or {}
    with LOCKS[sid]:
        line = body.get("line")
        if line is None:
            # auto-pick the worst dangerous (non-islanding) contingency
            risks = sess.env.screen_all_contingencies()
            dang = [r for r in risks if r["converged"]
                    and r["n_overloaded"] > 0 and not r.get("islands")]
            if not dang:
                return jsonify({"error": "grid is N-1 secure — no dangerous "
                                "contingency to trip"}), 400
            line = dang[0]["line_index"]
        line = int(line)
        try:
            sess.env.trigger_n_1_fault(line)
            sess.env.run_powerflow()
            sess.faulted = line
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"faulted_line": line, **_state(sess)})


@app.post("/api/advance/<sid>")
def advance(sid):
    sess = SESSIONS.get(sid)
    if sess is None:
        return jsonify({"error": "unknown session"}), 404
    with LOCKS[sid]:
        r = sess.env.advance_hour()
        if not r["ok"]:
            return jsonify({"ok": False, "error": r.get("error"), **_state(sess)})
        return jsonify({"ok": True, "weather_now": r["weather"], **_state(sess)})


@app.get("/api/scada/<sid>")
def scada(sid):
    sess = SESSIONS.get(sid)
    if sess is None:
        return jsonify({"error": "unknown session"}), 404
    return jsonify(sess.env.get_scada_measurements())


@app.post("/api/apply/<sid>")
def apply_manual(sid):
    """Apply a list of GridEnvironment-format actions (manual operator)."""
    sess = SESSIONS.get(sid)
    if sess is None:
        return jsonify({"error": "unknown session"}), 404
    body = request.get_json(force=True) or {}
    actions = body.get("actions", [])
    with LOCKS[sid]:
        cost = sess.action_cost(actions)
        ok = sess.env.execute_actions(actions)
        if not ok:
            return jsonify({"ok": False, "error": "action execution failed",
                            **_state(sess)})
        sess.cost += cost
        sess.action_log.extend(actions)
        return jsonify({"ok": True, "applied_cost": round(cost, 0), **_state(sess)})


@app.post("/api/baseline/<sid>")
def baseline(sid):
    sess = SESSIONS.get(sid)
    if sess is None:
        return jsonify({"error": "unknown session"}), 404
    from env import GridEnv
    from baseline import run_baseline
    with LOCKS[sid]:
        adapter = GridEnv(sess.env)
        result = run_baseline(adapter, target_n1=False, verbose=False)
        sess.cost += adapter.cost
        sess.action_log.extend(adapter.action_log)
        return jsonify({
            "result": {
                "base_secure": result["base_secure"],
                "cost": round(adapter.cost, 0),
                "n_actions": result["n_actions"],
                "actions": adapter.action_log,
            },
            **_state(sess),
        })


@app.get("/api/llm/<sid>")
def llm_stream(sid):
    """Run the DeepSeek operator agent, streaming SSE events."""
    sess = SESSIONS.get(sid)
    if sess is None:
        return jsonify({"error": "unknown session"}), 404
    api_key = os.getenv("DEEPSEEK_API_KEY")
    q: queue.Queue = queue.Queue()

    def worker():
        if not api_key:
            q.put({"type": "error",
                   "message": "DEEPSEEK_API_KEY not set — export it before "
                              "starting webapp.py to use the LLM agent."})
            q.put(None)
            return
        try:
            agent = GridAgent(api_key=api_key)
            q.put({"type": "sys", "message": f"DeepSeek model {agent.model}"})
            with LOCKS[sid]:
                for it in range(1, 4):
                    state = sess.env.get_grid_state()
                    if not state.overloaded_lines and not state.voltage_violations:
                        q.put({"type": "sys", "message": "Grid secure — no overloads remain."})
                        break
                    space = sess.env.get_action_space(grid_state=state)
                    if not space:
                        q.put({"type": "sys", "message": "Action space empty."})
                        break
                    decision = agent.get_decision(state, space,
                                                  context=sess.env.context_block())
                    reasoning = decision.get("reasoning", "")
                    if reasoning:
                        q.put({"type": "narration", "text": reasoning})
                    actions = decision.get("actions", [])
                    if not actions:
                        q.put({"type": "sys", "message": "No actions proposed."})
                        break
                    for a in actions:
                        q.put({"type": "action",
                               "text": f"{a['action_type']} {a['target_id']} "
                                       f"{a['amount_mw']:.3f} MW"})
                    cost = sess.action_cost(actions)
                    sess.env.execute_actions(actions)
                    sess.cost += cost
                    sess.action_log.extend(actions)
                    q.put({"type": "sys",
                           "message": f"Iteration {it}: {len(actions)} action(s), "
                                      f"EUR {cost:,.0f}."})
            final = sess.env.get_grid_state()
            q.put({"type": "done", "result": {
                "base_secure": final.converged and not final.overloaded_lines
                               and not final.voltage_violations,
                "max_loading_pct": final.max_loading_percent,
                "cost": round(sess.cost, 0),
            }})
        except Exception as exc:
            q.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def gen():
        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8051))
    print(f"Energy_H console -> http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
