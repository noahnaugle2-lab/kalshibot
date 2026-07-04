"""Fill simulation and tape replay — shadow mode and parameter iteration."""

from kalshibot.sim.fills import FillResult, SimFill, settle_position, simulate_maker, simulate_taker
from kalshibot.sim.replay import ReplayEngine, ReplayResult

__all__ = [
    "FillResult",
    "SimFill",
    "settle_position",
    "simulate_maker",
    "simulate_taker",
    "ReplayEngine",
    "ReplayResult",
]
