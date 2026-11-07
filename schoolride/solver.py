from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .evaluator import EvaluationResult, evaluate_solution
from .models import DisruptionEvent, Scenario, Settings, Stop, STOP_PICKUP

try:
    from ortools.linear_solver import pywraplp
except ModuleNotFoundError as exc:  # pragma: no cover - exercised only when dependency is missing.
    pywraplp = None
    _ORTOOLS_IMPORT_ERROR = exc
else:
    _ORTOOLS_IMPORT_ERROR = None


OBJECTIVE_SCALE = 1000
MISSING_WORK_PRIORITY_FACTOR = 1000


CONSTRAINT_FACTOR_BY_CODE = {
    "C1": "coverage and pending drop-off requirements",
    "C2": "pickup uniqueness requirements",
    "C3": "capacity limits",
    "C4": "mixed-load restrictions",
    "C5": "pickup-to-school drop-off continuity",
    "C6": "early-arrival limits",
    "T1": "maximum onboard time limits",
    "T2": "school start-time deadlines",
}


@dataclass
class SolveResult:
    routes: Dict[str, List[str]]
    evaluation: EvaluationResult
    disrupted_pickups: List[str] = field(default_factory=list)
    best_effort: bool = False
    best_effort_reasons: List[str] = field(default_factory=list)
    unresolved_pickups: List[str] = field(default_factory=list)


@dataclass
class CandidateRoute:
    bus_id: str
    route: tuple[str, ...]
    pickups: frozenset[str]
    school_visits: Dict[str, int]
    local_violation_count: int
    scaled_weighted_cost: int
    tie_breaker: int = 0


class AdvInsSolver:
    """OR-Tools-backed disruption solver with the original public API."""

    def __init__(self, scenario: Scenario, settings: Settings) -> None:
        self.scenario = deepcopy(scenario)
        self.settings = settings

    def evaluate_current_plan(self) -> EvaluationResult:
        routes = {
            bus_id: list(state.remaining)
            for bus_id, state in self.scenario.states.items()
            if state.active
        }
        required_pickups, pending_dropoffs = self._required_work_items(removed_buses=set())
        return evaluate_solution(
            self.scenario,
            self.settings,
            routes,
            required_pickups=required_pickups,
            pending_dropoffs=pending_dropoffs,
        )

    def solve(self, event: DisruptionEvent) -> SolveResult:
        self._ensure_ortools_available()
        self._apply_event_updates(event)

        active_buses = [
            bus_id
            for bus_id, state in self.scenario.states.items()
            if state.active and bus_id not in event.removed_buses
        ]

        required_pickups, pending_dropoffs = self._required_work_items(event.removed_buses)
        disrupted_pickups = self._initial_disrupted_pickups(event.removed_buses)

        for bus_id in event.removed_buses:
            disrupted_pickups.extend(self._rescue_pickups_from_removed_bus(bus_id, required_pickups))

        for u, v in sorted(event.disrupted_arcs):
            for bus_id in active_buses:
                route = list(self.scenario.states[bus_id].remaining)
                if not self._has_arc(route, u, v):
                    continue
                _, spill = self._reorder_segment(route, u)
                disrupted_pickups.extend(spill)

        disrupted_pickups = sorted(
            set(disrupted_pickups),
            key=lambda pid: (self.scenario.stops[pid].planned_time, pid),
        )

        if not active_buses:
            final_eval = evaluate_solution(
                self.scenario,
                self.settings,
                {},
                required_pickups=required_pickups,
                pending_dropoffs=pending_dropoffs,
                ignore_coverage=False,
            )
            unresolved_pickups = sorted(
                required_pickups,
                key=lambda pid: (self.scenario.stops[pid].planned_time, pid),
            )
            best_effort = not final_eval.feasible
            best_effort_reasons = self._build_best_effort_reasons(
                required_pickups=required_pickups,
                pending_dropoffs=pending_dropoffs,
                routes={},
                unresolved_pickups=unresolved_pickups,
                evaluation=final_eval,
            )
            return SolveResult(
                routes={},
                evaluation=final_eval,
                disrupted_pickups=disrupted_pickups,
                best_effort=best_effort,
                best_effort_reasons=best_effort_reasons,
                unresolved_pickups=unresolved_pickups,
            )

        assignable_pickups = self._build_assignable_pickups(active_buses, required_pickups)
        candidates_by_bus = {
            bus_id: self._build_candidates_for_bus(
                bus_id=bus_id,
                assignable_pickups=assignable_pickups[bus_id],
                disrupted_arcs=event.disrupted_arcs,
            )
            for bus_id in active_buses
        }

        routes = self._select_routes_with_ortools(
            active_buses=active_buses,
            candidates_by_bus=candidates_by_bus,
            required_pickups=required_pickups,
            pending_dropoffs=pending_dropoffs,
        )

        final_eval = evaluate_solution(
            self.scenario,
            self.settings,
            routes,
            required_pickups=required_pickups,
            pending_dropoffs=pending_dropoffs,
            ignore_coverage=False,
        )

        scheduled_pickups = {
            stop_id
            for route in routes.values()
            for stop_id in route
            if self.scenario.stops[stop_id].is_pickup()
        }
        unresolved_pickups = sorted(
            required_pickups - scheduled_pickups,
            key=lambda pid: (self.scenario.stops[pid].planned_time, pid),
        )

        best_effort = not final_eval.feasible
        best_effort_reasons = (
            self._build_best_effort_reasons(
                required_pickups=required_pickups,
                pending_dropoffs=pending_dropoffs,
                routes=routes,
                unresolved_pickups=unresolved_pickups,
                evaluation=final_eval,
            )
            if best_effort
            else []
        )

        return SolveResult(
            routes=routes,
            evaluation=final_eval,
            disrupted_pickups=disrupted_pickups,
            best_effort=best_effort,
            best_effort_reasons=best_effort_reasons,
            unresolved_pickups=unresolved_pickups,
        )

    def _ensure_ortools_available(self) -> None:
        if pywraplp is not None:
            return
        raise ModuleNotFoundError(
            "Google OR-Tools is required for `schoolride.solver`. Install it with `pip install ortools`."
        ) from _ORTOOLS_IMPORT_ERROR

    def _apply_event_updates(self, event: DisruptionEvent) -> None:
        for (u, v), minutes in event.travel_time_updates.items():
            self.scenario.travel_time[(u, v)] = int(minutes)

    def _required_work_items(self, removed_buses: set[str]) -> tuple[set[str], Dict[str, int]]:
        required_pickups: set[str] = set()
        pending_dropoffs: Counter[str] = Counter()

        for bus_id, state in self.scenario.states.items():
            for stop_id in state.remaining:
                if self.scenario.stops[stop_id].is_pickup():
                    required_pickups.add(stop_id)

            if bus_id in removed_buses:
                continue

            for school_id, qty in state.onboard.items():
                if qty > 0:
                    pending_dropoffs[school_id] += 1

        return required_pickups, dict(pending_dropoffs)

    def _initial_disrupted_pickups(self, removed_buses: set[str]) -> List[str]:
        pickups: List[str] = []
        for bus_id in removed_buses:
            state = self.scenario.states.get(bus_id)
            if state is None:
                continue
            for stop_id in state.remaining:
                if self.scenario.stops[stop_id].is_pickup():
                    pickups.append(stop_id)
        return pickups

    def _rescue_pickups_from_removed_bus(self, bus_id: str, required_pickups: set[str]) -> List[str]:
        state = self.scenario.states.get(bus_id)
        if state is None:
            return []

        created: List[str] = []
        for school_id, qty in state.onboard.items():
            if qty <= 0:
                continue

            rescue_id = f"RESCUE_{bus_id}_{school_id}"
            suffix = 1
            while rescue_id in self.scenario.stops:
                suffix += 1
                rescue_id = f"RESCUE_{bus_id}_{school_id}_{suffix}"

            self.scenario.stops[rescue_id] = Stop(
                id=rescue_id,
                stop_type=STOP_PICKUP,
                planned_time=state.eta,
                load=int(qty),
                school_id=school_id,
            )

            self.scenario.travel_time[(state.loc, rescue_id)] = 0
            self.scenario.travel_time[(rescue_id, state.loc)] = 0

            for other_id in list(self.scenario.stops.keys()):
                if other_id in {state.loc, rescue_id}:
                    continue
                self.scenario.travel_time.setdefault(
                    (rescue_id, other_id), self.scenario.travel(state.loc, other_id)
                )
                self.scenario.travel_time.setdefault(
                    (other_id, rescue_id), self.scenario.travel(other_id, state.loc)
                )

            required_pickups.add(rescue_id)
            created.append(rescue_id)

        return created

    def _build_assignable_pickups(
        self,
        active_buses: List[str],
        required_pickups: set[str],
    ) -> Dict[str, set[str]]:
        assignable = {bus_id: set() for bus_id in active_buses}
        baseline_routes = {
            bus_id: list(self.scenario.states[bus_id].remaining) for bus_id in active_buses
        }

        original_owners: Dict[str, set[str]] = {}
        for bus_id in active_buses:
            for stop_id in self.scenario.states[bus_id].remaining:
                if self.scenario.stops[stop_id].is_pickup():
                    original_owners.setdefault(stop_id, set()).add(bus_id)

        for pickup_id in sorted(required_pickups, key=self._pickup_sort_key):
            school_id = self.scenario.school_for_pickup(pickup_id)
            candidate_buses = self._candidate_buses_for_pickup(
                pickup_id=pickup_id,
                school_id=school_id,
                routes=baseline_routes,
            )
            for bus_id in candidate_buses:
                assignable[bus_id].add(pickup_id)
            for owner in original_owners.get(pickup_id, set()):
                assignable[owner].add(pickup_id)

            if not any(pickup_id in stops for stops in assignable.values()):
                for bus_id in active_buses:
                    assignable[bus_id].add(pickup_id)

        return assignable

    def _build_candidates_for_bus(
        self,
        bus_id: str,
        assignable_pickups: set[str],
        disrupted_arcs: set[tuple[str, str]],
    ) -> List[CandidateRoute]:
        candidate_map: Dict[tuple[str, ...], CandidateRoute] = {}
        state = self.scenario.states[bus_id]
        initial_onboard = {
            school_id: int(qty) for school_id, qty in state.onboard.items() if int(qty) > 0
        }
        remaining_pickups = tuple(sorted(assignable_pickups, key=self._pickup_sort_key))

        self._enumerate_routes_recursive(
            bus_id=bus_id,
            route=(),
            remaining_pickups=remaining_pickups,
            onboard=initial_onboard,
            disrupted_arcs=disrupted_arcs,
            candidate_map=candidate_map,
        )

        if not candidate_map:
            self._store_candidate_route(bus_id=bus_id, route=(), candidate_map=candidate_map)

        ordered = sorted(
            candidate_map.values(),
            key=lambda candidate: (
                candidate.local_violation_count,
                candidate.scaled_weighted_cost,
                len(candidate.route),
                candidate.route,
            ),
        )
        for idx, candidate in enumerate(ordered):
            candidate.tie_breaker = idx
        return ordered

    def _enumerate_routes_recursive(
        self,
        bus_id: str,
        route: tuple[str, ...],
        remaining_pickups: tuple[str, ...],
        onboard: Dict[str, int],
        disrupted_arcs: set[tuple[str, str]],
        candidate_map: Dict[tuple[str, ...], CandidateRoute],
    ) -> None:
        nonempty_onboard = {school_id: qty for school_id, qty in onboard.items() if qty > 0}
        if not nonempty_onboard:
            self._store_candidate_route(bus_id=bus_id, route=route, candidate_map=candidate_map)

        for school_id in sorted(nonempty_onboard, key=self._school_sort_key):
            if self._would_use_disrupted_arc(bus_id, route, school_id, disrupted_arcs):
                continue
            next_onboard = dict(nonempty_onboard)
            next_onboard.pop(school_id, None)
            self._enumerate_routes_recursive(
                bus_id=bus_id,
                route=route + (school_id,),
                remaining_pickups=remaining_pickups,
                onboard=next_onboard,
                disrupted_arcs=disrupted_arcs,
                candidate_map=candidate_map,
            )

        carried_schools = {school_id for school_id, qty in nonempty_onboard.items() if qty > 0}
        for idx, pickup_id in enumerate(remaining_pickups):
            school_id = self.scenario.school_for_pickup(pickup_id)
            if (
                self.settings.constraints.enforce_c4_mixed_load
                and carried_schools
                and school_id not in carried_schools
            ):
                continue
            if self._would_use_disrupted_arc(bus_id, route, pickup_id, disrupted_arcs):
                continue

            next_onboard = dict(nonempty_onboard)
            next_onboard[school_id] = next_onboard.get(school_id, 0) + max(
                0, int(self.scenario.stops[pickup_id].load)
            )
            next_remaining = remaining_pickups[:idx] + remaining_pickups[idx + 1 :]

            self._enumerate_routes_recursive(
                bus_id=bus_id,
                route=route + (pickup_id,),
                remaining_pickups=next_remaining,
                onboard=next_onboard,
                disrupted_arcs=disrupted_arcs,
                candidate_map=candidate_map,
            )

    def _store_candidate_route(
        self,
        bus_id: str,
        route: tuple[str, ...],
        candidate_map: Dict[tuple[str, ...], CandidateRoute],
    ) -> None:
        if route in candidate_map:
            return

        route_list = list(route)
        evaluation = evaluate_solution(
            self.scenario,
            self.settings,
            {bus_id: route_list},
            ignore_coverage=True,
        )
        violation_count = sum(len(issues) for issues in evaluation.violations.values())
        weighted_cost = self._scaled_weighted_cost(evaluation)

        candidate_map[route] = CandidateRoute(
            bus_id=bus_id,
            route=route,
            pickups=frozenset(
                stop_id for stop_id in route if self.scenario.stops[stop_id].is_pickup()
            ),
            school_visits=dict(
                Counter(stop_id for stop_id in route if self.scenario.stops[stop_id].is_school())
            ),
            local_violation_count=violation_count,
            scaled_weighted_cost=weighted_cost,
        )

    def _scaled_weighted_cost(self, evaluation: EvaluationResult) -> int:
        weights = self.settings.objective
        value = (
            weights.wait * evaluation.wait_penalty
            + weights.school_delay * evaluation.school_delay_penalty
            + weights.route_change * evaluation.route_change_penalty
            + weights.ride_violation * evaluation.ride_violation_penalty
        )
        return int(round(value * OBJECTIVE_SCALE))

    def _select_routes_with_ortools(
        self,
        active_buses: List[str],
        candidates_by_bus: Dict[str, List[CandidateRoute]],
        required_pickups: set[str],
        pending_dropoffs: Dict[str, int],
    ) -> Dict[str, List[str]]:
        solver = pywraplp.Solver.CreateSolver("CBC")
        if solver is None:
            raise RuntimeError("Google OR-Tools CBC backend is unavailable.")

        choice_vars: Dict[tuple[str, int], object] = {}

        for bus_id in active_buses:
            selections = []
            for idx, _candidate in enumerate(candidates_by_bus[bus_id]):
                var = solver.BoolVar(f"route_{bus_id}_{idx}")
                choice_vars[(bus_id, idx)] = var
                selections.append(var)
            solver.Add(sum(selections) == 1)

        missing_pickup_vars: Dict[str, object] = {}
        for pickup_id in sorted(required_pickups, key=self._pickup_sort_key):
            coverage_terms = [
                choice_vars[(bus_id, idx)]
                for bus_id in active_buses
                for idx, candidate in enumerate(candidates_by_bus[bus_id])
                if pickup_id in candidate.pickups
            ]
            missing = solver.BoolVar(f"missing_pickup_{pickup_id}")
            if coverage_terms:
                solver.Add(sum(coverage_terms) + missing == 1)
            else:
                solver.Add(missing == 1)
            missing_pickup_vars[pickup_id] = missing

        dropoff_shortfalls: Dict[str, object] = {}
        for school_id, required_visits in sorted(pending_dropoffs.items()):
            if required_visits <= 0:
                continue

            visit_terms = [
                candidate.school_visits[school_id] * choice_vars[(bus_id, idx)]
                for bus_id in active_buses
                for idx, candidate in enumerate(candidates_by_bus[bus_id])
                if candidate.school_visits.get(school_id, 0) > 0
            ]
            visit_total = sum(visit_terms) if visit_terms else 0

            shortfall = solver.IntVar(0, required_visits, f"dropoff_shortfall_{school_id}")
            solver.Add(shortfall >= required_visits - visit_total)
            dropoff_shortfalls[school_id] = shortfall

        scaled_infeasible_penalty = int(
            round(self.settings.solver.infeasible_penalty * OBJECTIVE_SCALE)
        )
        scaled_missing_work_penalty = (
            scaled_infeasible_penalty * MISSING_WORK_PRIORITY_FACTOR
        )

        objective = solver.Objective()
        objective.SetMinimization()
        for missing in missing_pickup_vars.values():
            objective.SetCoefficient(missing, scaled_missing_work_penalty)
        for shortfall in dropoff_shortfalls.values():
            objective.SetCoefficient(shortfall, scaled_missing_work_penalty)
        for bus_id in active_buses:
            for idx, candidate in enumerate(candidates_by_bus[bus_id]):
                route_cost = (
                    candidate.local_violation_count * scaled_infeasible_penalty
                    + candidate.scaled_weighted_cost
                    + candidate.tie_breaker
                )
                objective.SetCoefficient(choice_vars[(bus_id, idx)], route_cost)

        solver.SetTimeLimit(10000)
        status = solver.Solve()
        if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
            raise RuntimeError("OR-Tools could not produce a route selection.")

        routes: Dict[str, List[str]] = {}
        for bus_id in active_buses:
            selected_idx = next(
                idx
                for idx in range(len(candidates_by_bus[bus_id]))
                if choice_vars[(bus_id, idx)].solution_value() > 0.5
            )
            routes[bus_id] = list(candidates_by_bus[bus_id][selected_idx].route)

        return routes

    def _would_use_disrupted_arc(
        self,
        bus_id: str,
        route: tuple[str, ...],
        next_stop: str,
        disrupted_arcs: set[tuple[str, str]],
    ) -> bool:
        previous = route[-1] if route else self.scenario.states[bus_id].loc
        return (previous, next_stop) in disrupted_arcs

    def _pickup_sort_key(self, pickup_id: str) -> tuple[int, str]:
        stop = self.scenario.stops[pickup_id]
        return (stop.planned_time, pickup_id)

    def _school_sort_key(self, school_id: str) -> tuple[int, str]:
        return (self.scenario.school_bell_time(school_id), school_id)

    def _has_arc(self, route: List[str], u: str, v: str) -> bool:
        for idx in range(len(route) - 1):
            if route[idx] == u and route[idx + 1] == v:
                return True
        return False

    def _reorder_segment(self, route: List[str], start_stop: str) -> tuple[List[str], List[str]]:
        if start_stop not in route:
            return route, []

        start_idx = route.index(start_stop)
        prefix = list(route[: start_idx + 1])
        remaining = list(route[start_idx + 1 :])

        if not remaining:
            return route, []

        spill: List[str] = []
        reordered: List[str] = []
        current = start_stop

        while remaining:
            remaining.sort(key=lambda stop_id: self.scenario.travel(current, stop_id))
            next_stop = remaining[0]
            if self.scenario.travel(current, next_stop) >= self.scenario.default_travel_time:
                spill.extend([s for s in remaining if self.scenario.stops[s].is_pickup()])
                break

            reordered.append(next_stop)
            current = next_stop
            remaining.pop(0)

        candidate = prefix + reordered
        cleaned: List[str] = []
        for idx, stop_id in enumerate(candidate):
            stop = self.scenario.stops[stop_id]
            if stop.is_pickup():
                school_id = self.scenario.school_for_pickup(stop_id)
                if school_id not in candidate[idx + 1 :]:
                    spill.append(stop_id)
                    continue
            cleaned.append(stop_id)

        dedup_spill = list(dict.fromkeys(spill))
        return cleaned, dedup_spill

    def _candidate_buses_for_pickup(
        self,
        pickup_id: str,
        school_id: str,
        routes: Dict[str, List[str]],
    ) -> List[str]:
        bus_ids = list(routes.keys())
        if not self.settings.solver.enable_pruning:
            return bus_ids

        scored: List[Tuple[float, str]] = []
        for bus_id in bus_ids:
            state = self.scenario.states[bus_id]
            route = routes[bus_id]

            distance = self.scenario.travel(state.loc, pickup_id)
            same_school_bonus = 0 if school_id in route or state.onboard.get(school_id, 0) > 0 else 30
            finish_proxy = len(route)
            score = distance + same_school_bonus + finish_proxy
            scored.append((score, bus_id))

        scored.sort(key=lambda pair: pair[0])
        top = scored[: self.settings.solver.max_candidate_buses]
        return [bus_id for _, bus_id in top]

    def _build_best_effort_reasons(
        self,
        required_pickups: set[str],
        pending_dropoffs: Dict[str, int],
        routes: Dict[str, List[str]],
        unresolved_pickups: List[str],
        evaluation: EvaluationResult,
    ) -> List[str]:
        reasons: List[str] = []

        if not routes and (required_pickups or pending_dropoffs):
            reasons.append(
                "No active buses remained after the disruption, so unfinished work could not be reassigned."
            )

        scheduled_pickups = {
            stop_id
            for route in routes.values()
            for stop_id in route
            if self.scenario.stops[stop_id].is_pickup()
        }
        missing_pickups = sorted(
            required_pickups - scheduled_pickups,
            key=lambda pid: self.scenario.stops[pid].planned_time,
        )
        if unresolved_pickups:
            reasons.append(
                "Some disrupted pickups could not be assigned to any active bus: "
                f"{', '.join(unresolved_pickups)}."
            )

        unresolved_pickup_set = set(unresolved_pickups)
        missing_not_already_listed = [
            pickup_id for pickup_id in missing_pickups if pickup_id not in unresolved_pickup_set
        ]
        if missing_not_already_listed:
            reasons.append(
                "The returned plan still leaves required pickups uncovered: "
                f"{', '.join(missing_not_already_listed)}."
            )

        school_visits: Counter[str] = Counter()
        for route in routes.values():
            for stop_id in route:
                if self.scenario.stops[stop_id].is_school():
                    school_visits[stop_id] += 1
        missing_dropoffs = sorted(
            school_id
            for school_id, need in pending_dropoffs.items()
            if school_visits[school_id] < need
        )
        if missing_dropoffs:
            reasons.append(
                "Pending onboard students still have no downstream school visit for: "
                f"{', '.join(missing_dropoffs)}."
            )

        violation_summary = self._summarize_violation_factors(evaluation.violations)
        if violation_summary:
            reasons.append(
                "Enabled constraints are still violated in the best available plan: "
                f"{violation_summary}."
            )

        if not reasons:
            reasons.append(
                "No fully feasible reroute was found, so the solver returned the lowest-penalty plan it could construct."
            )

        return reasons

    def _summarize_violation_factors(self, violations: Dict[str, List[str]]) -> str:
        counts: Counter[str] = Counter()
        for issues in violations.values():
            for issue in issues:
                code = issue.split(":", 1)[0].strip()
                if code in CONSTRAINT_FACTOR_BY_CODE:
                    counts[code] += 1

        if counts:
            return ", ".join(
                f"{CONSTRAINT_FACTOR_BY_CODE[code]} ({code} x{counts[code]})"
                for code in sorted(counts)
            )

        total = sum(len(issues) for issues in violations.values())
        if total <= 0:
            return ""
        if total == 1:
            return "1 outstanding rule violation"
        return f"{total} outstanding rule violations"


ORToolsSolver = AdvInsSolver
