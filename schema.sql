-- =============================================================================
-- H-1B SPONSOR-FIRST JOB DISCOVERY — SCHEMA
-- =============================================================================
-- Target: DuckDB (recommended) or Postgres. SQLite works with minor tweaks.
--
-- DuckDB is the pragmatic choice: it reads the DOL XLSX/CSV files directly,
-- handles the 500k-row/yr analytical rollups without a server, and the whole
-- database is one file you can back up to Dropbox.
--
-- LAYERS
--   0. raw_*        immutable landing zone, everything TEXT
--   1. ref_*        reference / crosswalk data
--   2. employer     entity resolution hub
--   3. facts        LCA / USCIS / PERM sponsorship evidence
--   4. cap-exempt   evidence + scoring
--   5. profile      materialized employer rollup
--   6. jobs         ATS boards + postings
--   7. scoring      versioned job scores
--   8. personal     application tracker
-- =============================================================================


-- =============================================================================
-- LAYER 0 — RAW / STAGING
-- =============================================================================
-- Everything TEXT. No casting, no cleaning, no dedupe. The DOL changes column
-- names between fiscal-year layouts (and split the PERM file in two when they
-- revised the ETA-9089). Always pull the record-layout PDF for the FY you're
-- ingesting; do NOT assume the columns below are stable across years.

CREATE TABLE raw_lca (
    raw_id              BIGINT PRIMARY KEY,
    source_file         TEXT NOT NULL,      -- e.g. 'LCA_Disclosure_Data_FY2026_Q1.xlsx'
    fiscal_year         INTEGER NOT NULL,
    fiscal_quarter      INTEGER NOT NULL,
    loaded_at           TIMESTAMP NOT NULL,
    row_hash            TEXT NOT NULL,      -- dedupe across quarterly re-releases
                                            -- (DOL restates rows after appeals)
    -- Payload columns, all TEXT, named as-published. Representative subset —
    -- land ALL columns from the file, not just these.
    CASE_NUMBER                 TEXT,
    CASE_STATUS                 TEXT,   -- Certified | Certified-Withdrawn | Denied | Withdrawn
    RECEIVED_DATE               TEXT,
    DECISION_DATE               TEXT,
    VISA_CLASS                  TEXT,   -- H-1B | H-1B1 Chile | H-1B1 Singapore | E-3 Australian
    JOB_TITLE                   TEXT,
    SOC_CODE                    TEXT,
    SOC_TITLE                   TEXT,
    FULL_TIME_POSITION          TEXT,
    BEGIN_DATE                  TEXT,   -- the cap-exempt timing signal lives here
    END_DATE                    TEXT,
    TOTAL_WORKER_POSITIONS      TEXT,
    NEW_EMPLOYMENT              TEXT,
    CONTINUED_EMPLOYMENT        TEXT,
    CHANGE_EMPLOYER             TEXT,
    EMPLOYER_NAME               TEXT,
    TRADE_NAME_DBA              TEXT,
    EMPLOYER_CITY               TEXT,
    EMPLOYER_STATE              TEXT,
    EMPLOYER_POSTAL_CODE        TEXT,
    NAICS_CODE                  TEXT,
    WORKSITE_CITY               TEXT,
    WORKSITE_COUNTY             TEXT,
    WORKSITE_STATE              TEXT,
    WORKSITE_POSTAL_CODE        TEXT,
    WAGE_RATE_OF_PAY_FROM       TEXT,
    WAGE_RATE_OF_PAY_TO         TEXT,
    WAGE_UNIT_OF_PAY            TEXT,   -- Year | Hour | Month | Bi-Weekly | Week
    PREVAILING_WAGE             TEXT,
    PW_UNIT_OF_PAY              TEXT,
    PW_WAGE_LEVEL               TEXT,   -- I | II | III | IV  (LCA determination — NOT the
                                        -- same thing as the FY2027 registration level)
    PW_OES_YEAR                 TEXT,
    H_1B_DEPENDENT              TEXT,
    WILLFUL_VIOLATOR            TEXT,
    AGENT_ATTORNEY_NAME         TEXT
);
CREATE UNIQUE INDEX ux_raw_lca_hash ON raw_lca(row_hash);

-- USCIS H-1B Employer Data Hub. Grain: employer x fiscal year.
-- This is your reality check: LCAs are filings, these are actual approvals.
CREATE TABLE raw_uscis_hub (
    raw_id              BIGINT PRIMARY KEY,
    source_file         TEXT NOT NULL,
    loaded_at           TIMESTAMP NOT NULL,
    fiscal_year                 TEXT,
    employer             TEXT,
    tax_id                      TEXT,   -- often masked, but when present it's gold
                                        -- for entity resolution
    industry_naics_code         TEXT,
    petitioner_city             TEXT,
    petitioner_state            TEXT,
    petitioner_zip              TEXT,
    initial_approval            TEXT,
    initial_denial              TEXT,
    continuing_approval         TEXT,
    continuing_denial           TEXT
);

-- PERM disclosure data. Signal: does this employer sponsor green cards, or
-- will they H-1B you for six years and never file an I-140?
-- Column names below were corrected 2026-07-19 against the actual DOL PERM
-- ETA-9089 file headers (verified identical across FY2025_Q4 and FY2026_Q2 —
-- both are post-June-2023-revision "new form" extracts; no old/new schema
-- split was found between the two files actually on disk). The original
-- names here (EMPLOYER_NAME, PW_SOC_CODE, WORKSITE_STATE, WAGE_OFFER_FROM,
-- WAGE_OFFER_UNIT_OF_PAY) never matched the real file — see CLAUDE.md
-- landmine #1 ("never trust the column names in schema.sql"). Deliberately
-- minimal (not "land every column" like raw_lca) — only what green_card_score
-- and staffing_firm_score need; wage is out of scope for this batch.
CREATE TABLE raw_perm (
    raw_id              BIGINT PRIMARY KEY,
    source_file         TEXT NOT NULL,
    fiscal_year         INTEGER NOT NULL,
    loaded_at           TIMESTAMP NOT NULL,
    row_hash            TEXT NOT NULL,
    CASE_NUMBER                 TEXT,
    CASE_STATUS                 TEXT,   -- Certified | Certified - Expired | Denied | Withdrawn
    RECEIVED_DATE               TEXT,
    DECISION_DATE               TEXT,
    EMP_BUSINESS_NAME           TEXT,
    EMP_FEIN                    TEXT,
    EMP_STATE                   TEXT,
    JOB_TITLE                   TEXT,
    PWD_SOC_CODE                TEXT,   -- O*NET-suffixed, e.g. '15-2051.00' -- strip suffix
    PRIMARY_WORKSITE_STATE      TEXT
);
CREATE UNIQUE INDEX ux_raw_perm_hash ON raw_perm(row_hash);

-- Institution registries feeding the cap-exempt derivation.
-- One table, discriminated by source, because you'll add sources over time.
CREATE TABLE raw_institution (
    raw_id              BIGINT PRIMARY KEY,
    source              TEXT NOT NULL,  -- 'IPEDS' | 'IRS_BMF' | 'COTH' | 'NSF_FFRDC'
    source_file         TEXT NOT NULL,
    loaded_at           TIMESTAMP NOT NULL,
    payload             JSON NOT NULL   -- schemas differ wildly per source; normalize
                                        -- into ref_institution downstream
);


-- =============================================================================
-- LAYER 1 — REFERENCE / CROSSWALKS
-- =============================================================================

CREATE TABLE ref_soc (
    soc_code            TEXT PRIMARY KEY,   -- '15-2051'
    soc_title           TEXT NOT NULL,
    is_target           BOOLEAN NOT NULL DEFAULT FALSE  -- your AI/ML/DS shortlist
);

-- Seed with the codes that actually matter for your profile.
INSERT INTO ref_soc (soc_code, soc_title, is_target) VALUES
    ('15-2051', 'Data Scientists',                              TRUE),
    ('15-1252', 'Software Developers',                           TRUE),
    ('15-1221', 'Computer and Information Research Scientists',  TRUE),
    ('15-2041', 'Statisticians',                                 TRUE),
    ('15-1211', 'Computer Systems Analysts',                     TRUE),
    ('15-1243', 'Database Architects',                           TRUE),
    ('13-1111', 'Management Analysts',                           FALSE), -- consultancy catch-all
    ('11-2021', 'Marketing Managers',                            FALSE); -- where PM roles hide

-- OEWS/OFLC geographic areas. The join headache: LCA data has city/county/state
-- text, postings have freeform location strings, and wage data is keyed by area.
CREATE TABLE ref_area (
    area_code           TEXT PRIMARY KEY,   -- OFLC area code
    area_name           TEXT NOT NULL,
    area_type           TEXT,               -- MSA | BOS | statewide
    state               TEXT
);

CREATE TABLE ref_zip_to_area (
    zip                 TEXT PRIMARY KEY,
    area_code           TEXT NOT NULL REFERENCES ref_area(area_code),
    county_fips         TEXT
);

-- FIX 3 (2026-07-19): real city->CBSA crosswalk, built from the 'city' column
-- already present in the ZIP-CBSA HUD file (USPS-preferred place name per
-- ZIP) -- not a substring match against OFLC's own hyphenated principal-city
-- MSA names, which silently failed real, valid non-principal cities like
-- "Palo Alto" or "Mountain View" (they're in the San Jose MSA but neither
-- name appears in that MSA's own display string). Fixes ~78% of SOC-known
-- job postings previously failing area resolution. See Phase 5/fix-batch report.
CREATE TABLE ref_city_to_area (
    city                TEXT NOT NULL,   -- normalized upper-case
    state               TEXT NOT NULL,   -- 2-letter
    area_code           TEXT NOT NULL REFERENCES ref_area(area_code),
    PRIMARY KEY (city, state)
);

-- FIX 1 discovery (2026-07-19, Gazetteer batch): some sources (Adzuna
-- especially) give a bare COUNTY name as the location instead of a city --
-- "Contra Costa, CA", "Broward, FL", "Wake, NC". Built from Geography.csv's
-- CountyTownName column (same OFLC wage zip already used for ref_area;
-- load_ref_area discarded this column after using it only to dedupe Area
-- rows). Verified 1:1 -- zero counties span multiple Areas in this file.
CREATE TABLE ref_county_to_area (
    county              TEXT NOT NULL,   -- normalized upper-case, includes "COUNTY" suffix
    state               TEXT NOT NULL,   -- 2-letter
    area_code           TEXT NOT NULL REFERENCES ref_area(area_code),
    PRIMARY KEY (county, state)
);

-- FIX 1 (2026-07-19, Gazetteer batch): Census Gazetteer Places, resolved to an
-- OFLC area via nearest-ZCTA-centroid (no geocoding API -- pure local
-- Haversine distance between two downloaded government coordinate sets: the
-- Census Places gazetteer's own place centroid vs the Census ZCTA gazetteer's
-- own ZCTA centroids, the latter then looked up in the already-loaded
-- ref_zip_to_area). Bottom of the fallback chain -- only reached when
-- ref_city_to_area, the OFLC substring match, AND ref_county_to_area all miss.
CREATE TABLE ref_gazetteer_place_to_area (
    place               TEXT NOT NULL,   -- normalized upper-case, LSAD suffix stripped
    state               TEXT NOT NULL,   -- 2-letter
    area_code           TEXT NOT NULL REFERENCES ref_area(area_code),
    nearest_zcta         TEXT,           -- audit trail: which ZCTA centroid won
    distance_km          NUMERIC,        -- audit trail: how far away it was
    PRIMARY KEY (place, state)
);

-- THE table for lottery prediction. Pull OFLC's published four-level wage files
-- (FLC Data Center / OFLC wage data), NOT raw OEWS percentiles — OFLC already
-- publishes the Level I-IV cutoffs so you don't have to re-derive them.
CREATE TABLE ref_prevailing_wage (
    area_code           TEXT NOT NULL REFERENCES ref_area(area_code),
    soc_code            TEXT NOT NULL REFERENCES ref_soc(soc_code),
    wage_year           TEXT NOT NULL,      -- '2025-2026'
    level_1_annual      NUMERIC,
    level_2_annual      NUMERIC,
    level_3_annual      NUMERIC,
    level_4_annual      NUMERIC,
    PRIMARY KEY (area_code, soc_code, wage_year)
);

CREATE TABLE ref_institution (
    institution_id      BIGINT PRIMARY KEY,
    -- IPEDS | NSF_FFRDC | IRS_BMF_RESEARCH | IRS_BMF_HOSPITAL | COTH (unused --
    -- no clean free COTH source; teaching hospitals approximated via
    -- IRS_BMF_HOSPITAL instead, see KNOWN_ISSUES.md). The two IRS_BMF_*
    -- sub-tags (added 2026-07-19, FIX 1) let match_registry()'s
    -- WHERE source = ? filter emit two DIFFERENT evidence_types
    -- (irs_research_nonprofit vs coth_match) from the same raw IRS extract.
    source              TEXT NOT NULL,
    source_key          TEXT NOT NULL,      -- UNITID, EIN, etc.
    name                TEXT NOT NULL,
    normalized_name     TEXT NOT NULL,
    ein                 TEXT,               -- when you have it, match on it first
    city                TEXT,
    state               TEXT,
    ntee_code           TEXT,               -- IRS BMF: research orgs live in U/H-codes,
                                            -- hospitals in E20-E22
    exempt_basis        TEXT,               -- 'higher_ed' | 'nonprofit_research'
                                            -- | 'govt_research' | 'affiliated_nonprofit'
                                            -- | 'hospital'
    UNIQUE (source, source_key)
);
CREATE INDEX ix_ref_institution_norm ON ref_institution(normalized_name);
CREATE INDEX ix_ref_institution_ein  ON ref_institution(ein);


-- =============================================================================
-- LAYER 2 — ENTITY RESOLUTION
-- =============================================================================
-- The hub. One row per real-world employer you'd actually apply to.
-- Amazon files under Services / AWS / Development Center; those are three
-- aliases pointing at one employer_id. Whether you WANT them merged is a real
-- question — different entities can have genuinely different sponsorship
-- behavior. Start merged, split if the data tells you to.

CREATE TABLE employer (
    employer_id         BIGINT PRIMARY KEY,
    canonical_name      TEXT NOT NULL,
    normalized_name     TEXT NOT NULL,
    website_domain      TEXT,               -- best cross-source join key you'll get
    ein                 TEXT,
    hq_city             TEXT,
    hq_state            TEXT,
    primary_naics       TEXT,
    is_staffing_firm    BOOLEAN DEFAULT FALSE,  -- body-shop flag; see profile rollup
    notes               TEXT,
    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP NOT NULL
);
CREATE INDEX ix_employer_norm   ON employer(normalized_name);
CREATE INDEX ix_employer_domain ON employer(website_domain);

CREATE TABLE employer_alias (
    alias_id            BIGINT PRIMARY KEY,
    employer_id         BIGINT NOT NULL REFERENCES employer(employer_id),
    raw_name            TEXT NOT NULL,
    normalized_name     TEXT NOT NULL,
    source              TEXT NOT NULL,      -- 'LCA' | 'USCIS' | 'PERM' | 'ATS' | 'MANUAL'
    match_method        TEXT NOT NULL,      -- 'exact' | 'normalized' | 'fuzzy'
                                            -- | 'ein' | 'llm' | 'manual'
    confidence          NUMERIC,            -- 0..1
    verified_by_human   BOOLEAN DEFAULT FALSE,  -- once TRUE, resolution never overwrites
    created_at          TIMESTAMP NOT NULL,
    UNIQUE (normalized_name, source)
);
CREATE INDEX ix_alias_employer ON employer_alias(employer_id);

-- Queue for the ones the matcher couldn't call. Work this by hand over coffee;
-- it's the highest-leverage hour you'll spend on the whole project.
CREATE TABLE employer_match_review (
    review_id           BIGINT PRIMARY KEY,
    raw_name            TEXT NOT NULL,
    source              TEXT NOT NULL,
    candidate_ids       JSON,               -- [{employer_id, score, reason}, ...]
    resolved_alias_id   BIGINT REFERENCES employer_alias(alias_id),
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending | resolved | new_employer
    created_at          TIMESTAMP NOT NULL
);


-- =============================================================================
-- LAYER 3 — SPONSORSHIP FACTS
-- =============================================================================
-- Grain: one row per LCA case. Note a case can cover N workers
-- (total_worker_positions) and multiple worksites (Appendix A). Model the
-- primary worksite here; if you need the full worksite list later, add
-- lca_case_worksite as a child table. You probably won't need it.

CREATE TABLE lca_case (
    case_number             TEXT PRIMARY KEY,
    employer_id             BIGINT NOT NULL REFERENCES employer(employer_id),
    fiscal_year             INTEGER NOT NULL,
    case_status             TEXT NOT NULL,
    visa_class              TEXT NOT NULL,
    received_date           DATE,
    decision_date           DATE,
    begin_date              DATE,           -- cap-exempt heuristic input
    end_date                DATE,
    job_title               TEXT,
    soc_code                TEXT REFERENCES ref_soc(soc_code),
    total_worker_positions  INTEGER,
    new_employment          INTEGER,
    continued_employment    INTEGER,
    change_employer         INTEGER,
    worksite_city           TEXT,
    worksite_state          TEXT,
    worksite_postal_code    TEXT,
    area_code               TEXT REFERENCES ref_area(area_code),  -- resolved via zip
    wage_from_annual        NUMERIC,        -- normalized to annual from any unit
    wage_to_annual          NUMERIC,
    prevailing_wage_annual  NUMERIC,
    pw_wage_level           TEXT,           -- LCA determination, kept for reference
    h1b_dependent           BOOLEAN,
    willful_violator        BOOLEAN,
    is_off_cycle            BOOLEAN,        -- begin_date NOT within ~Sep 25 - Oct 8
                                            -- => strong cap-exempt tell
    raw_id                  BIGINT REFERENCES raw_lca(raw_id)
);
CREATE INDEX ix_lca_employer  ON lca_case(employer_id);
CREATE INDEX ix_lca_soc       ON lca_case(soc_code);
CREATE INDEX ix_lca_begin     ON lca_case(begin_date);
CREATE INDEX ix_lca_emp_soc   ON lca_case(employer_id, soc_code);

CREATE TABLE uscis_employer_year (
    employer_id         BIGINT NOT NULL REFERENCES employer(employer_id),
    fiscal_year         INTEGER NOT NULL,
    initial_approval    INTEGER,
    initial_denial      INTEGER,
    continuing_approval INTEGER,
    continuing_denial   INTEGER,
    PRIMARY KEY (employer_id, fiscal_year)
);

CREATE TABLE perm_case (
    case_number         TEXT PRIMARY KEY,
    employer_id         BIGINT NOT NULL REFERENCES employer(employer_id),
    fiscal_year         INTEGER NOT NULL,
    case_status         TEXT,
    decision_date       DATE,
    job_title           TEXT,
    soc_code            TEXT,
    worksite_state      TEXT,
    wage_offer_annual   NUMERIC
);
CREATE INDEX ix_perm_employer ON perm_case(employer_id);


-- =============================================================================
-- LAYER 4 — CAP-EXEMPT DERIVATION
-- =============================================================================
-- There is no official cap-exempt list. You are inferring. Store the evidence
-- and let a view do the scoring, so that when you're wrong you can see why.

CREATE TABLE cap_exempt_evidence (
    evidence_id         BIGINT PRIMARY KEY,
    employer_id         BIGINT NOT NULL REFERENCES employer(employer_id),
    evidence_type       TEXT NOT NULL,
        -- 'ipeds_match'          university/college       -> near-certain
        -- 'ffrdc_match'          national lab             -> near-certain
        -- 'coth_match'           teaching hospital        -> strong
        -- 'irs_research_nonprofit'  501(c)(3) + NTEE U    -> moderate
        -- 'off_cycle_filings'    begin_date histogram     -> moderate, corroborating
        -- 'naics_signal'         NAICS 6113/5417          -> weak
        -- 'manual_confirmed'     you checked              -> overrides everything
    institution_id      BIGINT REFERENCES ref_institution(institution_id),
    match_method        TEXT,
    confidence          NUMERIC NOT NULL,   -- 0..1
    detail              JSON,               -- e.g. {"off_cycle_ratio": 0.87, "n": 143}
    created_at          TIMESTAMP NOT NULL,
    UNIQUE (employer_id, evidence_type, institution_id)
);
CREATE INDEX ix_capex_employer ON cap_exempt_evidence(employer_id);

-- Weighted vote. Manual confirmation wins outright. Tune the weights once you
-- have eyes on real output — do not tune them before.
CREATE VIEW v_cap_exempt_score AS
SELECT
    employer_id,
    MAX(CASE WHEN evidence_type = 'manual_confirmed' THEN 1 ELSE 0 END) AS manually_confirmed,
    MAX(CASE
        WHEN evidence_type IN ('ipeds_match', 'ffrdc_match', 'manual_confirmed')
            THEN confidence * 1.00
        WHEN evidence_type = 'coth_match'              THEN confidence * 0.85
        WHEN evidence_type = 'irs_research_nonprofit'  THEN confidence * 0.60
        WHEN evidence_type = 'off_cycle_filings'       THEN confidence * 0.45
        WHEN evidence_type = 'naics_signal'            THEN confidence * 0.20
        ELSE 0
    END) AS cap_exempt_score,
    -- FIX (off-cycle sort-exclusion batch, 2026-07-19): off_cycle_filings is
    -- weak corroborating-only evidence (CLAUDE.md landmine #7) that was
    -- silently acting as v_ranked_jobs's #1 sort key for ~22% of cap-subject
    -- employers with NO real cap-exempt evidence at all (Microsoft, LinkedIn,
    -- Insight Global, etc.). cap_exempt_score_sort is identical to
    -- cap_exempt_score above, minus the off_cycle_filings branch (falls to
    -- ELSE 0 instead) — off_cycle_filings evidence rows and the DISPLAY
    -- column above are both untouched; only what v_ranked_jobs sorts on changes.
    MAX(CASE
        WHEN evidence_type IN ('ipeds_match', 'ffrdc_match', 'manual_confirmed')
            THEN confidence * 1.00
        WHEN evidence_type = 'coth_match'              THEN confidence * 0.85
        WHEN evidence_type = 'irs_research_nonprofit'  THEN confidence * 0.60
        WHEN evidence_type = 'naics_signal'            THEN confidence * 0.20
        ELSE 0
    END) AS cap_exempt_score_sort,
    COUNT(*) AS evidence_count
FROM cap_exempt_evidence
GROUP BY employer_id;


-- =============================================================================
-- LAYER 5 — EMPLOYER ROLLUP (materialized; rebuild after each DOL quarter)
-- =============================================================================
-- Don't compute this on the fly. You're aggregating ~500k LCA rows/yr and you
-- want sub-second dashboard filters.

CREATE TABLE employer_h1b_profile (
    employer_id             BIGINT PRIMARY KEY REFERENCES employer(employer_id),
    as_of_date              DATE NOT NULL,

    -- volume (filings, NOT people)
    lca_certified_24mo      INTEGER,
    lca_target_soc_24mo     INTEGER,        -- only your AI/ML/DS codes
    distinct_soc_count      INTEGER,
    last_lca_decision_date  DATE,

    -- reality check (people, from USCIS)
    uscis_initial_approvals_3y  INTEGER,
    uscis_denial_rate_3y        NUMERIC,

    -- lottery posture under the FY2027+ weighted rule
    median_offered_wage_target_soc  NUMERIC,
    pct_lca_level_3_plus            NUMERIC,

    -- cap-exempt
    cap_exempt_score        NUMERIC,
    cap_exempt_score_sort   NUMERIC,        -- same as above minus off_cycle_filings;
                                            -- what v_ranked_jobs actually sorts on
    off_cycle_ratio         NUMERIC,        -- share of begin_dates outside the Oct 1 window
    off_cycle_n             INTEGER,        -- guard: ratio is noise below ~10 filings

    -- green card follow-through
    perm_filed_24mo         INTEGER,
    perm_to_lca_ratio       NUMERIC,        -- low ratio = H-1B mill, no I-140 path

    -- risk flags
    h1b_dependent           BOOLEAN,
    willful_violator        BOOLEAN,
    staffing_firm_score     NUMERIC,        -- high LCA volume + low PERM + NAICS 5613
                                            -- + many worksites = body shop
    rebuilt_at              TIMESTAMP NOT NULL
);


-- =============================================================================
-- LAYER 6 — JOBS
-- =============================================================================

CREATE TABLE ats_board (
    board_id            BIGINT PRIMARY KEY,
    employer_id         BIGINT NOT NULL REFERENCES employer(employer_id),
    provider            TEXT NOT NULL,      -- greenhouse | lever | ashby
                                            -- | smartrecruiters | workday | recruitee
    board_token         TEXT NOT NULL,      -- the slug in the API URL
    board_url           TEXT,
    discovered_via      TEXT,               -- 'careers_page' | 'manual' | 'guess'
    is_active           BOOLEAN DEFAULT TRUE,
    last_polled_at      TIMESTAMP,
    last_poll_status    TEXT,               -- ok | 404 | rate_limited | parse_error
    consecutive_failures INTEGER DEFAULT 0, -- back off, then deactivate
    UNIQUE (provider, board_token)
);
CREATE INDEX ix_board_employer ON ats_board(employer_id);

CREATE TABLE job_posting (
    posting_id          BIGINT PRIMARY KEY,
    employer_id         BIGINT NOT NULL REFERENCES employer(employer_id),
    board_id            BIGINT REFERENCES ats_board(board_id),
    external_id         TEXT NOT NULL,      -- ATS's own job id
    title               TEXT NOT NULL,
    description_text    TEXT,
    apply_url           TEXT NOT NULL,

    -- location: the join headache
    location_raw        TEXT,
    city                TEXT,
    state               TEXT,
    postal_code         TEXT,
    area_code           TEXT REFERENCES ref_area(area_code),
    is_remote           BOOLEAN,

    -- SOC is predicted, never given. Track confidence so you can filter on it.
    soc_code_predicted  TEXT REFERENCES ref_soc(soc_code),
    soc_confidence      NUMERIC,
    soc_method          TEXT,               -- 'title_regex' | 'embedding' | 'llm' | 'manual'

    -- seniority (added 2026-07-19, Phase 6 follow-up): a DISPLAY/FILTER axis,
    -- orthogonal to SOC and to wage-level tickets by design — a Data Scientist
    -- title can be any seniority, and a Level-IV-wage role can still be entry
    -- (high-wage metro). Never read by scoring.py or any ranking sort.
    seniority_tier       TEXT,               -- 'entry' | 'mid' | 'senior' | 'staff_plus'
    seniority_method     TEXT,               -- 'regex' | 'llm'
    seniority_confidence NUMERIC,

    -- salary: present only when the law forces it (CO/CA/WA/NY/IL and friends).
    -- Those states are effectively your free training set for inferring the rest.
    salary_min          NUMERIC,
    salary_max          NUMERIC,
    salary_unit         TEXT,               -- year | hour
    salary_source       TEXT,               -- 'posted' | 'inferred_lca'
                                            -- | 'inferred_model' | 'unknown'

    -- sponsorship language scraped from the description
    mentions_no_sponsorship BOOLEAN,        -- 'must be authorized without sponsorship'
    mentions_clearance      BOOLEAN,        -- security clearance = hard no for you
    mentions_sponsorship_ok BOOLEAN,

    -- lifecycle: soft delete only
    first_seen_at       TIMESTAMP NOT NULL,
    last_seen_at        TIMESTAMP NOT NULL,
    closed_at           TIMESTAMP,          -- set when it drops off the board
    raw_json            JSON,
    UNIQUE (board_id, external_id)
);
CREATE INDEX ix_posting_employer ON job_posting(employer_id);
CREATE INDEX ix_posting_open     ON job_posting(closed_at, first_seen_at);
CREATE INDEX ix_posting_soc      ON job_posting(soc_code_predicted);


-- =============================================================================
-- LAYER 7 — SCORING
-- =============================================================================
-- Versioned rows. You will rewrite the weights constantly; keep the history so
-- you can tell whether last week's change actually surfaced better jobs.

CREATE TABLE job_score (
    posting_id          BIGINT NOT NULL REFERENCES job_posting(posting_id),
    scoring_version     TEXT NOT NULL,      -- 'v0.3.1'
    scored_at           TIMESTAMP NOT NULL,

    -- the four things you actually care about
    sponsor_score       NUMERIC,            -- has this employer filed for THIS soc, recently?
    cap_exempt_score    NUMERIC,            -- no lottery at all = the whole ballgame
    wage_level_predicted INTEGER,           -- 1-4, computed from salary vs ref_prevailing_wage
                                            -- for (soc_code_predicted, area_code)
    lottery_tickets     INTEGER,            -- = wage_level_predicted under FY2027+ rule
    fit_score           NUMERIC,            -- title/description vs your profile
    green_card_score    NUMERIC,            -- perm_to_lca_ratio, for the long game
    -- FIX 2 (Gazetteer batch, 2026-07-19): added as a first-class column so
    -- v_ranked_jobs can sort on it (demote-within-tier). Previously computed
    -- only into explanation JSON — no dedicated column existed at all (not
    -- the same bug as employer_h1b_profile.staffing_firm_score, which is a
    -- different, never-wired column this view used to read instead).
    staffing_firm_score NUMERIC,            -- high volume + low PERM + NAICS 5613
                                            -- + many worksites = body shop

    total_score         NUMERIC,
    explanation         JSON,               -- WHY. every subscore's inputs, so you can
                                            -- debug a bad ranking without re-running
    PRIMARY KEY (posting_id, scoring_version)
);
CREATE INDEX ix_score_total ON job_score(scoring_version, total_score DESC);


-- =============================================================================
-- LAYER 8 — PERSONAL / TRACKER
-- =============================================================================

CREATE TABLE application (
    application_id      BIGINT PRIMARY KEY,
    posting_id          BIGINT REFERENCES job_posting(posting_id),
    employer_id         BIGINT NOT NULL REFERENCES employer(employer_id),
    status              TEXT NOT NULL,      -- interested | applied | referred | screen
                                            -- | onsite | offer | rejected | ghosted
    applied_at          TIMESTAMP,
    resume_version      TEXT,
    referral_contact    TEXT,
    sponsorship_confirmed BOOLEAN,          -- what the recruiter ACTUALLY said
    notes               TEXT,
    next_action         TEXT,
    next_action_at      DATE,
    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP NOT NULL
);
CREATE INDEX ix_app_status ON application(status, next_action_at);


-- =============================================================================
-- THE MONEY QUERY — what the whole schema exists to answer
-- =============================================================================
CREATE VIEW v_ranked_jobs AS
SELECT
    e.canonical_name,
    j.title,
    j.location_raw,
    j.apply_url,
    s.total_score,
    s.lottery_tickets,
    p.cap_exempt_score,
    p.lca_target_soc_24mo    AS sponsored_my_soc_2yr,
    p.perm_to_lca_ratio,
    -- FIX 2 (Gazetteer batch, 2026-07-19): was p.staffing_firm_score, an
    -- employer_h1b_profile column that was NEVER wired/populated (same class
    -- of bug as the earlier cap_exempt_score fix — always NULL). job_score's
    -- own staffing_firm_score is the real, per-posting computed value.
    s.staffing_firm_score,
    j.first_seen_at
FROM job_posting j
JOIN employer e             ON e.employer_id = j.employer_id
JOIN job_score s            ON s.posting_id  = j.posting_id
LEFT JOIN employer_h1b_profile p ON p.employer_id = j.employer_id
WHERE j.closed_at IS NULL
  AND j.mentions_no_sponsorship IS NOT TRUE
  AND j.mentions_clearance      IS NOT TRUE
  AND s.scoring_version = 'v0.1.0'
  AND (p.cap_exempt_score > 0.5 OR p.lca_target_soc_24mo > 0)
ORDER BY
    -- FIX (off-cycle sort-exclusion batch, 2026-07-19): sorts on
    -- cap_exempt_score_sort, not the display cap_exempt_score above — see
    -- v_cap_exempt_score's comment. off_cycle_filings no longer buys sort
    -- position, only real cap-exempt evidence does.
    p.cap_exempt_score_sort DESC NULLS LAST,   -- no lottery beats good lottery odds
    s.lottery_tickets  DESC NULLS LAST,   -- then wage level
    s.fit_score        DESC NULLS LAST,   -- then role fit
    -- FIX 2: demote WITHIN tier, never a hard filter — a staffing firm at the
    -- same (cap_exempt, tickets, fit) standing as a non-staffing role sinks
    -- below it, but still appears in the list.
    s.staffing_firm_score ASC NULLS LAST;
