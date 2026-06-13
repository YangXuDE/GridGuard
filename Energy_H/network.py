"""Adapter module — provides clone/runpf for baseline.py compatibility."""

import copy

import pandapower as pp

OVERLOAD_PCT = 100.0


def clone(net):
    """Deep-copy a pandapower network."""
    return copy.deepcopy(net)


def runpf(net, dc=False):
    """Run power flow.  dc=True uses the DC (linear) approximation."""
    try:
        if dc:
            pp.rundcpp(net)
        else:
            pp.runpp(net, numba=False)
        return True
    except pp.LoadflowNotConverged:
        return False
