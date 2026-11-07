# Portfolio snapshot validation

Checked November 7, 2026 on Windows with Python 3.12.14 in a fresh virtual environment, using this repository's `requirements.txt`.

Command: `python -m pytest -q`

Result: **31 passed**, three SWIG dependency deprecation warnings. The existing tests cover the routing solver, timing constraints, edge-case scenarios and general LP/MILP solver. No application source, JSON examples, schemas or tests were changed for this publication.

Selected installed versions: pytest 9.2.0, OR-Tools 9.15.6755, Streamlit 1.67.0, NumPy 2.5.4, pandas 3.1.1.

Passing these tests supports the particular cases exercised; it does not establish correctness for arbitrary formulations or validate the complete XOPT agent framework. This repository contains the experimental testbed described in the portfolio contribution statement.

The public source was copied from the original repository's tracked files. The bundled anonymous SchoolRide manuscript was excluded. A scan for common credential formats and literal secrets found no matches. No Git history, credentials, private operational datasets or unrelated research code is included. The README preserves the collaborative research context; this snapshot adds no new license.
