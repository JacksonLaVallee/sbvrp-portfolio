from pathlib import Path

import pytest

from schoolride.io import load_scenario, load_settings
from schoolride.models import DisruptionEvent
from schoolride.solver import AdvInsSolver


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("scenario_name", "expected_feasible", "expected_code", "min_wait_penalty"),
    [
        ("scenario_edge_time_exact_60min.json", True, None, 0.0),
        ("scenario_edge_time_over_60min.json", False, "T1", 0.0),
        ("scenario_edge_time_late_pickup_wait.json", True, None, 60.0),
        ("scenario_edge_time_multi_pickup_chain.json", False, "T1", 0.0),
        ("scenario_edge_time_existing_onboard_over_60min.json", False, "T1", 0.0),
        ("scenario_edge_start_exact_0800.json", True, None, 0.0),
        ("scenario_edge_start_after_0800.json", False, "T2", 0.0),
        ("scenario_edge_start_early_arrival.json", True, None, 0.0),
    ],
)
def test_example_edge_scenarios_match_expected_outcomes(
    scenario_name: str,
    expected_feasible: bool,
    expected_code: str | None,
    min_wait_penalty: float,
) -> None:
    scenario = load_scenario(ROOT / "examples" / scenario_name)
    settings = load_settings(ROOT / "examples" / "config_default.json")

    result = AdvInsSolver(scenario, settings).solve(DisruptionEvent())

    assert result.evaluation.feasible is expected_feasible
    assert result.evaluation.wait_penalty >= min_wait_penalty

    issues = [
        issue
        for bucket in result.evaluation.violations.values()
        for issue in bucket
    ]
    if expected_code is None:
        assert all(not issue.startswith(("T1:", "T2:")) for issue in issues)
    else:
        assert any(issue.startswith(f"{expected_code}:") for issue in issues)


@pytest.mark.parametrize(
    ("scenario_name", "expected_arrival"),
    [
        ("scenario_edge_start_exact_0800.json", 8 * 60),
        ("scenario_edge_start_after_0800.json", 8 * 60 + 1),
        ("scenario_edge_start_early_arrival.json", 7 * 60 + 40),
    ],
)
def test_start_time_example_arrivals_are_runnable_and_deterministic(
    scenario_name: str,
    expected_arrival: int,
) -> None:
    scenario = load_scenario(ROOT / "examples" / scenario_name)
    settings = load_settings(ROOT / "examples" / "config_default.json")

    result = AdvInsSolver(scenario, settings).solve(DisruptionEvent())

    assert result.evaluation.bus_results["B1"].steps[-1].arrival == expected_arrival
