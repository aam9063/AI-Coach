# ODD Feature: engine-zones

Status: pending | Feature 4 of 11 (brief section 12) | Source: PROJECT_BRIEF.md

## Objective

Implement the thresholds and training-zones module of the science engine (PROJECT_BRIEF.md §7.3): Critical Power (CP/W') and power zones, run Critical Speed (CS/D') and VDOT-based paces, swim CSS and swim zones, heart-rate zone tables per sport, and threshold change detection with athlete confirmation. Pure functions in `backend/app/engine/zones.py` that later back the `get_zones` tool and threshold-proposal flows.

## Problem

Zones and thresholds are the reference points every other metric depends on: load intensification factors (§7.1), intensity distribution boundaries (§7.5), durability (§7.6) and prediction target powers/paces (§7.7). The brief requires these to be derived from the athlete's own efforts with published, validated models rather than guessed, and kept current with a human-in-the-loop confirmation step (§7.3).

## Scope

- `backend/app/engine/zones.py` (§6 layout), pure, typed, docstring-documented functions covering §7.3:
  - Bike Critical Power (Monod & Scherrer 1965; Jones et al. 2019): from the mean-maximal power curve, best efforts between 2 and 20 minutes; fit Work = CP × t + W' (linear form); report CP, W' and fit error.
  - FTP: configurable as manual value, CP-derived estimate, or 95% of best 20-min power; always state which one is in use.
  - Power zones (Coggan, % FTP): Z1 < 55, Z2 56–75, Z3 76–90, Z4 91–105, Z5 106–120, Z6 121–150, Z7 > 150.
  - Run Critical Speed: fit Distance = CS × t + D' from best efforts between roughly 3 and 20 minutes.
  - VDOT (Daniels & Gilbert): VO2 = −4.60 + 0.182258 × v + 0.000104 × v² (v in m/min); %VO2max = 0.8 + 0.1894393 × e^(−0.012778 × t) + 0.2989558 × e^(−0.1932605 × t) (t in min); VDOT = VO2 / %VO2max; training paces derived from VDOT tables (easy, marathon, threshold, interval, repetition).
  - Swim Critical Swim Speed (Wakayoshi et al. 1992): CSS (m/s) = (400 − 200) / (T400 − T200), from time trials logged via WhatsApp or detected in pool sessions; output pace per 100 m and swim zones relative to CSS.
  - Heart-rate zones (Friel, % LTHR), separate tables per sport — Run: Z1 < 85, Z2 85–89, Z3 90–94, Z4 95–99, Z5a 100–102, Z5b 103–106, Z5c > 106; Bike: Z1 < 81, Z2 81–89, Z3 90–93, Z4 94–99, Z5a 100–102, Z5b 103–106, Z5c > 106.
- Mean-maximal power curve computation from stored streams (Feature 2) — pure derivation, exposed for reuse by charts (§10) and prediction (§7.7).
- Threshold change detection (§7.3): when a new effort exceeds the model by a configurable margin, propose (not apply) an update; athlete confirms via WhatsApp (proposal flow lands with the agent in Feature 6); store history.
- Persistence of thresholds and their history to `athlete_profile` (§6 data model) with `engine_version` on outputs — thin DB integration outside the pure module.

## Constraints

- Engine functions: pure, no I/O, no DB, no network, fully typed, docstring with formula and reference (§7, §14).
- Fits must be validated on synthetic data with known parameters (§12.4 acceptance) before trusting real data.
- Zone tables must match the published percentages exactly (§12.4 acceptance): Coggan power % FTP and Friel % LTHR tables as listed in §7.3.
- FTP source must always be stated (manual / CP-derived / 95% of 20-min power) (§7.3).
- Threshold changes are proposed, never auto-applied; the athlete confirms; history is stored (§7.3).
- Configurable margins and defaults live in config with source comments or owner-choice markers (§14).
- Depends on Feature 1 (`project-scaffold`) and Feature 3 (`engine-load`, for shared engine conventions and threshold-speed inputs like run threshold pace); consumes stream data from Feature 2 for mean-maximal curves and best efforts.

## Checklist

- [ ] `ZON-1`: RED: unit tests for the linear 2-parameter CP/W' fit on synthetic mean-maximal power data generated with known CP and W' parameters (§12.4), asserting recovered parameters and fit error within tolerance.
- [ ] `ZON-2`: GREEN: implement mean-maximal power curve extraction and CP/W' fitting (best efforts 2–20 min, Work = CP × t + W') in `backend/app/engine/zones.py`; tests green.
- [ ] `ZON-3`: RED+GREEN: FTP resolution with the three configurable sources (manual, CP-derived, 95% of best 20-min) and an output that always states which source is in use (§7.3).
- [ ] `ZON-4`: RED+GREEN: power zone table tests asserting exact Coggan percentage boundaries (Z1 < 55 … Z7 > 150) (§7.3, §12.4).
- [ ] `ZON-5`: RED+GREEN: run CS/D' fit on synthetic data with known parameters (best efforts ~3–20 min) (§7.3, §12.4).
- [ ] `ZON-6`: RED+GREEN: VDOT formulas tested against hand-computed reference values (VO2, %VO2max, VDOT) and training-pace derivation (easy, marathon, threshold, interval, repetition) (§7.3).
- [ ] `ZON-7`: RED+GREEN: CSS formula tested on hand-computed 400/200 time-trial examples, with pace per 100 m output and swim zones relative to CSS (§7.3).
- [ ] `ZON-8`: RED+GREEN: heart-rate zone tables per sport asserted against the published Friel percentages for run and bike (§7.3, §12.4).
- [ ] `ZON-9`: RED+GREEN: threshold change detection — new effort exceeding the model by a configurable margin produces a *proposal* (not an applied update); proposal object carries evidence and prior value (§7.3).
- [ ] `ZON-10`: Persist thresholds and their history to `athlete_profile` with `engine_version` (§6); wire athlete confirmation to record the accepted update (full WhatsApp interaction lands in Feature 6).
- [ ] `ZON-11`: Document chosen models, configurable margins and default constants in module docstrings and config with source references (§7, §14).

## Acceptance criteria

From PROJECT_BRIEF.md §12.4:

- Fits validated on synthetic data with known parameters; zone tables match the published percentages.

Directly implied conditions:

- All zone/threshold functions are pure, typed and docstring-documented with formula and reference (§7, §14).
- Threshold updates are proposed for confirmation, never applied silently, and their history is stored (§7.3).

## Verification evidence

To be filled when the feature is implemented (commits, test runs, cross-checks).

## Progress

Not started.

Commits: (none yet)
