# ODD Feature: engine-load

Status: in progress | Feature 3 of 11 (brief section 12) | Source: PROJECT_BRIEF.md

## Objective

Implement the training-load core of the deterministic science engine (PROJECT_BRIEF.md §7.1 and §7.2): TSS variants for bike, run and swim, HR-based TRIMP fallback, strength sRPE, the method selection rule, and the Performance Manager model (CTL/ATL/TSB), plus ACWR (EWMA) and the Banister impulse-response fit. These are pure functions in `backend/app/engine/load.py` that later feed `daily_load` persistence, the agent's `get_load_status` tool, charts and predictions.

## Problem

The brief's core principle is "deterministic engine decides, LLM explains" (PROJECT_BRIEF.md §3): the LLM never calculates a performance number itself. Generic chatbots invent numbers (§2); this engine makes every load number computed, typed and unit-tested against reference values. Load is the foundation for form (TSB), readiness warnings (§7.4), intensity distribution (§7.5) and race prediction inputs.

## Scope

- `backend/app/engine/load.py` (§6 layout), pure, typed functions covering §7.1:
  - Bike power-based TSS (Coggan): NP = (mean(rolling_30s_mean(power)^4))^(1/4); IF = NP / FTP; TSS = (duration_s × NP × IF) / (FTP × 3600) × 100.
  - Run rTSS (pace-based): normalized graded pace (grade-adjusted speed) vs threshold run speed; IF = NGS / threshold_speed; rTSS = duration_h × IF² × 100; documented grade adjustment model (e.g. Minetti et al. 2002).
  - Swim sTSS: IF = normalized swim speed / CSS speed; sTSS = duration_h × IF³ × 100.
  - HR-based TRIMP fallback (Banister): ΔHRr = (HRavg − HRrest) / (HRmax − HRrest); TRIMP = duration_min × ΔHRr × 0.64 × e^(1.92 × ΔHRr) (male coefficients; female 0.86 and 1.67; configurable); hrTSS = TRIMP / TRIMP_of_one_hour_at_LTHR × 100.
  - Strength: session RPE method (Foster et al. 2001): load = RPE (CR-10) × minutes, scaled to TSS-equivalent with a documented, configurable factor.
  - Selection rule: per activity choose best available method in order power → pace/speed → HR → sRPE; persist which method was used.
- Performance Manager (§7.2): CTL_t = CTL_{t-1} + (TSS_t − CTL_{t-1}) × (1 − e^(−1/42)); ATL with τ = 7; TSB_t = CTL_{t-1} − ATL_{t-1}; time constants configurable; computed on combined load and per sport; low-confidence flag before 90 days of history.
- Banister impulse-response model (§7.2): p(t) = p0 + k1 × Σ w(s) e^(−(t−s)/τ1) − k2 × Σ w(s) e^(−(t−s)/τ2); default τ1 = 42, τ2 = 7; fit k1, k2 (optionally τ) with `scipy.optimize` only with enough performance markers; report fit quality; never present an unfitted model as personalized.
- ACWR (EWMA version, Williams et al. 2017): acute 7-day / chronic 28-day EWMA ratio (§7.2).
- Persistence of engine outputs to `daily_load` (§6 data model) carrying `engine_version` — thin DB integration outside the pure module.
- Docstrings on every function listing the formula and reference (§7, §14).

## Constraints

- Engine functions: **pure, no I/O, no DB, no network, fully typed (mypy strict), docstring with formula and reference** (§7, §14).
- Defaults must be configurable; every assumption or default constant lives in config with a comment citing its source or marking it as an owner choice (§7, §14).
- Unit tests against hand-calculated or published reference values for each formula (§7, §12.3 acceptance).
- ACWR is displayed as context only and must **not** trigger warnings on its own (§7.2); warning logic belongs to Feature 5.
- Banister model must never be presented as personalized without a fitted, quality-reported fit (§7.2).
- Method selection order is fixed: power, pace/speed, HR, sRPE; the chosen method must be persisted (§7.1).
- Depends on Feature 1 (`project-scaffold`) for the engine package and tooling; consumes ingestion output structures (Feature 2) for stream inputs; math only — no HTTP calls to Intervals.icu.
- Cross-check tolerance vs Intervals.icu is agreed for the same data, cross-check only — Intervals values are never authoritative (§5.1, §12.3).

## Checklist

- [x] `LOAD-1`: RED: unit tests with hand-calculated reference values for bike NP/IF/TSS (§7.1), including the rolling 30 s fourth-power mean edge cases (missing samples, short files).
- [x] `LOAD-2`: GREEN: implement bike power-based TSS functions in `backend/app/engine/load.py` (pure, typed, docstrings with formula and Coggan reference); tests green.
- [x] `LOAD-3`: RED+GREEN: run rTSS with documented grade-adjustment model (e.g. Minetti et al. 2002) tested on hand-computed examples (§7.1).
- [x] `LOAD-4`: RED+GREEN: swim sTSS tested against hand-computed values with configurable CSS input (§7.1).
- [x] `LOAD-5`: RED+GREEN: TRIMP/hrTSS with configurable male/female coefficients tested on hand-computed examples, plus strength sRPE with configurable TSS-equivalent scaling factor (§7.1).
- [x] `LOAD-6`: RED+GREEN: method selection rule (power → pace/speed → HR → sRPE) with the chosen method returned/persisted (§7.1).
- [x] `LOAD-7`: RED+GREEN: CTL/ATL/TSB recursion tests (seed values, time-constant configurability, combined and per-sport, low-confidence flag before 90 days of history) (§7.2).
- [x] `LOAD-8`: RED+GREEN: EWMA ACWR computation tested on synthetic series; assert it is returned as context-only metadata, not a warning (§7.2).
- [x] `LOAD-9`: RED+GREEN: Banister model evaluation and `scipy.optimize` fit gated on performance-marker availability, with fit-quality reporting and a guard that unfitted models are marked non-personalized (§7.2).
- [x] `LOAD-10`: Integrate engine outputs into `daily_load` persistence with `engine_version` (§6) and write a cross-check test comparing PMC values to stored Intervals.icu values within the agreed tolerance (§12.3), documented as cross-check-only.
- [x] `LOAD-12` (owner-requested expansion, 2026-10-05): record owner-entered RPE so strength sessions get a real sRPE load. Intervals.icu stores an athlete-entered RPE per activity (`icu_rpe`, scale 1–10, editable on the activity page; `feel` is a separate 1–5 field returned inverted and is deliberately ignored). Scope: (a) ingest `activity.rpe` as an owner-reported INPUT (not a computed metric) with a migration and sync mapping, tolerating `session_rpe`/`perceived_exertion` aliases; (b) pass it into the engine so strength sports are rated by sRPE when RPE is present — the brief assigns sRPE to strength (§7.1) and HR is not a valid strength-load proxy, so for strength sports sRPE takes precedence over the generic HR step, documented as an explicit decision; (c) a calibration report computing the implied sRPE→TSS factor as the median of `hrTSS / sRPE_AU` over sessions that have both, which is how the placeholder factor gets replaced by a measured value once RPE data exists.
- [x] `LOAD-11`: Add configurable constants (time constants, coefficient sets, scaling factors) to settings with source comments (§14) and document module references in the `load.py` docstring.

## Acceptance criteria

From PROJECT_BRIEF.md §12.3:

- Unit tests with reference values for each formula; PMC values within an agreed tolerance of Intervals.icu for the same data (cross-check only).

Directly implied conditions:

- All load functions are pure, typed and docstring-documented with formula and reference (§7, §14).
- Every persisted engine output carries `engine_version` (§6).

## Verification evidence

Independently verified by `gentle-ai-verify` (read-only) at HEAD `5e3e9ac`, 2026-10-05 — 12/12 items PASS, no blocking defect:

1. **Quality gates** — ruff clean; mypy strict clean (86 source files); `uv run pytest` 438 passed, 0 failed, 0 skipped. DB tests genuinely ran against the dedicated test database and the dev database kept its rows (26 activities / 131 streams / 177 wellness) across the run.
2. **Engine purity (§6/§7)** — `app/engine/{load,pmc,banister}.py` import only stdlib, numpy and scipy; no `app.db`/`app.ingest`/`app.core`, no I/O libraries, no settings reads. The purity test auto-discovers every module in the package via `pkgutil`.
3. **Checklist completeness** — all of LOAD-1..LOAD-12 mapped to code and to passing tests (see the progress list above).
4. **§12.3 cross-check** — read-only tool; verdict PASS with like-for-like statistics: CTL aerobic-only median relative deviation `0.0000%` (max 9.05%, max absolute 0.4865 points, 0 failures over 175 days), ATL all-loads median `0.0000%` (max absolute 0.3868 points, 0 failures over 176 days); three Intervals-side revisions detected and excluded (2026-07-19 ATL, 2026-08-04 CTL, 2026-08-28 CTL); the non-like-for-like views stay printed and failing (19 and 15 days). The report states on every run that Intervals values are never authoritative (§5.1).
5. **Method selection (§7.1)** — fixed order power → pace/speed → HR → sRPE, the chosen method returned and persisted in `daily_load.methods` (verified on the real rows), and the strength-only sRPE-before-HR inversion documented as deliberate.
6. **ACWR (§7.2)** — `AcwrResult` carries exactly `acute_ewma`/`chronic_ewma`/`ratio`; no warning/risk/flag field anywhere, and no warning logic exists in the engine or tools.
7. **Banister (§7.2)** — the `parameters is not None ⇔ personalized` invariant holds; insufficient markers or a failed fit return `personalized=False` with a reason and no parameters, so an unfitted model cannot be presented as personalized.
8. **`engine_version` (§6)** — non-nullable column, set from settings, present on every persisted row (verified in the database).
9. **Configurable constants (§14)** — 20 `engine_*` fields, each with a source comment (literature reference or explicit owner choice), locked by a defaults test. Only flag: the Minetti polynomial coefficients remain inline with a citation and a note that they may move to settings later.
10. **Owner decisions recorded** — the sRPE anchor (`100/420`, equivalent effort, with the rejected alternatives), the hybrid cross-check tolerance (10% or 0.5 points) and the deliberate strength-in-CTL choice all appear with their rationale in code and in this document.
11. **Migration hygiene** — single head `b2f8d4c6a9e1`; the dev database is at head, chain base → `daa3ba6946b9` → `b7e4c9a1d2f3` → `e5f6a7b8c9d0` → `b2f8d4c6a9e1`.
12. **Real-data plausibility** — `python -m app.db.daily_load --days 200` considered all 26 activities (ride 17, walk 4, weight-training 5), persisted 200 days × 4 sport keys, all through the HR method because no RPE is entered yet, with **no skipped activities**; a second run upserted the same 800 rows (idempotent).

**Review findings fixed after verification** (commit `63cafcd`): the calibration tool crashed on a Windows cp1252 console (U+2248 is not representable) and now prints an ASCII-safe report; two functions gained docstrings; and `tests/test_dbsupport.py` had imported `test_database_url` under its own name, so pytest collected it as a test and warned — now aliased with an explanatory comment.

**Later closed with real data (2026-10-05)**: the owner entered RPE in Intervals.icu, so the strength sRPE path is no longer test-only. After a re-sync, 14 of 26 activities carry an RPE (the five weight-training sessions all RPE 4) and the daily-load recompute switched exactly those five from `hr` to `srpe`: 17-jun 6.62 → **38.40**, 4-jul 7.18 → **64.27**, 7-jul 12.13 → **59.30** TSS, with 21 activities still on HR. The PMC reflects it (17-jun CTL 18.67, TSB −15.52). The informational comparison printed by `app.tools.calibrate_srpe` over the 14 sessions with both RPE and HR gives a median hrTSS-implied factor of **0.0768** (gym-only ≈ 0.041), i.e. the owner-agreed equivalent-effort anchor (0.2381) deliberately rates strength work about three times higher than an hrTSS-anchored calibration would — recorded here so the magnitude of that choice stays visible.

Remaining limitation: the hand-derived reference values inside the test docstrings were accepted from the parent's independent re-derivation rather than recomputed by the verifier.

## Progress

In progress (Feature 3/11) on branch `feat/engine-load`, stacked on `fix/ingest-activity-id` (an Alembic migration in each branch would otherwise create multiple heads at merge time).

- LOAD-11 (first half, thresholds): done at commit `8451fb3` — `get_sport_settings()` / `get_athlete_profile()` live-verified models, pure `extract_athlete_thresholds()` per-sport mapping with explicit gap reporting (never silent defaults), seven `athlete_*` settings fields documented as owner configuration (§14) and non-authoritative (§5.1), and a print-only CLI (`python -m app.ingest.thresholds`). Owner values seeded into the gitignored `.env` files: FTP 180 W, LTHR 169 bpm, max HR 186 bpm, resting HR 65 bpm, swim CSS 0.8333 m/s. **Gaps the owner must fill in Intervals.icu: run threshold speed and body weight.**
- Real data available for validation: the live 180-day backfill ingested 26 activities (2026-04-05..2026-10-02) and 128 stream rows; the owner has **no power meter**, so cycling load must come from HR (TRIMP/hrTSS) — hence LOAD-5 is implemented before the power-based LOAD-1/2.

- LOAD-5 (HR-based load): done at commit `46e6964` — pure `app/engine/load.py` with ΔHRr, Banister TRIMP (male 0.64/1.92, female 0.86/1.67, overridable), the one-hour-at-LTHR reference, hrTSS and Foster sRPE (load = RPE × minutes × explicit TSS-equivalent factor). Out-of-range inputs raise `ValueError` rather than silently defaulting; an AST purity test asserts no `app.db`/`app.ingest`/`app.core` imports. Parent re-derived the reference values independently against the module: `dHRr@LTHR 0.8595041322`, `TRIMP 1h@LTHR 171.898768`, `TRIMP 60min@150bpm 103.925937`, `hrTSS 60.457639`, `sRPE 210 / 420` — exact match. This is the primary load path for the owner's data (no power meter).
- Settings robustness: commit `5e55275` — `env_ignore_empty=True`, because the README's "copy .env.example to .env" flow leaves optional numeric keys empty and previously made `Settings` raise at import. Also fixed a non-hermetic settings test that read the developer's local `.env`.

- LOAD-1/2 (bike power load): done at commit `6b13cd7` — `normalized_power` (mean of the fourth power of 30 s rolling means), `intensity_factor`, `power_tss` and a typed `BikePowerLoad` result. Documented edge semantics: a window counts only when `valid_count/30 >= min_valid_fraction` (default 1.0), a file shorter than the window falls back to the mean of valid samples, and zero usable data raises `ValueError`. Parent re-derived independently, exact match: NP(constant 200 W)=200, NP(0/300 W step)=203.092828 (mean of fourth powers exactly 1,701,290,000), NP(gapped step)=204.764515, NP(short)=180, IF=1.1111111111, TSS(1 h @200 W, FTP 180)=123.456790, TSS(1 h @FTP)=100, TSS(30 min @FTP)=50. Currently unused for the owner's data (no power meter), required by the checklist.

- LOAD-3/4 (pace/speed load): done at commit `cde4877` — Minetti et al. 2002 grade model, flat-equivalent speed, normalized graded speed (grade unavailable → 0, the neutral element of Cr), rTSS = duration_h × IF² × 100; swim normalized speed excludes rest/zero-speed samples and sTSS = duration_h × IF³ × 100. Validation precedes any division. Parent re-derived independently, exact match: Cr(+0.10)=5.9682140000, v_flat(+10%)=4.9735116667, NGS(uphill)=4.4801337500, rTSS(uphill 1 h)=163.84978300, NGS(downhill)=2.4662024609, NGS(altitude gap)=3.9867558333, NSS=1.0500000000, sTSS(IF 1.5)=337.5, sTSS(at CSS)=16.666667. **Blocked for real runs: the owner still has no run threshold pace in Intervals.icu.**

- LOAD-6 (method selection): done at commit `2193f4a` — `select_load_method` applies the fixed order power → pace/speed → HR → sRPE over a typed `ActivityLoadInput`/`ThresholdBundle`, returning the chosen `method` key, the TSS-equivalent value, the method detail and one skip reason per rejected method. Parent verified: power Ride 123.457; the owner's real case (Ride without power) → `hr` 60.458, bit-identical to the direct LOAD-5 computation; Run without a configured threshold → `hr` with `pace_speed` skipped; Run/Swim at threshold → 100; strength → `srpe`; nothing applicable raises `ValueError` listing all four reasons.
- **Open item for LOAD-11**: the sRPE TSS-equivalent factor is still an uncalibrated caller knob — with 1.0 it returns raw Foster AU (~420 for a 1 h RPE-7 session), which is not comparable to TSS. It needs a documented, sourced value.

- LOAD-7/8 (Performance Manager + ACWR): done at commit `37b197e` — new pure module `app/engine/pmc.py`: CTL/ATL recursions (τ 42/7, overridable explicit seeds), `TSB_t = CTL_{t−1} − ATL_{t−1}`, combined and per-sport series, and a `confident` flag below the 90-day history threshold. `acwr_ewma` implements the Williams et al. 2017 EWMA variant with an `AcwrResult` carrying exactly `acute_ewma`/`chronic_ewma`/`ratio` — no `warning`/`risk`/`flag` field, so ACWR cannot warn on its own (§7.2 belongs to Feature 5); zero chronic yields `ratio=None`. The engine purity test now auto-discovers every module in `app/engine` (pkgutil), covering future modules. Parent re-derived every reference with the closed form `100 × (1 − e^(−n/τ))`, bit-exact: CTL day1 `2.3528313348`, day2 `4.6503045167`, day3 `6.8937220296`; ATL day1 `13.3122100250`, day2 `24.8522706925`; TSB day2 `-10.9593786902`; seeds 50/40 → `51.1764156674`/`47.9873260150` (TSB 10.0); τ=14 → `6.8937220296`; 300 days → `99.9209509677`; confidence 89 d False / 90 d True; ACWR day1 ratio `3.7943760454`, 300 d `1.0000222257`.
- **Review point for LOAD-10**: `acwr_ewma` returns the latest value only, not a per-day series. Persisting daily ACWR or charting it (Feature 7) would need a series variant — decide then whether to extend.

- LOAD-9 (Banister): done at commit `9515107` — pure `app/engine/banister.py` with `evaluate_banister` (causal p(t), no fitting) and `fit_banister` (scipy `least_squares`; τ fitted only when asked). The personalization guard is the tested core: unfitted/insufficient/failed fits return `personalized=False` with a machine-readable reason and `parameters=None`/`quality=None` (§7.2: never present an unfitted model as personalized). `min_markers` defaults to 10 (2× the 5 free parameters of the richest fit). Owner reality: **no performance markers exist**, so the unfitted path is today's default. Parent verified independently: p(t) = `50.3253197280` exact (fitness `129.0364487494`, fatigue `61.2650270950`); guard holds at 0 and 9 markers; with a varying synthetic load the fit recovers the truth (p₀ 0.18%, k₁ 0.13%, k₂ 0.36% error, R² `0.999991`) and the parent's own oracle reproduces R²/RMSE exactly. Lesson recorded: a **constant** load series makes fitness/fatigue collinear and the parameters unidentifiable, so it is not a valid recovery test.

- LOAD-10 (in progress): first half done at commit `0311617` — `daily_load` table + migration `e5f6a7b8c9d0`, idempotent upsert, services-layer integration deriving engine inputs from stored rows, and `python -m app.db.daily_load --days N`. Verified on the owner's real data: 570 rows persisted for 190 days and the stored TSS matched the parent's independent hand computation exactly (`62.526432`, `123.696399`, `71.356727`). Second half (Intervals.icu cross-check + walking sports) landed the cross-check tool, non-authoritative `wellness.intervals_icu_ctl/atl` columns and migration `a8c3e5f70b12`, and the `Walk`/`Hike`/`Snowshoe` vocabulary.

  **Parent diagnostic of the first cross-check FAIL (2026-10-05)** — the writer's "Intervals edited their loads" hypothesis was refuted: all 26 stored `intervals_icu_load` values equal the live API today (0 differences). The real causes are:
  1. **Intervals' wellness CTL excludes strength sessions' load** (their ATL includes it). Feeding our CTL only aerobic loads matches theirs on **153/177 days** (vs 67/177 with all loads), and every observed jump equals the decayed excluded load: `7k = 0.1647` on 06-17, `8k` on 07-04, `13k` on 07-07 (28 strength load points in total), with `k = 1 − e^(−1/42)`. Our engine deliberately includes strength load — the right call for a triathlete who lifts — so the cross-check must compare like with like instead of scaling the tolerance.
  2. **Two unexplained Intervals-side discontinuities on days with no activity**: ATL drops `0.4462` on 2026-07-19 and CTL drops `0.4982` on 2026-08-04 (their recomputation, not derivable from the activity data).
  3. A **pure relative tolerance is the wrong yardstick** for an exponentially decaying series near zero (ATL showed 223% on values of order `1e-4`); the rule needs a hybrid relative+absolute bound.

  Positive result: day by day from 2026-04-11 to 2026-08-03 our CTL/ATL matched Intervals to `0.0000` given the same aerobic loads — an independent validation of LOAD-7/8 against a production implementation. Intervals values remain cross-check only (§5.1).

  **LOAD-10 acceptance met (§12.3)** — like-for-like verdict on the owner's real data, 200-day window: **PASS**. CTL compared on the aerobic-only view (matching Intervals' CTL definition) over 175 comparable days: median relative deviation **`0.0000%`**, max 9.05%, max absolute 0.4865 points, **0 failures**. ATL compared on the all-loads view over 176 days: median `0.0000%`, max absolute 0.3868 points, 0 failures. Three Intervals-side revisions detected and excluded (2026-07-19 ATL, 2026-08-04 CTL, 2026-08-28 CTL). The two non-like-for-like views remain printed and failing (19 and 15 days) as the evidence that the earlier FAIL was a definition difference. Owner decisions recorded: strength load **counts** toward our CTL (deliberate; Intervals excludes it), and the agreed tolerance is the hybrid rule (≤10% relative **or** ≤0.5 points absolute).

  **LOAD-11 (completed at `fec813d`)**: 20 `engine_*` Settings fields, each commented with its source — PMC τ 42/7 (Allen & Coggan), ACWR τ 7/28 (Williams et al. 2017), 90-day confidence (owner choice), NP window 30 and `min_valid_fraction` (Coggan / owner choice), TRIMP male 0.64/1.92 and female 0.86/1.67 plus sex and the 1-hour LTHR reference (Banister 1991 / Coggan), Banister τ 42/7 and `min_markers` 10 (Banister 1991; Morton et al. 1990 / owner choice), and the cross-check values (owner-agreed 10% rel or 0.5 points; revision threshold 0.25, an owner choice set above decay float-noise and below the measured real revisions). The pure engine keeps documented module fallbacks and only gained parameters; the read layer passes Settings in with CLI flag > Settings > fallback precedence. The sRPE factor stays `1.0` as an explicit PLACEHOLDER (raw Foster AU, not calibrated) with the calibration method documented. `scipy-stubs` added as a dev dependency; dropping the old `type: ignore[import-untyped]` exposed a **real** type error in the Banister residual closure, fixed properly by annotating it `NDArray[numpy.float64]` rather than restoring the ignore. Parent-verified: τ=14 → CTL `6.893722`, τ=42 → `2.352831`, sRPE factor 0.7 → `294` vs default `420`, invalid sex raises.

  **LOAD-12 done at `1eb249b`** (RPE intake) with the migration-target hardening at `55ac31b`: owner-entered RPE (`icu_rpe`, 1–10, aliases `session_rpe`/`perceived_exertion`, inverted `feel` ignored) is ingested as owner INPUT into the new `activity.rpe` column (migration `b2f8d4c6a9e1`), out-of-range/non-numeric values are rejected with reportable reasons, and strength sports now rate by sRPE when an RPE is present (strength-only inversion of the §7.1 chain, documented; other sports unchanged). Parent-verified: gym 40.3 min at RPE 7 → method `srpe`, 282.1 AU with the placeholder factor, versus 6.62 TSS from HR alone — the reason this intake exists; a Ride with RPE still selects `hr` (60.458).

  **OWNER DECISION (2026-10-05) — sRPE scale anchor**: the TSS-equivalent factor is anchored to equivalent effort, `factor = 100/420 ≈ 0.2381` ("one hour at RPE 7 (Foster AU 420) is treated as one hour at threshold ≈ 100 TSS"), so the owner's 40-minute RPE-7 gym session ≈ 67 TSS-equivalent, comparable to an hour of riding. The raw Foster AU (factor 1.0) was rejected as PMC-distorting (it would make a 40-minute gym session, 282, dwarf a 60-minute ride, 60); anchoring instead to the gym's own hrTSS (~0.024) was rejected because it merely reproduces HR's undervaluation of strength work. The calibration tool still reports the hrTSS-implied factor as informational evidence, and the convention is a documented owner choice, not a literature constant.

Commits: 8451fb3 (LOAD-11 first half), 5e55275, 46e6964 (LOAD-5), 6b13cd7 (LOAD-1/2), cde4877 (LOAD-3/4), 2193f4a (LOAD-6), 37b197e (LOAD-7/8), 9515107 (LOAD-9), 5cf3b5d (test DB isolation), 0311617 (LOAD-10 first half), 7f8fa0c (cross-check diagnosis), 7ab07c0 + 2edef32 (cross-check + columns), fec813d (LOAD-11)
