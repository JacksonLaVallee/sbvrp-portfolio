# SBVRP and LP/MILP Research Testbed

## MS portfolio source snapshot

Public source for Jackson La Vallee's Vanderbilt MS Computer Science portfolio, Section 6. Developed during Spring 2026 research with Meiyi Ma and Simon Plotkin, this repository contains the school-bus routing interface, scenario tests and general linear/mixed-integer optimization testbed used in the XOPT work.

The joint research report credits Jackson with leading the configurable routing interface, edge cases and extension to general LP/MILP experimentation. It credits Simon with leading the baseline review and implementing the deterministic JSON translation and validation layer in the broader project. Integration with Simon's work is recorded in the source history. This snapshot is not the complete XOPT agent framework and does not claim that Jackson authored every component of the broader project.

Source: `JacksonLaVallee/sbvrp`, commit `357c64433275d87f6b5e7e0bda1fff8de5367996`. All Python source, example JSON, schemas and existing tests are preserved. The README now uses a portable Python setup command and includes this context. A bundled anonymous research manuscript is excluded from public distribution. No new license is asserted over collaborators' or third parties' work.

The original implementation notes below refer to the research manuscript *SchoolRide: A Platform for School Bus Disruption Management and Operational Resilience*. Its publication status is not established by this snapshot. The prototype uses explicit JSON scenarios and travel-time matrices; it does not include the full platform's operational datasets or telemetry integration.

## General optimization interface

`schoolride/benchmark_solver.py` supports structured LP, MILP, ILP and binary problems with OR-Tools. The Streamlit interface includes this workflow. See [the JSON schema](benchmark_problem.schema.json), [schema documentation](benchmark_problem.schema.md), and the small [LP](examples/benchmark_lp_simple.json) and [MILP](examples/benchmark_milp_simple.json) examples.

## Tests

After installing the requirements, run `python -m pytest -q` from this repository's root. The tests exercise routing behavior, timing boundaries, scenario edge cases and general optimization.

## Original implementation notes

This project reproduces the ICCPS SchoolRide disruption-management formulation in Python:
- Problem state reconstruction after disruptions.
- Constraints `C1..C6` from Section 4.
- Objective terms `O1..O4` and weighted objective `J`.
- Google OR-Tools rerouting with optional request-bus pruning.

## Install

The rerouting solver now depends on Google OR-Tools:

```bash
python -m pip install ortools
```

For a clean local setup on Windows, use the repo-local virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Quick Start

Run the example with a bus breakdown event:

```bash
python -m schoolride --scenario examples/scenario_small.json --config examples/config_default.json --event examples/event_breakdown.json --show-baseline
```

Run a road-closure event:

```bash
python -m schoolride --scenario examples/scenario_small.json --config examples/config_default.json --event examples/event_road_closure.json
```

## GUI

Launch the interactive dashboard:

```bash
python -m streamlit run schoolride/gui.py
```

What you can edit in the GUI:
- Objective weights (`omega_*`)
- Constraint switches (`C1..C6`) and numeric thresholds
- Solver settings (pruning + candidate limits)
- Live bus state (`active`, `eta`, `loc`, `remaining`, `passed`, `onboard`)
- Disruption event (`removed_buses`, disrupted arcs, travel-time updates)

Then run baseline vs rerouted evaluation and export results as JSON.

Default timing policy:
- Students can remain on the bus for at most 60 minutes.
- Late pickup arrivals contribute to `Twait`, which is the main temporal pressure before boarding.
- Students must arrive at school by 08:00, even if a scenario's scheduled school stop is later.

## Runnable Edge Scenarios

Use the default config with the no-op event file:

```bash
python -m schoolride --scenario examples/<scenario_name>.json --config examples/config_default.json --event examples/event_none.json
```

Time-constraint scenarios:
- `scenario_edge_time_exact_60min.json`
- `scenario_edge_time_over_60min.json`
- `scenario_edge_time_late_pickup_wait.json`
- `scenario_edge_time_multi_pickup_chain.json`
- `scenario_edge_time_existing_onboard_over_60min.json`

Start-time scenarios:
- `scenario_edge_start_exact_0800.json`
- `scenario_edge_start_after_0800.json`
- `scenario_edge_start_early_arrival.json`

## Flexible Interface For Constraints and State

Use `--set` to modify parameters at runtime:

```bash
python -m schoolride \
  --scenario examples/scenario_small.json \
  --config examples/config_default.json \
  --event examples/event_breakdown.json \
  --set constraints.max_ride_time=60 \
  --set constraints.early_arrival_buffer=2 \
  --set objective.route_change=0.4 \
  --set state.B3.active=true
```

Disable a paper constraint by code:

```bash
python -m schoolride --scenario examples/scenario_small.json --config examples/config_default.json --event examples/event_breakdown.json --disable C4
```

Supported override paths:
- `objective.wait`, `objective.school_delay`, `objective.route_change`, `objective.ride_violation`
- `constraints.*` (all fields in `ConstraintSettings`)
- `solver.*` (all fields in `SolverSettings`)
- `state.<bus_id>.eta`, `state.<bus_id>.loc`, `state.<bus_id>.active`
- `state.<bus_id>.remaining` and `state.<bus_id>.passed` as comma-separated lists
- `state.<bus_id>.onboard.<school_id>=<count>`

## Formulation Mapping

- `R_pre(k)`: `BusPlan.route_pre`
- `R_pass(k), R_rem(k), ETA(k), loc(k), Cap(k)`: `BusState`
- `Trav'`: scenario travel-time matrix after event updates
- `V*`: active buses not removed by disruption
- `C1..C6`: evaluated in `schoolride/evaluator.py`
- `Twait, Tsch, Croute, Cride`: evaluated in `schoolride/evaluator.py`
- `J`: weighted sum in `evaluate_solution`
- OR-Tools route selection and pruning: `schoolride/solver.py`

## Notes

- This reproduction focuses on the paper's optimization structure and configurable interface.
- The disruption solver is implemented with Google OR-Tools while preserving the existing evaluator and CLI/GUI outputs.
- OSRM data and live telemetry integration from the full platform architecture are represented here by a configurable travel-time matrix and live state JSON.
