# CLAUDE.md

Persistent context for this repo. Read fully before any task.

**Companion docs:** `BUILD_PLAN.md` (what to build, in what order) · `schema.sql` (data model — source of truth)

---

## What this is

A single-user local tool that finds AI/ML/DS jobs at employers who will sponsor an H-1B, ranked by how likely that sponsorship is to actually work.

**The thesis, in one line:** don't scrape job boards and filter for sponsors — start from the DOL sponsor list and go find their jobs.

**The ranking is the product:**
1. Cap-exempt employers first (no lottery at all)
2. Then wage level (FY2027+ lottery is wage-weighted: Level IV = 4 tickets, I = 1)
3. Then role fit

If a change makes the ranking worse, it's a bad change no matter how clean the code is.

---

## Sourcing architecture (SETTLED after Phase 0.2 — do not revert)

The ATS-coverage spike showed Tier 1 (Greenhouse/Lever/Ashby) at 8.2% and confident
resolution at 21.4% — but the real finding was that **zero universities appear in the
top-500-by-volume sample.** The volume leaderboard structurally excludes cap-exempt
employers. So sourcing is a deliberate hybrid:

- **Aggregator (Adzuna) = primary breadth.** Reaches proprietary-site giants (rank LOW)
  and universities (rank HIGH).
- **Direct ATS (GH/Lever/Ashby/SmartRecruiters + confident Workday hits) = quality.** Reuse
  the spike's cached resolutions. Clean structured data, ~107 mid-market sponsors.
- **Cap-exempt universe is built from IPEDS/FFRDC matched against ALL LCA filers**, never a
  volume-filtered subset. An institution with 3 target-SOC LCAs is a top target.
- **Workday is DEFERRED** — largest surface but fragile POST/anti-bot work, off the day-1 path.

Do not "fix" the low ATS % by going ATS-first or by dropping the aggregator. That number was
answering the wrong question. Full reasoning: BUILD_PLAN.md § "PHASE 0.2 — RESULT & ARCHITECTURE DECISION".

---

## Hard rules

**Never:**
- Scrape LinkedIn, Indeed, Glassdoor, ZipRecruiter. ToS violation, bot-detected, worse data. If a task seems to require it, the task is wrong — stop and say so.
- Auto-submit job applications. Deep-link only.
- Add auth, Docker, a REST API, multi-tenancy, or a React frontend. One user, one laptop, one DuckDB file.
- Hard-delete rows. Soft-delete via `closed_at`. Disappearance is signal.
- Store `is_cap_exempt BOOLEAN`. Cap-exempt is *inferred* — store evidence rows and score them.
- Overwrite an `employer_alias` where `verified_by_human = TRUE`.
- Tune scoring weights before a human has looked at real ranked output.

**Always:**
- Persist `raw_json` / raw rows before transforming. Reparse without re-fetching.
- Set `salary_source` and `soc_method` — never let inferred data look like observed data.
- Write `explanation` JSON on every score. If a human can't tell why something ranked where it did, the score is broken.
- Rate-limit ATS polling to 2 req/sec/provider, honor 429s, identify in User-Agent. These are free public endpoints; don't get them locked down.

---

## Data gotchas that silently corrupt everything

These have all bitten this project's design. Treat as landmines.

1. **DOL column names shift between fiscal-year layouts.** Pull the record-layout PDF for the exact FY being ingested. Never trust the column names in `schema.sql` — they're representative, not guaranteed.

2. **Wage units.** `WAGE_UNIT_OF_PAY` ∈ {Year, Hour, Month, Bi-Weekly, Week}. A "$50" Hour row is $104,000/yr (2080 hrs). One missed conversion poisons the entire wage-level engine.
   → Assert post-conversion: no `wage_from_annual` < $15,000 or > $2,000,000.

3. **`PW_WAGE_LEVEL` is NOT the lottery wage level.** The LCA's DOL-assigned level and the FY2027 registration level are different determinations. Registration looks only at which OEWS level the **offered wage** meets or exceeds for that SOC + location. Compute it from `ref_prevailing_wage`. If your computed level matches `PW_WAGE_LEVEL` 100% of the time, you read the wrong column.

4. **`Certified-Withdrawn` is positive signal.** They filed. Only `Denied` and `Withdrawn` are negative.

5. **LCA count ≠ people sponsored.** One case covers `TOTAL_WORKER_POSITIONS` workers, and consultancies file speculatively at volume. LCA (filings) and USCIS Data Hub (approvals) are separate facts. Never merge them into one "sponsorship" number.

6. **DOL restates rows** in later quarterly releases after appeals. Dedupe on `row_hash`, keep latest `loaded_at`.

7. **Off-cycle ratio needs an n-guard.** Below ~10 filings it's noise. Require `off_cycle_n >= 10`. It's corroborating evidence only — plenty of cap-subject employers file off-cycle transfers.

8. **EIN is the best join key in the project.** Use it first, everywhere it exists (IRS BMF, sometimes USCIS `tax_id`). Fuzzy name matching is the fallback, not the default.

---

## Stack

DuckDB · Polars · httpx (async) · rapidfuzz · Anthropic SDK (Haiku for volume, Sonnet for hard calls) · pydantic-settings · typer · Streamlit (Phase 6 only)

```bash
uv run h1b ingest lca --fy 2026 --quarter 1
uv run h1b resolve employers
uv run h1b review                      # interactive: work the match queue
uv run h1b capexempt build
uv run h1b wage-level --salary 145000 --soc 15-2051 --city "Austin, TX"
uv run h1b jobs poll --provider greenhouse
uv run h1b score --version v0.1.0
uv run h1b export sponsors             # → sponsors.csv
uv run streamlit run src/h1b_finder/app.py
```

---

## Code conventions

- Direct over clever. Inline the computation if it reads better than a helper. No abstraction until the third occurrence.
- No config in code. Weights, paths, SOC lists, thresholds → `config.yaml`.
- Type hints on function signatures. Not religious about internals.
- Pure functions for anything scoring- or normalization-related — they need to be testable in isolation.
- `notebooks/` is for exploration and is never imported by `src/`.
- Commit `data/raw/manifest.json` (URLs + SHA256). Never commit `data/raw/` itself.

## Testing

- Fixtures: a committed 500-row LCA sample in `tests/fixtures/`. Every ingest change runs against it.
- `normalize_employer_name()` is load-bearing — test it standalone, exhaustively.
- Wage conversion gets known-value tests for every unit in the enum.
- Acceptance criteria live in `BUILD_PLAN.md` per phase. Write the acceptance test before the implementation.

---

## Human in the loop

Two places to stop and ask rather than proceed:

1. **After entity resolution v1** — the `employer_match_review` queue gets resolved by hand. An hour of human eyeballs on ambiguous employer names beats any fuzzy-matching cleverness. Don't automate past it.
2. **When a kill criterion in `BUILD_PLAN.md` trips** — especially Phase 0.2 (ATS coverage < 25%). Report and stop. Don't silently push on with a broken premise.

---

## Framing

This tool ranks public data. It is not immigration advice, and nothing it outputs should be presented as legal certainty — particularly cap-exempt status, which is inferred and sometimes wrong. Surface `cap_exempt_score` with its evidence. Never a green checkmark.
