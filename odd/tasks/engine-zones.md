# ODD Feature: engine-zones

Status: in progress | Feature 4 of 11 (brief section 12) | Source: PROJECT_BRIEF.md

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

- [x] `ZON-1`: RED: unit tests for the linear 2-parameter CP/W' fit on synthetic mean-maximal power data generated with known CP and W' parameters (§12.4), asserting recovered parameters and fit error within tolerance.
- [x] `ZON-2`: GREEN: implement mean-maximal power curve extraction and CP/W' fitting (best efforts 2–20 min, Work = CP × t + W') in `backend/app/engine/zones.py`; tests green.
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

In progress (Feature 4/11) on branch `feat/engine-zones`, branched from `dev` (which already carries Features 1-3, verified and merged).

Owner decisions taken before starting (§15 and sequencing): the WhatsApp agent (Feature 6) uses **OpenAI** as the LLM provider, **voice notes / STT are postponed** (WA-10 deferred), the webhook will be exposed through a **dev tunnel** rather than a deploy, and Features 4-5 are implemented **before** Feature 6 so the agent's `get_zones` / `get_readiness` tools are real instead of stubbed.

Data note: the owner has **no power meter**, so the mean-maximal power curve and the CP/W' fit are validated on synthetic data with known parameters (§12.4 requires exactly that); the athlete's FTP stays a manual value (180 W) and the resolution output must state that source. Run threshold pace is still missing in Intervals.icu, which blocks the CS/V-DOT pace side until it is filled in.

- ZON-1/2 done at `4edad90`: `app/engine/zones.py` with `mean_maximal_power_curve` (rolling sums, O(k·n), documented) and `fit_critical_power` (closed-form least squares on `Work = CP·t + W'` over the 2–20 min window, reporting CP/W'/R²/RMSE/n_points in `CriticalPowerFit`). Gap rule mirrors `load.py` (`valid_count/width ≥ min_valid_fraction`, default 1.0 = complete windows only; a duration with no qualifying window is absent, never zero). Parent-verified: a synthetic curve from CP 250 W / W' 18,000 J is recovered **exactly** (rel_err `0.00e+00`, R² 1.0, RMSE 0.0); hand-computed OLS on two exact points matches; MMP on a constant 200 W stream gives 200 for every duration and on a 300 s@250 W + 300 s@150 W step gives 250 (300 s) and 200 (600 s) as computed by hand; empty / all-`None` / single-point / zero-interval inputs raise `ValueError`. Real-data limitation: no power meter, so this is validated synthetically (§12.4) and FTP remains a manual value.

- ZON-3/4 done at `3d07db7`: `resolve_ftp` with the brief's three sources (manual / CP-derived / 95% of best 20-minute power) returning a typed `FtpResolution` that **always states the source in use**, a configurable precedence (default manual > CP-derived > 20-minute, documented as the human-in-the-loop choice for an owner whose FTP is the confirmed manual 180 W), an explicit CP-to-FTP factor (default 1.0, standard convention + owner choice) and a clear none-available error. `power_zones` builds the published Coggan table with absolute watt ranges and explicit bound-inclusivity, plus `power_zone_for` as the consistent classifier; the brief's fractional gaps (e.g. 55.5%) are resolved by one documented, tested rule (continuous partition at the printed boundaries: Z1 < 55, Z2 [55,76), ..., Z6 [121,150], Z7 > 150). Parent-verified: 95% of 265 W = **251.75**, precedence picks manual, and the FTP-180 W zone bounds match `pct x 1.8` exactly (99.0 / 136.8 / 163.8 / 190.8 / 217.8 / 270.0) with the classifier landing on the documented zone at every printed boundary.

Commits: 4edad90 (ZON-1/2), 3d07db7 (ZON-3/4)
