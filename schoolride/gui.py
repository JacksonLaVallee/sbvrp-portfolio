from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

try:
    from schoolride.benchmark_solver import parse_benchmark_problem_json, solve_benchmark_problem
    from schoolride.io import load_event, load_scenario, load_settings
    from schoolride.models import DisruptionEvent
    from schoolride.solver import AdvInsSolver
except ModuleNotFoundError:
    ROOT = Path(__file__).resolve().parents[1]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from schoolride.benchmark_solver import parse_benchmark_problem_json, solve_benchmark_problem
    from schoolride.io import load_event, load_scenario, load_settings
    from schoolride.models import DisruptionEvent
    from schoolride.solver import AdvInsSolver


EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
BENCHMARK_EDITOR_KEY = "benchmark_problem_json"
BENCHMARK_SOURCE_KEY = "benchmark_problem_source"
BENCHMARK_TEMPLATE = json.dumps(
    {
        "name": "Editable LP Example",
        "problem_type": "LP",
        "objective": {
            "sense": "max",
            "coefficients": {"x": 3, "y": 2},
        },
        "variables": {
            "x": {"type": "continuous", "lb": 0},
            "y": {"type": "continuous", "lb": 0},
        },
        "constraints": [
            {
                "name": "capacity",
                "coefficients": {"x": 1, "y": 1},
                "sense": "<=",
                "rhs": 8,
            },
            {
                "name": "mix",
                "coefficients": {"x": 2, "y": 1},
                "sense": "<=",
                "rhs": 10,
            },
        ],
    },
    indent=2,
)


def _minutes_to_hhmm(value: int) -> str:
    sign = "-" if value < 0 else ""
    m = abs(int(value))
    return f"{sign}{m // 60:02d}:{m % 60:02d}"


def _hhmm_to_minutes(text: str) -> int:
    value = str(text).strip()
    if ":" in value:
        h, m = value.split(":", 1)
        return int(h) * 60 + int(m)
    return int(float(value))


def _list_json_files(dir_path: Path, prefix: str) -> List[str]:
    if not dir_path.exists():
        return []
    files = [path.name for path in dir_path.glob("*.json") if path.name.startswith(prefix)]
    files.sort()
    return files


def _label_from_filename(filename: str) -> str:
    return Path(filename).stem.replace("_", " ").title()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _event_payload(
    removed_buses: Iterable[str],
    arc_rows: pd.DataFrame,
    travel_rows: pd.DataFrame,
) -> Dict[str, object]:
    disrupted_arcs: List[List[str]] = []
    for _, row in arc_rows.iterrows():
        u = str(row.get("from_stop", "")).strip()
        v = str(row.get("to_stop", "")).strip()
        if u and v:
            disrupted_arcs.append([u, v])

    travel_time_updates: Dict[str, Dict[str, int]] = {}
    for _, row in travel_rows.iterrows():
        u = str(row.get("from_stop", "")).strip()
        v = str(row.get("to_stop", "")).strip()
        minutes = row.get("minutes", None)
        if not u or not v:
            continue
        if minutes is None or (isinstance(minutes, float) and pd.isna(minutes)):
            continue
        travel_time_updates.setdefault(u, {})[v] = int(minutes)

    return {
        "removed_buses": sorted({str(bus_id) for bus_id in removed_buses}),
        "disrupted_arcs": disrupted_arcs,
        "travel_time_updates": travel_time_updates,
    }


def _event_from_ui(
    removed_buses: Iterable[str],
    arc_rows: pd.DataFrame,
    travel_rows: pd.DataFrame,
) -> DisruptionEvent:
    payload = _event_payload(removed_buses, arc_rows, travel_rows)
    return DisruptionEvent(
        removed_buses=set(payload["removed_buses"]),
        disrupted_arcs={tuple(pair) for pair in payload["disrupted_arcs"]},
        travel_time_updates={
            (from_stop, to_stop): int(minutes)
            for from_stop, row in payload["travel_time_updates"].items()
            for to_stop, minutes in row.items()
        },
    )


def _build_timeline_figure(result) -> go.Figure:
    rows = []
    for bus_id in sorted(result.routes):
        sim = result.evaluation.bus_results[bus_id]
        for step in sim.steps:
            rows.append(
                {
                    "bus_id": bus_id,
                    "stop_id": step.stop_id,
                    "arrival": step.arrival,
                }
            )

    fig = go.Figure()
    if not rows:
        return fig

    df = pd.DataFrame(rows)
    for bus_id in sorted(df["bus_id"].unique()):
        sub = df[df["bus_id"] == bus_id].sort_values("arrival")
        fig.add_trace(
            go.Scatter(
                x=sub["arrival"],
                y=sub["bus_id"],
                mode="lines+markers+text",
                text=sub["stop_id"],
                textposition="top center",
                name=bus_id,
            )
        )

    fig.update_layout(
        title="Rerouted Timeline",
        xaxis_title="Time (minutes)",
        yaxis_title="Bus",
        template="plotly_white",
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig


def _render_violations(title: str, violations: Dict[str, List[str]]) -> None:
    st.markdown(f"#### {title}")
    if not violations:
        st.success("No violations.")
        return
    for scope, issues in violations.items():
        for issue in issues:
            st.error(f"[{scope}] {issue}")


def _render_best_effort_notice(result) -> None:
    if not result.best_effort:
        return

    st.warning(
        "No fully feasible reroute satisfied the enabled constraints. Showing the best available plan instead."
    )
    st.markdown("\n".join(f"- {reason}" for reason in result.best_effort_reasons))


def _set_benchmark_editor(text: str, source_label: str) -> None:
    st.session_state[BENCHMARK_EDITOR_KEY] = text
    st.session_state[BENCHMARK_SOURCE_KEY] = source_label


def _ensure_benchmark_editor(default_example: str | None) -> None:
    if BENCHMARK_EDITOR_KEY in st.session_state:
        return

    if default_example:
        _set_benchmark_editor(_read_text(EXAMPLES_DIR / default_example), default_example)
        return

    _set_benchmark_editor(BENCHMARK_TEMPLATE, "Built-in template")


def _benchmark_variable_rows(problem) -> List[Dict[str, Any]]:
    return [
        {
            "variable": name,
            "type": variable.kind,
            "lower_bound": variable.lower_bound,
            "upper_bound": variable.upper_bound,
            "objective_coefficient": problem.objective.coefficients.get(name, 0.0),
        }
        for name, variable in problem.variables.items()
    ]


def _benchmark_constraint_rows(problem) -> List[Dict[str, Any]]:
    return [
        {
            "constraint": constraint.name,
            "sense": constraint.sense,
            "rhs": constraint.rhs,
            "coefficients": json.dumps(constraint.coefficients, sort_keys=True),
        }
        for constraint in problem.constraints
    ]


def _benchmark_result_payload(problem, result) -> Dict[str, Any]:
    return {
        "problem": {
            "name": problem.name,
            "problem_type": problem.problem_type,
            "objective": {
                "sense": problem.objective.sense,
                "coefficients": problem.objective.coefficients,
            },
            "variables": {
                name: {
                    "type": variable.kind,
                    "lb": variable.lower_bound,
                    "ub": variable.upper_bound,
                }
                for name, variable in problem.variables.items()
            },
            "constraints": [
                {
                    "name": constraint.name,
                    "sense": constraint.sense,
                    "rhs": constraint.rhs,
                    "coefficients": constraint.coefficients,
                }
                for constraint in problem.constraints
            ],
        },
        "result": {
            "status": result.status,
            "backend": result.backend,
            "objective_value": result.objective_value,
            "variable_values": result.variable_values,
            "constraint_results": [
                {
                    "name": row.name,
                    "sense": row.sense,
                    "lhs_value": row.lhs_value,
                    "rhs": row.rhs,
                    "slack": row.slack,
                    "satisfied": row.satisfied,
                }
                for row in result.constraint_results
            ],
        },
    }


def _render_benchmark_page() -> None:
    benchmark_files = _list_json_files(EXAMPLES_DIR, "benchmark")
    default_benchmark = (
        "benchmark_lp_simple.json"
        if "benchmark_lp_simple.json" in benchmark_files
        else (benchmark_files[0] if benchmark_files else None)
    )
    _ensure_benchmark_editor(default_benchmark)

    st.title("Benchmark LP / MILP Solver")
    st.caption(
        "Load a JSON benchmark instance, adjust coefficients or bounds, and solve LP, MILP, ILP, or binary models with OR-Tools."
    )

    with st.sidebar:
        st.header("Benchmark Inputs")
        selected_example = None
        if benchmark_files:
            default_index = benchmark_files.index(default_benchmark) if default_benchmark else 0
            selected_example = st.selectbox(
                "Bundled example",
                benchmark_files,
                index=default_index,
                format_func=_label_from_filename,
                key="benchmark_example_selector",
            )
            if st.button("Load selected example", key="benchmark_load_example"):
                _set_benchmark_editor(
                    _read_text(EXAMPLES_DIR / selected_example),
                    selected_example,
                )
        else:
            st.info("No bundled benchmark examples were found. The editor is using the built-in template.")

        uploaded_file = st.file_uploader(
            "Upload benchmark JSON",
            type=["json"],
            key="benchmark_json_upload",
        )
        if uploaded_file is not None and st.button("Load uploaded JSON", key="benchmark_load_upload"):
            _set_benchmark_editor(
                uploaded_file.getvalue().decode("utf-8-sig"),
                uploaded_file.name,
            )

    current_source = st.session_state.get(
        BENCHMARK_SOURCE_KEY,
        selected_example or "Built-in template",
    )
    st.caption(f"Current editor source: `{current_source}`")

    with st.expander("Supported JSON format", expanded=False):
        st.markdown(
            "- Supported `problem_type` values: `LP`, `MILP`, `ILP`, `BINARY`\n"
            "- Variable `type` can be `continuous`, `integer`, or `binary`\n"
            "- Constraint `sense` can be `<=`, `>=`, or `=`\n"
            "- Edit the JSON directly to adjust coefficients, bounds, senses, or variable types"
        )
        st.code(BENCHMARK_TEMPLATE, language="json")

    raw_json = st.text_area(
        "Benchmark problem JSON",
        key=BENCHMARK_EDITOR_KEY,
        height=420,
    )

    parsed_problem = None
    parse_error: Exception | None = None
    try:
        parsed_problem = parse_benchmark_problem_json(raw_json)
    except Exception as exc:
        parse_error = exc

    if parse_error is not None:
        st.warning(f"JSON is not ready to solve yet: {parse_error}")
    else:
        counts = {"continuous": 0, "integer": 0, "binary": 0}
        for variable in parsed_problem.variables.values():
            counts[variable.kind] += 1

        p1, p2, p3, p4, p5 = st.columns(5)
        p1.metric("Problem type", parsed_problem.problem_type)
        p2.metric("Objective", parsed_problem.objective.sense.upper())
        p3.metric("Variables", len(parsed_problem.variables))
        p4.metric("Constraints", len(parsed_problem.constraints))
        p5.metric("Discrete vars", counts["integer"] + counts["binary"])

        preview_tabs = st.tabs(["Variables", "Constraints"])
        with preview_tabs[0]:
            st.dataframe(
                pd.DataFrame(_benchmark_variable_rows(parsed_problem)),
                width="stretch",
                hide_index=True,
            )
        with preview_tabs[1]:
            st.dataframe(
                pd.DataFrame(_benchmark_constraint_rows(parsed_problem)),
                width="stretch",
                hide_index=True,
            )

    solve = st.button("Solve benchmark model", type="primary", key="benchmark_solve")
    if not solve:
        return

    try:
        problem = parse_benchmark_problem_json(raw_json)
        result = solve_benchmark_problem(problem)
    except Exception as exc:
        st.exception(exc)
        return

    result_payload = _benchmark_result_payload(problem, result)

    st.subheader("Benchmark Results")
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Status", result.status)
    r2.metric("Backend", result.backend)
    r3.metric(
        "Objective value",
        f"{result.objective_value:.4f}" if result.objective_value is not None else "n/a",
    )
    r4.metric("Solved vars", len(result.variable_values))

    if not result.has_solution:
        st.error(
            "The solver did not return a feasible solution for this model. Check the status and adjust the JSON if needed."
        )
        st.download_button(
            "Download result JSON",
            data=json.dumps(result_payload, indent=2),
            file_name="benchmark_solver_result.json",
            mime="application/json",
            key="benchmark_download_infeasible",
        )
        with st.expander("Result JSON preview", expanded=False):
            st.code(json.dumps(result_payload, indent=2), language="json")
        return

    variable_rows = [
        {
            "variable": name,
            "type": problem.variables[name].kind,
            "value": value,
            "lower_bound": problem.variables[name].lower_bound,
            "upper_bound": problem.variables[name].upper_bound,
        }
        for name, value in result.variable_values.items()
    ]
    constraint_rows = [
        {
            "constraint": row.name,
            "sense": row.sense,
            "lhs_value": row.lhs_value,
            "rhs": row.rhs,
            "slack": row.slack,
            "satisfied": row.satisfied,
        }
        for row in result.constraint_results
    ]

    result_tabs = st.tabs(["Variable values", "Constraint checks", "Export"])
    with result_tabs[0]:
        st.dataframe(
            pd.DataFrame(variable_rows),
            width="stretch",
            hide_index=True,
        )

    with result_tabs[1]:
        st.dataframe(
            pd.DataFrame(constraint_rows),
            width="stretch",
            hide_index=True,
        )

    with result_tabs[2]:
        st.download_button(
            "Download result JSON",
            data=json.dumps(result_payload, indent=2),
            file_name="benchmark_solver_result.json",
            mime="application/json",
            key="benchmark_download_result",
        )
        with st.expander("Result JSON preview", expanded=False):
            st.code(json.dumps(result_payload, indent=2), language="json")


def _render_schoolride_page() -> None:

    st.title("SchoolRide Disruption Management")
    st.caption(
        "Edit the current operating state, apply a disruption, and compare the baseline plan against the rerouted plan."
    )

    scenario_files = _list_json_files(EXAMPLES_DIR, "scenario")
    config_files = _list_json_files(EXAMPLES_DIR, "config")
    event_files = _list_json_files(EXAMPLES_DIR, "event")

    if not scenario_files or not config_files:
        st.error("Expected example scenario and config files inside `examples/`.")
        return

    default_scenario = "scenario_small.json" if "scenario_small.json" in scenario_files else scenario_files[0]
    default_config = "config_default.json" if "config_default.json" in config_files else config_files[0]

    with st.sidebar:
        st.header("Inputs")
        scenario_name = st.selectbox(
            "Scenario",
            scenario_files,
            index=scenario_files.index(default_scenario),
            format_func=_label_from_filename,
        )
        config_name = st.selectbox(
            "Config",
            config_files,
            index=config_files.index(default_config),
            format_func=_label_from_filename,
        )
        event_mode = st.radio("Event source", ["Load event file", "Manual event"], index=0)

        selected_event_name = None
        if event_mode == "Load event file":
            if event_files:
                selected_event_name = st.selectbox(
                    "Event",
                    event_files,
                    index=0,
                    format_func=_label_from_filename,
                )
            else:
                st.warning("No event files found. Switching to manual event.")
                event_mode = "Manual event"

    scenario = load_scenario(EXAMPLES_DIR / scenario_name)
    settings = load_settings(EXAMPLES_DIR / config_name)
    base_event = (
        load_event(EXAMPLES_DIR / selected_event_name)
        if event_mode == "Load event file" and selected_event_name
        else DisruptionEvent()
    )

    with st.expander("What this page does", expanded=False):
        st.markdown(
            "- `Objective weights` change how the solver scores route options.\n"
            "- `Constraints` turn the paper's C1-C6 checks on or off.\n"
            "- `Live bus state` is the most important editable input because rerouting starts from the current bus positions and loads.\n"
            "- `Disruption event` removes buses, blocks arcs, or changes travel times.\n"
            "- `Run comparison` evaluates the current plan first, then reroutes from the same starting state."
        )

    st.subheader("Objective Weights")
    col1, col2, col3, col4 = st.columns(4)
    settings.objective.wait = col1.number_input(
        "omega_wait",
        min_value=0.0,
        value=float(settings.objective.wait),
        step=0.1,
        help="Penalty weight for late pickup arrivals.",
    )
    settings.objective.school_delay = col2.number_input(
        "omega_school_delay",
        min_value=0.0,
        value=float(settings.objective.school_delay),
        step=0.1,
        help="Penalty weight for late school arrivals.",
    )
    settings.objective.route_change = col3.number_input(
        "omega_route_change",
        min_value=0.0,
        value=float(settings.objective.route_change),
        step=0.1,
        help="Penalty weight for changing the route shape.",
    )
    settings.objective.ride_violation = col4.number_input(
        "omega_ride_violation",
        min_value=0.0,
        value=float(settings.objective.ride_violation),
        step=0.1,
        help="Penalty weight for exceeding maximum ride time.",
    )

    st.subheader("Constraints")
    flag1, flag2, flag3 = st.columns(3)
    settings.constraints.enforce_c1_coverage = flag1.toggle("C1 Coverage", value=settings.constraints.enforce_c1_coverage)
    settings.constraints.enforce_c2_uniqueness = flag2.toggle("C2 Uniqueness", value=settings.constraints.enforce_c2_uniqueness)
    settings.constraints.enforce_c3_capacity = flag3.toggle("C3 Capacity", value=settings.constraints.enforce_c3_capacity)

    flag4, flag5, flag6 = st.columns(3)
    settings.constraints.enforce_c4_mixed_load = flag4.toggle("C4 Mixed-load", value=settings.constraints.enforce_c4_mixed_load)
    settings.constraints.enforce_c5_dropoff = flag5.toggle("C5 Drop-off", value=settings.constraints.enforce_c5_dropoff)
    settings.constraints.enforce_c6_early_arrival = flag6.toggle("C6 Early-arrival", value=settings.constraints.enforce_c6_early_arrival)

    num1, num2, num3 = st.columns(3)
    settings.constraints.early_arrival_buffer = int(
        num1.number_input(
            "early_arrival_buffer (min)",
            min_value=0,
            value=int(settings.constraints.early_arrival_buffer),
            step=1,
        )
    )
    settings.constraints.service_time_per_student = float(
        num2.number_input(
            "service_time_per_student",
            min_value=0.0,
            value=float(settings.constraints.service_time_per_student),
            step=0.05,
        )
    )
    settings.constraints.max_ride_time = int(
        num3.number_input(
            "max_ride_time (min)",
            min_value=1,
            value=int(settings.constraints.max_ride_time),
            step=1,
            help="Hard limit for time between pickup and school arrival. Default is 60 minutes.",
        )
    )
    school_start_text = st.text_input(
        "school_start_time",
        value=_minutes_to_hhmm(int(settings.constraints.school_start_time)),
        help="Hard latest school arrival time. Students must be at school by this time.",
    )
    try:
        settings.constraints.school_start_time = _hhmm_to_minutes(school_start_text)
    except ValueError:
        st.warning("school_start_time must be `HH:MM` or a numeric minute value.")

    st.subheader("Solver Settings")
    s1, s2, s3, s4 = st.columns(4)
    settings.solver.enable_pruning = s1.toggle("enable_pruning", value=settings.solver.enable_pruning)
    settings.solver.max_candidate_buses = int(
        s2.number_input(
            "max_candidate_buses",
            min_value=1,
            value=int(settings.solver.max_candidate_buses),
            step=1,
        )
    )
    settings.solver.per_vehicle_top_n = int(
        s3.number_input(
            "per_vehicle_top_n",
            min_value=1,
            value=int(settings.solver.per_vehicle_top_n),
            step=1,
        )
    )
    settings.solver.global_top_m = int(
        s4.number_input(
            "global_top_m",
            min_value=1,
            value=int(settings.solver.global_top_m),
            step=1,
        )
    )
    st.caption("Note: `per_vehicle_top_n` and `global_top_m` are visible for completeness, but the current solver does not actively use them.")

    st.subheader("Live Bus State")
    st.caption("Edit the buses' current ETA, location, remaining route, and onboard student loads.")
    bus_rows = []
    for bus_id in sorted(scenario.states):
        state = scenario.states[bus_id]
        bus_rows.append(
            {
                "bus_id": bus_id,
                "active": bool(state.active),
                "eta": _minutes_to_hhmm(int(state.eta)),
                "loc": state.loc,
                "remaining": ",".join(state.remaining),
                "passed": ",".join(state.passed),
                "onboard_json": json.dumps(state.onboard, sort_keys=True),
            }
        )

    edited_bus_df = st.data_editor(
        pd.DataFrame(bus_rows),
        width="stretch",
        hide_index=True,
        disabled=["bus_id"],
        num_rows="fixed",
        key=f"bus_state_editor_{scenario_name}",
        column_config={
            "bus_id": st.column_config.TextColumn("Bus ID"),
            "active": st.column_config.CheckboxColumn("Active"),
            "eta": st.column_config.TextColumn("ETA"),
            "loc": st.column_config.TextColumn("Current stop"),
            "remaining": st.column_config.TextColumn("Remaining route"),
            "passed": st.column_config.TextColumn("Passed stops"),
            "onboard_json": st.column_config.TextColumn("Onboard load JSON"),
        },
    )

    state_errors: List[str] = []
    for _, row in edited_bus_df.iterrows():
        bus_id = str(row["bus_id"])
        current_state = scenario.states[bus_id]

        try:
            parsed_eta = _hhmm_to_minutes(str(row.get("eta", current_state.eta)))
        except ValueError:
            state_errors.append(f"{bus_id}: ETA must be `HH:MM` or a numeric minute value.")
            continue

        try:
            onboard_json = str(row.get("onboard_json", "{}")).strip() or "{}"
            parsed_onboard = json.loads(onboard_json)
            if not isinstance(parsed_onboard, dict):
                raise ValueError("onboard_json must decode to an object")
        except (json.JSONDecodeError, ValueError):
            state_errors.append(f"{bus_id}: onboard_json must look like `{{\"S_A\": 10}}`.")
            continue

        current_state.active = bool(row.get("active", True))
        current_state.eta = parsed_eta
        current_state.loc = str(row.get("loc", current_state.loc)).strip()
        current_state.remaining = [x.strip() for x in str(row.get("remaining", "")).split(",") if x.strip()]
        current_state.passed = [x.strip() for x in str(row.get("passed", "")).split(",") if x.strip()]
        current_state.onboard = {str(k): int(v) for k, v in parsed_onboard.items()}

    if state_errors:
        st.warning("\n".join(state_errors))

    st.subheader("Disruption Event")
    all_bus_ids = sorted(scenario.states.keys())

    if event_mode == "Load event file":
        removed_default = sorted(base_event.removed_buses)
        arc_default_rows = [{"from_stop": u, "to_stop": v} for (u, v) in sorted(base_event.disrupted_arcs)]
        update_default_rows = [
            {"from_stop": u, "to_stop": v, "minutes": t}
            for (u, v), t in sorted(base_event.travel_time_updates.items())
        ]
    else:
        removed_default = []
        arc_default_rows = []
        update_default_rows = []

    removed_buses = st.multiselect(
        "Removed buses",
        options=all_bus_ids,
        default=removed_default,
        help="Use this for breakdowns, no-show drivers, or buses taken out of service.",
    )

    event_left, event_right = st.columns(2)
    with event_left:
        arc_df = st.data_editor(
            pd.DataFrame(arc_default_rows or [{"from_stop": "", "to_stop": ""}]),
            width="stretch",
            hide_index=True,
            num_rows="dynamic",
            key=f"arc_editor_{selected_event_name or 'manual'}_{scenario_name}",
            column_config={
                "from_stop": st.column_config.TextColumn("From stop"),
                "to_stop": st.column_config.TextColumn("To stop"),
            },
        )
        st.caption("Blocked directed arcs. The solver will try to avoid them.")

    with event_right:
        update_df = st.data_editor(
            pd.DataFrame(update_default_rows or [{"from_stop": "", "to_stop": "", "minutes": None}]),
            width="stretch",
            hide_index=True,
            num_rows="dynamic",
            key=f"travel_update_editor_{selected_event_name or 'manual'}_{scenario_name}",
            column_config={
                "from_stop": st.column_config.TextColumn("From stop"),
                "to_stop": st.column_config.TextColumn("To stop"),
                "minutes": st.column_config.NumberColumn("Minutes", min_value=0, step=1),
            },
        )
        st.caption("Directional travel-time overrides in minutes.")

    current_event_payload = _event_payload(removed_buses, arc_df, update_df)
    with st.expander("Event JSON preview", expanded=False):
        st.code(json.dumps(current_event_payload, indent=2), language="json")

    run = st.button("Run baseline vs reroute comparison", type="primary")
    if not run:
        return

    try:
        event = _event_from_ui(removed_buses, arc_df, update_df)
        baseline_solver = AdvInsSolver(copy.deepcopy(scenario), copy.deepcopy(settings))
        baseline_eval = baseline_solver.evaluate_current_plan()

        reroute_solver = AdvInsSolver(copy.deepcopy(scenario), copy.deepcopy(settings))
        result = reroute_solver.solve(event)
    except Exception as exc:
        st.exception(exc)
        return

    baseline_routes = {
        bus_id: list(state.remaining)
        for bus_id, state in scenario.states.items()
        if state.active
    }
    changed_buses = sum(
        1 for bus_id, route in result.routes.items() if route != baseline_routes.get(bus_id, [])
    )

    st.subheader("Results")
    st.caption("Delta values below are `rerouted - baseline`.")
    _render_best_effort_notice(result)

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Objective", f"{result.evaluation.objective:.2f}", delta=f"{result.evaluation.objective - baseline_eval.objective:.2f}")
    m2.metric("Twait", f"{result.evaluation.wait_penalty:.2f}", delta=f"{result.evaluation.wait_penalty - baseline_eval.wait_penalty:.2f}")
    m3.metric("Tsch", f"{result.evaluation.school_delay_penalty:.2f}", delta=f"{result.evaluation.school_delay_penalty - baseline_eval.school_delay_penalty:.2f}")
    m4.metric("Croute", f"{result.evaluation.route_change_penalty:.2f}", delta=f"{result.evaluation.route_change_penalty - baseline_eval.route_change_penalty:.2f}")
    m5.metric("Cride", f"{result.evaluation.ride_violation_penalty:.2f}", delta=f"{result.evaluation.ride_violation_penalty - baseline_eval.ride_violation_penalty:.2f}")
    m6.metric("Changed buses", changed_buses)

    status_left, status_right = st.columns(2)
    with status_left:
        st.write(f"Baseline feasible: `{baseline_eval.feasible}`")
    with status_right:
        st.write(f"Rerouted feasible: `{result.evaluation.feasible}`")

    comparison_rows = [
        {"metric": "Objective", "baseline": baseline_eval.objective, "rerouted": result.evaluation.objective, "delta": result.evaluation.objective - baseline_eval.objective},
        {"metric": "Pickup wait", "baseline": baseline_eval.wait_penalty, "rerouted": result.evaluation.wait_penalty, "delta": result.evaluation.wait_penalty - baseline_eval.wait_penalty},
        {"metric": "School delay", "baseline": baseline_eval.school_delay_penalty, "rerouted": result.evaluation.school_delay_penalty, "delta": result.evaluation.school_delay_penalty - baseline_eval.school_delay_penalty},
        {"metric": "Route change", "baseline": baseline_eval.route_change_penalty, "rerouted": result.evaluation.route_change_penalty, "delta": result.evaluation.route_change_penalty - baseline_eval.route_change_penalty},
        {"metric": "Ride-time violation", "baseline": baseline_eval.ride_violation_penalty, "rerouted": result.evaluation.ride_violation_penalty, "delta": result.evaluation.ride_violation_penalty - baseline_eval.ride_violation_penalty},
    ]
    st.dataframe(pd.DataFrame(comparison_rows), width="stretch", hide_index=True)

    route_rows = []
    step_rows = []
    for bus_id in sorted(result.routes):
        route = result.routes[bus_id]
        route_rows.append(
            {
                "bus_id": bus_id,
                "stops": len(route),
                "route": " -> ".join(route) if route else "(empty)",
            }
        )
        sim = result.evaluation.bus_results[bus_id]
        for step in sim.steps:
            step_rows.append(
                {
                    "bus_id": bus_id,
                    "stop_id": step.stop_id,
                    "arrival_min": step.arrival,
                    "arrival_hhmm": _minutes_to_hhmm(step.arrival),
                    "load_before": step.load_before,
                    "load_after": step.load_after,
                }
            )

    result_tabs = st.tabs(["Routes", "Violations", "Export"])

    with result_tabs[0]:
        st.markdown("#### Rerouted routes")
        st.dataframe(pd.DataFrame(route_rows), width="stretch", hide_index=True)
        st.markdown("#### Stop-by-stop simulation")
        st.dataframe(pd.DataFrame(step_rows), width="stretch", hide_index=True)
        fig = _build_timeline_figure(result)
        if fig.data:
            st.plotly_chart(fig, width="stretch")

    with result_tabs[1]:
        v1, v2 = st.columns(2)
        with v1:
            _render_violations("Baseline violations", baseline_eval.violations)
        with v2:
            _render_violations("Rerouted violations", result.evaluation.violations)

    json_payload = {
        "scenario_file": scenario_name,
        "config_file": config_name,
        "event": current_event_payload,
        "baseline": {
            "objective": baseline_eval.objective,
            "feasible": baseline_eval.feasible,
            "routes": baseline_routes,
            "metrics": {
                "Twait": baseline_eval.wait_penalty,
                "Tsch": baseline_eval.school_delay_penalty,
                "Croute": baseline_eval.route_change_penalty,
                "Cride": baseline_eval.ride_violation_penalty,
            },
            "violations": baseline_eval.violations,
        },
        "rerouted": {
            "objective": result.evaluation.objective,
            "feasible": result.evaluation.feasible,
            "best_effort": result.best_effort,
            "best_effort_reasons": result.best_effort_reasons,
            "unresolved_pickups": result.unresolved_pickups,
            "routes": result.routes,
            "disrupted_pickups": result.disrupted_pickups,
            "metrics": {
                "Twait": result.evaluation.wait_penalty,
                "Tsch": result.evaluation.school_delay_penalty,
                "Croute": result.evaluation.route_change_penalty,
                "Cride": result.evaluation.ride_violation_penalty,
            },
            "violations": result.evaluation.violations,
        },
    }

    with result_tabs[2]:
        st.write(f"Disrupted pickups inserted: `{result.disrupted_pickups}`")
        st.write(f"Changed buses: `{changed_buses}`")
        st.write(f"Best-effort fallback: `{result.best_effort}`")
        if result.best_effort_reasons:
            st.markdown("\n".join(f"- {reason}" for reason in result.best_effort_reasons))
        st.download_button(
            "Download result JSON",
            data=json.dumps(json_payload, indent=2),
            file_name="schoolride_gui_result.json",
            mime="application/json",
        )
        with st.expander("JSON preview", expanded=False):
            st.code(json.dumps(json_payload, indent=2), language="json")


def main() -> None:
    st.set_page_config(
        page_title="Optimization Solver Interface",
        page_icon="Bus",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    with st.sidebar:
        st.header("Workspace")
        workspace = st.radio(
            "Choose a solver page",
            ["SchoolRide disruption solver", "Benchmark LP/MILP solver"],
            index=0,
        )

    if workspace == "Benchmark LP/MILP solver":
        _render_benchmark_page()
        return

    _render_schoolride_page()


if __name__ == "__main__":
    main()
