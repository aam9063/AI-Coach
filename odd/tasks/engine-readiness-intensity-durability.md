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
- [ ] `RID-10`: Wire readiness/intensity/durability outputs into persistence with `engine_version` (§6) and document configurable windows/thresholds in settings with source comments (§14).

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

Commits: f4aec88 (RID-1/2)
