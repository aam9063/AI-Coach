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
- [x] `ZON-3`: RED+GREEN: FTP resolution with the three configurable sources (manual, CP-derived, 95% of best 20-min) and an output that always states which source is in use (§7.3).
- [x] `ZON-4`: RED+GREEN: power zone table tests asserting exact Coggan percentage boundaries (Z1 < 55 … Z7 > 150) (§7.3, §12.4).
- [x] `ZON-5`: RED+GREEN: run CS/D' fit on synthetic data with known parameters (best efforts ~3–20 min) (§7.3, §12.4).
- [x] `ZON-6`: RED+GREEN: VDOT formulas tested against hand-computed reference values (VO2, %VO2max, VDOT) and training-pace derivation (easy, marathon, threshold, interval, repetition) (§7.3).
- [x] `ZON-7`: RED+GREEN: CSS formula tested on hand-computed 400/200 time-trial examples, with pace per 100 m output and swim zones relative to CSS (§7.3).
- [x] `ZON-8`: RED+GREEN: heart-rate zone tables per sport asserted against the published Friel percentages for run and bike (§7.3, §12.4).
- [x] `ZON-9`: RED+GREEN: threshold change detection — new effort exceeding the model by a configurable margin produces a *proposal* (not an applied update); proposal object carries evidence and prior value (§7.3).
- [x] `ZON-10`: Persist thresholds and their history to `athlete_profile` with `engine_version` (§6); wire athlete confirmation to record the accepted update (full WhatsApp interaction lands in Feature 6).
- [x] `ZON-11`: Document chosen models, configurable margins and default constants in module docstrings and config with source references (§7, §14).

## Acceptance criteria

From PROJECT_BRIEF.md §12.4:

- Fits validated on synthetic data with known parameters; zone tables match the published percentages.

Directly implied conditions:

- All zone/threshold functions are pure, typed and docstring-documented with formula and reference (§7, §14).
- Threshold updates are proposed for confirmation, never applied silently, and their history is stored (§7.3).

## Verification evidence

Independently verified by `gentle-ai-verify` (read-only) at HEAD `2393c00`, 2026-10-07 — **11/11 items PASS, no blockers**, dev database provably untouched (26 activities / 131 streams / 177 wellness / 800 daily_load before and after, `alembic current` unchanged at `c4d5e6f7a8b9`).

1. **Quality gates** — ruff clean; mypy strict clean (102 source files); `uv run pytest` **703 passed, 0 skipped**, with the DB tests genuinely running against `tri_coach_test` (verified live via `current_database()`), and `assert_is_test_database` guarding schema-mutating infrastructure.
2. **Engine purity** — every import in `app/engine/{zones,load,pmc,banister}.py` is stdlib, numpy or scipy; no `app.db`/`app.ingest`/`app.core`, no I/O library, no settings read; the purity test discovers modules dynamically via `pkgutil`; persistence and the flow live in `app/services/` and `app/db/`.
3. **Checklist** — all eleven items mapped to code and to passing tests (engine suite 420 tests).
4. **§12.4 synthetic fits, reproduced independently by the verifier** (not via the repo's tests): `Work = 250·t + 18000` over ten durations → CP **250.0** and W' **18 000.0** with relative error `0.0`, R² 1.0; with ±2 W seeded noise → CP 249.25 / W' 17 989.7 / R² 0.99997. `v = 4.0 + 200/t` → CS **4.0 m/s**, D' **200 m**, RMSE 4.3e-14.
5. **§12.4 published tables** — Coggan, Friel-run and Friel-bike all honour every printed boundary (no printed boundary dishonoured), the continuous-partition rule for the printed gaps is documented and tested, and `power_zones(180)` bounds equal `pct × 1.8` exactly.
6. **VDOT** — the verifier's own Daniels–Gilbert arithmetic for 5 km in 20:00 gives VDOT **49.806233428066335**, identical to the implementation (absolute difference `0.0`), with T pace 4:21/km at VDOT 50.
7. **Proposals never apply** — no apply/commit/persist/save/write symbol in the engine; results frozen; status single-valued. The service flow was exercised on the test database: a `NoThresholdChange`, a runtime-crafted `status="applied"`, an unknown source key and a **stale** proposal (prior 180 vs profile 190) are all refused, and declining left the profile value and source unchanged while appending a `declined` history row with `new_value = NULL`.
8. **Migration hygiene** — single head `c4d5e6f7a8b9`, full chain verified, dev DB at head, both tables present, and the previous head `b2f8d4c6a9e1` proven **not** modified in place (its only "athlete" occurrences are comments; the schema change is the new revision with `down_revision = "b2f8d4c6a9e1"`).
9. **Configurable constants** — sixteen `engine_*` fields, each with a literature reference or an owner-choice marker, mirrored in both `.env.example` files. Owner-reviewable: swim boundaries, Daniels midpoints and the threshold-change margin. One documented residual: the speed ladder `DEFAULT_MMS_DURATIONS_S` is deliberately not mirrored into Settings yet (no consumer until Feature 6).
10. **Boundary precision** — 14 regression tests pass; the verifier's independent probes confirm a computed `0.95 × 169` now classifies as Z4 (was Z3) and `1.06 × 169` as Z5b (was Z5c), that values just inside each boundary are unaffected, and that the bike table behaves the same.
11. **Cross-document consistency** — no contradictions found; every anchor recorded in this document reproduces live.

Minor observations from the verifier, not defects and deliberately left as follow-ups: the purity test bans import *prefixes* rather than naming I/O libraries (the actual imports were verified clean by inspection), one internal `assert` sits in the service happy path, and the speed ladder awaits a consumer before being mirrored into Settings.

## Progress

In progress (Feature 4/11) on branch `feat/engine-zones`, branched from `dev` (which already carries Features 1-3, verified and merged).

Owner decisions taken before starting (§15 and sequencing): the WhatsApp agent (Feature 6) uses **OpenAI** as the LLM provider, **voice notes / STT are postponed** (WA-10 deferred), the webhook will be exposed through a **dev tunnel** rather than a deploy, and Features 4-5 are implemented **before** Feature 6 so the agent's `get_zones` / `get_readiness` tools are real instead of stubbed.

Data note: the owner has **no power meter**, so the mean-maximal power curve and the CP/W' fit are validated on synthetic data with known parameters (§12.4 requires exactly that); the athlete's FTP stays a manual value (180 W) and the resolution output must state that source. Run threshold pace is still missing in Intervals.icu, which blocks the CS/V-DOT pace side until it is filled in.

- ZON-1/2 done at `4edad90`: `app/engine/zones.py` with `mean_maximal_power_curve` (rolling sums, O(k·n), documented) and `fit_critical_power` (closed-form least squares on `Work = CP·t + W'` over the 2–20 min window, reporting CP/W'/R²/RMSE/n_points in `CriticalPowerFit`). Gap rule mirrors `load.py` (`valid_count/width ≥ min_valid_fraction`, default 1.0 = complete windows only; a duration with no qualifying window is absent, never zero). Parent-verified: a synthetic curve from CP 250 W / W' 18,000 J is recovered **exactly** (rel_err `0.00e+00`, R² 1.0, RMSE 0.0); hand-computed OLS on two exact points matches; MMP on a constant 200 W stream gives 200 for every duration and on a 300 s@250 W + 300 s@150 W step gives 250 (300 s) and 200 (600 s) as computed by hand; empty / all-`None` / single-point / zero-interval inputs raise `ValueError`. Real-data limitation: no power meter, so this is validated synthetically (§12.4) and FTP remains a manual value.

- ZON-3/4 done at `3d07db7`: `resolve_ftp` with the brief's three sources (manual / CP-derived / 95% of best 20-minute power) returning a typed `FtpResolution` that **always states the source in use**, a configurable precedence (default manual > CP-derived > 20-minute, documented as the human-in-the-loop choice for an owner whose FTP is the confirmed manual 180 W), an explicit CP-to-FTP factor (default 1.0, standard convention + owner choice) and a clear none-available error. `power_zones` builds the published Coggan table with absolute watt ranges and explicit bound-inclusivity, plus `power_zone_for` as the consistent classifier; the brief's fractional gaps (e.g. 55.5%) are resolved by one documented, tested rule (continuous partition at the printed boundaries: Z1 < 55, Z2 [55,76), ..., Z6 [121,150], Z7 > 150). Parent-verified: 95% of 265 W = **251.75**, precedence picks manual, and the FTP-180 W zone bounds match `pct x 1.8` exactly (99.0 / 136.8 / 163.8 / 190.8 / 217.8 / 270.0) with the classifier landing on the documented zone at every printed boundary.

- ZON-5/6 done at `3db7f95`: run Critical Speed reuses the bike design instead of duplicating it (the rolling-sum extraction became a shared `_best_mean_curve` and the closed-form OLS `_fit_linear_time_model`, so CP and CS share one implementation), fitting the 3–20 minute window and reporting CS, D', R², RMSE and the point count. VDOT implements the brief's Daniels & Gilbert formulas verbatim, inverts the quadratic for the positive root, and derives the five training paces from documented Daniels intensity bands (easy 59–74, marathon 75–84, threshold 83–88, interval 95–100, repetition 105–120 %VO2max; Daniels' Running Formula 3rd ed.) with the band midpoint as a documented, per-call overridable default. Parent-verified independent anchors, all exact: VDOT **49.806** (5 km in 20:00; the published table says ≈50), **51.944** (10 km in 40:00), **70.100** (1500 m in 4:00); threshold pace at VDOT 50 hand-derived as 229.6917 m/min = 3.8282 m/s = 261.22 s/km = **4:21/km**, matching Daniels' T pace; CS/D' recovered exactly (4.0 m/s, 200 m, R² 1.0); the speed curve's step case gives 4.0 (300 s) and 3.5 (600 s). Documented limitation: no run threshold pace configured, so this is validated synthetically.

- ZON-7/8 done at `8d1ab04`: `css_from_time_trials` implements `CSS = (400−200)/(T400−T200)` (Wakayoshi et al. 1992) with pace per 100 m and explicit validation. The swim zone table is reported as paces per 100 m with the direction documented (faster pace = higher intensity), and because the brief publishes no swim percentage table its provenance is **split explicitly**: only the 100% CSS = threshold anchor is literature, while the five-band structure and the 85/95/105 boundaries are a documented, per-call configurable OWNER choice — **the owner should confirm those three boundaries before ZON-11 turns them into Settings defaults**. The two Friel tables are implemented as separate per-sport tables (%LTHR) with absolute bpm bounds, a per-sport classifier and the same continuous-partition gap rule as the power zones; the run/bike difference at Z1/Z2/Z3 (85/89/94 vs 81/89/93) is asserted so a copy-paste mistake cannot pass. Parent-verified anchors: CSS **1.052632 m/s = 95 s/100 m** (6:00 / 2:50) and **0.833333 m/s = 120 s/100 m** (8:00 / 4:00), the second matching the owner's configured Intervals.icu value; bpm bounds at LTHR 169 match `pct × 1.69` (143.65 / 152.10 / 160.55 / 169.00 / 174.07 / 179.14, bike 136.89 and 158.86).
- **Parent correction at `8d1ab04`** (found by independent verification, not by the tests): the HR classifier contradicted its own table at two boundaries — 95% of LTHR classified as Z3 instead of Z4 and 106% as Z5c instead of Z5b. Cause: the table rounds its bpm bounds to two decimals while `contains_bpm` compared exactly, and `0.95 × 169 = 160.54999999999998` sits one ULP below the rounded literal `160.55` (while `1.06 × 169` sits one ULP above `179.14`). Boundary comparisons now go through `_same_boundary` (`math.isclose`, 1e-12 relative / 1e-9 absolute — far tighter than any real heart-rate difference), with 14 regression tests covering computed vs literal boundaries, the bike table and values just inside each zone. Lesson recorded: exact equality on a rounded bound is fragile when the caller may compute that bound himself.

- ZON-9/11 done at `022880e`: `propose_threshold_change` is a pure engine function that returns either a typed proposal or an explicit no-proposal outcome with a machine-readable reason (`margin_not_exceeded` / `no_improvement`). It **cannot apply anything by construction**: the module defines no apply/commit/persist/save/write symbol, the results are frozen dataclasses, and the proposal's `status` is the single-valued literal `proposal_not_applied` carrying the prior value, delta, margin and evidence. The margin comparison is strict — 189.00 W against 180 W is exactly 5% and does **not** propose, 189.01 does — and validated with `ValueError`. Sixteen `engine_*` settings fields now cover the CP and CS fit windows, the MMP ladder, the FTP precedence and both factors (1.0 convention, 0.95 Allen & Coggan), the five Daniels pace midpoints, the swim boundaries and the threshold-change margin, each with a literature reference or an explicit owner-choice marker, documented in both `.env.example` files; `app/db/zones_config.py` is the settings→engine mapping helper and states plainly that **no caller exists yet** (Feature 6 consumes it). Parent-verified: the margin boundary on both sides, immutability, the single-valued status, the no-improvement and validation paths, and the complete settings mapping.

- ZON-10 done at `96cd6ed`: `athlete_profile` stores the current thresholds with per-metric provenance (the engine's source keys) and `engine_version`; `athlete_threshold_history` is the append-only log of proposals and decisions carrying the prior value, the proposed value, the applied value (NULL when declined), the evidence, the confirming actor and `engine_version`. The thin guarded flow lives in `app/services/athlete_profile.py` (the engine stays pure) and the acceptance path **refuses** anything that is not an explicit proposal (a `NoThresholdChange` or a proposal whose status is not `proposal_not_applied`), refuses an unknown source key, and refuses a **stale** proposal whose prior value no longer matches the profile — which also makes silent re-recording impossible. Declining appends a history row and leaves the profile untouched, so applying a threshold stays human-in-the-loop (§7.3). The WhatsApp interaction is a documented seam for Feature 6.
- **Parent correction at `96cd6ed`** (migration hygiene, worth remembering): the first attempt created those tables by editing the **already-applied** revision `b2f8d4c6a9e1` in place, so any environment stamped with that revision — the local development database included — would report "up to date" while silently missing the tables. Migrations are immutable once applied: the tables now live in a new revision `c4d5e6f7a8b9` (revises `b2f8d4c6a9e1`), the previous head was restored byte-identical to its commit, and a plain `alembic upgrade head` created both tables in the dev database while leaving its 26 activities / 131 streams / 177 wellness / 800 daily_load rows intact.

Commits: 4edad90 (ZON-1/2), 3d07db7 (ZON-3/4), 3db7f95 (ZON-5/6), 8d1ab04 (ZON-7/8), 022880e (ZON-9/11), 96cd6ed (ZON-10)
