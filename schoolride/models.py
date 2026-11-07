from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Set

STOP_PICKUP = "pickup"
STOP_SCHOOL = "school"
STOP_DEPOT = "depot"


@dataclass
class Stop:
    id: str
    stop_type: str
    planned_time: int
    load: int = 0
    school_id: str = ""
    bell_time: int | None = None

    def is_pickup(self) -> bool:
        return self.stop_type == STOP_PICKUP

    def is_school(self) -> bool:
        return self.stop_type == STOP_SCHOOL


@dataclass
class BusPlan:
    bus_id: str
    capacity: int
    start_time: int
    route_pre: List[str]


@dataclass
class BusState:
    bus_id: str
    eta: int
    loc: str
    onboard: Dict[str, int] = field(default_factory=dict)
    passed: List[str] = field(default_factory=list)
    remaining: List[str] = field(default_factory=list)
    active: bool = True

    @property
    def current_load(self) -> int:
        return sum(self.onboard.values())


@dataclass
class DisruptionEvent:
    removed_buses: Set[str] = field(default_factory=set)
    disrupted_arcs: Set[Tuple[str, str]] = field(default_factory=set)
    travel_time_updates: Dict[Tuple[str, str], int] = field(default_factory=dict)


@dataclass
class ObjectiveWeights:
    wait: float = 1.0
    school_delay: float = 1.0
    route_change: float = 0.1
    ride_violation: float = 1.0


@dataclass
class ConstraintSettings:
    enforce_c1_coverage: bool = True
    enforce_c2_uniqueness: bool = True
    enforce_c3_capacity: bool = True
    enforce_c4_mixed_load: bool = True
    enforce_c5_dropoff: bool = True
    enforce_c6_early_arrival: bool = True
    school_start_time: int = 8 * 60
    early_arrival_buffer: int = 5
    service_time_per_student: float = 0.25
    max_ride_time: int = 60


@dataclass
class SolverSettings:
    enable_pruning: bool = True
    max_candidate_buses: int = 4
    per_vehicle_top_n: int = 5
    global_top_m: int = 20
    infeasible_penalty: float = 1e8


@dataclass
class Settings:
    objective: ObjectiveWeights = field(default_factory=ObjectiveWeights)
    constraints: ConstraintSettings = field(default_factory=ConstraintSettings)
    solver: SolverSettings = field(default_factory=SolverSettings)


@dataclass
class Scenario:
    stops: Dict[str, Stop]
    buses: Dict[str, BusPlan]
    states: Dict[str, BusState]
    travel_time: Dict[Tuple[str, str], int]
    default_travel_time: int = 10**6

    def travel(self, from_stop: str, to_stop: str) -> int:
        if from_stop == to_stop:
            return 0
        return self.travel_time.get((from_stop, to_stop), self.default_travel_time)

    def school_for_pickup(self, pickup_stop_id: str) -> str:
        return self.stops[pickup_stop_id].school_id

    def school_bell_time(self, school_stop_id: str) -> int:
        stop = self.stops[school_stop_id]
        return stop.bell_time if stop.bell_time is not None else stop.planned_time
