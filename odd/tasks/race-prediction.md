# ODD Feature: race-prediction

Status: pending | Feature 8 of 11 (brief section 12) | Source: PROJECT_BRIEF.md

## Objective

Implement the race prediction module (PROJECT_BRIEF.md §7.7): run predictions via Riegel and VDOT, bike segment times via the physics power model, swim pace from CSS, the run-off-the-bike slowdown factor, transitions, and the composite triathlon total with Monte Carlo uncertainty. Exposed to the agent through the `predict_race` tool. Every prediction returns the full structured envelope — never a bare number.

## Problem

The owner wants forecasts for sprint, olympic, 70.3 and full distance races with uncertainty ranges (PROJECT_BRIEF.md §1). Guessing race times is exactly the "generic chatbot invents numbers" failure (§2); instead, each segment uses a published, unit-tested model with explicitly stated assumptions, and uncertainty is propagated so the athlete sees a realistic range, not false precision (§7.7).

## Scope

- `backend/app/engine/predict.py` (§6 layout), pure, typed, docstring-documented functions (§7.7):
  - Run: Riegel (1981) T2 = T1 × (D2/D1)^1.06, plus VDOT-based equivalent (reusing Feature 4's VDOT functions); show both and their spread.
  - Bike: physics model (Martin et al. 1998) P × η = [0.5 × ρ × CdA × v_air² × v] + [Crr × m × g × cos(θ) × v] + [m × g × sin(θ) × v]; solve for v per course segment given a target power (fraction of CP/FTP chosen per distance); inputs CdA, Crr, total mass, air density (from temperature and altitude), drivetrain efficiency η (default 0.976), course gradient profile.
  - Swim: pace from CSS with a distance-dependent fraction and an open-water adjustment factor (configurable, documented as an assumption).
  - Run off the bike: personal slowdown factor learned from bricks and past races; until enough data exists, use a configurable prior and widen the uncertainty band.
  - Transitions: T1/T2 from past races or configurable defaults.
  - Triathlon total: sum of segments; uncertainty propagated (Monte Carlo over input distributions acceptable and preferred).
- Prediction envelope (§7.7): all predictions return `{point, lower, upper, method, assumptions, model_version}` — never a bare number.
- `race` and `prediction` DB models (§6 data model) with course data (elevation profile, expected temperature), goal, and per-segment predicted times with lower/upper bounds and model version.
- `predict_race(race_id | distance, target_date?)` tool (§9.3) returning per-segment and total prediction with bounds and assumptions as a typed Pydantic model with `computed_at`, `engine_version`, coverage info.
- Course data (elevation profile, expected temperature) originates from the `race` table seeded by the owner (§6, §15 open decision on target races).

## Constraints

- **Every prediction returns `{point, lower, upper, method, assumptions, model_version}` — never a bare number** (§7.7, §12.8 acceptance).
- Engine functions: pure, no I/O, no DB, no network, fully typed, docstring with formula and reference (§7, §14).
- Run predictions must match Riegel/VDOT reference calculations (§12.8); bike solver must match a hand-solved flat-course example (§12.8).
- The run-off-the-bike factor must not hardcode an unsourced constant: configurable prior, widened uncertainty until personal data exists (§7.7).
- Open-water swim adjustment is configurable and documented as an assumption (§7.7).
- Banister-style personalization discipline applies: assumptions must be explicit; Monte Carlo input distributions are documented assumptions (§7.7).
- Configurable constants (η default 0.976, distance-dependent power fractions, priors) live in config with source comments or owner-choice markers (§14).
- Depends on Features 1 (`project-scaffold`), 4 (`engine-zones`, for CP/FTP and VDOT inputs) and 3 (`engine-load`, for engine conventions); the `predict_race` tool is registered by Feature 6; the prediction-breakdown chart is Feature 7's consumer.

## Checklist

- [ ] `RP-1`: RED: unit tests for Riegel T2 = T1 × (D2/D1)^1.06 and the VDOT-equivalent against published/hand-calculated reference values, asserting the spread between both methods is returned (§7.7, §12.8).
- [ ] `RP-2`: GREEN: implement run prediction functions returning both Riegel and VDOT results with their spread (§7.7); tests green.
- [ ] `RP-3`: RED: write a hand-solved flat-course bike example (fixed power, CdA, Crr, mass, ρ, η) with the analytic expected v and test that the numeric solver matches it (§12.8).
- [ ] `RP-4`: GREEN: implement the Martin et al. 1998 bike physics solver for v per course segment with gradient profile, target power as a fraction of CP/FTP per distance, and air density from temperature and altitude (§7.7); tests green.
- [ ] `RP-5`: RED+GREEN: swim prediction from CSS with distance-dependent fraction and configurable open-water adjustment documented as an assumption (§7.7).
- [ ] `RP-6`: RED+GREEN: run-off-the-bike slowdown: personal factor from bricks/past races when available; configurable prior + widened uncertainty band otherwise; test that the insufficient-data path widens the band and never hardcodes an unsourced constant (§7.7).
- [ ] `RP-7`: RED+GREEN: T1/T2 transitions from past races or configurable defaults (§7.7).
- [ ] `RP-8`: RED+GREEN: composite triathlon total with Monte Carlo uncertainty propagation over documented input distributions; per-segment and total bounds stable under a fixed seed (§7.7).
- [ ] `RP-9`: RED+GREEN: prediction envelope contract test — every prediction (segment and total) returns exactly `{point, lower, upper, method, assumptions, model_version}` and lower ≤ point ≤ upper (§7.7, §12.8).
- [ ] `RP-10`: Add `race` and `prediction` DB models with course data, goal, bounds and model version (§6); implement the `predict_race(race_id | distance, target_date?)` tool returning typed Pydantic output with `computed_at`, `engine_version`, coverage info (§9.3).

## Acceptance criteria

From PROJECT_BRIEF.md §12.8:

- Run predictions match Riegel/VDOT reference calculations; bike solver matches a hand-solved flat-course example; every prediction exposes bounds and assumptions.

Directly implied conditions:

- All prediction functions are pure, typed and docstring-documented with formula and reference (§7, §14).
- No unsourced hardcoded constants: configurable priors and defaults with cited sources or owner-choice markers (§7.7, §14).

## Verification evidence

To be filled when the feature is implemented (commits, test runs, cross-checks).

## Progress

Not started.

Commits: (none yet)
