# Phase 0.2 — ATS Coverage Spike

Top **500** employers by certified H-1B LCA count (FY2025 Q1–Q4 + FY2026 Q2, deduped on CASE_NUMBER), filtered to target SOC codes in `config.yaml`, CASE_STATUS in ('Certified','Certified-Withdrawn').

Resolution = token guessed from normalized employer name, confirmed by a live request to the provider's public API. Greenhouse and SmartRecruiters hits are name-verified against the API's returned company name; Lever/Ashby/Workday are token-existence only. A first-word-only guess for a multiword employer counts as **low-confidence**, not resolved. Every confident hit was then manually audited; 12 hits whose board clearly belongs to a different company were reclassified as false positives and excluded from all counts below.

## Headline

| Bucket | Employers | % of 500 |
|---|---:|---:|
| Resolved (confident) | 107 | 21.4% |
| Low-confidence only | 48 | 9.6% |
| Unresolved | 345 | 69.0% |

- **Tier 1 (Greenhouse/Lever/Ashby) confident: 41 / 500 = 8.2%**
- LCA-volume-weighted confident coverage: 23,804 / 163,127 certified LCAs = 14.6%

## By provider

| Provider | Confident | Low-confidence | Total hits |
|---|---:|---:|---:|
| greenhouse | 38 | 0 | 38 |
| lever | 3 | 8 | 11 |
| ashby | 0 | 2 | 2 |
| workday | 41 | 45 | 86 |
| smartrecruiters | 29 | 0 | 29 |

(Employers can hit on multiple providers; provider rows sum to more than the headline.)

## Top 20 unresolved by LCA volume

| Rank | Employer | Certified LCAs (target SOCs) | State |
|---:|---|---:|---|
| 1 | Amazon.com Services LLC | 11,500 | WA |
| 2 | Google LLC | 9,208 | CA |
| 3 | Microsoft Corporation | 8,849 | WA |
| 4 | Meta Platforms, Inc | 6,658 | CA |
| 5 | Apple Inc. | 4,555 | CA |
| 7 | JPMorgan Chase & Co. | 3,698 | IL |
| 8 | Deloitte Consulting LLP | 3,445 | PA |
| 9 | Amazon Web Services, Inc. | 3,071 | WA |
| 10 | Amazon Development Center U.S., Inc. | 2,565 | WA |
| 11 | Fidelity Technology Group, LLC d/b/a Fidelity Investments | 2,565 | MA |
| 12 | Ernst & Young U.S. LLP | 2,088 | NJ |
| 13 | COMPUNNEL SOFTWARE GROUP, INC | 1,818 | NJ |
| 14 | Salesforce, Inc. | 1,684 | CA |
| 16 | AMAZON.COM SERVICES LLC | 1,398 | VA |
| 19 | CAPGEMINI AMERICA INC | 1,231 | IL |
| 21 | Oracle America, Inc. | 1,179 | TX |
| 24 | Qualcomm Technologies, Inc. | 930 | CA |
| 25 | RANDSTAD DIGITAL, LLC | 895 | MA |
| 26 | IBM Corporation | 841 | NC |
| 28 | KFORCE INC.  | 809 | FL |

## Assessment

**The Tier 1 share does not clear 50%. It is not close: 8.2% confident (10% even counting every low-confidence hit).** The BUILD_PLAN prediction was right about the mechanism — Greenhouse/Lever/Ashby skew startup, and the top of the LCA list is enterprise (FAANG on homegrown systems, banks on Oracle/SuccessFactors, consultancies and staffing firms with no public board at all).

What the spike actually found:

1. **Workday is the largest single surface** — 41 confident + 45 low-confidence tenants (~17% of the 500), ahead of Greenhouse (38). Under the BUILD_PLAN kill criteria, Workday support is mandatory and moves to Phase 4a — except the overall number also matters, see below.

2. **Overall confident resolution is 21.4%, which sits below the <25% kill line.** Counting low-confidence hits it's 31%, which lands in the 25–50% "proceed with Workday mandatory" band. The measurement genuinely straddles the kill line, so the honest reading depends on two caveats:
   - **Token-guessing systematically undercounts.** Walmart's Workday tenant was only found after a detector fix; Qualcomm and Fidelity are known Workday/enterprise-ATS shops behind vanity career domains this method can't see. Real ATS presence is higher than name-guess resolvability — but Phase 4 polling would rely on the same discovery method, so the resolvable number is the operationally relevant one unless discovery gets smarter (e.g., following careers links from employer websites).
   - **The unresolved 69% is dominated by exactly two archetypes:** giants with proprietary career sites (Amazon, Google, Microsoft, Meta, Apple — ~35k LCAs among them) and staffing/consulting firms (Compunnel, Randstad, Kforce) that the ranking would deprioritize anyway. Coverage of the employers this tool most wants to surface (mid-size product companies) is meaningfully better than the headline.

3. **Universities — the highest-value cap-exempt targets — are essentially absent.** Only 9 of the top 500 employers are education/health institutions, and the 3 that resolved are health-tech companies, not universities. The cap-exempt half of the thesis cannot be served by these five ATS providers at any coverage level; it needs the Tier 3 aggregator fallback (Adzuna/JSearch) or dedicated university-ATS work regardless of what happens with Tier 1/2.

**Read against the kill criteria:** this is not the >50% "proceed as planned" world, and it is arguably the <25% "stop and report" world. Per instructions I am stopping here and not acting on any kill criterion. The decision on whether to proceed with Workday-first polling, invert to an aggregator-primary architecture, or split the approach (aggregator for enterprise/universities, direct ATS for the resolvable ~30%) is the owner's call.

### Method notes / caveats

- Providers probed at 2 req/sec each, all responses cached under `data/spike/ats_cache/`. Rerunning `spikes/phase0_ats_resolve.py` is free (cache-only).
- Workday "resolved" = tenant exists on wd1/wd5/wd3 (via CXS error-code discrimination). Site slugs — required for actual job polling — were not discovered; that is additional Phase 4b work per tenant.
- SmartRecruiters requires ≥1 public posting to register as a hit; Lever/Ashby empty boards were excluded as stale/squatted.
- Duplicate employer entities (e.g. three Synechron spellings, two LinkedIn casings) were not merged — this is pre-entity-resolution by design, so "500 employers" overstates distinct companies somewhat.
