"""
ieee123.py — IEEE 123-Node Test Feeder for pandapower.

Builds the IEEE 123-bus distribution feeder (4.16 kV, ~3.5 MW) from the
official OpenDSS test-case files (vendored in ./data/ieee123/, fetched
from the EPRI OpenDSS distribution via dss-extensions/electricdss-tst).

Modeling choices (balanced positive-sequence approximation):
  - Phase impedance matrices are reduced to positive sequence:
    z1 = mean(self) - mean(mutual). Single-phase laterals become
    three-phase equivalents with their per-phase impedance.
  - Lateral thermal ratings are divided by 3 (two-phase by 1.5) so that
    loading_percent matches the true per-phase current of the original
    unbalanced feeder.
  - The four voltage regulators and the 150 kVA service transformer are
    modeled as near-zero-impedance links (Kersting assumes ideal
    regulators); the source is held at 1.03 pu like a regulated
    substation bus.
  - Spot loads are aggregated per bus, capacitor banks become shunts.
  - The 8 sectionalizing switches are short, switchable lines; closing
    the normally-open ties (which the GridEnvironment does at startup)
    meshes the backbone so N-1 line outages become survivable.

Ampacity assumptions (the DSS files carry no ratings): 336.4 ACSR
backbone 600 A, two-phase laterals 180 A, single-phase laterals 225 A
(=75 A balanced-equivalent), underground 300 A, switches/links 800 A.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Tuple

import pandapower as pp

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "ieee123")

KFT_TO_KM = 0.3048  # 1 kft = 304.8 m

# max_i_ka per linecode (balanced-equivalent, see module docstring)
AMPACITY_KA = {
    **{str(c): 0.60 for c in range(1, 7)},   # 3-phase 336.4 ACSR backbone
    "7": 0.18, "8": 0.18,                     # two-phase laterals
    "9": 0.075, "10": 0.075, "11": 0.075,     # single-phase laterals
    "12": 0.30,                               # underground 3-phase
}
LINK_AMPACITY_KA = 0.80   # switches, regulators, service transformer links
SOURCE_BUS = "150"
# The real feeder holds the head at ~1.0 pu and four LDC voltage
# regulators boost the long laterals. We collapse the regulators to
# ideal links, so we lift the substation setpoint to 1.045 pu to stand
# in for their boost and keep the deep-feeder profile in band.
SOURCE_VM_PU = 1.045
VN_KV = 4.16


# ---------------------------------------------------------------------------
# DSS parsing helpers
# ---------------------------------------------------------------------------

def _statements(path: str) -> List[str]:
    """Read a DSS file into statements, joining '~' continuation lines and
    stripping '!' comments."""
    out: List[str] = []
    with open(path) as f:
        for raw in f:
            line = raw.split("!")[0].strip()
            if not line:
                continue
            if line.startswith("~"):
                if out:
                    out[-1] += " " + line[1:].strip()
            else:
                out.append(line)
    return out


def _kv_pairs(stmt: str) -> Dict[str, str]:
    """Parse key=value tokens (values may be [..] or (..) groups)."""
    pairs: Dict[str, str] = {}
    for m in re.finditer(r"(\w+)\s*=\s*(\[[^\]]*\]|\([^)]*\)|\S+)", stmt):
        pairs[m.group(1).lower()] = m.group(2)
    return pairs


def _matrix(text: str) -> List[List[float]]:
    """Parse a DSS lower-triangular matrix '[a | b c | d e f]'."""
    rows = []
    for chunk in text.strip("[]()").split("|"):
        vals = [float(v) for v in chunk.split()]
        if vals:
            rows.append(vals)
    return rows


def _pos_seq(mat: List[List[float]]) -> float:
    """Positive-sequence value from a lower-triangular phase matrix:
    mean(self) - mean(mutual). For a 1x1 matrix it is the value itself."""
    diag = [row[-1] for row in mat]
    off = [v for i, row in enumerate(mat) for v in row[:-1]]
    zs = sum(diag) / len(diag)
    zm = sum(off) / len(off) if off else 0.0
    return zs - zm


def _busname(token: str) -> str:
    """'9r.1' -> '9r', '1.1.2.3' -> '1'."""
    return token.split(".")[0].lower()


# ---------------------------------------------------------------------------
# File parsers
# ---------------------------------------------------------------------------

def parse_linecodes(path: str) -> Dict[str, dict]:
    codes: Dict[str, dict] = {}
    for stmt in _statements(path):
        low = stmt.lower()
        if not low.startswith("new linecode"):
            continue
        name = low.split()[1].split(".")[1]
        kv = _kv_pairs(stmt)
        codes[name] = {
            "r1": _pos_seq(_matrix(kv["rmatrix"])),     # ohm / kft
            "x1": _pos_seq(_matrix(kv["xmatrix"])),     # ohm / kft
            "c1": max(_pos_seq(_matrix(kv["cmatrix"])), 0.0),  # nF / kft
        }
    return codes


def parse_master(path: str) -> Tuple[List[dict], List[dict], List[dict]]:
    """Returns (lines, links, capacitors). Links are near-zero-impedance
    connections (head regulator, switches, service transformer)."""
    lines, links, caps = [], [], []
    for stmt in _statements(path):
        low = stmt.lower()
        kv = _kv_pairs(stmt)
        if low.startswith("new line."):
            name = stmt.split()[1].split(".")[1]
            b1, b2 = _busname(kv["bus1"]), _busname(kv["bus2"])
            # normally-open tie switches point at dummy '<bus>_open'
            # buses in the DSS model — map to the real bus, flag as open
            is_open = b1.endswith("_open") or b2.endswith("_open")
            rec = {
                "name": name,
                "from": b1.replace("_open", ""),
                "to": b2.replace("_open", ""),
            }
            if "switch" in kv and kv["switch"].lower().startswith("y"):
                rec["switch"] = True
                rec["open"] = is_open
                rec["phases"] = int(kv.get("phases", "3"))
                links.append(rec)
            else:
                rec["code"] = kv["linecode"].lower()
                rec["kft"] = float(kv["length"])
                rec["phases"] = int(kv.get("phases", "3"))
                lines.append(rec)
        elif low.startswith("new capacitor"):
            caps.append({
                "bus": _busname(kv["bus1"]),
                "kvar": float(kv["kvar"]),
            })
        elif low.startswith("new transformer"):
            # head regulator (150->150r) and the 150 kVA service
            # transformer (61s->610): both become near-zero-Z links.
            # Handles both the buses=[a b] form and the wdg=1 bus=a
            # wdg=2 bus=b continuation form.
            buses = kv.get("buses")
            if buses:
                names = [_busname(b) for b in buses.strip("[]").split()]
            else:
                names = [_busname(b) for b in
                         re.findall(r"bus\s*=\s*(\S+)", stmt, re.IGNORECASE)]
            names = list(dict.fromkeys(names))
            if len(names) == 2 and names[0] != names[1]:
                links.append({
                    "name": stmt.split()[1].split(".")[1],
                    "from": names[0], "to": names[1],
                })
    return lines, links, caps


def parse_regulators(path: str) -> List[dict]:
    """Mid-feeder regulator banks -> one link per distinct bus pair."""
    pairs = {}
    for stmt in _statements(path):
        if not stmt.lower().startswith("new transformer"):
            continue
        kv = _kv_pairs(stmt)
        buses = kv.get("buses")
        if not buses:
            continue
        names = [_busname(b) for b in buses.strip("[]").split()]
        if len(names) == 2 and names[0] != names[1]:
            pairs[(names[0], names[1])] = {
                "name": stmt.split()[1].split(".")[1],
                "from": names[0], "to": names[1],
            }
    return list(pairs.values())


def parse_loads(path: str) -> Dict[str, dict]:
    """Aggregate spot loads per bus (kW, kvar)."""
    loads: Dict[str, dict] = {}
    for stmt in _statements(path):
        if not stmt.lower().startswith("new load"):
            continue
        kv = _kv_pairs(stmt)
        bus = _busname(kv["bus1"])
        rec = loads.setdefault(bus, {"kw": 0.0, "kvar": 0.0})
        rec["kw"] += float(kv.get("kw", 0))
        rec["kvar"] += float(kv.get("kvar", 0))
    return loads


def parse_coords(path: str) -> Dict[str, Tuple[float, float]]:
    coords = {}
    with open(path) as f:
        for raw in f:
            parts = raw.replace(",", " ").split()
            if len(parts) >= 3:
                try:
                    coords[parts[0].lower()] = (float(parts[1]), float(parts[2]))
                except ValueError:
                    continue
    return coords


# ---------------------------------------------------------------------------
# Network builder
# ---------------------------------------------------------------------------

def build_ieee123(data_dir: str = DATA_DIR) -> pp.pandapowerNet:
    """Build the IEEE 123-node feeder as a pandapower network."""
    codes = parse_linecodes(os.path.join(data_dir, "IEEELineCodes.DSS"))
    lines, links, caps = parse_master(os.path.join(data_dir, "IEEE123Master.dss"))
    links += parse_regulators(os.path.join(data_dir, "IEEE123Regulators.DSS"))
    loads = parse_loads(os.path.join(data_dir, "IEEE123Loads.DSS"))
    coords = parse_coords(os.path.join(data_dir, "BusCoords.dat"))

    net = pp.create_empty_network(name="ieee123", sn_mva=10.0)

    # --- buses -----------------------------------------------------------
    bus_names: List[str] = []
    for rec in lines + links:
        for b in (rec["from"], rec["to"]):
            if b not in bus_names:
                bus_names.append(b)
    for b in loads:
        if b not in bus_names:
            bus_names.append(b)

    # derived buses (150r, 9r, 61s, 610...) inherit a neighbor's coords
    def coord_of(name: str) -> Tuple[float, float]:
        if name in coords:
            return coords[name]
        base = name.rstrip("rs")
        if base in coords:
            x, y = coords[base]
            return (x + 40.0, y + 40.0)
        return (0.0, 0.0)

    bus_idx: Dict[str, int] = {}
    for name in bus_names:
        x, y = coord_of(name)
        bus_idx[name] = pp.create_bus(net, vn_kv=VN_KV, name=name,
                                      geodata=(x, y))

    pp.create_ext_grid(net, bus_idx[SOURCE_BUS], vm_pu=SOURCE_VM_PU,
                       name="substation_150")

    # --- line segments -----------------------------------------------------
    for rec in lines:
        code = codes[rec["code"]]
        km = rec["kft"] * KFT_TO_KM
        amp = AMPACITY_KA.get(rec["code"], 0.30)
        pp.create_line_from_parameters(
            net,
            from_bus=bus_idx[rec["from"]],
            to_bus=bus_idx[rec["to"]],
            length_km=km,
            r_ohm_per_km=code["r1"] / KFT_TO_KM,
            x_ohm_per_km=code["x1"] / KFT_TO_KM,
            c_nf_per_km=code["c1"] / KFT_TO_KM,
            max_i_ka=amp,
            name=rec["name"],
        )

    # --- switches / regulators / service transformer as short links --------
    tie_lines = []
    for rec in links:
        idx = pp.create_line_from_parameters(
            net,
            from_bus=bus_idx[rec["from"]],
            to_bus=bus_idx[rec["to"]],
            length_km=0.001,
            r_ohm_per_km=0.01,
            x_ohm_per_km=0.01,
            c_nf_per_km=0.0,
            max_i_ka=LINK_AMPACITY_KA,
            name=rec["name"],
            in_service=not rec.get("open", False),
        )
        if rec.get("open") and rec.get("phases", 3) == 3:
            tie_lines.append(int(idx))
    # three-phase normally-open ties (Sw7: 151-300); closing them meshes
    # the backbone so N-1 line outages become survivable. The 1-phase
    # tie (Sw8: 54-94) stays open — meshing through a single-phase spur
    # would overload it with backbone through-flow.
    net["tie_lines"] = tie_lines

    # --- loads and capacitor banks -----------------------------------------
    for bus, rec in sorted(loads.items()):
        pp.create_load(net, bus_idx[bus], p_mw=rec["kw"] / 1000.0,
                       q_mvar=rec["kvar"] / 1000.0, name=f"load_{bus}")
    for cap in caps:
        pp.create_shunt(net, bus_idx[cap["bus"]],
                        q_mvar=-cap["kvar"] / 1000.0,
                        name=f"cap_{cap['bus']}")

    # distribution feeders run a wider band than transmission (ANSI-ish)
    net["vmin_pu"] = 0.92
    net["vmax_pu"] = 1.08
    return net


if __name__ == "__main__":
    net = build_ieee123()
    pp.runpp(net, numba=False)
    print(f"IEEE 123 feeder: {len(net.bus)} buses, {len(net.line)} lines, "
          f"{len(net.load)} load buses, "
          f"{net.load.p_mw.sum() * 1000:.0f} kW / "
          f"{net.load.q_mvar.sum() * 1000:.0f} kvar")
    print(f"slack: {net.res_ext_grid.p_mw.sum() * 1000:.0f} kW, "
          f"losses: {(net.res_ext_grid.p_mw.sum() - net.load.p_mw.sum()) * 1000:.0f} kW")
    print(f"voltage range: {net.res_bus.vm_pu.min():.4f} - "
          f"{net.res_bus.vm_pu.max():.4f} pu")
    print(f"max line loading: {net.res_line.loading_percent.max():.1f}%")
    top = net.res_line.loading_percent.sort_values(ascending=False).head(5)
    for i, v in top.items():
        print(f"  {net.line.at[i, 'name']:>6}: {v:.1f}%")
