from pathlib import Path

import pytest

from schoolride.benchmark_solver import (
    load_benchmark_problem,
    parse_benchmark_problem_json,
    solve_benchmark_problem,
)


ROOT = Path(__file__).resolve().parents[1]


def test_lp_example_solves_with_expected_optimum() -> None:
    problem = load_benchmark_problem(ROOT / "examples" / "benchmark_lp_simple.json")

    result = solve_benchmark_problem(problem)

    assert result.status == "OPTIMAL"
    assert result.backend == "GLOP"
    assert result.objective_value == pytest.approx(36.0)
    assert result.variable_values["x"] == pytest.approx(2.0)
    assert result.variable_values["y"] == pytest.approx(6.0)
    assert all(row.satisfied for row in result.constraint_results)


def test_milp_example_solves_with_expected_optimum() -> None:
    problem = load_benchmark_problem(ROOT / "examples" / "benchmark_milp_simple.json")

    result = solve_benchmark_problem(problem)

    assert result.status == "OPTIMAL"
    assert result.backend == "CBC"
    assert result.objective_value == pytest.approx(32.0)
    assert result.variable_values["x"] == pytest.approx(1.5)
    assert result.variable_values["y"] == pytest.approx(4.0)
    assert all(row.satisfied for row in result.constraint_results)


def test_invalid_lp_rejects_integer_variables() -> None:
    with pytest.raises(ValueError, match="problem_type` LP requires all variables to be continuous"):
        parse_benchmark_problem_json(
            """
            {
              "name": "Bad LP",
              "problem_type": "LP",
              "objective": {
                "sense": "max",
                "coefficients": {"x": 1}
              },
              "variables": {
                "x": {"type": "integer", "lb": 0, "ub": 3}
              },
              "constraints": [
                {
                  "name": "cap",
                  "coefficients": {"x": 1},
                  "sense": "<=",
                  "rhs": 2
                }
              ]
            }
            """
        )
