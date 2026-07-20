# Aggregator (Adzuna) coverage gaps — known, logged, not solved tonight

From the Phase 4 Stop 1 university sub-spike (2026-07-18), updated after the Stop 2 real
poll run (same day) once the `clean_query_name()` fix went in (stripping legal-entity
formatting like "TRUSTEES OF THE..." before querying — see the Stop 2 report for why that
mattered). Two of the four originally-flagged gaps improved; two remain genuinely absent
under every name variant tried. Adzuna is still queried for all of them in the real poller
(no special-casing), but don't expect much — a lack of results doesn't mean the employer
isn't hiring.

| Employer | Sub-spike finding | Stop 2 real-poll result | Status |
|---|---|---|---|
| University of Michigan | 2 postings, 0 target-role | 1 open posting (via Adzuna) | Slightly better, still thin |
| Ohio State University | 16 postings, ~0 target-role | 9 open postings (via Adzuna, "The Ohio State University" → "Ohio State University") | Improved by the query-cleanup fix |
| Virginia Tech (Virginia Polytechnic Institute & State University) | 0 postings, 2 name variants tried | Still 0 | **Confirmed absent** |
| University of Illinois (Urbana-Champaign) | 0 postings (1 spurious mismatch), 4 name variants tried | Still 0 | **Confirmed absent** |

**Needed in a future session:** direct ATS adapters specifically for Virginia Tech and
UIUC — PageUp and Interfolio are the most likely platforms (per BUILD_PLAN's Phase 4a
fallback note) given large state universities commonly run one of those. Confirm which
platform each actually uses before building anything; don't assume. Michigan and Ohio
State are thin but no longer zero — lower priority for a dedicated adapter.
