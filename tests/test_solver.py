from pathlib import Path

from schoolride.cli import main
from schoolride.io import load_event, load_scenario, load_settings
from schoolride.models import DisruptionEvent
from schoolride.solver import AdvInsSolver


ROOT = Path(__file__).resolve().parents[1]


def test_breakdown_event_is_feasible() -> None:
    scenario = load_scenario(ROOT / "examples" / "scenario_small.json")
    settings = load_settings(ROOT / "examples" / "config_default.json")
    event = load_event(ROOT / "examples" / "event_breakdown.json")

    result = AdvInsSolver(scenario, settings).solve(event)

    assert result.evaluation.feasible
    assert "P2" in result.disrupted_pickups
    assert any(p.startswith("RESCUE_B1_S_A") for p in result.disrupted_pickups)


def test_road_closure_reroutes_blocked_arc() -> None:
    scenario = load_scenario(ROOT / "examples" / "scenario_small.json")
    settings = load_settings(ROOT / "examples" / "config_default.json")
    event = load_event(ROOT / "examples" / "event_road_closure.json")

    result = AdvInsSolver(scenario, settings).solve(event)

    for route in result.routes.values():
        assert ("P3", "P4") not in zip(route, route[1:])
    assert result.evaluation.feasible or any(
        "early-arrival" in reason.lower() or "school start-time" in reason.lower()
        for reason in result.best_effort_reasons
    )


def test_single_vehicle_single_customer_edge_case_is_feasible() -> None:
    scenario = load_scenario(
        ROOT / "examples" / "scenario_edge_single_vehicle_single_customer.json"
    )
    settings = load_settings(ROOT / "examples" / "config_default.json")

    result = AdvInsSolver(scenario, settings).solve(DisruptionEvent())

    assert result.evaluation.feasible
    assert result.routes["B1"] == ["P1", "S_A"]
    assert result.disrupted_pickups == []


def test_more_vehicles_than_customers_keeps_extra_buses_idle() -> None:
    scenario = load_scenario(
        ROOT / "examples" / "scenario_edge_more_vehicles_than_customers.json"
    )
    settings = load_settings(ROOT / "examples" / "config_default.json")

    result = AdvInsSolver(scenario, settings).solve(DisruptionEvent())

    assert result.evaluation.feasible
    assert result.routes["B1"] == ["P1", "S_A"]
    assert result.routes["B2"] == ["P2", "S_B"]
    assert result.routes["B3"] == []
    assert result.routes["B4"] == []


def test_capacity_overflow_edge_case_flags_c3_violation() -> None:
    scenario = load_scenario(ROOT / "examples" / "scenario_edge_capacity_overflow.json")
    settings = load_settings(ROOT / "examples" / "config_default.json")

    result = AdvInsSolver(scenario, settings).solve(DisruptionEvent())

    assert not result.evaluation.feasible
    assert result.best_effort
    assert any("capacity" in reason.lower() for reason in result.best_effort_reasons)
    assert any(
        "C3: capacity exceeded" in issue
        for issue in result.evaluation.violations.get("B1", [])
    )


def test_same_location_edge_case_handles_zero_travel_between_pickups() -> None:
    scenario = load_scenario(
        ROOT / "examples" / "scenario_edge_all_customers_same_location.json"
    )
    settings = load_settings(ROOT / "examples" / "config_default.json")

    result = AdvInsSolver(scenario, settings).solve(DisruptionEvent())

    assert result.evaluation.feasible
    assert result.routes["B1"] == ["P1", "P2", "P3", "S_A"]
    assert result.routes["B2"] == []


def test_impossible_disruption_returns_best_effort_plan_with_explanation() -> None:
    scenario = load_scenario(ROOT / "examples" / "scenario_small.json")
    settings = load_settings(ROOT / "examples" / "config_default.json")
    event = DisruptionEvent(removed_buses={"B1", "B2", "B3"})

    result = AdvInsSolver(scenario, settings).solve(event)

    assert not result.evaluation.feasible
    assert result.best_effort
    assert result.routes == {}
    assert set(result.unresolved_pickups) >= {"P2", "P3", "P4"}
    assert any(p.startswith("RESCUE_B1_S_A") for p in result.unresolved_pickups)
    assert any("no active buses remained" in reason.lower() for reason in result.best_effort_reasons)


def test_cli_reports_best_effort_fallback(capsys) -> None:
    exit_code = main(
        [
            "--scenario",
            str(ROOT / "examples" / "scenario_edge_capacity_overflow.json"),
            "--config",
            str(ROOT / "examples" / "config_default.json"),
        ]
    )

    captured = capsys.readouterr().out

    assert exit_code == 0
    assert "best-effort fallback:" in captured
    assert "best available plan" in captured
