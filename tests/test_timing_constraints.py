from schoolride.evaluator import evaluate_solution
from schoolride.models import (
    BusPlan,
    BusState,
    ConstraintSettings,
    DisruptionEvent,
    ObjectiveWeights,
    STOP_DEPOT,
    STOP_PICKUP,
    STOP_SCHOOL,
    Scenario,
    Settings,
    SolverSettings,
    Stop,
)
from schoolride.solver import AdvInsSolver


def _mins(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def _settings(max_ride_time: int = 60) -> Settings:
    return Settings(
        objective=ObjectiveWeights(
            wait=1.0,
            school_delay=1.0,
            route_change=0.0,
            ride_violation=1.0,
        ),
        constraints=ConstraintSettings(
            school_start_time=_mins("08:00"),
            max_ride_time=max_ride_time,
            service_time_per_student=0.0,
        ),
        solver=SolverSettings(
            enable_pruning=True,
            max_candidate_buses=3,
            per_vehicle_top_n=5,
            global_top_m=20,
            infeasible_penalty=100000000,
        ),
    )


def _scenario(
    *,
    stops: list[Stop],
    travel_time: dict[tuple[str, str], int],
    eta: str,
    loc: str,
    remaining: list[str],
    onboard: dict[str, int] | None = None,
    capacity: int = 50,
) -> Scenario:
    return Scenario(
        stops={stop.id: stop for stop in stops},
        buses={
            "B1": BusPlan(
                bus_id="B1",
                capacity=capacity,
                start_time=_mins("07:00"),
                route_pre=list(remaining),
            )
        },
        states={
            "B1": BusState(
                bus_id="B1",
                eta=_mins(eta),
                loc=loc,
                onboard=dict(onboard or {}),
                passed=[],
                remaining=list(remaining),
                active=True,
            )
        },
        travel_time=travel_time,
        default_travel_time=999999,
    )


def test_exactly_sixty_minutes_on_bus_is_feasible() -> None:
    scenario = _scenario(
        stops=[
            Stop(id="DEPOT", stop_type=STOP_DEPOT, planned_time=_mins("00:00")),
            Stop(id="P1", stop_type=STOP_PICKUP, planned_time=_mins("07:00"), load=4, school_id="S_A"),
            Stop(id="S_A", stop_type=STOP_SCHOOL, planned_time=_mins("08:00"), bell_time=_mins("08:00")),
        ],
        travel_time={
            ("DEPOT", "P1"): 0,
            ("P1", "S_A"): 60,
        },
        eta="07:00",
        loc="DEPOT",
        remaining=["P1", "S_A"],
    )

    result = evaluate_solution(
        scenario,
        _settings(),
        {"B1": ["P1", "S_A"]},
        required_pickups={"P1"},
    )

    assert result.feasible
    assert result.wait_penalty == 0
    assert result.ride_violation_penalty == 0


def test_sixty_one_minutes_on_bus_is_infeasible() -> None:
    scenario = _scenario(
        stops=[
            Stop(id="DEPOT", stop_type=STOP_DEPOT, planned_time=_mins("00:00")),
            Stop(id="P1", stop_type=STOP_PICKUP, planned_time=_mins("07:00"), load=4, school_id="S_A"),
            Stop(id="S_A", stop_type=STOP_SCHOOL, planned_time=_mins("08:01"), bell_time=_mins("08:01")),
        ],
        travel_time={
            ("DEPOT", "P1"): 0,
            ("P1", "S_A"): 61,
        },
        eta="07:00",
        loc="DEPOT",
        remaining=["P1", "S_A"],
    )

    result = evaluate_solution(
        scenario,
        _settings(),
        {"B1": ["P1", "S_A"]},
        required_pickups={"P1"},
    )

    assert not result.feasible
    assert result.ride_violation_penalty == 4
    assert any("T1:" in issue for issue in result.violations["B1"])


def test_late_pickup_wait_is_penalized_without_breaking_ride_limit() -> None:
    scenario = _scenario(
        stops=[
            Stop(id="DEPOT", stop_type=STOP_DEPOT, planned_time=_mins("00:00")),
            Stop(id="P1", stop_type=STOP_PICKUP, planned_time=_mins("07:00"), load=3, school_id="S_A"),
            Stop(id="S_A", stop_type=STOP_SCHOOL, planned_time=_mins("08:00"), bell_time=_mins("08:00")),
        ],
        travel_time={
            ("DEPOT", "P1"): 20,
            ("P1", "S_A"): 40,
        },
        eta="07:00",
        loc="DEPOT",
        remaining=["P1", "S_A"],
    )

    result = evaluate_solution(
        scenario,
        _settings(),
        {"B1": ["P1", "S_A"]},
        required_pickups={"P1"},
    )

    assert result.feasible
    assert result.wait_penalty == 60
    assert result.ride_violation_penalty == 0


def test_travel_time_update_can_push_route_over_the_sixty_minute_limit() -> None:
    scenario = _scenario(
        stops=[
            Stop(id="DEPOT", stop_type=STOP_DEPOT, planned_time=_mins("00:00")),
            Stop(id="P1", stop_type=STOP_PICKUP, planned_time=_mins("07:00"), load=3, school_id="S_A"),
            Stop(id="S_A", stop_type=STOP_SCHOOL, planned_time=_mins("08:05"), bell_time=_mins("08:05")),
        ],
        travel_time={
            ("DEPOT", "P1"): 0,
            ("P1", "S_A"): 60,
        },
        eta="07:00",
        loc="DEPOT",
        remaining=["P1", "S_A"],
    )

    result = AdvInsSolver(scenario, _settings()).solve(
        DisruptionEvent(travel_time_updates={("P1", "S_A"): 65})
    )

    assert not result.evaluation.feasible
    assert result.best_effort
    assert result.evaluation.ride_violation_penalty == 15
    assert any("maximum onboard time" in reason.lower() for reason in result.best_effort_reasons)


def test_existing_onboard_students_use_current_eta_for_the_sixty_minute_limit() -> None:
    scenario = _scenario(
        stops=[
            Stop(id="MID", stop_type=STOP_DEPOT, planned_time=_mins("00:00")),
            Stop(id="S_A", stop_type=STOP_SCHOOL, planned_time=_mins("08:01"), bell_time=_mins("08:01")),
        ],
        travel_time={
            ("MID", "S_A"): 61,
        },
        eta="07:00",
        loc="MID",
        remaining=["S_A"],
        onboard={"S_A": 2},
    )

    result = evaluate_solution(scenario, _settings(), {"B1": ["S_A"]})

    assert not result.feasible
    assert result.ride_violation_penalty == 2
    assert any("existing onboard students" in issue for issue in result.violations["B1"])


def test_second_pickup_can_keep_first_students_over_the_limit() -> None:
    scenario = _scenario(
        stops=[
            Stop(id="DEPOT", stop_type=STOP_DEPOT, planned_time=_mins("00:00")),
            Stop(id="P1", stop_type=STOP_PICKUP, planned_time=_mins("07:00"), load=2, school_id="S_A"),
            Stop(id="P2", stop_type=STOP_PICKUP, planned_time=_mins("07:10"), load=3, school_id="S_A"),
            Stop(id="S_A", stop_type=STOP_SCHOOL, planned_time=_mins("08:01"), bell_time=_mins("08:01")),
        ],
        travel_time={
            ("DEPOT", "P1"): 0,
            ("P1", "P2"): 10,
            ("P2", "S_A"): 51,
        },
        eta="07:00",
        loc="DEPOT",
        remaining=["P1", "P2", "S_A"],
    )

    result = evaluate_solution(
        scenario,
        _settings(),
        {"B1": ["P1", "P2", "S_A"]},
        required_pickups={"P1", "P2"},
    )

    assert not result.feasible
    assert result.ride_violation_penalty == 2
    assert sum("T1:" in issue for issue in result.violations["B1"]) == 1


def test_school_arrival_at_eight_am_is_feasible() -> None:
    scenario = _scenario(
        stops=[
            Stop(id="DEPOT", stop_type=STOP_DEPOT, planned_time=_mins("00:00")),
            Stop(id="P1", stop_type=STOP_PICKUP, planned_time=_mins("07:20"), load=3, school_id="S_A"),
            Stop(id="S_A", stop_type=STOP_SCHOOL, planned_time=_mins("08:20"), bell_time=_mins("08:20")),
        ],
        travel_time={
            ("DEPOT", "P1"): 0,
            ("P1", "S_A"): 40,
        },
        eta="07:20",
        loc="DEPOT",
        remaining=["P1", "S_A"],
    )

    result = evaluate_solution(
        scenario,
        _settings(),
        {"B1": ["P1", "S_A"]},
        required_pickups={"P1"},
    )

    assert result.feasible
    assert result.bus_results["B1"].steps[-1].arrival == _mins("08:00")
    assert result.school_delay_penalty == 0


def test_school_arrival_after_eight_am_is_infeasible_even_if_bell_is_later() -> None:
    scenario = _scenario(
        stops=[
            Stop(id="DEPOT", stop_type=STOP_DEPOT, planned_time=_mins("00:00")),
            Stop(id="P1", stop_type=STOP_PICKUP, planned_time=_mins("07:20"), load=3, school_id="S_A"),
            Stop(id="S_A", stop_type=STOP_SCHOOL, planned_time=_mins("08:20"), bell_time=_mins("08:20")),
        ],
        travel_time={
            ("DEPOT", "P1"): 0,
            ("P1", "S_A"): 41,
        },
        eta="07:20",
        loc="DEPOT",
        remaining=["P1", "S_A"],
    )

    result = evaluate_solution(
        scenario,
        _settings(),
        {"B1": ["P1", "S_A"]},
        required_pickups={"P1"},
    )

    assert not result.feasible
    assert result.school_delay_penalty == 3
    assert any("T2:" in issue for issue in result.violations["B1"])


def test_early_school_arrival_is_not_delayed_to_the_school_stop_plan() -> None:
    scenario = _scenario(
        stops=[
            Stop(id="DEPOT", stop_type=STOP_DEPOT, planned_time=_mins("00:00")),
            Stop(id="P1", stop_type=STOP_PICKUP, planned_time=_mins("07:10"), load=2, school_id="S_A"),
            Stop(id="S_A", stop_type=STOP_SCHOOL, planned_time=_mins("08:15"), bell_time=_mins("08:15")),
        ],
        travel_time={
            ("DEPOT", "P1"): 0,
            ("P1", "S_A"): 30,
        },
        eta="07:10",
        loc="DEPOT",
        remaining=["P1", "S_A"],
    )

    result = evaluate_solution(
        scenario,
        _settings(),
        {"B1": ["P1", "S_A"]},
        required_pickups={"P1"},
    )

    assert result.feasible
    assert result.bus_results["B1"].steps[-1].arrival == _mins("07:40")
    assert result.school_delay_penalty == 0
