# ODD Feature: engine-readiness-intensity-durability

Status: in progress | Feature 5 of 11 (brief section 12) | Source: PROJECT_BRIEF.md

## Objective

Implement the remaining science-engine modules (PROJECT_BRIEF.md §7.4, §7.5, §7.6): readiness from HRV/resting HR/sleep against the athlete's own baseline with a multi-signal warning rule, 3-zone intensity distribution, and durability (Efficiency Factor and aerobic decoupling). Pure functions in `backend/app/engine/readiness.py`, `intensity.py` and `durability.py` that back `get_readiness`, `get_intensity_distribution` and `get_activity_analysis` tool outputs.

## Problem

Readiness must be a structured, evidence-based view of the athlete's own baseline — not a single magic score — and warnings must combine multiple signals before suggesting reduced intensity (PROJECT_BRIEF.md §7.4). Intensity distribution describes how training is distributed versus polarized/pyramidal patterns without moralizing (§7.5), and durability metrics show aerobic resilience on long sessions (§7.6). All of these must come from the deterministic engine so the agent never improvises (§3).

## Scope

- `backend/app/engine/readiness.py` (§6, §7.4), pure functions:
  - HRV: ln(rMSSD) daily, 7-day rolling mean vs 60-day baseline; flag when the rolling mean falls outside baseline ± 0.5 SD (smallest worthwhile change approach; Plews et al. 2013, Kiviniemi et al. 2007).
  - Resting HR: deviation vs 30-day baseline.
  - Sleep: duration vs personal baseline.
  - Readiness output is a structured object (each signal, its direction, its confidence), not a single magic score.
  - Warning rule: suggest reducing intensity only when **two or more** signals agree (e.g. HRV below band + TSB very negative + subjective fatigue reported).
- `backend/app/engine/intensity.py` (§6, §7.5):
  - Time in zone per sport per week.
  - 3-zone model: Z1 below first threshold, Z2 between thresholds, Z3 above second threshold; mapping from 5/7-zone tables documented in code.
  - Report vs polarized (~80/20) and pyramidal distributions (Seiler 2010; Stöggl & Sperlich 2014); descriptive comparison only.
- `backend/app/engine/durability.py` (§6, §7.6):
  - Efficiency Factor: bike NP / avg HR; run NGS / avg HR.
  - Aerobic decoupling (Pa:HR): (EF first half − EF second half) / EF first half; < 5% on long steady sessions suggests good aerobic durability (Friel).
  - Durability trends on long sessions (Maunder et al. 2021).
- Persistence of readiness inputs from `wellness` rows (Feature 2) and TSB from `daily_load` (Feature 3) — thin DB integration outside the pure modules, with `engine_version` on outputs (§6).

## Constraints

- Engine functions: pure, no I/O, no DB, no network, fully typed, docstring with formula and reference (§7, §14).
- The warning rule is exactly "two or more signals agree" — a single signal alone must never trigger a reduce-intensity suggestion (§7.4, §12.5 acceptance).
- ACWR must not be used as a warning signal on its own (§7.2); it stays context-only.
- Readiness output is structured (signal, direction, confidence), never a single magic score (§7.4).
- Intensity distribution reporting describes, does not moralize (§7.5).
- Baselines are the athlete's own (rolling windows per §7.4); no population norms.
- Configurable windows (7-day, 60-day, 30-day), SD band and thresholds live in config with source comments (§14).
- Depends on Features 1 (`project-scaffold`), 2 (`intervals-ingestion`, wellness/stream data) and 3 (`engine-load`, TSB input to the warning rule and zone boundaries via Feature 4 for the 3-zone mapping).

## Checklist

- [ ] `RID-1`: RED: unit tests for HRV ln(rMSSD) 7-day rolling mean vs 60-day baseline flagging outside ± 0.5 SD, on synthetic series with a known injected shift (§7.4).
- [ ] `RID-2`: GREEN: implement readiness signals — HRV baseline logic, resting HR vs 30-day baseline, sleep vs personal baseline — each returning direction and confidence (§7.4); tests green.
- [ ] `RID-3`: RED+GREEN: multi-signal warning rule — fires only when two or more signals agree; explicitly tested negative cases: single-signal deviations do NOT warn (§7.4, §12.5 acceptance).
- [ ] `RID-4`: RED+GREEN: structured readiness output object (each signal, direction, confidence; no composite score) with typed Pydantic-ready shape (§7.4).
- [ ] `RID-5`: RED+GREEN: time-in-zone aggregation per sport per week from stored zone tables (Feature 4) (§7.5).
- [ ] `RID-6`: RED+GREEN: 3-zone model mapping from 5/7-zone tables (Z1 below first threshold, Z2 between, Z3 above second) with the mapping documented in code (§7.5).
- [ ] `RID-7`: RED+GREEN: comparison output vs polarized (~80/20) and pyramidal reference distributions, descriptive only (Seiler 2010; Stöggl & Sperlich 2014) (§7.5).
- [ ] `RID-8`: RED+GREEN: Efficiency Factor (bike NP / avg HR; run NGS / avg HR) and aerobic decoupling (Pa:HR) tests including a hand-calculated decoupling example (§7.6, §12.5 acceptance).
- [ ] `RID-9`: RED+GREEN: durability trend computation on long sessions (§7.6, Maunder et al. 2021) with insufficient-data handling for short activities.
- [x] `RID-10`: Wire readiness/intensity/durability outputs into persistence with `engine_version` (§6) and document configurable windows/thresholds in settings with source comments (§14).

## Acceptance criteria

From PROJECT_BRIEF.md §12.5:

- Warning fires only when two or more signals agree (tested); decoupling matches hand-calculated example.

Directly implied conditions:

- All functions are pure, typed and docstring-documented with formula and reference (§7, §14).
- Readiness is a structured multi-signal object, not a single magic score (§7.4).

## Verification evidence

To be filled when the feature is implemented (commits, test runs, cross-checks).

## Progress

In progress (Feature 5/11) on branch `feat/engine-readiness`, **stacked on `feat/engine-zones`** because the 3-zone intensity mapping consumes Feature 4's zone tables and `zones.py` is not in `dev` yet (the engine-zones pull request is still open). Sequencing was decided with the owner: Features 4 and 5 are implemented before the WhatsApp agent (Feature 6) so the agent's `get_readiness` and `get_intensity_distribution` tools wrap real engine outputs instead of stubs. The PR for this branch should be opened after the engine-zones PR merges, so its diff shows only this feature.

Owner decisions carried into this feature (§15): the agent uses **OpenAI**; voice notes/STT are postponed; the Twilio webhook will be exposed through a development tunnel rather than a deployment.

Data note: the owner's `wellness` rows are the readiness input (§7.4) and currently cover 177 days (Apr–Oct 2026) with HRV, resting HR and sleep partly populated; where a signal is absent the engine must report it as missing rather than substituting a default.

- RID-1/2 done at `f4aec88`: new pure module `app/engine/readiness.py` with the three §7.4 signals (HRV ln(rMSSD) 7-day rolling mean vs a 60-day baseline flagged outside ± 0.5 SD — Plews et al. 2013 / Kiviniemi et al. 2007; resting HR and sleep against the athlete's own baselines), each returning a frozen structured signal — **never a composite score** — with observed value, baseline mean/SD, deviation in absolute and SD units, a direction literal, a confidence derived from the weaker evidence source's coverage and the valid-sample counts for audit. Window semantics are documented exactly (the observed day never feeds its own baseline; baselines use `ddof=1` over valid days only), a constant baseline collapses the band without dividing by zero, and missing data returns `insufficient_data` with counts instead of a substituted default. Parent-verified independently: baseline SD `0.10084389681792201` reproduced by hand, the band is **strict** (exactly ± 0.5 SD does not flag — one ULP of float dust included, the ZON-9 precedent), the drift probe matches to the digit (`1.918204 SD`), the insufficient-data paths behave as documented (`0.9833 = 59/60` confidence with one missing baseline day), and no composite-score field exists.

- RID-3/4 done at `ef913fe`: `readiness_assessment` implements the §7.4 rule and the **§12.5 acceptance criterion** — a reduce-intensity suggestion appears **only when two or more adverse signals agree**. Adverse = HRV below the band, resting HR elevated, sleep short, TSB strictly below a configurable "very negative" threshold (**−10.0, explicit OWNER CHOICE** because the brief gives no number; documented against the TrainingPeaks/Friel high-fatigue onset) and a reported subjective fatigue; opposite directions are never adverse. A signal with `status == insufficient_data` is **skipped before the adverse check** (absence of evidence is not evidence), and **ACWR is carried as context but never read for the verdict** (§7.2). The frozen `ReadinessAssessment` carries every signal with direction and confidence, the adverse keys, the agreement count, an explicit `suggest_reduce_intensity`, human-readable reasons and the context inputs — **no composite score** (§7.4) — and maps 1:1 to the future `get_readiness` tool without importing pydantic into the engine. Parent-verified independently: all five adverse signals in isolation leave the count at 1 and do **not** warn; two agreeing signals warn; the TSB boundary is strict (−10.00 not adverse, −10.01 adverse); ACWR at 0.8/1.3/**9.9** leaves the verdict identical; an insufficient-data HRV beside an elevated resting HR yields count 1 with no warning.

- RID-5/6/7 done at `d03b738`: new pure module `app/engine/intensity.py`. The 3-zone mapping carries an explicit provenance tag per threshold — the second threshold sits at the top of the published threshold zone (**literature**: top of Coggan Z4, LTHR for Friel, CSS for Wakayoshi) while the first-threshold cut points are marked **OWNER CHOICE** (bike 76% FTP, run/bike HR 90% LTHR, swim 95% CSS) — and a custom threshold that would split a source zone raises instead of losing seconds. Weekly aggregation uses ISO weeks (Monday start) with whole-session attribution, and a week with no sessions reports `status = no_data` with `None` percentages rather than a fabricated 0% split; the three zones provably partition the time exactly. The descriptive comparison against the polarized (~80/20, Seiler 2010) and pyramidal (Stöggl & Sperlich 2014) references uses owner-reviewable bands, a neutral label, a fixed caveat and a minimum-volume rule (under two hours → `insufficient_volume`), and §7.5's describe-don't-moralize is enforced by a forbidden-word test over every text field. Parent-verified: the partition arithmetic on a real-shaped session (Z1+Z2 = 300, Z3+Z4 = 700, Z5+Z6+Z7 = 1800, total 2800), all four mapping tables, ISO week starts, a no-data interior week, and the percentages.

- RID-8/9 done at `64cf4f6`: new pure module `app/engine/durability.py`. Efficiency Factor **reuses** the engine's normalized power and normalized graded speed (bike NP / avg HR; run NGS / avg HR) rather than reimplementing them, and aerobic decoupling implements `(EF_first − EF_second)/EF_first` with the split rule and **the sign convention documented explicitly** (positive = EF deteriorated in the second half). The Friel < 5% reference is a documented band whose boundary is **strict** — exactly 5%, including the real float case `0.04999999999999999`, is reported as not within, using the engine's tolerant comparison. A configurable steadiness guard (15% intensity drift, OWNER CHOICE) rejects interval-style sessions as `not_steady` with no value, since decoupling only means something on long steady work. Trends use a trailing 84-day window over long sessions (≥ 90 min, OWNER CHOICE with the rationale documented) as a closed-form OLS slope with a descriptive direction; short sessions are ineligible with a reason and cannot enter the slope, and fewer than three eligible sessions yields `insufficient_data`, never a one-point trend. **Parent-verified against the §12.5 acceptance criterion**: the hand-computed example (200 W/HR 150 then 190 W/HR 155) gives EF `1.3333333333333333` and `1.2258064516129032` with a decoupling of exactly **5/62 = 0.08064516129032251**, matching the module bit for bit; the 2% case is inside the band, the exact-5% case outside, a reversed intensity is negative, a 50%-drift session is `not_steady`, the monotone slopes (±0.01/7) and the stable case match by hand, a 45-minute session with an extreme value stays ineligible, and two sessions give `insufficient_data`.

- RID-10a (configuration half) done at `0984b59`: twenty-four `engine_*` fields mirror the Feature 5 constants with per-field provenance — literature where it exists (Plews 2013 / Kiviniemi 2007 for the HRV band, Friel for the 5% decoupling reference, Seiler 2010 and Stöggl & Sperlich 2014 for the reference bands, Maunder et al. 2021 as the trend motivation) and an explicit **OWNER CHOICE** marker where the brief gives no number (TSB threshold, LT1 cut points, minimum volume, minimum long session, steadiness drift, trend window, session minimums, completeness floors, band edges). Every default is **pinned by tests to the engine's own module constant**, so editing one side alone fails loudly, and no number was changed. `app/db/engine_readiness_config.py` maps settings to engine parameters with validating parsers that fail loudly on malformed environment values; its docstring states plainly that no caller exists yet (Feature 6 wires it). Design nuance recorded: the readiness keys are a per-signal settings view (`hrv_window_days` → `hrv_readiness(window_days=...)`) while the durability mapping is directly unpackable. Parent-verified: defaults equal the engine constants, non-default values flow (0.75 band, −20 TSB threshold, 3600 s floor), and malformed structured values raise named `ValueError`s.

Commits: f4aec88 (RID-1/2), ef913fe (RID-3/4), d03b738 (RID-5/6/7), 64cf4f6 (RID-8/9), 0984b59 (RID-10a)
