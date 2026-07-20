# H-1B Sponsor-First Job Discovery — Build Plan

**Audience:** an agentic coding model (Claude Fable) executing this end-to-end.
**Owner:** Abdullah Y. Khan

---

## 0. Thesis

Do not scrape job boards and filter for sponsors. **Start from the sponsor list and go find their jobs.**

The DOL LCA disclosure data defines a tractable universe (~5–15k US employers filed for data/ML SOC codes in the last 24 months). Everything else — ATS polling, scoring, UI — is enrichment on that spine.

Two facts that shape the product:

1. **The FY2027+ lottery is wage-weighted.** Level IV = 4 entries, Level III = 3, II = 2, I = 1. Projected selection: ~61% at IV, ~45% at III, versus roughly random-lottery odds at I. So the question is never "does this company sponsor" — it's **"will this specific offer clear OEWS Level III for its SOC + metro."**
2. **Cap-exempt employers skip the lottery entirely.** A Level I role at a university beats a Level IV role at a startup. Ranking must reflect this.

---

## 1. Guardrails — read before writing any code

**Do NOT:**
- Scrape LinkedIn, Indeed, Glassdoor, or ZipRecruiter. ToS violation, aggressive bot detection, and the data is worse (ghost jobs, reposts, stale). If a phase seems to need it, the phase is wrong.
- Build auth, multi-tenancy, Docker, Kubernetes, a REST API, or a React SPA. This is a **single-user local tool**. One DuckDB file on one laptop.
- Build the dashboard before Phase 5. Every phase before it ships a CSV that is immediately useful.
- Auto-submit applications. Deep-link + track. Mass-applying tanks response rates and breaks constantly.
- Tune scoring weights before seeing real output. Ship weights of 1.0 and look at the ranking first.
- Delete rows. Ever. Soft-delete with `closed_at`. A posting vanishing in 3 days is signal.

**DO:**
- Ship a usable artifact at the end of every phase.
- Commit the `data/raw/` manifest (checksums + URLs), not the files.
- Write the acceptance test before the implementation for each phase.
- Stop and ask when a kill criterion trips.

---

## 2. Stack

| Concern | Choice | Why |
|---|---|---|
| Store | **DuckDB** (single file) | Reads XLSX/CSV/Parquet natively, handles 500k-row rollups with no server, whole DB is one portable file. Postgres only if concurrent writes ever matter. They won't. |
| Dataframes | **Polars** | 500k+ rows/quarter; pandas is fine but slower here. |
| HTTP | **httpx** (async) | Concurrent ATS polling with sane rate limits. |
| Fuzzy match | **rapidfuzz** | `token_set_ratio` on normalized names. |
| LLM | **Anthropic SDK** (Haiku for volume, Sonnet for hard calls) | Entity resolution tiebreaks + SOC classification. |
| Config | **pydantic-settings** + `config.yaml` | No hardcoded paths or weights. |
| Dashboard | **Streamlit** | Phase 6 only. Already familiar from `llm-eval-harness`. |
| Migrations | Plain `.sql` files, numbered | Overkill to add Alembic for one user. |

```
h1b-finder/
├── config.yaml
├── schema.sql                  # already written — source of truth
├── data/
│   ├── raw/                    # gitignored; manifest.json IS committed
│   ├── parquet/                # converted DOL files
│   └── h1b.duckdb
├── src/h1b_finder/
│   ├── ingest/                 # dol_lca.py, uscis_hub.py, perm.py,
│   │                           #   institutions.py, oflc_wage.py
│   ├── resolve/                # normalize.py, matcher.py, llm_tiebreak.py
│   ├── capexempt/              # evidence.py, off_cycle.py
│   ├── wage/                   # area_crosswalk.py, wage_level.py
│   ├── jobs/                   # ats_discovery.py, pollers/, soc_predict.py,
│   │                           #   salary_extract.py
│   ├── score/                  # scorer.py, weights.py
│   └── cli.py                  # typer
├── tests/
│   └── fixtures/               # 500-row LCA sample, committed
└── notebooks/                  # exploration only, never imported
```

---

## PHASE 0 — Spike (3 hours, do not skip)

**Purpose:** retire the two risks that can kill this project before writing real code.

### Task 0.1 — Does the DOL data parse into a sane sponsor list?
- Download the single most recent LCA quarterly XLSX from `dol.gov/agencies/eta/foreign-labor/performance`.
- **Also download the record-layout PDF for that exact FY.** Column names shift between fiscal-year layouts. Never assume the names in `schema.sql` — verify against the layout doc every ingest. This is the #1 source of silent breakage.
- Load to Polars. Filter `CASE_STATUS IN ('Certified','Certified-Withdrawn')` and `SOC_CODE` in the target list.
- Group by `EMPLOYER_NAME`, count, sort desc.

**Acceptance:** top 20 looks like a plausible list of large tech/consulting employers. If you see obvious garbage, the parse is wrong.
**Sanity check:** Amazon's entities combined should be in the tens of thousands of certified LCAs per FY. If your total is off by an order of magnitude, you're double-counting worksites or dropping rows.

### Task 0.2 — THE CRITICAL SPIKE: ATS coverage
This is the real project risk. **Measure it before building the poller.**

- Take the top 500 employers from 0.1.
- For each, try to resolve a public ATS board:
  - Greenhouse: `boards-api.greenhouse.io/v1/boards/{token}/jobs`
  - Lever: `api.lever.co/v0/postings/{company}?mode=json`
  - Ashby: `api.ashbyhq.com/posting-api/job-board/{token}`
  - Workday: `{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` (POST)
  - SmartRecruiters: `api.smartrecruiters.com/v1/companies/{id}/postings`
- Guess tokens from the company's normalized name; confirm with a HEAD/GET.
- Report: **% of top 500 resolvable, broken out by ATS provider.**

**Why this is the crux:** Greenhouse/Lever/Ashby skew toward startups. The heaviest H-1B sponsors skew enterprise — Workday, iCIMS, Taleo, SuccessFactors. And universities (your best cap-exempt targets) are the worst offenders: PageUp, Interfolio, homegrown systems.

**Kill criteria:**
- **>50% resolvable via Tier 1 (GH/Lever/Ashby):** proceed as planned.
- **25–50%:** proceed, but Workday support moves from Phase 4b to Phase 4a. It's mandatory, not optional.
- **<25%:** STOP AND REPORT. The architecture may need to invert — use Adzuna/JSearch API as the primary posting source and use the H-1B data purely as a scoring overlay. That's a different (and simpler) product. Do not silently push on.

---

## PHASE 0.2 — RESULT & ARCHITECTURE DECISION (settled — do not re-litigate)

Spike ran on the top 500 employers by certified LCA count in target SOCs
(FY2025 Q1–Q4 + FY2026 Q2, ~163k cases, deduped on CASE_NUMBER).

| Bucket | Count | % |
|---|---|---|
| Confident resolution | 107 | 21.4% |
| Low-confidence (first-word token guess) | 48 | 9.6% |
| Unresolved | 345 | 69.0% |

By provider (confident): Workday 41, Greenhouse 38, SmartRecruiters 29, Lever 3, Ashby 0.
Tier 1 (GH/Lever/Ashby) share: **8.2%**. Confident overall: **21.4%** (just under the 25% stop line).

**The decisive finding is not the percentage — it's that ZERO universities and only
9 edu/health employers appear in the top 500 at all.** The spike measured coverage of
*high-volume corporate sponsors* and the answer is "poor." But that segment matters least
to the thesis. The top-500-by-volume sample structurally cannot contain cap-exempt
employers (universities file a handful of DS LCAs each, scattered across departments),
so the ATS-coverage number was answering the wrong question.

**DECISION — hybrid, aggregator-primary, Workday deferred. This is settled.**

1. **Keep the working ATS tier** (Greenhouse/Lever/Ashby + confident Workday hits from the
   spike). ~107 mid-market sponsors with clean, fresh, structured data. Do not discard this
   because the aggregate % looked bad — it's the highest-quality posting source we have.

2. **Aggregator (Adzuna) is PRIMARY breadth, not a fallback.** It reaches the proprietary-site
   giants (Amazon/Google/etc. — rank these LOW; the user already knows about them) and, critically,
   the universities (rank HIGH).

3. **Reconstruct the cap-exempt universe independently of the top 500.** This is the single most
   important change. See revised Phase 2 — start from IPEDS+FFRDC and match against ALL LCA filers,
   not the volume leaders.

4. **Workday deferred to a later session.** Largest single surface (86 hits) but per-tenant
   discovery + POST + anti-bot is fragile, time-sinking work with the worst time-to-value. Not on
   the day-1 critical path. Add when not racing a Fable window.

Rationale: not pure aggregator-primary (that wastes the clean ATS data), not Workday-first (that
trades the whole day for one fragile poller). Aggregator for reach, ATS for quality, IPEDS/FFRDC
for the cap-exempt spine.

---

## PHASE 1 — Sponsor universe (weekend 1)

**Ships:** `sponsors.csv` — ranked list of employers who sponsored your SOC codes in the last 24 months. **Immediately usable by hand even with zero further phases built.**

### 1.1 Ingest
- 8 quarters of LCA (FY2024–FY2026). Land raw → Parquet → `raw_lca`.
- `row_hash` over all payload columns. DOL **restates rows** in later quarterly releases after appeals — dedupe on hash, keep latest `loaded_at`.
- Write `data/raw/manifest.json`: URL, SHA256, download date, FY/Q. Commit it.
- USCIS Employer Data Hub → `raw_uscis_hub`.
- PERM (FY2024–FY2026) → `raw_perm`. Note: OFLC split PERM into two files with separate layouts after the ETA-9089 revision. Handle both.

### 1.2 Normalize into `lca_case`
Gotchas that will silently corrupt everything:
- **Wage units.** `WAGE_UNIT_OF_PAY` ∈ {Year, Hour, Month, Bi-Weekly, Week}. A "$50" Hour row is $104,000/yr. Normalize everything to annual. Assume 2080 hrs/yr. **A single missed conversion poisons the entire wage-level engine.** Assert: no `wage_from_annual` < $15,000 or > $2,000,000 after conversion.
- **`Certified-Withdrawn` still counts.** They filed. Only `Denied` and `Withdrawn` are negative signal.
- **`is_off_cycle`:** TRUE when `begin_date` falls outside ~Sept 25 – Oct 8. Cap-subject petitions cluster hard on Oct 1; cap-exempt filings scatter year-round.
- Resolve `area_code` via `ref_zip_to_area` on `WORKSITE_POSTAL_CODE`; fall back to city/state match, then statewide BOS area.

### 1.3 Entity resolution v1
Cascade, highest-confidence first, **stop at first hit**:
1. EIN match (from USCIS `tax_id` when unmasked) → confidence 1.0
2. Exact normalized-name match → 0.95
3. `rapidfuzz.token_set_ratio` ≥ 92 **and** same state → 0.80
4. Fuzzy 85–92 → enqueue to `employer_match_review`, do not auto-link
5. No match → new `employer` row

Normalization function (test this in isolation, it's load-bearing):
- lowercase; strip punctuation
- strip suffixes: `inc|llc|corp|corporation|ltd|limited|lp|llp|plc|co|company|holdings|group|usa|us|na`
- collapse whitespace
- expand common abbreviations (`intl`→`international`, `tech`→`technologies`, `svcs`→`services`)

**Never overwrite an alias where `verified_by_human = TRUE`.**

### 1.4 Rollup → `employer_h1b_profile`
Materialize. Don't compute on the fly.

**Acceptance:**
- Spot-check 25 employers by hand against h1bgrader/myvisajobs. ≥90% of counts within ±10%.
- `employer_match_review` queue < 15% of distinct raw names.
- `sponsors.csv` exists, is sorted sensibly, and you'd actually use it.

**Then do the highest-leverage hour of the project:** sit down and manually resolve the review queue. Set `verified_by_human = TRUE`. This is not optional and cannot be automated away.

---

## PHASE 2 — Cap-exempt layer (weekend 2)

**Ships:** `cap_exempt_sponsors.csv`. **Arguably the single most valuable artifact in this entire build** — these employers can hire you year-round with no lottery.

> **CRITICAL — universe construction (per Phase 0.2 decision).** Do NOT filter the LCA
> data to the top 500 (or any volume threshold) before building this list. Universities and
> research nonprofits file only a handful of target-SOC LCAs each, so any volume ranking
> erases them. Match IPEDS/FFRDC/etc. against **every distinct LCA employer** in the target
> SOCs. An institution that sponsored even 3 data scientists across two years is a top-tier
> target and must appear here. This list is built from the institution registries outward,
> not from the sponsor-volume leaderboard inward.

### 2.1 Load institution registries
- **IPEDS** (all US colleges/universities) → `exempt_basis='higher_ed'`
- **IRS Exempt Organizations Business Master File** → filter to 501(c)(3); NTEE codes starting `U` (science/tech research) and `H` (medical research) → `'nonprofit_research'`
- **NSF FFRDC list** (national labs) → `'govt_research'`
- **AAMC/COTH teaching hospitals** → `'affiliated_nonprofit'`

### 2.2 Match to employers, emit evidence
Match on EIN first (IRS BMF has EINs — this is your best join key anywhere in the project), then normalized name + state.

Emit `cap_exempt_evidence` rows. **Never write a boolean.** Confidence per type:

| evidence_type | weight | note |
|---|---|---|
| `manual_confirmed` | 1.00 | overrides everything |
| `ipeds_match` | 1.00 | near-certain |
| `ffrdc_match` | 1.00 | near-certain |
| `coth_match` | 0.85 | affiliation nuance exists |
| `irs_research_nonprofit` | 0.60 | NTEE is noisy |
| `off_cycle_filings` | 0.45 | corroborating only |
| `naics_signal` | 0.20 | weak (NAICS 6113, 5417) |

### 2.3 Off-cycle heuristic
Per employer: `off_cycle_ratio = count(is_off_cycle) / count(*)` over 24 months.
- **Guard on n.** Below ~10 filings the ratio is noise. Store `off_cycle_n` and require n ≥ 10 before emitting evidence.
- Ratio > 0.70 with n ≥ 10 → emit `off_cycle_filings` evidence.
- This is corroborating evidence, never standalone. Plenty of cap-subject employers file off-cycle transfers.

**Acceptance:**
- GMU appears with `cap_exempt_score` ≥ 0.9. (You're already there — worth knowing what else is.)
- Hand-verify 15 employers scoring 0.4–0.7 (the ambiguous band, where the value is). Document each in `notes`.
- Zero obvious for-profits (Google, Deloitte, Infosys) scoring > 0.5. If any do, the matcher is over-firing — fix before proceeding.

---

## PHASE 3 — Wage level engine

**Ships:** a CLI calculator: `h1b wage-level --salary 145000 --soc 15-2051 --city "Austin, TX"` → `Level III, 3 tickets`. Useful standalone — you can evaluate real offers with it today.

### 3.1 Load `ref_prevailing_wage`
Pull **OFLC's published four-level wage files** (FLC Data Center / OFLC wage data). Do **not** re-derive levels from raw OEWS percentiles — OFLC already publishes the Level I–IV cutoffs. Re-deriving them introduces error for no benefit.

### 3.2 Area crosswalk
The persistent join headache. `location_raw` → geocode → zip → `area_code`.
- Use a local ZIP→CBSA crosswalk (HUD or Census). No geocoding API in the hot path.
- Remote roles: default to the employer's HQ area, flag `is_remote`, and lower confidence. The actual worksite drives the wage determination, and for remote roles that's genuinely ambiguous — don't pretend otherwise.

### 3.3 Compute level
```
level = highest L in {4,3,2,1} where offered_salary >= ref_prevailing_wage.level_L_annual
lottery_tickets = level
```

**Critical, and easy to get wrong:** do **not** use `PW_WAGE_LEVEL` from the LCA for this. The LCA's DOL-assigned level and the FY2027 registration level are *different determinations*. Registration looks only at which OEWS level the **offered wage** meets or exceeds for that SOC + location. A role properly assigned Level I on its LCA can register at Level III if the salary clears the Level III cutoff. Compute your own.

**Encode this too:** if multiple employers register for the same beneficiary, USCIS assigns the **lowest** wage level across all registrations. Shotgunning registrations actively lowers your odds. Surface this as a warning in the UI if you ever track multiple concurrent registrations.

**Acceptance:**
- Take 200 real LCA rows, compute your level from `wage_from_annual`, compare to `PW_WAGE_LEVEL`. They should correlate strongly but **not match exactly** — divergence is expected and correct. If they match 100%, you've accidentally read the wrong column.
- Known-value test: hand-check 5 cases against the OFLC wage lookup.

---

## PHASE 4 — Job ingestion

> Structure below reflects the Phase 0.2 decision: aggregator is primary breadth, ATS is
> quality, Workday is deferred. Build 4a and 4b this session; 4c is a stretch; 4d is a later session.

### 4a. Aggregator — PRIMARY breadth source (Adzuna)
- Adzuna API (free tier), keyed by company name, restricted to the resolved sponsor + cap-exempt
  employer sets. `discovered_via='aggregator'`, lower confidence than direct ATS.
- This is how the proprietary-site giants and the universities enter the dashboard at all.
- **University sub-spike (20 min, do FIRST inside 4a):** query Adzuna for ~10 known university
  data/ML roles (pick real ones — e.g. a GMU, UMD, Johns Hopkins data scientist posting) and confirm
  they actually return. University coverage is the exact segment we most need and the least certain.
  - If they show up → proceed, aggregator carries the cap-exempt half.
  - If Adzuna is thin on universities → fall back to handling a few university ATS platforms directly
    (PageUp + Interfolio cover a large share). Do NOT build that unless the sub-spike proves it's needed.

### 4b. Direct ATS pollers — QUALITY source (Greenhouse, Lever, Ashby + confident Workday hits)
- Reuse the confident resolutions from the Phase 0.2 spike (cached in `data/spike/ats_resolution.csv`).
- Async, **2 req/sec per provider max**, exponential backoff, honor 429s. Identify in the User-Agent.
  Free public endpoints — don't get them locked down for everyone.
- `consecutive_failures >= 5` → `is_active = FALSE`, stop polling.
- Persist `raw_json` always. Reparse later without re-fetching.
- Upsert on `(board_id, external_id)`. Set `last_seen_at` on every poll. Any posting not seen in a full
  successful poll cycle → `closed_at = now()`.
- SmartRecruiters (29 confident hits) folds in here too — same pattern, GET-based.

### 4c. Workday — DEFERRED (later session, not day 1)
Largest single surface (86 spike hits) but per-tenant discovery (tenant + site slug) + POST body +
anti-bot handling is the most fragile, time-sinking work in the project. Explicitly out of scope for
the initial build per the Phase 0.2 decision. Add when there's time to do it properly.

### 4d. SOC prediction
- Title regex for the obvious 60% ("data scientist" → 15-2051).
- Embedding similarity against SOC titles for the next chunk.
- Claude Haiku for the ambiguous remainder, batched. Prompt returns JSON only: `{soc_code, confidence, reasoning}`.
- Always store `soc_method` and `soc_confidence`. Filter on confidence downstream.

### 4e. Salary extraction
- Parse posted ranges where present.
- **Pay-transparency states (CO, CA, WA, NY, IL, and growing) are effectively a free labeled training set.** Postings there carry real ranges. Use them to fit a model that infers salary elsewhere from title + company + location.
- Fallback: employer's median `wage_from_annual` for that SOC from `lca_case`. That's a genuinely good prior — it's what they actually paid.
- Always set `salary_source`. Never let inferred and posted salary look identical downstream.

### 4f. Sponsorship language flags
Regex the description:
- `mentions_no_sponsorship`: "without sponsorship", "not able to sponsor", "no visa sponsorship", "must be authorized to work ... without"
- `mentions_clearance`: "security clearance", "TS/SCI", "must be a US citizen" → hard filter for you
- `mentions_sponsorship_ok`: "will sponsor", "sponsorship available"

**Acceptance:** open postings from ≥150 distinct sponsor employers across aggregator + direct ATS combined
(the ≥300/≥5,000 target from the original ATS-heavy plan no longer applies — hybrid sourcing is narrower
but higher-signal). **At least 15 of those employers must be cap-exempt** (`cap_exempt_score > 0.5`) — if
zero universities/research orgs surface any postings, the university sub-spike in 4a failed and must be
addressed before proceeding. SOC prediction ≥85% accurate on a 100-posting hand-labeled set. Zero postings
with `mentions_clearance = TRUE` surviving into `v_ranked_jobs`.

---

## PHASE 5 — Scoring

Implement `job_score`. **Ship every weight at 1.0 first.** Look at the top 50. Then tune.

Subscores (each 0–1, each writes its inputs to `explanation` JSON):
- `sponsor_score` — has this employer filed for **this SOC**, recently? Recency-decayed. Filing for your exact SOC last quarter ≫ filing for anything 3 years ago.
- `cap_exempt_score` — from `v_cap_exempt_score`
- `wage_level_predicted` / `lottery_tickets` — Phase 3
- `fit_score` — title/description vs. your profile (CS BS, MS Data Analytics Engineering, AI/ML/DS)
- `green_card_score` — `perm_to_lca_ratio`. Low ratio = H-1B mill with no I-140 path. This is the long-game filter most tools ignore.
- Penalty: `staffing_firm_score` (high LCA volume + low PERM + NAICS 5613 + many distinct worksites)

Ranking order is the thesis, not a tunable:
```
1. cap_exempt_score DESC     -- no lottery beats good lottery odds
2. lottery_tickets DESC      -- then wage level
3. fit_score DESC            -- then whether you'd actually want it
```

**Acceptance:** you look at the top 50 and disagree with fewer than 10. For each disagreement, `explanation` tells you exactly why it ranked there. If it can't, the explanation JSON is underspecified — fix that before touching weights.

---

## PHASE 6 — Interface

Streamlit. One page. Filters: cap-exempt only / min tickets / state / SOC / remote. Table → `apply_url` deep link.

**Not** an auto-applier. Deep-link + a kanban on the `application` table. Ranking 40 high-signal jobs beats blasting 400.

---

## PHASE 7 — Operate

- **Quarterly:** DOL drops new LCA/PERM ~30–60 days after quarter end. Re-ingest, re-resolve (respecting human-verified aliases), rebuild profile.
- **Daily:** poll ATS boards. Alert on new postings scoring above threshold.
- **Annually:** OFLC wage data refresh (~July). Wage levels shift; re-score.

---

## Appendix A — Facts to encode, easy to get wrong

1. **The $100k H-1B fee does not apply to you.** Proclamation 10973 hit petitions for beneficiaries *outside* the US requiring consular processing. USCIS explicitly confirmed F-1 students changing status inside the US are exempt — and a federal court has since vacated the fee anyway. Many recruiters don't know this and reflexively say "no sponsorship." Put a one-line explainer in the outreach template. This is free alpha in conversations.
2. **LCA count ≠ people sponsored.** One case covers `TOTAL_WORKER_POSITIONS` workers, and consultancies file speculatively at volume. Keep LCA and USCIS Data Hub as separate facts. Never conflate them in a single "sponsorship" number.
3. **DOL column names shift between FY layouts.** Read the record-layout doc every single ingest.
4. **Cap-exempt is inferred, never certified.** No official list exists. Surface `cap_exempt_score` and its evidence in the UI — never a green checkmark. You will be wrong sometimes, and the user needs to see why.
5. **Concurrent cap-exempt employment is a real strategy.** A cap-exempt H-1B (e.g., a university) can be held concurrently with other work. Worth understanding as a fallback path.
6. **This tool is not immigration advice.** It ranks public data. Every real decision goes through an immigration attorney.

## Appendix B — Definition of done

Ship gate: **for a given week, the tool surfaces ≥10 open roles at cap-exempt employers, or at Level III+ wage for your SOC and metro, that you would not have found by hand.**

If it can't do that, no amount of additional phases will fix it — the sponsor universe or the ATS coverage is the problem. Go back to Phase 0.2.
