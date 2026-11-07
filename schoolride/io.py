from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from .models import (
    BusPlan,
    BusState,
    ConstraintSettings,
    DisruptionEvent,
    ObjectiveWeights,
    Scenario,
    Settings,
    SolverSettings,
    Stop,
)


def _to_minutes(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
        if ":" in text:
            h, m = text.split(":", 1)
            return int(h) * 60 + int(m)
    raise ValueError(f"Invalid time value: {value!r}")


def _load_json(path: str | Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def load_scenario(path: str | Path) -> Scenario:
    raw = _load_json(path)

    stops: Dict[str, Stop] = {}
    for row in raw["stops"]:
        stop_id = row["id"]
        stops[stop_id] = Stop(
            id=stop_id,
            stop_type=row["type"],
            planned_time=_to_minutes(row.get("planned_time", 0)),
            load=int(row.get("load", 0)),
            school_id=row.get("school_id", ""),
            bell_time=_to_minutes(row["bell_time"]) if "bell_time" in row else None,
        )

    buses: Dict[str, BusPlan] = {}
    for row in raw["buses"]:
        bus_id = row["id"]
        buses[bus_id] = BusPlan(
            bus_id=bus_id,
            capacity=int(row["capacity"]),
            start_time=_to_minutes(row.get("start_time", 0)),
            route_pre=list(row.get("route_pre", [])),
        )

    states: Dict[str, BusState] = {}
    for row in raw["states"]:
        bus_id = row["bus_id"]
        states[bus_id] = BusState(
            bus_id=bus_id,
            eta=_to_minutes(row.get("eta", 0)),
            loc=row["loc"],
            onboard={str(k): int(v) for k, v in row.get("onboard", {}).items()},
            passed=list(row.get("passed", [])),
            remaining=list(row.get("remaining", [])),
            active=bool(row.get("active", True)),
        )

    travel: Dict[Tuple[str, str], int] = {}
    for from_stop, row in raw["travel_time"].items():
        for to_stop, minutes in row.items():
            travel[(from_stop, to_stop)] = int(minutes)

    return Scenario(
        stops=stops,
        buses=buses,
        states=states,
        travel_time=travel,
        default_travel_time=int(raw.get("default_travel_time", 10**6)),
    )


def load_settings(path: str | Path) -> Settings:
    raw = _load_json(path)
    objective = raw.get("objective", {})
    constraints = raw.get("constraints", {})
    solver = raw.get("solver", {})

    return Settings(
        objective=ObjectiveWeights(
            wait=float(objective.get("wait", 1.0)),
            school_delay=float(objective.get("school_delay", 1.0)),
            route_change=float(objective.get("route_change", 0.1)),
            ride_violation=float(objective.get("ride_violation", 1.0)),
        ),
        constraints=ConstraintSettings(
            enforce_c1_coverage=bool(constraints.get("enforce_c1_coverage", True)),
            enforce_c2_uniqueness=bool(constraints.get("enforce_c2_uniqueness", True)),
            enforce_c3_capacity=bool(constraints.get("enforce_c3_capacity", True)),
            enforce_c4_mixed_load=bool(constraints.get("enforce_c4_mixed_load", True)),
            enforce_c5_dropoff=bool(constraints.get("enforce_c5_dropoff", True)),
            enforce_c6_early_arrival=bool(constraints.get("enforce_c6_early_arrival", True)),
            school_start_time=_to_minutes(constraints.get("school_start_time", "08:00")),
            early_arrival_buffer=int(constraints.get("early_arrival_buffer", 5)),
            service_time_per_student=float(constraints.get("service_time_per_student", 0.25)),
            max_ride_time=int(constraints.get("max_ride_time", 60)),
        ),
        solver=SolverSettings(
            enable_pruning=bool(solver.get("enable_pruning", True)),
            max_candidate_buses=int(solver.get("max_candidate_buses", 4)),
            per_vehicle_top_n=int(solver.get("per_vehicle_top_n", 5)),
            global_top_m=int(solver.get("global_top_m", 20)),
            infeasible_penalty=float(solver.get("infeasible_penalty", 1e8)),
        ),
    )


def load_event(path: str | Path) -> DisruptionEvent:
    raw = _load_json(path)

    removed_buses = set(raw.get("removed_buses", []))
    disrupted_arcs = {
        (str(pair[0]), str(pair[1]))
        for pair in raw.get("disrupted_arcs", [])
        if isinstance(pair, (list, tuple)) and len(pair) == 2
    }

    travel_time_updates: Dict[Tuple[str, str], int] = {}
    updates = raw.get("travel_time_updates", {})
    if isinstance(updates, dict):
        for key, value in updates.items():
            if isinstance(value, dict):
                from_stop = str(key)
                for to_stop, minutes in value.items():
                    travel_time_updates[(from_stop, str(to_stop))] = int(minutes)
            else:
                if "|" not in key:
                    raise ValueError(
                        "Flat travel_time_updates keys must use the form 'from|to'."
                    )
                from_stop, to_stop = key.split("|", 1)
                travel_time_updates[(from_stop, to_stop)] = int(value)

    return DisruptionEvent(
        removed_buses=removed_buses,
        disrupted_arcs=disrupted_arcs,
        travel_time_updates=travel_time_updates,
    )


def _parse_scalar(text: str) -> Any:
    low = text.lower()
    if low in {"true", "false"}:
        return low == "true"
    if low == "none":
        return None
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        pass

    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]

    return text


def _set_attr_path(obj: Any, path: Iterable[str], value: Any) -> None:
    parts = list(path)
    target = obj
    for part in parts[:-1]:
        target = getattr(target, part)
    setattr(target, parts[-1], value)


def apply_overrides(scenario: Scenario, settings: Settings, overrides: Iterable[str]) -> None:
    for item in overrides:
        if "=" not in item:
            raise ValueError(f"Override must look like path=value, got: {item}")
        key, raw_value = item.split("=", 1)
        value = _parse_scalar(raw_value)
        key = key.strip()

        if key.startswith("objective."):
            _set_attr_path(settings, key.split("."), value)
            continue
        if key.startswith("constraints."):
            if key == "constraints.school_start_time":
                value = _to_minutes(value)
            _set_attr_path(settings, key.split("."), value)
            continue
        if key.startswith("solver."):
            _set_attr_path(settings, key.split("."), value)
            continue

        if key.startswith("state."):
            parts = key.split(".")
            if len(parts) < 4:
                raise ValueError(
                    "State override path format: state.<bus_id>.<field>[.<subfield>]"
                )
            bus_id = parts[1]
            field_name = parts[2]
            state = scenario.states[bus_id]

            if field_name in {"eta"}:
                setattr(state, field_name, _to_minutes(value))
            elif field_name in {"loc"}:
                setattr(state, field_name, str(value))
            elif field_name in {"active"}:
                setattr(state, field_name, bool(value))
            elif field_name in {"remaining", "passed"}:
                if not isinstance(value, list):
                    raise ValueError(f"{field_name} requires comma-separated list value")
                setattr(state, field_name, [str(x) for x in value])
            elif field_name == "onboard":
                if len(parts) == 3:
                    if not isinstance(value, dict):
                        raise ValueError("onboard override requires key: state.<bus>.onboard.<school>=n")
                else:
                    school_id = parts[3]
                    state.onboard[school_id] = int(value)
            else:
                raise ValueError(f"Unsupported state field: {field_name}")
            continue

        raise ValueError(
            f"Unsupported override path '{key}'. Use objective.*, constraints.*, solver.*, or state.*"
        )
