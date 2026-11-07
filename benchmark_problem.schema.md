# Benchmark Problem Schema Guide

This document explains how to produce JSON that the benchmark LP/MILP parser accepts in `schoolride/benchmark_solver.py`.

Use this file together with `benchmark_problem.schema.json`:

- The schema is the machine-readable contract.
- This guide explains defaults, alias behavior, and the small number of semantic rules that plain JSON Schema cannot express exactly.

## Canonical Recommendation

Other systems should emit the canonical form below unless they have a strong reason not to:

```json
{
  "name": "My Problem",
  "problem_type": "LP",
  "objective": {
    "sense": "max",
    "coefficients": {
      "x": 3,
      "y": 5
    }
  },
  "variables": {
    "x": {
      "type": "continuous",
      "lb": 0
    },
    "y": {
      "type": "continuous",
      "lb": 0
    }
  },
  "constraints": [
    {
      "name": "cap",
      "coefficients": {
        "x": 1,
        "y": 1
      },
      "sense": "<=",
      "rhs": 10
    }
  ]
}
```

If a system validates against the schema and emits the canonical form above, the parser will accept it.

## Top-Level Fields

- `name`: optional string. Default: `"Benchmark Problem"`.
- `problem_type`: optional string.
- `type`: optional alias for `problem_type`.
- `variables`: required. Either an object keyed by variable name or an array of variable objects.
- `objective`: required object.
- `constraints`: required non-empty array.

## Problem Type Values

Canonical values:

- `LP`
- `MILP`
- `ILP`
- `BINARY`

Accepted aliases:

- `linear program` -> `LP`
- `MIP` or `mixed integer linear program` -> `MILP`
- `integer linear program` -> `ILP`
- `binary ILP` or `BILP` -> `BINARY`

Parser behavior:

- Matching is case-insensitive.
- Leading and trailing whitespace is ignored.
- For multi-word aliases, a single space, underscore, or hyphen works.
- If both `problem_type` and top-level `type` are present, `problem_type` wins.
- If neither `problem_type` nor `type` is present, the parser infers the type from the variable kinds.

Inference rules:

- all continuous -> `LP`
- all binary -> `BINARY`
- all integer-or-binary -> `ILP`
- any mix including continuous and integer/binary -> `MILP`

## Variables

Two encodings are accepted.

Object form:

```json
{
  "variables": {
    "x": { "type": "continuous", "lb": 0 },
    "y": { "type": "integer", "lb": 0, "ub": 4 }
  }
}
```

Array form:

```json
{
  "variables": [
    { "name": "x", "type": "continuous", "lb": 0 },
    { "name": "y", "type": "integer", "lb": 0, "ub": 4 }
  ]
}
```

Variable fields:

- `name`: required only in array form.
- `type`: preferred variable-kind field.
- `kind`: alias for `type`.
- `lb`: preferred lower bound field.
- `lower_bound`: alias for `lb`.
- `ub`: preferred upper bound field.
- `upper_bound`: alias for `ub`.

Variable kind aliases:

- continuous: `continuous`, `cont`, `real`, `float`
- integer: `integer`, `int`
- binary: `binary`, `bool`, `boolean`

Variable defaults and precedence:

- default variable kind is `continuous`
- default lower bound is `0`
- default upper bound is unbounded
- if both `type` and `kind` are present, `type` wins
- if both `lb` and `lower_bound` are present, `lb` wins
- if both `ub` and `upper_bound` are present, `ub` wins
- in object form, the outer property key is the real variable name; any inner `name` field is ignored

Bounds by variable kind:

- continuous:
  - `lb` must be a number if provided
  - `ub` may be a number or `null`
- integer:
  - `lb` must be an integer if provided
  - `ub` may be an integer or `null`
- binary:
  - effective bounds must stay within `0..1`
  - omitted bounds default to `0` and `1`

## Objective

Shape:

```json
{
  "objective": {
    "sense": "max",
    "coefficients": {
      "x": 3,
      "y": 5
    }
  }
}
```

Fields:

- `sense`: optional. Default: `max`.
- `coefficients`: optional sparse map from variable name to numeric coefficient. Default: `{}`.

Objective sense aliases:

- maximize: `max`, `maximize`
- minimize: `min`, `minimize`

## Constraints

Shape:

```json
{
  "constraints": [
    {
      "name": "cap",
      "coefficients": {
        "x": 1,
        "y": 2
      },
      "sense": "<=",
      "rhs": 18
    }
  ]
}
```

Fields:

- `name`: optional string. If omitted, runtime names become `c1`, `c2`, and so on.
- `coefficients`: optional sparse map from variable name to numeric coefficient. Default: `{}`.
- `sense`: optional. Default: `<=`.
- `rhs`: required number.

Constraint sense aliases:

- less-than-or-equal: `<=`, `le`, `leq`
- greater-than-or-equal: `>=`, `ge`, `geq`
- equality: `=`, `==`, `eq`

## Semantic Rules Beyond JSON Schema

These rules are enforced by the parser, but generic JSON Schema validators cannot express them completely:

- Every coefficient-map key must match a declared variable name exactly.
- In array-form `variables`, variable names must be unique.
- `LP` requires all variables to be continuous.
- `ILP` cannot contain continuous variables.
- `BINARY` requires all variables to be binary.
- For binary variables, the effective lower bound cannot exceed the effective upper bound.
- For all variables, the effective upper bound cannot be less than the effective lower bound.

## Handoff Checklist

Tell external systems to do all of the following:

1. Validate against `benchmark_problem.schema.json`.
2. Emit the canonical field names:
   top-level `problem_type`, variable `type`, `lb`, `ub`, canonical `sense` values, and object-form `variables`.
3. Make sure every coefficient key is the name of a declared variable.
4. Use only real JSON numbers for numeric fields.
   Numeric strings like `"3"` are not accepted by the parser.
5. Keep variable names unique.

## Safe Producer Rules

If you want the safest possible contract for external systems, ask them to always:

- include `problem_type`
- use object-form `variables`
- use canonical problem types: `LP`, `MILP`, `ILP`, `BINARY`
- use canonical variable types: `continuous`, `integer`, `binary`
- use canonical objective senses: `max`, `min`
- use canonical constraint senses: `<=`, `>=`, `=`
- avoid sending both alias fields at once
- avoid relying on defaults when the value matters

Following those rules will produce JSON that is valid for both the schema and the current parser implementation.
