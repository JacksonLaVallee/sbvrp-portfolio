from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, List

from .io import apply_overrides, load_event, load_scenario, load_settings
from .models import DisruptionEvent
from .solver import AdvInsSolver


CONSTRAINT_FLAG_BY_CODE = {
    "C1": "enforce_c1_coverage",
    "C2": "enforce_c2_uniqueness",
    "C3": "enforce_c3_capacity",
    "C4": "enforce_c4_mixed_load",
    "C5": "enforce_c5_dropoff",
    "C6": "enforce_c6_early_arrival",
}


def _fmt_minutes(total: int) -> str:
    sign = "-" if total < 0 else ""
    t = abs(total)
    return f"{sign}{t // 60:02d}:{t % 60:02d}"


def _print_eval_block(label: str, ev) -> None:
    print(label)
    print(f"  objective: {ev.objective:.2f}")
    print(f"  Twait (O1): {ev.wait_penalty:.2f}")
    print(f"  Tsch  (O2): {ev.school_delay_penalty:.2f}")
    print(f"  Croute(O3): {ev.route_change_penalty:.2f}")
    print(f"  Cride (O4): {ev.ride_violation_penalty:.2f}")
    print(f"  feasible: {ev.feasible}")

    if ev.violations:
        print("  violations:")
        for scope, issues in ev.violations.items():
            for issue in issues:
                print(f"    - [{scope}] {issue}")


def _print_routes(routes: Dict[str, List[str]], bus_results) -> None:
    print("routes:")
    for bus_id in sorted(routes):
        route = routes[bus_id]
        print(f"  {bus_id}: {' -> '.join(route) if route else '(empty)'}")
        sim = bus_results[bus_id]
        for step in sim.steps:
            print(
                f"    {step.stop_id:18s} arrival={_fmt_minutes(step.arrival)} "
                f"load={step.load_before}->{step.load_after}"
            )


def _print_best_effort_block(result) -> None:
    if not result.best_effort:
        return

    print("best-effort fallback:")
    print("  No fully feasible reroute was found. Showing the best available plan instead.")
    for reason in result.best_effort_reasons:
        print(f"  - {reason}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "SchoolRide SBDM reproduction: ICCPS formulation + OR-Tools disruption solver."
        )
    )
    parser.add_argument("--scenario", required=True, help="Path to scenario JSON")
    parser.add_argument("--config", required=True, help="Path to settings JSON")
    parser.add_argument("--event", help="Path to disruption event JSON")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        help=(
            "Override values using dotted paths, e.g. "
            "constraints.max_ride_time=60 or state.B2.active=false"
        ),
    )
    parser.add_argument(
        "--disable",
        action="append",
        default=[],
        help="Disable a paper constraint by code: C1..C6 (repeatable)",
    )
    parser.add_argument(
        "--show-baseline",
        action="store_true",
        help="Print evaluation of current remaining routes before rerouting",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print compact JSON summary in addition to human-readable output",
    )
    parser.add_argument(
        "--fail-on-infeasible",
        action="store_true",
        help="Exit non-zero if the final solution violates any enabled constraints",
    )
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    scenario = load_scenario(args.scenario)
    settings = load_settings(args.config)

    for code in args.disable:
        code_up = code.upper()
        if code_up not in CONSTRAINT_FLAG_BY_CODE:
            parser.error(f"Unknown constraint code: {code}. Expected one of {sorted(CONSTRAINT_FLAG_BY_CODE)}")
        flag_name = CONSTRAINT_FLAG_BY_CODE[code_up]
        setattr(settings.constraints, flag_name, False)

    apply_overrides(scenario, settings, args.set)

    event = load_event(args.event) if args.event else DisruptionEvent()

    solver = AdvInsSolver(scenario, settings)

    if args.show_baseline:
        baseline = solver.evaluate_current_plan()
        _print_eval_block("baseline:", baseline)
        print()

    result = solver.solve(event)

    _print_eval_block("rerouted:", result.evaluation)
    _print_routes(result.routes, result.evaluation.bus_results)
    if result.best_effort:
        print()
        _print_best_effort_block(result)

    if args.json:
        payload = {
            "objective": result.evaluation.objective,
            "feasible": result.evaluation.feasible,
            "best_effort": result.best_effort,
            "best_effort_reasons": result.best_effort_reasons,
            "unresolved_pickups": result.unresolved_pickups,
            "metrics": {
                "Twait": result.evaluation.wait_penalty,
                "Tsch": result.evaluation.school_delay_penalty,
                "Croute": result.evaluation.route_change_penalty,
                "Cride": result.evaluation.ride_violation_penalty,
            },
            "routes": result.routes,
            "disrupted_pickups": result.disrupted_pickups,
            "violations": result.evaluation.violations,
        }
        print()
        print(json.dumps(payload, indent=2))

    if args.fail_on_infeasible and not result.evaluation.feasible:
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
