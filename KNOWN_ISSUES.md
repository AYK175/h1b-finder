# KNOWN ISSUES & DEFERRED WORK

Running ledger of gaps, deferrals, and flagged risks accumulated during the build.
Nothing here blocks shipping the day-1 tool. Address after the first working version.

Priority key:
- **P1** — materially affects results you'll act on; fix first after ship.
- **P2** — real gap, bounded impact; fix when convenient.
- **P3** — note-for-completeness; may never need fixing.

Status: OPEN unless marked DONE.

---

## Sourcing / coverage

**[P1] Cap-exempt list is high-precision, low-recall (IRS BMF + COTH deferred).**
Phase 2 ran on IPEDS + FFRDC only. Everything scoring 1.0 is trustworthy, but the entire
population of research nonprofits and standalone/teaching hospitals is *absent*, not
mis-scored. This is a real slice of cap-exempt AI/ML/DS roles (independent biomedical
institutes, nonprofit hospitals).
Fix: download the 4 IRS EO BMF regional CSVs + Publication 5926 codebook. Re-run Phase 2
with NTEE filter U/H (research) + E20-E22 (hospitals). Expect the empty 0.5-0.85 "ambiguous
band" to fill in. Weight irs_research_nonprofit at 0.60 (NTEE is noisy by design).

**[P2] COTH (teaching hospitals) has no clean free source — approximate via BMF.**
AAMC/COTH membership sits behind a paywall; no tidy CSV. Decision: approximate teaching
hospitals from IRS BMF NTEE E-codes + medical H-codes at coth_match weight 0.85 rather than
chasing COTH directly. True COTH list is derivable from CMS Medicare cost-report GME data if
ever needed, but that's a sub-project not worth doing.
Currently: teaching hospitals that fuzzy-matched an IPEDS university name are getting full
ipeds_match (1.0) instead of coth_match (0.85) — minor overstatement, self-corrects when BMF
loads.

**[P1] Aggregator university gap: Virginia Tech, UIUC, Michigan, Ohio State.**
Adzuna returns little/nothing for these specific schools (they don't syndicate to it —
likely PageUp/Interfolio/homegrown). NOT a systemic aggregator failure; 5 of 10 tested
universities returned strong results. See reports/aggregator_gaps.md for the running list.
Fix: build one targeted university-ATS adapter (PageUp + Interfolio cover a large share) in a
future session. Only worth it once the day-1 tool proves useful.

**[P2] ~384 employers unrecognized by Adzuna under any name variant.**
After the clean_query_name() fix (strips Trustees/Regents prefixes, legal suffixes,
parentheticals, prefers d/b/a) employers-with-postings doubled 42→94. But ~384 sponsor/cap-exempt
employers still return nothing from Adzuna under any tried name — genuine non-coverage, not a
formatting issue. These are reachable only via direct ATS (if they have one) or the deferred
Workday work.
Fix: cross-reference the 384 against the ATS resolution cache; anything with a Greenhouse/Lever/
Ashby board is already covered by 4b. The true remainder is proprietary-site-only and waits for
Workday or is accepted as out-of-reach.

**[P2] Workday poller deferred entirely.**
Largest single ATS surface from the Phase 0.2 spike (86 hits) but per-tenant discovery + POST
body + anti-bot is fragile, high-time-cost work. Enterprise sponsors (Qualcomm, Fidelity,
Walmart tenant, etc.) live here. Deferred off the day-1 critical path.
Fix: dedicated session. Reuse the confident Workday hits already cached in
data/spike/ats_resolution.csv as the starting tenant list.

---

## Data quality / entity resolution

**[P1] Manual review queue never worked (Option B — skipped).**
11,853 employer_match_review rows shipped un-reviewed; verified_by_human is FALSE everywhere.
Harmless today (no re-ingest), but the flag's whole job is protecting manual calls from being
overwritten on the next quarterly re-ingest.
Fix: before the first Phase 7 quarterly re-ingest, build the `h1b review` CLI (spec already
drafted in an earlier session) and work at least the cap-exempt slice
(--filter university/college/institute/hospital).

**[P2] Residual ~15-18 arguable fuzzy matches in Phase 2 need a human eyeball.**
After the over-firing fix (single-token exclusion, threshold 90→95), 75 fuzzy matches remain;
~15-18 are arguable — mostly city/county/state governments colliding with same-named public
college systems ("City of Chicago" → "City Colleges of Chicago"). Full list:
/tmp/fuzzy_matches_v3.csv (COPY THIS OUT OF /tmp before it's lost).
Fix: 10-minute manual keep/kill pass.

**[P1] For-profit universities scored cap-exempt 1.0 — status is genuinely ambiguous.**
University of Phoenix (for-profit) and WGU (nonprofit-online) got ipeds_match 1.0. IPEDS
presence is necessary but NOT sufficient for H-1B cap-exemption — cap-exempt status requires
being an institution of higher ed per the HEA, or an affiliated nonprofit. For-profit Phoenix
may not qualify. Tool currently ranks them top-tier no-lottery.
Fix: filter cap-exempt eligibility on IPEDS CONTROL field (public / private-nonprofit only;
exclude private-for-profit) or verify individually. Don't trust the 1.0 on for-profit schools.

**[P3] "Tech" nickname normalizer bug — DONE (fixed in Phase 4).**
normalize_employer_name() expanded tech→technologies, corrupting proper-noun school nicknames
("Virginia Tech" → "virginia technologies"). Fixed in Phase 4 Stop 2; verified zero regressions
on existing Phase 1 matches (3+ word legal names like "Texas Tech University" normalize
unchanged; only the bare 2-word nickname was affected). Marked DONE.

**[P3] "Certified - Withdrawn" status string had spaces (fixed in Phase 1).**
config.yaml originally had "Certified-Withdrawn" (no spaces); real value has spaces around the
hyphen. Silently undercounted Phase 0 spike AND Phase 1 until caught.
Status: FIXED in Phase 1. Consequence: the Phase 0.2 "21.4%" ATS-coverage number ran on
undercounted data — treat it as approximate, not exact. No action needed.

---

## Scoring / wage engine

**[DONE — CRITICAL] cap_exempt_score was never populated in the profile the ranking sorts on.**
v_ranked_jobs sorts on employer_h1b_profile.cap_exempt_score, but build_employer_profile()
never wired v_cap_exempt_score into the rollup — NULL for all 71,509 employers since the view
was written. The #1 ranking thesis (cap-exempt first) was a silent no-op the entire project.
Before fix: 0/50 top-50 recognized cap-exempt. After: 50/50. Fixed + backfilled in Phase 5.
Lesson: caught ONLY by inspecting real ranked output — "phase ran successfully" would have
shipped a confidently-wrong list. This is why every phase stops for eyes-on review.

**[DONE — CRITICAL] sponsor_score used wall-clock time, not the data horizon.**
run_scoring() anchored recency decay on date.today() while the data ends 2026-03-31 — a 110-day
skew. Recent active sponsors were penalized as stale: Amazon/Apple/Capital One's most recent
filings (2026-03-25) scored as "116 days old" (decay≈0.80) instead of "6 days old" (≈0.99).
CONSEQUENCE: every ranking reviewed in ALL prior phases was computed with a wrong recency signal
— plausible but not correct. Third silent thesis-level bug caught by output inspection (after the
cap_exempt_score no-op). Fixed by anchoring on MAX(received_date). Do not trust pre-fix rankings.
(lca_certified_24mo was ALSO made a real rolling window, but that was a minor effect — data only
spans ~18mo so nothing truncates yet; the wall-clock anchor was the actual bug.)

**[P1 — REFRAME] "17% wage coverage" is the wrong denominator.**
The 6,450 "no-SOC" postings are NOT unclassified — Haiku already ran and confidently rejected
them as "none / not a target role." They SHOULD be unscoreable; they're jobs the user doesn't
want. So coverage-over-all-postings (17%) is misleadingly low: the denominator is padded with
correctly-excluded non-target roles.
THE METRIC THAT MATTERS AND IS NOT YET COMPUTED: of postings with a TARGET SOC + a salary + a
resolvable location, what % get a ticket level? Compute coverage over target-SOC postings only.
That's the real health number. Remaining genuine gaps within target-SOC: no-salary (1,081),
unresolvable-location (1,770, incl. 218 that are literally just "US").
Fix: report target-SOC-denominator coverage; then the only real lever left is location
resolution (Census Gazetteer for the USPS-preferred-name mismatches like Foster City→San Mateo).

**[P1 — CRITICAL, root cause of two other issues] off_cycle_filings pollutes the primary sort.**
cap_exempt_score is the #1 ranking key. off_cycle_filings evidence gives 5,332 cap-SUBJECT
employers (22%) a non-zero cap_exempt_score on its own — Microsoft 0.403, LinkedIn 0.442, Roblox
0.395, Chime 0.364. These are NOT cap-exempt; they're being partially elevated into cap-exempt
territory and reordering a fifth of the cap-subject tier for reasons unrelated to tickets/fit.
Same bug CLASS as the cap_exempt_score no-op and wall-clock skew: a silent distortion of the top
priority — but worse, because it makes cap-exempt ranking work INCORRECTLY (elevating non-exempt
firms) rather than not at all. CLAUDE.md landmine #7 warned off-cycle is common among cap-subject
filers; the guard wasn't strong enough. A signal firing on 22% of cap-subject employers has almost
no power to discriminate the thing it's sorting on.
ALSO the root cause of the staffing-demotion no-op: Insight Global (0.371) / CGI (0.392) get their
non-zero cap_exempt_score ENTIRELY from off_cycle. Remove off_cycle from the sort and they drop to
0, landing correctly below all genuine cap-exempt roles — fixing both problems at once.
RECOMMENDED FIX: exclude off_cycle_filings from cap_exempt_score's SORT contribution entirely; keep
it as a displayed evidence tag only. Real cap-exempt employers still rank right (ipeds/ffrdc score
>=0.95, don't need off_cycle). Do NOT just lower weight / raise n-guard — that quiets noise without
making the signal meaningful for ranking.

**[DONE] Location resolution — Gazetteer + parsing fixes. Coverage 40%→51% target-SOC.**
The 1,770 failures were 3 separate problems: (a) regex rejected full state names ("...California")
— 305 postings never reached the existing crosswalk; (b) county-as-city ("Contra Costa, CA") —
Geography.csv's CountyTownName column had been discarded, re-read into ref_county_to_area; (c) ~58
genuine missing places (Foster City etc.) — solved via nearest-ZCTA-centroid Haversine matching off
Census Places + ZCTA gazetteers, local, no API. area_code 3,819→5,419. Remaining 26% unresolved is
the honest floor: "US"/"Remote"/"Hybrid" with no location, or foreign cities with no US OFLC area.

**[SUPERSEDED — see "staffing demotion now live" above] staffing_firm_score wired into sort.**
Wired two batches ago but inert (cap_exempt_score continuous → no ties → tiebreaker never engaged).
Now live after off_cycle removed from sort. Also fixed here: view was reading a never-populated
employer_h1b_profile.staffing_firm_score (same bug class as cap_exempt no-op); added a real
job_score.staffing_firm_score column and pointed the view at it.

**[P3] LinkedIn's public Lever board contains QA/test postings.**
13/22 ingested LinkedIn postings are junk ("TEst", "Rug Maker", "lorem upsum" descriptions) — real
public data on their board, not an ingestion bug. Correctly rejected to no-SOC so they don't pollute
rankings. Noted only so a stray test title in the data isn't mistaken for a bug later.

**[DONE] off_cycle_filings removed from the ranking sort. #1 key now correct.**
Added cap_exempt_score_sort (ipeds/ffrdc/coth/irs evidence only, excludes off_cycle); v_ranked_jobs
sorts on it. Full cap_exempt_score (incl. off_cycle) retained for DISPLAY as evidence. GATE PASSED:
Microsoft/LinkedIn/Insight Global sort-value → 0 (display unchanged 0.40/0.44/0.37); genuine
cap-exempt top-20 unchanged; 5,899 employers that had a fake off_cycle-only cap_exempt score now
sort at 0, zero retain a spurious sort value. This was the last silent-distortion-of-#1-priority
bug (family: cap_exempt no-op, wall-clock skew, this).

**[DONE] staffing demotion now live (was inert).**
With off_cycle removed, Microsoft/LinkedIn/Insight Global sit at cap_exempt=0 where the
staffing tiebreaker (cap_exempt DESC, tickets DESC, fit DESC, staffing_firm_score ASC) can finally
engage. CGI (0.523) and Insight Global (0.692) now land ranks 75-78, below non-staffing roles of
equal tickets/fit (Roku, Chime, Roblox, Databricks, NVIDIA, etc.). One fix resolved both bugs.

**[DONE, fuzzy disabled] IRS BMF ingested — 522 new cap-exempt employers, EIN+exact-name only.**
15,572 institutions filtered to 501(c)(3) + NTEE U/H (12,958 research) or E20-E22 (2,614 hospitals).
Added 522 cap-exempt employers absent from IPEDS/FFRDC: Brigham & Women's, Mass General Brigham,
Mount Sinai, Broad Institute, Scripps Research, Allen Institute, HHMI, etc. The empty 0.4-0.85 band
is now populated (3,010 employers): research institutes @0.60, health systems @0.8075, hospitals
@0.85 (coth). NOTE: no codebook (eo-info) was actually found/used — decoded from CSV headers +
public BMF format knowledge; acceptable but re-verify NTEE column mapping if results look off.
CRITICAL over-firing caught + handled: 30 of 133 fuzzy matches were unambiguous false positives at
>0.5 (People.ai→"Partnership on AI", Brown & Brown Inc→a foundation, County of Riverside→ambulance
assoc). token_set_ratio collides constantly at BMF's 15k scale. Fable DISABLED fuzzy matching for
BMF evidence (EIN + exact-name only, 543/676 survive) per the human-in-loop rule. Correct call —
precision over recall is right for a cap-exempt flag. Residual: real BMF matches that need fuzzy are
now missed; revisit with a better discriminator if the hospital/nonprofit coverage feels thin.
CODE CHECK (2026-10-14): the loader (`load_irs_bmf` in ingest/institutions.py) and the
`enable_fuzzy=False` option on `match_registry()` (capexempt/evidence.py) exist, but the committed
`h1b capexempt build` CLI (cli.py) only calls `match_registry` for IPEDS and FFRDC and still prints
"Deferred ... IRS BMF". The BMF match with fuzzy disabled is therefore not reproducible from the CLI
as committed (it was presumably run ad hoc). Re-running `capexempt build` will not regenerate the
BMF evidence rows; wire a BMF `match_registry(..., enable_fuzzy=False)` call in before relying on it.

**[RESOLVED — not a bug] PERM "entity-resolution miss" was a data-coverage gap.**
Investigated: the 2 loaded PERM files genuinely have zero Google rows — only "Google Public Sector
LLC" (separate EIN, correctly kept apart). The join is fine (Microsoft/Meta/Apple/Oracle/etc all
resolve at volume). Google's 0.000 is real absence in an ~18-month PERM window, not a join bug. My
prompt's premise was wrong; investigation correctly caught it before any code change. perm_to_lca_ratio
stays structurally thin until more PERM years are ingested — read low values as "thin data," not
"confirmed non-sponsor." (One n=1 Amazon EIN-typo collision flagged, not fixed — negligible.)

**[P2] 522 new cap-exempt employers have ZERO ingested job postings.**
IRS BMF strengthened the evidence layer but changed nothing in ranked output — Adzuna/ATS sourcing
doesn't reach these hospitals/research institutes, so none have postings to rank. Real gap for a
future job-sourcing phase (university/hospital ATS: PageUp/Interfolio/Workday-for-health-systems),
not a bug. The cap-exempt evidence is ready and waiting for when sourcing reaches them.







**[DONE] green_card_score populated (PERM ingested).**
All 11,224 postings now non-null (was 0). PERM ingested from FY2025 Q4 + FY2026 Q2, two ETA-9089
schemas reconciled. Caught+fixed a row_hash bug mid-ingest (LCA's case-number-excluding hash
collapsed ~24% of distinct PERM cases; now keyed correctly). BUT see two caveats below.

**[SUPERSEDED — code now fixed, see note] lca_certified_24mo is actually LIFETIME/unbounded — pre-existing naming bug, untouched.**
Code check (2026-10-13): `build_employer_profile()` in src/h1b_finder/pipeline.py now computes
lca_certified_24mo as a true rolling 24-month count by decision_date anchored on MAX(decision_date)
(CTEs `ref_date` / `windowed_certified`), so the "Fix" below has been applied in code. The
perm_to_lca_ratio re-derive and sponsor_score recency audit were not verified from code alone;
data-derived consequences below are left as originally written.
The column named "24mo" counts all-time LCA certifications, not a 24-month window. Consequences:
(a) perm_to_lca_ratio is structurally depressed for everyone (thin ~1.5yr PERM window ÷ unbounded
    LCA count); (b) any recency logic relying on this column — possibly sponsor_score's recency
    decay — is broken. Latent, predates the fix batch, now sits under green_card_score.
Fix: make lca_certified_24mo a true rolling 24-month count by decision_date; re-derive
perm_to_lca_ratio and audit sponsor_score's recency component against it.

**[P2] PERM→employer join drops real cases (Google green_card_score = 0.000).**
Google shows 0.000 because zero PERM rows resolved under its name/EIN — but Google files PERM at
huge volume under subsidiary/entity names. The zero is an entity-resolution MISS on the PERM side,
not a real absence. The PERM join is less clean than the LCA one.
Fix: audit PERM employer resolution against known high-volume green-card sponsors; extend the
alias table with PERM-side name variants. Read low ratios as "thin/unresolved PERM data," never
"confirmed non-sponsor," until this is fixed AND PERM spans more years.

**[DONE] staffing_firm_score — all 4 legs live, NAICS finding logged.**
PERM + NAICS now wired. KEY FINDING: NAICS 5613 alone catches ZERO real consultancies — Cognizant/
Infosys/TCS/Wipro are all 541511/541512 (Computer Systems Design), not 5613. The averaged 4-leg
formula flags them via volume/worksite-spread/low-PERM instead. ByteDance correctly de-penalized
(real NAICS 51321x, not staffing). Don't rely on the NAICS leg alone.



**[DONE] SOC prediction tier 2 fixed via Haiku reclassification.**
Was: TF-IDF tier-2 ~21% accurate, force-fitting sales/marketing/PM titles to "Data Scientist"
(606 of 632 tier-2 postings mislabeled as Data Scientist). Fixed by routing all 632 tier-2
postings through Claude Haiku with an explicit "none/not a target SOC" reject option.
Result: 479 (76%) correctly ejected to "none"; 123 re-routed to correct target SOC; Data
Scientist count 606→27. Overall accuracy 88%→95%, tier-2 band 21%→71%. Tier-1 untouched (still
100%). Cost ~$0.18, responses cached. Old values preserved on prior_* columns for audit.
Residual: 4 tier-2 misses are defensible SOC-boundary calls (e.g. Data Engineer → 15-1252 vs
15-2051), not mislabels — not worth chasing.
NOTE carried forward: SOC label feeds the wage engine (different SOCs → different OEWS cutoffs →
different lottery-ticket levels), so borderline Data-Engineer-ish calls carry slight wage-level
consequence. Not a bug; watch when a role's ticket count looks off.

**[P2] EDC (ACWIA higher-ed) wage table not used.**
The OFLC zip bundles EDC_Export.csv — a separate higher-education wage table that cap-exempt
universities are statutorily entitled to use, at lower cutoffs than the standard table loaded
in Phase 3. The CLI understates wage level for actual university offers.
Impact is narrow: for a genuinely cap-exempt hire, wage level is irrelevant (no lottery), so
this rarely matters. It DOES matter for (a) universities filing cap-subject petitions, and
(b) employers whose cap-exempt status is uncertain.
Fix: in scoring, when cap_exempt_score is high AND a wage level is being computed anyway, use
EDC cutoffs instead of the standard table.

**[P3] ~20 nonstandard/stale SOC codes have zero wage data.**
15-1132, 15-1295, 17-2144, etc. — 0.48% of all lca_case rows, none in target SOCs. Likely
retired/nonstandard codes on the LCA side. Doesn't touch target SOCs.
Fix: none needed. Noted for completeness.

---

## Reproducibility / ops

**[P2] FY2026 Q2 source URL unresolved in manifest.**
data/raw/manifest.json has the file + SHA256 but not the confirmed DOL source URL for
FY2026 Q2. Ingestion is fine; reproducibility and the Phase 7 quarterly refresh want the
pinned URL.
Fix: 60 seconds — grab the real URL from DOL browser history and pin it.

**[P2] Cumulative-file model is NOT stable across fiscal years.**
Phase 1 measured: FY2025 Q1-Q4 are DISCRETE quarterly extracts by DECISION_DATE (not
cumulative), but FY2026 Q2 IS cumulative for its FY. DOL's own page says "cumulative" which
contradicts the FY2025 measurement. row_hash dedupe handles both correctly, but do NOT assume
the model when FY2027 files arrive.
Fix: re-measure containment on first ingest of any new fiscal year's files.

**[P3] Spike artifacts live in /tmp and data/spike — don't lose them.**
data/spike/ats_resolution.csv (~107 confident ATS hits, reused by Phase 4b) and
/tmp/fuzzy_matches_v3.csv, /tmp/spotcheck_candidates.csv. /tmp is ephemeral.
Fix: move anything referenced by later phases out of /tmp into the repo before it's cleared.

---

## Dashboard (Phase 6)

**[DONE] Phase 6 dashboard shipped — Streamlit, one page, reads job_posting/job_score/
employer_h1b_profile directly (same filter/order logic as v_ranked_jobs, richer columns).**
`src/h1b_finder/app.py`. Cap-exempt tier (sort value >0.5) visually separated from cap-subject,
default 4-postings-per-employer cap with a lift toggle (CMU alone has 140 cap-exempt postings
pre-cap). Honest-display rules enforced: NULL lottery_tickets shows "not scored — <reason>" (read
from job_score.explanation.wage_level.reason, never "0"); cap-exempt badge driven by
cap_exempt_score_sort, not the display score — employers whose only evidence is off_cycle_filings
get an explicit "not treated as cap-exempt" note instead of the badge; salary badges visually
distinguish posted (solid) vs inferred (dashed/muted); green_card_score labeled "thin/no PERM
data" when perm_to_lca_ratio is 0/null, never "won't sponsor." Filters: cap-exempt only, min
tickets, state, predicted SOC, remote (text-matched on location_raw — job_posting.is_remote is
NULL for all 11,224 postings, no structured flag exists yet, disclosed in the filter's help text),
hide staffing firms (slider threshold), posted-salary-only. Tracker persists to the existing
`application` table (upsert on posting_id), no localStorage. Verified via Streamlit's AppTest
harness (script execution, not just HTTP 200) — caught one real bug this way: the cap-exempt/
cap-subject section-visibility conditional was inverted (checking "cap-exempt only" hid the
cap-exempt section and left the empty cap-subject one), fixed and re-verified. Run via
`uv run streamlit run src/h1b_finder/app.py` (added `streamlit>=1.38` to pyproject + uv.lock).

---

## Seniority tier (Phase 6 follow-up)

**[DONE] Seniority tier added — display/filter axis only, never touches scoring.**
`job_posting.seniority_tier/seniority_method/seniority_confidence` (`src/h1b_finder/jobs/
seniority.py`). Title-regex first (staff_plus > senior > entry > mid-by-absence), description
used only as a CONFLICT CHECK against an existing title marker — never as a freestanding source of
truth. Ambiguous cases (no title marker, or title/description conflict) route through Haiku, same
disk-cache pattern as soc_predict/soc_reclassify. All 11,224 postings classified (4,977 needed
Haiku on the first pass).

**[DONE, caught via spot-check] Description-tiebreak-alone was a real false-positive source —
fixed before shipping.** The original design let a description-only regex hit (no title marker at
all) decide the tier directly at 0.75 confidence. Spot-checking surfaced "Data Scientist, Search,
Multimodal" classified 'entry' because its description said "...mentoring more junior team
members" — the regex matched "junior" describing OTHER people the role mentors, not the role's own
level (the text was literally a leveling blurb labeled "Mid" one word earlier). 383/11,224 postings
(3.4%) had been resolved this way. Fixed by removing that path entirely — a title with no marker
now always goes to Haiku, which has the contextual judgment naive regex lacks; description regex is
only ever trusted to confirm or contradict a marker the TITLE already provided. All 383 were reset
and correctly reclassified via Haiku (that specific posting flipped to 'senior', 0.95 confidence).

**[DONE] Two Haiku JSON key-typo variants handled defensively.** Haiku returned `"sentiority_tier"`
and separately `"seriority_tier"` (misspellings of the requested key) for a handful of responses,
which a strict `.get("seniority_tier")` silently dropped (left NULL). Fixed by matching on VALUE
among all response fields (any field whose value is one of the 4 valid tiers) rather than chasing
individual key-name typos — robust to whatever variant shows up next. Final null count: 0/11,224.

**How to apply:** distribution over the 4,774 target-SOC postings: senior 36.7%, staff_plus 36.3%,
mid 23.5%, entry 3.5%. Filtered to (entry+mid seniority) AND (tickets>=3): 293 postings pre-cap
(137 after the dashboard's 4-per-employer display cap), 47 distinct employers, 7 cap-exempt
employers / 33 cap-exempt postings. Confirmed job_score/employer_h1b_profile completely untouched
(same row counts and timestamps before/after this batch) — seniority is purely additive to
job_posting, exactly as scoped.

---

## Post-ship polish (from BUILD_PLAN, not yet reached)

**[P2] Phase 5 scoring ships with all weights at 1.0 — must tune against real output.**
By design. After Phase 5, look at the top 50, disagree-count, tune. Not a bug — a required
step. The explanation JSON exists to support this.

**[P2] Remaining Phase 1 spot-check (20 of 25 employers) not human-verified.**
5 of 25 verified within ±5%; public trackers (h1bgrader/myvisajobs) 403-block automated
fetches. /tmp/spotcheck_candidates.csv has 30 candidates for a browser-based manual check.
Fix: few-minute manual pass in a browser when convenient.
