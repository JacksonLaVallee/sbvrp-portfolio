from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Tuple

from .models import Scenario, Settings


@dataclass
class RouteStep:
    stop_id: str
    arrival: int
    load_before: int
    load_after: int


@dataclass
class BusSimulation:
    bus_id: str
    steps: List[RouteStep] = field(default_factory=list)
    wait_penalty: float = 0.0
    school_delay_penalty: float = 0.0
    ride_violation_penalty: float = 0.0
    violations: List[str] = field(default_factory=list)


@dataclass
class EvaluationResult:
    objective: float
    wait_penalty: float
    school_delay_penalty: float
    route_change_penalty: float
    ride_violation_penalty: float
    feasible: bool
    violations: Dict[str, List[str]]
    bus_results: Dict[str, BusSimulation]


def _edges(path: Iterable[str]) -> set[Tuple[str, str]]:
    seq = list(path)
    return {(seq[i], seq[i + 1]) for i in range(len(seq) - 1)}


def _route_change_penalty(scenario: Scenario, routes: Dict[str, List[str]]) -> float:
    total = 0.0
    for bus_id, new_route in routes.items():
        state = scenario.states[bus_id]
        pre = [state.loc] + list(state.remaining)
        new = [state.loc] + list(new_route)
        pre_edges = _edges(pre)
        new_edges = _edges(new)
        total += len(new_edges - pre_edges) + len(pre_edges - new_edges)
    return total


def _school_id_for_stop(scenario: Scenario, stop_id: str) -> str:
    stop = scenario.stops[stop_id]
    if stop.is_school():
        return stop.id
    return stop.school_id


def simulate_bus_route(
    scenario: Scenario,
    settings: Settings,
    bus_id: str,
    route: List[str],
) -> BusSimulation:
    state = scenario.states[bus_id]
    plan = scenario.buses[bus_id]
    conf = settings.constraints

    sim = BusSimulation(bus_id=bus_id)

    school_positions: Dict[str, List[int]] = defaultdict(list)
    for idx, stop_id in enumerate(route):
        stop = scenario.stops[stop_id]
        if stop.is_school():
            school_positions[stop.id].append(idx)

    if conf.enforce_c5_dropoff:
        for idx, stop_id in enumerate(route):
            stop = scenario.stops[stop_id]
            if not stop.is_pickup():
                continue
            school_id = stop.school_id
            future = school_positions.get(school_id, [])
            if not any(pos > idx for pos in future):
                sim.violations.append(
                    f"C5: pickup {stop_id} on {bus_id} has no downstream school drop-off {school_id}."
                )
        for school_id, load in state.onboard.items():
            if load <= 0:
                continue
            if not school_positions.get(school_id):
                sim.violations.append(
                    f"C5: onboard students for {school_id} on {bus_id} have no downstream drop-off."
                )

    onboard = {school: int(load) for school, load in state.onboard.items() if load > 0}
    ride_queues: Dict[str, List[Tuple[str, int, int]]] = defaultdict(list)
    for school_id, load in onboard.items():
        # For in-progress trips we do not know individual pickup times from telemetry.
        # Seed at current ETA so we do not inject artificial ride-time violations.
        seed_time = state.eta
        ride_queues[school_id].append((f"__initial__:{bus_id}:{school_id}", seed_time, load))

    now = state.eta
    loc = state.loc
    last_service_students = 0

    for stop_id in route:
        stop = scenario.stops[stop_id]

        travel = scenario.travel(loc, stop_id)
        raw_arrival = now + travel + int(round(last_service_students * conf.service_time_per_student))
        planned = stop.planned_time

        if conf.enforce_c6_early_arrival and stop.is_pickup():
            earliest = planned - conf.early_arrival_buffer
            if raw_arrival < earliest:
                sim.violations.append(
                    f"C6: early arrival at {stop_id} on {bus_id} ({raw_arrival} < {earliest})."
                )

        arrival = max(raw_arrival, planned) if stop.is_pickup() else raw_arrival

        load_before = sum(onboard.values())
        loaded = 0
        dropped = 0

        if stop.is_pickup():
            school_id = stop.school_id
            if school_id:
                nonempty_schools = {s for s, qty in onboard.items() if qty > 0}
                if conf.enforce_c4_mixed_load and nonempty_schools and school_id not in nonempty_schools:
                    sim.violations.append(
                        f"C4: mixed-load attempt on {bus_id} at pickup {stop_id} for school {school_id}."
                    )
                loaded = max(0, int(stop.load))
                onboard[school_id] = onboard.get(school_id, 0) + loaded
                ride_queues[school_id].append((stop_id, arrival, loaded))
                sim.wait_penalty += max(0, arrival - planned) * loaded

        elif stop.is_school():
            school_id = stop.id
            dropped = onboard.get(school_id, 0)
            school_deadline = min(
                scenario.school_bell_time(school_id),
                conf.school_start_time,
            )
            sim.school_delay_penalty += max(0, arrival - school_deadline) * dropped
            if dropped > 0 and arrival > school_deadline:
                sim.violations.append(
                    f"T2: school {school_id} on {bus_id} arrives after the start time "
                    f"({arrival} > {school_deadline})."
                )

            for pickup_id, pickup_time, qty in ride_queues.get(school_id, []):
                overage = max(0, arrival - pickup_time - conf.max_ride_time)
                sim.ride_violation_penalty += overage * qty
                if overage <= 0:
                    continue

                if pickup_id.startswith("__initial__:"):
                    sim.violations.append(
                        "T1: existing onboard students "
                        f"for {school_id} on {bus_id} remain onboard for {arrival - pickup_time} "
                        f"minutes ({conf.max_ride_time} max)."
                    )
                else:
                    sim.violations.append(
                        f"T1: pickup {pickup_id} on {bus_id} remains onboard for "
                        f"{arrival - pickup_time} minutes before {school_id} "
                        f"({conf.max_ride_time} max)."
                    )
            ride_queues[school_id] = []

            if dropped > 0:
                onboard.pop(school_id, None)

            if conf.enforce_c4_mixed_load and sum(onboard.values()) > 0:
                sim.violations.append(
                    f"C4: load not empty after school drop-off {school_id} on {bus_id}."
                )

        load_after = sum(onboard.values())
        if conf.enforce_c3_capacity and load_after > plan.capacity:
            sim.violations.append(
                f"C3: capacity exceeded on {bus_id} at {stop_id} ({load_after} > {plan.capacity})."
            )

        sim.steps.append(
            RouteStep(
                stop_id=stop_id,
                arrival=arrival,
                load_before=load_before,
                load_after=load_after,
            )
        )

        last_service_students = loaded + dropped
        now = arrival
        loc = stop_id

    if conf.enforce_c5_dropoff:
        for school_id, queue in ride_queues.items():
            outstanding = sum(qty for _, _, qty in queue)
            if outstanding > 0:
                sim.violations.append(
                    f"C5: undropped load {outstanding} for school {school_id} on {bus_id}."
                )

    return sim


def evaluate_solution(
    scenario: Scenario,
    settings: Settings,
    routes: Dict[str, List[str]],
    required_pickups: set[str] | None = None,
    pending_dropoffs: Dict[str, int] | None = None,
    ignore_coverage: bool = False,
) -> EvaluationResult:
    violations: Dict[str, List[str]] = defaultdict(list)
    bus_results: Dict[str, BusSimulation] = {}

    all_pickups: List[str] = []
    school_visits: Counter[str] = Counter()

    for bus_id, route in routes.items():
        sim = simulate_bus_route(scenario, settings, bus_id, route)
        bus_results[bus_id] = sim
        if sim.violations:
            violations[bus_id].extend(sim.violations)

        for stop_id in route:
            stop = scenario.stops[stop_id]
            if stop.is_pickup():
                all_pickups.append(stop_id)
            elif stop.is_school():
                school_visits[stop_id] += 1

    conf = settings.constraints

    if conf.enforce_c2_uniqueness:
        duplicate_pickups = [p for p, count in Counter(all_pickups).items() if count > 1]
        if duplicate_pickups:
            violations["GLOBAL"].append(
                f"C2: duplicate pickups across routes: {sorted(duplicate_pickups)}"
            )

    if conf.enforce_c1_coverage and not ignore_coverage:
        req = required_pickups or set()
        missing = sorted(req - set(all_pickups))
        if missing:
            violations["GLOBAL"].append(f"C1: missing pickups {missing}")

        if pending_dropoffs:
            missing_dropoffs = [
                school for school, need in pending_dropoffs.items() if school_visits[school] < need
            ]
            if missing_dropoffs:
                violations["GLOBAL"].append(
                    f"C1: missing pending school drop-offs {sorted(missing_dropoffs)}"
                )

    wait_penalty = sum(sim.wait_penalty for sim in bus_results.values())
    school_delay_penalty = sum(sim.school_delay_penalty for sim in bus_results.values())
    ride_violation_penalty = sum(sim.ride_violation_penalty for sim in bus_results.values())
    route_change_penalty = _route_change_penalty(scenario, routes)

    w = settings.objective
    objective = (
        w.wait * wait_penalty
        + w.school_delay * school_delay_penalty
        + w.route_change * route_change_penalty
        + w.ride_violation * ride_violation_penalty
    )

    feasible = not any(violations.values())
    if not feasible:
        objective += settings.solver.infeasible_penalty * sum(len(v) for v in violations.values())

    return EvaluationResult(
        objective=objective,
        wait_penalty=wait_penalty,
        school_delay_penalty=school_delay_penalty,
        route_change_penalty=route_change_penalty,
        ride_violation_penalty=ride_violation_penalty,
        feasible=feasible,
        violations=dict(violations),
        bus_results=bus_results,
    )
