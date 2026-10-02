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

- [ ] `LOAD-1`: RED: unit tests with hand-calculated reference values for bike NP/IF/TSS (§7.1), including the rolling 30 s fourth-power mean edge cases (missing samples, short files).
- [ ] `LOAD-2`: GREEN: implement bike power-based TSS functions in `backend/app/engine/load.py` (pure, typed, docstrings with formula and Coggan reference); tests green.
- [ ] `LOAD-3`: RED+GREEN: run rTSS with documented grade-adjustment model (e.g. Minetti et al. 2002) tested on hand-computed examples (§7.1).
- [ ] `LOAD-4`: RED+GREEN: swim sTSS tested against hand-computed values with configurable CSS input (§7.1).
- [ ] `LOAD-5`: RED+GREEN: TRIMP/hrTSS with configurable male/female coefficients tested on hand-computed examples, plus strength sRPE with configurable TSS-equivalent scaling factor (§7.1).
- [ ] `LOAD-6`: RED+GREEN: method selection rule (power → pace/speed → HR → sRPE) with the chosen method returned/persisted (§7.1).
- [ ] `LOAD-7`: RED+GREEN: CTL/ATL/TSB recursion tests (seed values, time-constant configurability, combined and per-sport, low-confidence flag before 90 days of history) (§7.2).
- [ ] `LOAD-8`: RED+GREEN: EWMA ACWR computation tested on synthetic series; assert it is returned as context-only metadata, not a warning (§7.2).
- [ ] `LOAD-9`: RED+GREEN: Banister model evaluation and `scipy.optimize` fit gated on performance-marker availability, with fit-quality reporting and a guard that unfitted models are marked non-personalized (§7.2).
- [ ] `LOAD-10`: Integrate engine outputs into `daily_load` persistence with `engine_version` (§6) and write a cross-check test comparing PMC values to stored Intervals.icu values within the agreed tolerance (§12.3), documented as cross-check-only.
- [ ] `LOAD-11`: Add configurable constants (time constants, coefficient sets, scaling factors) to settings with source comments (§14) and document module references in the `load.py` docstring.

## Acceptance criteria

From PROJECT_BRIEF.md §12.3:

- Unit tests with reference values for each formula; PMC values within an agreed tolerance of Intervals.icu for the same data (cross-check only).

Directly implied conditions:

- All load functions are pure, typed and docstring-documented with formula and reference (§7, §14).
- Every persisted engine output carries `engine_version` (§6).

## Verification evidence

To be filled when the feature is implemented (commits, test runs, cross-checks).

## Progress

In progress (Feature 3/11) on branch `feat/engine-load`, stacked on `fix/ingest-activity-id` (an Alembic migration in each branch would otherwise create multiple heads at merge time).

- LOAD-11 (first half, thresholds): done at commit `8451fb3` — `get_sport_settings()` / `get_athlete_profile()` live-verified models, pure `extract_athlete_thresholds()` per-sport mapping with explicit gap reporting (never silent defaults), seven `athlete_*` settings fields documented as owner configuration (§14) and non-authoritative (§5.1), and a print-only CLI (`python -m app.ingest.thresholds`). Owner values seeded into the gitignored `.env` files: FTP 180 W, LTHR 169 bpm, max HR 186 bpm, resting HR 65 bpm, swim CSS 0.8333 m/s. **Gaps the owner must fill in Intervals.icu: run threshold speed and body weight.**
- Real data available for validation: the live 180-day backfill ingested 26 activities (2026-04-05..2026-10-02) and 128 stream rows; the owner has **no power meter**, so cycling load must come from HR (TRIMP/hrTSS) — hence LOAD-5 is implemented before the power-based LOAD-1/2.

Commits: 8451fb3 (LOAD-11 first half)
