from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

try:
    from ortools.linear_solver import pywraplp
except ModuleNotFoundError as exc:  # pragma: no cover - exercised only when dependency is missing.
    pywraplp = None
    _ORTOOLS_IMPORT_ERROR = exc
else:
    _ORTOOLS_IMPORT_ERROR = None


SUPPORTED_PROBLEM_TYPES = {"LP", "MILP", "ILP", "BINARY"}
SUPPORTED_VARIABLE_TYPES = {"continuous", "integer", "binary"}
SUPPORTED_CONSTRAINT_SENSES = {"<=", ">=", "="}
_SOLUTION_TOLERANCE = 1e-8


@dataclass(frozen=True)
class BenchmarkVariable:
    name: str
    kind: str
    lower_bound: float
    upper_bound: float | None = None


@dataclass(frozen=True)
class BenchmarkObjective:
    sense: str
    coefficients: Dict[str, float]


@dataclass(frozen=True)
class BenchmarkConstraint:
    name: str
    coefficients: Dict[str, float]
    sense: str
    rhs: float


@dataclass(frozen=True)
class BenchmarkProblem:
    name: str
    problem_type: str
    variables: Dict[str, BenchmarkVariable]
    objective: BenchmarkObjective
    constraints: List[BenchmarkConstraint]


@dataclass(frozen=True)
class BenchmarkConstraintResult:
    name: str
    sense: str
    lhs_value: float
    rhs: float
    slack: float
    satisfied: bool


@dataclass(frozen=True)
class BenchmarkSolveResult:
    problem_name: str
    problem_type: str
    backend: str
    status: str
    objective_value: float | None
    variable_values: Dict[str, float]
    constraint_results: List[BenchmarkConstraintResult]

    @property
    def has_solution(self) -> bool:
        return self.status in {"OPTIMAL", "FEASIBLE"}


def load_benchmark_problem(path: str | Path) -> BenchmarkProblem:
    with open(path, "r", encoding="utf-8-sig") as handle:
        return parse_benchmark_problem(json.load(handle))


def parse_benchmark_problem_json(text: str) -> BenchmarkProblem:
    return parse_benchmark_problem(json.loads(text))


def parse_benchmark_problem(raw: Mapping[str, Any]) -> BenchmarkProblem:
    if not isinstance(raw, Mapping):
        raise ValueError("Benchmark problem JSON must decode to an object.")

    raw_variables = raw.get("variables")
    variables = _parse_variables(raw_variables)
    variable_names = set(variables.keys())

    raw_objective = raw.get("objective")
    if not isinstance(raw_objective, Mapping):
        raise ValueError("`objective` must be an object.")
    objective = BenchmarkObjective(
        sense=_normalize_objective_sense(raw_objective.get("sense", "max")),
        coefficients=_parse_coefficients(
            raw_objective.get("coefficients", {}),
            variable_names=variable_names,
            field_name="objective.coefficients",
        ),
    )

    raw_constraints = raw.get("constraints", [])
    if not isinstance(raw_constraints, list) or not raw_constraints:
        raise ValueError("`constraints` must be a non-empty array.")
    constraints: List[BenchmarkConstraint] = []
    for idx, constraint in enumerate(raw_constraints):
        if not isinstance(constraint, Mapping):
            raise ValueError(f"`constraints[{idx}]` must be an object.")
        constraints.append(
            BenchmarkConstraint(
                name=str(constraint.get("name", f"c{idx + 1}")),
                coefficients=_parse_coefficients(
                    constraint.get("coefficients", {}),
                    variable_names=variable_names,
                    field_name=f"constraints[{idx}].coefficients",
                ),
                sense=_normalize_constraint_sense(constraint.get("sense", "<=")),
                rhs=_as_number(constraint.get("rhs"), f"constraints[{idx}].rhs"),
            )
        )

    inferred_type = infer_problem_type(variables.values())
    declared_type = raw.get("problem_type", raw.get("type", inferred_type))
    problem_type = _normalize_problem_type(declared_type)
    _validate_problem_type(problem_type, variables.values())

    return BenchmarkProblem(
        name=str(raw.get("name", "Benchmark Problem")),
        problem_type=problem_type,
        variables=variables,
        objective=objective,
        constraints=constraints,
    )


def infer_problem_type(variables: Iterable[BenchmarkVariable]) -> str:
    variables = list(variables)
    if not variables:
        raise ValueError("At least one variable is required.")

    kinds = {variable.kind for variable in variables}
    if kinds == {"continuous"}:
        return "LP"
    if kinds == {"binary"}:
        return "BINARY"
    if kinds <= {"integer", "binary"}:
        return "ILP"
    return "MILP"


def solve_benchmark_problem(problem: BenchmarkProblem) -> BenchmarkSolveResult:
    _ensure_ortools_available()

    backend = "GLOP" if infer_problem_type(problem.variables.values()) == "LP" else "CBC"
    solver = pywraplp.Solver.CreateSolver(backend)
    if solver is None:
        raise RuntimeError(f"Google OR-Tools backend `{backend}` is unavailable.")

    infinity = solver.infinity()
    variable_map: Dict[str, Any] = {}
    for name, spec in problem.variables.items():
        upper_bound = spec.upper_bound if spec.upper_bound is not None else infinity
        if spec.kind == "continuous":
            variable_map[name] = solver.NumVar(spec.lower_bound, upper_bound, name)
        else:
            variable_map[name] = solver.IntVar(spec.lower_bound, upper_bound, name)

    objective = solver.Objective()
    for name, coefficient in problem.objective.coefficients.items():
        objective.SetCoefficient(variable_map[name], coefficient)
    if problem.objective.sense == "max":
        objective.SetMaximization()
    else:
        objective.SetMinimization()

    for constraint in problem.constraints:
        if constraint.sense == "<=":
            row = solver.RowConstraint(-infinity, constraint.rhs, constraint.name)
        elif constraint.sense == ">=":
            row = solver.RowConstraint(constraint.rhs, infinity, constraint.name)
        else:
            row = solver.RowConstraint(constraint.rhs, constraint.rhs, constraint.name)
        for name, coefficient in constraint.coefficients.items():
            row.SetCoefficient(variable_map[name], coefficient)

    status_code = solver.Solve()
    status = _status_name(status_code)

    if status not in {"OPTIMAL", "FEASIBLE"}:
        return BenchmarkSolveResult(
            problem_name=problem.name,
            problem_type=problem.problem_type,
            backend=backend,
            status=status,
            objective_value=None,
            variable_values={},
            constraint_results=[],
        )

    variable_values = {
        name: _clean_solution_value(variable_map[name].solution_value(), spec.kind)
        for name, spec in problem.variables.items()
    }
    constraint_results = [
        _evaluate_constraint(constraint, variable_values)
        for constraint in problem.constraints
    ]

    return BenchmarkSolveResult(
        problem_name=problem.name,
        problem_type=problem.problem_type,
        backend=backend,
        status=status,
        objective_value=objective.Value(),
        variable_values=variable_values,
        constraint_results=constraint_results,
    )


def _parse_variables(raw_variables: Any) -> Dict[str, BenchmarkVariable]:
    if isinstance(raw_variables, Mapping):
        raw_items = [
            dict(spec or {}, name=str(name))
            for name, spec in raw_variables.items()
        ]
    elif isinstance(raw_variables, list):
        raw_items = [dict(item) for item in raw_variables]
    else:
        raise ValueError("`variables` must be an object or an array.")

    if not raw_items:
        raise ValueError("At least one variable is required.")

    variables: Dict[str, BenchmarkVariable] = {}
    for idx, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, Mapping):
            raise ValueError(f"`variables[{idx}]` must be an object.")
        name = str(raw_item.get("name", "")).strip()
        if not name:
            raise ValueError(f"`variables[{idx}].name` is required.")
        if name in variables:
            raise ValueError(f"Duplicate variable name `{name}`.")

        kind = _normalize_variable_kind(raw_item.get("type", raw_item.get("kind", "continuous")))
        lower_bound_raw = raw_item.get("lb", raw_item.get("lower_bound", 0))
        upper_bound_raw = raw_item.get("ub", raw_item.get("upper_bound"))

        if kind == "binary":
            lower_bound = 0 if lower_bound_raw is None else _as_number(lower_bound_raw, f"{name}.lb")
            upper_bound = 1 if upper_bound_raw is None else _as_number(upper_bound_raw, f"{name}.ub")
            if lower_bound not in {0, 1} or upper_bound not in {0, 1} or lower_bound > upper_bound:
                raise ValueError(
                    f"Binary variable `{name}` must use bounds within [0, 1]."
                )
        elif kind == "integer":
            lower_bound = _as_number(lower_bound_raw, f"{name}.lb")
            upper_bound = None if upper_bound_raw is None else _as_number(upper_bound_raw, f"{name}.ub")
            if not _is_integral(lower_bound) or (
                upper_bound is not None and not _is_integral(upper_bound)
            ):
                raise ValueError(f"Integer variable `{name}` must use integer bounds.")
            lower_bound = int(round(lower_bound))
            upper_bound = None if upper_bound is None else int(round(upper_bound))
        else:
            lower_bound = _as_number(lower_bound_raw, f"{name}.lb")
            upper_bound = None if upper_bound_raw is None else _as_number(upper_bound_raw, f"{name}.ub")

        if upper_bound is not None and upper_bound < lower_bound:
            raise ValueError(f"Variable `{name}` has `ub < lb`.")

        variables[name] = BenchmarkVariable(
            name=name,
            kind=kind,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
        )

    return variables


def _parse_coefficients(
    raw_coefficients: Any,
    *,
    variable_names: set[str],
    field_name: str,
) -> Dict[str, float]:
    if not isinstance(raw_coefficients, Mapping):
        raise ValueError(f"`{field_name}` must be an object.")

    coefficients: Dict[str, float] = {}
    for key, value in raw_coefficients.items():
        variable_name = str(key)
        if variable_name not in variable_names:
            raise ValueError(
                f"`{field_name}` references unknown variable `{variable_name}`."
            )
        coefficients[variable_name] = float(_as_number(value, f"{field_name}.{variable_name}"))
    return coefficients


def _validate_problem_type(
    problem_type: str,
    variables: Iterable[BenchmarkVariable],
) -> None:
    variables = list(variables)
    if problem_type == "LP" and any(variable.kind != "continuous" for variable in variables):
        raise ValueError("`problem_type` LP requires all variables to be continuous.")
    if problem_type == "ILP" and any(variable.kind == "continuous" for variable in variables):
        raise ValueError("`problem_type` ILP cannot include continuous variables.")
    if problem_type == "BINARY" and any(variable.kind != "binary" for variable in variables):
        raise ValueError("`problem_type` BINARY requires all variables to be binary.")


def _evaluate_constraint(
    constraint: BenchmarkConstraint,
    variable_values: Dict[str, float],
) -> BenchmarkConstraintResult:
    lhs_value = sum(
        coefficient * variable_values.get(name, 0.0)
        for name, coefficient in constraint.coefficients.items()
    )
    if constraint.sense == "<=":
        slack = constraint.rhs - lhs_value
        satisfied = lhs_value <= constraint.rhs + _SOLUTION_TOLERANCE
    elif constraint.sense == ">=":
        slack = lhs_value - constraint.rhs
        satisfied = lhs_value + _SOLUTION_TOLERANCE >= constraint.rhs
    else:
        slack = abs(lhs_value - constraint.rhs)
        satisfied = abs(lhs_value - constraint.rhs) <= _SOLUTION_TOLERANCE

    return BenchmarkConstraintResult(
        name=constraint.name,
        sense=constraint.sense,
        lhs_value=lhs_value,
        rhs=constraint.rhs,
        slack=slack,
        satisfied=satisfied,
    )


def _clean_solution_value(value: float, kind: str) -> float:
    if abs(value) <= _SOLUTION_TOLERANCE:
        value = 0.0
    if kind in {"integer", "binary"} and abs(value - round(value)) <= _SOLUTION_TOLERANCE:
        return float(int(round(value)))
    return value


def _ensure_ortools_available() -> None:
    if pywraplp is not None:
        return
    raise ModuleNotFoundError(
        "Google OR-Tools is required for benchmark solving. Install it with `pip install ortools`."
    ) from _ORTOOLS_IMPORT_ERROR


def _as_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"`{field_name}` must be numeric.")
    return float(value)


def _is_integral(value: float) -> bool:
    return abs(value - round(value)) <= _SOLUTION_TOLERANCE


def _normalize_problem_type(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("`problem_type` must be a string.")
    text = value.strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "LP": "LP",
        "LINEAR_PROGRAM": "LP",
        "MILP": "MILP",
        "MIP": "MILP",
        "MIXED_INTEGER_LINEAR_PROGRAM": "MILP",
        "ILP": "ILP",
        "INTEGER_LINEAR_PROGRAM": "ILP",
        "BINARY": "BINARY",
        "BINARY_ILP": "BINARY",
        "BILP": "BINARY",
    }
    normalized = aliases.get(text)
    if normalized not in SUPPORTED_PROBLEM_TYPES:
        raise ValueError(
            f"Unsupported `problem_type` {value!r}. Use one of {sorted(SUPPORTED_PROBLEM_TYPES)}."
        )
    return normalized


def _normalize_variable_kind(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Variable `type` must be a string.")
    text = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "continuous": "continuous",
        "cont": "continuous",
        "real": "continuous",
        "float": "continuous",
        "integer": "integer",
        "int": "integer",
        "binary": "binary",
        "bool": "binary",
        "boolean": "binary",
    }
    normalized = aliases.get(text)
    if normalized not in SUPPORTED_VARIABLE_TYPES:
        raise ValueError(
            f"Unsupported variable type {value!r}. Use one of {sorted(SUPPORTED_VARIABLE_TYPES)}."
        )
    return normalized


def _normalize_objective_sense(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("`objective.sense` must be a string.")
    text = value.strip().lower()
    aliases = {
        "max": "max",
        "maximize": "max",
        "min": "min",
        "minimize": "min",
    }
    normalized = aliases.get(text)
    if normalized not in {"max", "min"}:
        raise ValueError("`objective.sense` must be `max` or `min`.")
    return normalized


def _normalize_constraint_sense(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Constraint `sense` must be a string.")
    text = value.strip().lower()
    aliases = {
        "<=": "<=",
        "le": "<=",
        "leq": "<=",
        ">=": ">=",
        "ge": ">=",
        "geq": ">=",
        "=": "=",
        "==": "=",
        "eq": "=",
    }
    normalized = aliases.get(text)
    if normalized not in SUPPORTED_CONSTRAINT_SENSES:
        raise ValueError(
            f"Unsupported constraint sense {value!r}. Use one of {sorted(SUPPORTED_CONSTRAINT_SENSES)}."
        )
    return normalized


def _status_name(status_code: int) -> str:
    mapping = {
        pywraplp.Solver.OPTIMAL: "OPTIMAL",
        pywraplp.Solver.FEASIBLE: "FEASIBLE",
        pywraplp.Solver.INFEASIBLE: "INFEASIBLE",
        pywraplp.Solver.UNBOUNDED: "UNBOUNDED",
        pywraplp.Solver.ABNORMAL: "ABNORMAL",
        pywraplp.Solver.NOT_SOLVED: "NOT_SOLVED",
    }
    return mapping.get(status_code, f"UNKNOWN_{status_code}")
