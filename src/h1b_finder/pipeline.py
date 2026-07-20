import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl

from h1b_finder.config import Config
from h1b_finder.db import LCA_RAW_COLUMNS
from h1b_finder.ingest.dol_lca import load_lca_files
from h1b_finder.ingest.dol_perm import PERM_RAW_COLUMNS, load_perm_files
from h1b_finder.resolve.matcher import resolve_against_existing_employers, resolve_employers
from h1b_finder.resolve.wage import annualize_wage

WAGE_MIN_ANNUAL = 15_000
WAGE_MAX_ANNUAL = 2_000_000


def find_lca_files(cfg: Config) -> list[Path]:
    # Both naming variants exist on disk ("Disclosure" and the observed typo
    # "Dislclosure") — glob loosely and parse FY/Q from the filename, not the prefix.
    return sorted(cfg.raw_dir.glob("LCA_Dis*Data_FY*.xlsx"))


def ingest(cfg: Config, con: duckdb.DuckDBPyConnection) -> dict:
    return load_lca_files(con, find_lca_files(cfg))


def cumulative_overlap_report(con: duckdb.DuckDBPyConnection) -> dict:
    """Per fiscal year, does the max-quarter file contain every case_number seen
    in earlier quarters of that same year? Tests the "quarterly files are
    cumulative within a FY" assumption the per-case dedupe relies on."""
    fy_max_q = con.execute(
        "SELECT fiscal_year, MAX(fiscal_quarter) AS max_q FROM raw_lca GROUP BY fiscal_year"
    ).pl()
    report = {}
    for row in fy_max_q.iter_rows(named=True):
        fy, max_q = row["fiscal_year"], row["max_q"]
        quarters = (
            con.execute(
                "SELECT DISTINCT fiscal_quarter FROM raw_lca WHERE fiscal_year = ? ORDER BY 1",
                [fy],
            )
            .pl()["fiscal_quarter"]
            .to_list()
        )
        max_set = set(
            con.execute(
                "SELECT DISTINCT CASE_NUMBER FROM raw_lca WHERE fiscal_year=? AND fiscal_quarter=?",
                [fy, max_q],
            ).pl()["CASE_NUMBER"]
        )
        for q in quarters:
            if q == max_q:
                continue
            q_set = set(
                con.execute(
                    "SELECT DISTINCT CASE_NUMBER FROM raw_lca WHERE fiscal_year=? AND fiscal_quarter=?",
                    [fy, q],
                ).pl()["CASE_NUMBER"]
            )
            contained = len(q_set & max_set)
            report[f"FY{fy}_Q{q}_in_Q{max_q}"] = {
                "q_case_count": len(q_set),
                "contained_in_max_q": contained,
                "pct_contained": round(100 * contained / len(q_set), 2) if q_set else None,
            }
    return report


def clean_cases_df(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """One row per CASE_NUMBER: the most recent (fiscal_year, fiscal_quarter,
    loaded_at) version, since DOL restates rows in later quarterly releases."""
    cols = ", ".join(f'"{c}"' for c in LCA_RAW_COLUMNS)
    return con.execute(f"""
        SELECT raw_id, fiscal_year, {cols}
        FROM raw_lca
        QUALIFY row_number() OVER (
            PARTITION BY CASE_NUMBER
            ORDER BY fiscal_year DESC, fiscal_quarter DESC, loaded_at DESC
        ) = 1
    """).pl()


def distinct_employers(clean: pl.DataFrame) -> list[dict]:
    agg = clean.group_by("EMPLOYER_NAME").agg(
        pl.col("EMPLOYER_STATE").mode().first().alias("state"),
        pl.col("EMPLOYER_FEIN").mode().first().alias("fein"),
        pl.len().alias("n_cases"),
    )
    return [
        {"raw_name": r["EMPLOYER_NAME"], "state": r["state"], "fein": r["fein"], "n_cases": r["n_cases"]}
        for r in agg.iter_rows(named=True)
    ]


def write_entity_resolution(con: duckdb.DuckDBPyConnection, result) -> dict[str, int]:
    """Writes employer / employer_alias / employer_match_review. Returns the
    raw_name -> employer_id map used to join lca_case, plus review-queue stats.

    employer_alias has a UNIQUE(normalized_name, source) constraint — two raw
    spellings that normalize identically (e.g. an all-caps duplicate) resolve to
    the same employer via the 'normalized' cascade step and must collapse to a
    single alias row, not two.
    """
    now = datetime.now(UTC).replace(tzinfo=None)

    con.executemany(
        "INSERT INTO employer (employer_id, canonical_name, normalized_name, ein, hq_state, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (e.employer_id, e.canonical_name, e.normalized_name, e.ein, e.hq_state, now, now)
            for e in result.employers
        ],
    )

    alias_map: dict[str, int] = {}
    seen_normalized: set[str] = set()
    alias_rows, review_rows = [], []
    for a in result.aliases:
        alias_map[a.raw_name] = a.employer_id
        if a.normalized_name in seen_normalized:
            continue
        seen_normalized.add(a.normalized_name)
        alias_rows.append(
            (len(alias_rows) + 1, a.employer_id, a.raw_name, a.normalized_name, "LCA",
             a.match_method, a.confidence, False, now)
        )
        if a.review_candidate:
            review_rows.append(
                (len(review_rows) + 1, a.raw_name, "LCA", json.dumps([a.review_candidate]),
                 None, "pending", now)
            )

    con.executemany(
        "INSERT INTO employer_alias "
        "(alias_id, employer_id, raw_name, normalized_name, source, match_method, confidence, verified_by_human, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        alias_rows,
    )
    con.executemany(
        "INSERT INTO employer_match_review "
        "(review_id, raw_name, source, candidate_ids, resolved_alias_id, status, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        review_rows,
    )
    return {
        "alias_map": alias_map,
        "n_employers": len(result.employers),
        "n_distinct_raw_names": len(result.aliases),
        "n_review_queue": len(review_rows),
    }


def build_lca_case_frame(clean: pl.DataFrame, alias_map: dict[str, int], cfg: Config) -> tuple[pl.DataFrame, int]:
    df = clean.with_columns(
        pl.col("EMPLOYER_NAME").replace_strict(alias_map, default=None).alias("employer_id"),
        pl.col("SOC_CODE").str.extract(r"^(\d{2}-\d{4})", 1).alias("soc_code"),
        pl.col("RECEIVED_DATE").str.slice(0, 10).str.to_date(strict=False).alias("received_date"),
        pl.col("DECISION_DATE").str.slice(0, 10).str.to_date(strict=False).alias("decision_date"),
        pl.col("BEGIN_DATE").str.slice(0, 10).str.to_date(strict=False).alias("begin_date"),
        pl.col("END_DATE").str.slice(0, 10).str.to_date(strict=False).alias("end_date"),
        pl.col("TOTAL_WORKER_POSITIONS").cast(pl.Int64, strict=False),
        pl.col("NEW_EMPLOYMENT").cast(pl.Int64, strict=False),
        pl.col("CONTINUED_EMPLOYMENT").cast(pl.Int64, strict=False),
        pl.col("CHANGE_EMPLOYER").cast(pl.Int64, strict=False),
        pl.col("WAGE_RATE_OF_PAY_FROM").cast(pl.Float64, strict=False),
        pl.col("WAGE_RATE_OF_PAY_TO").cast(pl.Float64, strict=False),
        pl.col("PREVAILING_WAGE").cast(pl.Float64, strict=False),
        (pl.col("H_1B_DEPENDENT") == "Yes").alias("h1b_dependent"),
        (pl.col("WILLFUL_VIOLATOR") == "Yes").alias("willful_violator"),
    )

    df = df.with_columns(
        pl.struct(["WAGE_RATE_OF_PAY_FROM", "WAGE_UNIT_OF_PAY"])
        .map_elements(
            lambda s: annualize_wage(s["WAGE_RATE_OF_PAY_FROM"], s["WAGE_UNIT_OF_PAY"]),
            return_dtype=pl.Float64,
        )
        .alias("wage_from_annual"),
        pl.struct(["WAGE_RATE_OF_PAY_TO", "WAGE_UNIT_OF_PAY"])
        .map_elements(
            lambda s: annualize_wage(s["WAGE_RATE_OF_PAY_TO"], s["WAGE_UNIT_OF_PAY"]),
            return_dtype=pl.Float64,
        )
        .alias("wage_to_annual"),
        pl.struct(["PREVAILING_WAGE", "PW_UNIT_OF_PAY"])
        .map_elements(
            lambda s: annualize_wage(s["PREVAILING_WAGE"], s["PW_UNIT_OF_PAY"]),
            return_dtype=pl.Float64,
        )
        .alias("prevailing_wage_annual"),
    )

    # CLAUDE.md landmine #2: a single missed unit conversion poisons the wage-level
    # engine. Assert the bound; anything outside it is bad input data, not ours to
    # keep — null it out rather than let it silently corrupt medians downstream.
    bad_wage = df.filter(
        pl.col("wage_from_annual").is_not_null()
        & ((pl.col("wage_from_annual") < WAGE_MIN_ANNUAL) | (pl.col("wage_from_annual") > WAGE_MAX_ANNUAL))
    ).height
    for col in ("wage_from_annual", "wage_to_annual"):
        df = df.with_columns(
            pl.when(
                pl.col(col).is_not_null() & ((pl.col(col) < WAGE_MIN_ANNUAL) | (pl.col(col) > WAGE_MAX_ANNUAL))
            )
            .then(None)
            .otherwise(pl.col(col))
            .alias(col)
        )

    window_start, window_end = cfg.capexempt.off_cycle_window
    df = df.with_columns(
        pl.col("begin_date").dt.strftime("%m-%d").alias("_begin_mmdd"),
    ).with_columns(
        pl.when(pl.col("begin_date").is_null())
        .then(None)
        .otherwise(
            ~pl.col("_begin_mmdd").is_between(pl.lit(window_start), pl.lit(window_end))
        )
        .alias("is_off_cycle")
    ).drop("_begin_mmdd")

    # DuckDB matches unquoted identifiers case-insensitively: registering this frame
    # with both "SOC_CODE" (raw, dotted O*NET suffix) and "soc_code" (cleaned) present
    # is ambiguous and silently prefers the raw column. Drop every raw source column
    # shadowed by a same-named-but-cased derived column before this frame is queried.
    df = df.drop(["SOC_CODE", "RECEIVED_DATE", "DECISION_DATE", "BEGIN_DATE", "END_DATE", "WILLFUL_VIOLATOR"])

    return df, bad_wage


def populate_ref_soc(con: duckdb.DuckDBPyConnection, df: pl.DataFrame, cfg: Config) -> int:
    """ref_soc ships pre-seeded with only the target SOC shortlist (schema.sql).
    lca_case.soc_code has an FK to it, and real filings cover hundreds of SOC
    codes — add every code actually observed, flagging is_target from config."""
    target = set(cfg.target_soc_codes)
    socs = (
        df.filter(pl.col("soc_code").is_not_null())
        .group_by("soc_code")
        .agg(pl.col("SOC_TITLE").mode().first().alias("soc_title"))
    )
    existing = {r[0] for r in con.execute("SELECT soc_code FROM ref_soc").fetchall()}
    rows = [
        (r["soc_code"], r["soc_title"] or r["soc_code"], r["soc_code"] in target)
        for r in socs.iter_rows(named=True)
        if r["soc_code"] not in existing
    ]
    con.executemany("INSERT INTO ref_soc (soc_code, soc_title, is_target) VALUES (?,?,?)", rows)
    return len(rows)


def insert_lca_case(con: duckdb.DuckDBPyConnection, df: pl.DataFrame) -> None:
    con.register("lca_case_stage", df)
    con.execute("""
        INSERT INTO lca_case (
            case_number, employer_id, fiscal_year, case_status, visa_class,
            received_date, decision_date, begin_date, end_date, job_title, soc_code,
            total_worker_positions, new_employment, continued_employment, change_employer,
            worksite_city, worksite_state, worksite_postal_code, area_code,
            wage_from_annual, wage_to_annual, prevailing_wage_annual, pw_wage_level,
            h1b_dependent, willful_violator, is_off_cycle, raw_id
        )
        SELECT
            CASE_NUMBER, employer_id, fiscal_year, CASE_STATUS, VISA_CLASS,
            received_date, decision_date, begin_date, end_date, JOB_TITLE, soc_code,
            total_worker_positions, new_employment, continued_employment, change_employer,
            WORKSITE_CITY, WORKSITE_STATE, WORKSITE_POSTAL_CODE, NULL,
            wage_from_annual, wage_to_annual, prevailing_wage_annual, PW_WAGE_LEVEL,
            h1b_dependent, willful_violator, is_off_cycle, raw_id
        FROM lca_case_stage
    """)
    con.unregister("lca_case_stage")


def _sql_list(values: list[str]) -> str:
    escaped = [v.replace("'", "''") for v in values]
    return "(" + ",".join(f"'{v}'" for v in escaped) + ")"


# =============================================================================
# PERM (green-card-follow-through signal) — FIX 1
# =============================================================================

def find_perm_files(cfg: Config) -> list[Path]:
    return sorted(cfg.raw_dir.glob("PERM_Disclosure_Data_FY*.xlsx"))


def ingest_perm(cfg: Config, con: duckdb.DuckDBPyConnection) -> dict:
    return load_perm_files(con, find_perm_files(cfg))


def clean_perm_df(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """One row per CASE_NUMBER: latest (fiscal_year, loaded_at) version, same
    restatement-across-releases handling as clean_cases_df for LCA."""
    cols = ", ".join(f'"{c}"' for c in PERM_RAW_COLUMNS)
    return con.execute(f"""
        SELECT raw_id, fiscal_year, {cols}
        FROM raw_perm
        QUALIFY row_number() OVER (
            PARTITION BY CASE_NUMBER
            ORDER BY fiscal_year DESC, loaded_at DESC
        ) = 1
    """).pl()


def distinct_perm_employers(clean: pl.DataFrame) -> list[dict]:
    agg = clean.group_by("EMP_BUSINESS_NAME").agg(
        pl.col("EMP_STATE").mode().first().alias("state"),
        pl.col("EMP_FEIN").mode().first().alias("fein"),
        pl.len().alias("n_cases"),
    )
    return [
        {"raw_name": r["EMP_BUSINESS_NAME"], "state": r["state"], "fein": r["fein"], "n_cases": r["n_cases"]}
        for r in agg.iter_rows(named=True)
    ]


def build_perm_case_frame(clean: pl.DataFrame, name_to_employer_id: dict[str, int]) -> tuple[pl.DataFrame, int]:
    """Attributes each PERM case to an existing employer_id (never mints a new
    one — a case that can't be matched has no LCA denominator to enrich).
    PWD_SOC_CODE carries an O*NET decimal suffix ('15-2051.00'); strip it to
    match ref_soc's plain 'NN-NNNN' shape."""
    df = clean.with_columns(
        pl.col("EMP_BUSINESS_NAME").replace_strict(name_to_employer_id, default=None).alias("employer_id"),
        pl.col("PWD_SOC_CODE").str.extract(r"^(\d{2}-\d{4})", 1).alias("soc_code"),
        pl.col("DECISION_DATE").str.slice(0, 10).str.to_date(strict=False).alias("decision_date"),
    )
    dropped = df.filter(pl.col("employer_id").is_null()).height
    df = df.filter(pl.col("employer_id").is_not_null())
    df = df.select(
        pl.col("CASE_NUMBER").alias("case_number"),
        pl.col("employer_id").cast(pl.Int64),
        "fiscal_year",
        pl.col("CASE_STATUS").alias("case_status"),
        "decision_date",
        pl.col("JOB_TITLE").alias("job_title"),
        "soc_code",
        pl.col("PRIMARY_WORKSITE_STATE").alias("worksite_state"),
    )
    return df, dropped


def insert_perm_case(con: duckdb.DuckDBPyConnection, df: pl.DataFrame) -> None:
    con.execute("DELETE FROM perm_case")
    con.register("perm_case_stage", df)
    con.execute("""
        INSERT INTO perm_case (case_number, employer_id, fiscal_year, case_status,
                                decision_date, job_title, soc_code, worksite_state)
        SELECT case_number, employer_id, fiscal_year, case_status, decision_date,
               job_title, soc_code, worksite_state
        FROM perm_case_stage
    """)
    con.unregister("perm_case_stage")


def build_perm_case(con: duckdb.DuckDBPyConnection) -> dict:
    """Full PERM pipeline: dedupe raw_perm, resolve employers against the
    ALREADY-PERSISTED employer table (same EIN -> normalized-name -> fuzzy>=92
    cascade as Phase 1's resolve_employers, via resolve_against_existing_employers),
    populate perm_case. Never creates new employers — this project's universe
    is built from LCA, so a PERM case with no LCA-derived employer match has no
    denominator to enrich perm_to_lca_ratio with and is dropped, not invented."""
    clean = clean_perm_df(con)
    distinct = distinct_perm_employers(clean)
    name_to_employer_id, review_candidates, resolve_stats = resolve_against_existing_employers(con, distinct)

    if review_candidates:
        now = datetime.now(UTC).replace(tzinfo=None)
        next_review_id = con.execute("SELECT COALESCE(MAX(review_id), 0) FROM employer_match_review").fetchone()[0]
        con.executemany(
            "INSERT INTO employer_match_review "
            "(review_id, raw_name, source, candidate_ids, resolved_alias_id, status, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (next_review_id + i + 1, r["raw_name"], "PERM",
                 json.dumps([{"candidate_employer_id": r["candidate_employer_id"], "candidate_score": r["candidate_score"]}]),
                 None, "pending", now)
                for i, r in enumerate(review_candidates)
            ],
        )

    df, dropped_cases = build_perm_case_frame(clean, name_to_employer_id)
    insert_perm_case(con, df)

    return {
        "clean_case_count": clean.height,
        "distinct_employers": len(distinct),
        "resolved_ein": resolve_stats["ein"],
        "resolved_normalized": resolve_stats["normalized"],
        "resolved_fuzzy": resolve_stats["fuzzy"],
        "flagged_for_review": resolve_stats["review"],
        "dropped_no_match": resolve_stats["dropped"],
        "perm_case_rows_inserted": df.height,
        "perm_case_rows_dropped_unresolved_employer": dropped_cases,
    }


def populate_employer_naics(con: duckdb.DuckDBPyConnection) -> int:
    """FIX 4: NAICS was always in raw_lca (NAICS_CODE), just never connected to
    employer.primary_naics. Most-common (mode) NAICS code per employer across
    their LCA filings — some employers report slightly inconsistent codes
    across filings, mode picks the representative one."""
    con.execute("""
        UPDATE employer e
        SET primary_naics = sub.naics
        FROM (
            SELECT lc.employer_id, mode(r.NAICS_CODE) AS naics
            FROM lca_case lc
            JOIN raw_lca r ON r.raw_id = lc.raw_id
            WHERE r.NAICS_CODE IS NOT NULL
            GROUP BY lc.employer_id
        ) sub
        WHERE sub.employer_id = e.employer_id
    """)
    return con.execute("SELECT COUNT(*) FROM employer WHERE primary_naics IS NOT NULL").fetchone()[0]


# =============================================================================
# Wage coverage part B: real area resolution — FIX 3
# =============================================================================

def backfill_lca_case_area_codes(con: duckdb.DuckDBPyConnection) -> dict:
    """lca_case.area_code has been hardcoded NULL since insert_lca_case() (the
    'resolved via zip' schema.sql comment was aspirational, never implemented).
    worksite_city/worksite_state are 100% populated, so resolve once per
    distinct pair (a few thousand, not 787k) via the now-fixed
    find_area_by_city_state(), then bulk-apply."""
    from h1b_finder.wage.area_crosswalk import find_area_by_city_state

    pairs = con.execute(
        "SELECT DISTINCT worksite_city, worksite_state FROM lca_case "
        "WHERE worksite_city IS NOT NULL AND worksite_state IS NOT NULL"
    ).fetchall()
    resolved_pairs = [
        (city, state, area_code)
        for city, state in pairs
        if (area_code := find_area_by_city_state(con, city, state)) is not None
    ]

    df = pl.DataFrame(resolved_pairs, schema=["worksite_city", "worksite_state", "area_code"], orient="row")
    con.register("lca_area_map_stage", df)
    con.execute("""
        UPDATE lca_case l
        SET area_code = m.area_code
        FROM lca_area_map_stage m
        WHERE l.worksite_city = m.worksite_city AND l.worksite_state = m.worksite_state
    """)
    con.unregister("lca_area_map_stage")
    return {"distinct_pairs": len(pairs), "resolved_pairs": len(resolved_pairs)}


def backfill_job_posting_area_codes(con: duckdb.DuckDBPyConnection) -> dict:
    """job_posting.area_code has always been NULL (never resolved by any
    poller). Reuses scoring.py's own city/state-or-location_raw resolution
    (the same logic run_scoring() applies transiently) but persists it, and
    now benefits from the fixed find_area_by_city_state() (FIX 3).

    job_posting is referenced by job_score's FK, and DuckDB rejects ANY update
    to a table that has referencing rows in another table via FK — even on an
    unrelated column, even row-by-row — per DuckDB's current FK-enforcement
    limitations. job_score is fully regenerated by the next run_scoring() call
    regardless, so clearing it first (rather than touching schema/constraints)
    is the safe, no-side-effect way to unblock this update.
    """
    from h1b_finder.scoring import _resolve_area_code

    rows = con.execute("SELECT posting_id, city, state, location_raw, postal_code FROM job_posting").fetchall()
    cache: dict[tuple[str, str], str | None] = {}
    resolved = [
        (area_code, posting_id)
        for posting_id, city, state, location_raw, postal_code in rows
        if (area_code := _resolve_area_code(con, city, state, location_raw, cache, postal_code)) is not None
    ]

    con.execute("DELETE FROM job_score")
    con.executemany("UPDATE job_posting SET area_code = ? WHERE posting_id = ?", resolved)
    return {"total_postings": len(rows), "resolved": len(resolved)}


def extract_postal_code_from_raw_json(provider: str | None, raw_json: str | None) -> str | None:
    """FIX 3 part A: only SmartRecruiters' payload (location.postalCode)
    actually carries a structured postal code among the 4 providers this
    project polls. Verified by direct inspection: Greenhouse (location.name is
    free text only), Lever (categories.location is free text only), and Adzuna
    (raw_json is never stored at all — see manifest note) all structurally
    lack one. Real coverage even within SmartRecruiters is partial (only set
    when the employer filled in that field)."""
    if provider != "smartrecruiters" or not raw_json:
        return None
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return None
    return data.get("location", {}).get("postalCode") or None


def backfill_job_posting_postal_codes(con: duckdb.DuckDBPyConnection) -> dict:
    """Populate job_posting.postal_code from raw_json where the ATS payload
    actually has one (SmartRecruiters only — see extract_postal_code_from_raw_json).
    Same FK-workaround as backfill_job_posting_area_codes."""
    rows = con.execute("""
        SELECT j.posting_id, b.provider, j.raw_json
        FROM job_posting j LEFT JOIN ats_board b ON b.board_id = j.board_id
        WHERE j.postal_code IS NULL
    """).fetchall()
    resolved = [
        (postal_code, posting_id)
        for posting_id, provider, raw_json in rows
        if (postal_code := extract_postal_code_from_raw_json(provider, raw_json)) is not None
    ]

    con.execute("DELETE FROM job_score")
    con.executemany("UPDATE job_posting SET postal_code = ? WHERE posting_id = ?", resolved)
    return {"total_checked": len(rows), "resolved": len(resolved)}


def build_employer_profile(con: duckdb.DuckDBPyConnection, cfg: Config) -> None:
    positive = _sql_list(cfg.ingest.case_statuses_positive)
    target_soc = _sql_list(cfg.target_soc_codes)
    # Made idempotent this batch: PERM ingestion needs a second rebuild (to
    # pick up perm_to_lca_ratio) in the same run as the LCA-only rebuild, and
    # this INSERT has no de-dup logic of its own.
    con.execute("DELETE FROM employer_h1b_profile")
    con.execute(f"""
        WITH ref_date AS (
            -- FIX 1: anchor the rolling window on the DATA's own horizon, not
            -- wall-clock CURRENT_DATE — LCA data lags real time (currently
            -- maxes out 2026-03-31), so CURRENT_DATE would make the window
            -- narrower than intended and drift further every day the DB
            -- isn't re-ingested.
            SELECT MAX(decision_date) AS max_date FROM lca_case
        ),
        windowed_certified AS (
            -- lca_certified_24mo was previously COUNT(*) with NO date filter
            -- at all (lifetime, despite the column name) — this is the actual
            -- rolling 24-month window by decision_date.
            SELECT l.employer_id, COUNT(*) AS lca_certified_24mo
            FROM lca_case l, ref_date rd
            WHERE l.case_status IN {positive}
              AND l.decision_date >= rd.max_date - INTERVAL 24 MONTH
            GROUP BY l.employer_id
        ),
        perm_agg AS (
            SELECT employer_id, COUNT(*) AS perm_filed_24mo
            FROM perm_case
            GROUP BY employer_id
        )
        INSERT INTO employer_h1b_profile (
            employer_id, as_of_date, lca_certified_24mo, lca_target_soc_24mo,
            distinct_soc_count, last_lca_decision_date, median_offered_wage_target_soc,
            off_cycle_ratio, off_cycle_n, h1b_dependent, willful_violator,
            cap_exempt_score, cap_exempt_score_sort, perm_filed_24mo, perm_to_lca_ratio, rebuilt_at
        )
        SELECT
            l.employer_id,
            CURRENT_DATE,
            COALESCE(MAX(wc.lca_certified_24mo), 0),
            -- lca_target_soc_24mo has the identical "_24mo but actually
            -- lifetime" naming bug — NOT fixed this batch (not in scope; only
            -- lca_certified_24mo was asked for). Flagged in the fix-batch
            -- report, not silently changed here.
            COUNT(*) FILTER (WHERE case_status IN {positive} AND soc_code IN {target_soc}),
            COUNT(DISTINCT soc_code),
            MAX(decision_date),
            MEDIAN(wage_from_annual) FILTER (WHERE case_status IN {positive} AND soc_code IN {target_soc}),
            AVG(CASE WHEN is_off_cycle THEN 1.0 ELSE 0.0 END),
            COUNT(*) FILTER (WHERE is_off_cycle IS NOT NULL),
            BOOL_OR(h1b_dependent),
            BOOL_OR(willful_violator),
            -- was previously never populated here, silently leaving v_ranked_jobs'
            -- cap_exempt_score DESC (the #1 ranking priority) a no-op for every
            -- row; v_cap_exempt_score itself was always correct. See Phase 5 report.
            MAX(vc.cap_exempt_score),
            -- FIX (off-cycle sort-exclusion batch, 2026-07-19): what
            -- v_ranked_jobs actually sorts on now — see v_cap_exempt_score.
            MAX(vc.cap_exempt_score_sort),
            COALESCE(MAX(pa.perm_filed_24mo), 0),
            -- FIX 1: re-derived off the corrected (now genuinely windowed)
            -- lca_certified_24mo denominator, not the old lifetime count.
            CASE WHEN COALESCE(MAX(wc.lca_certified_24mo), 0) > 0
                 THEN COALESCE(MAX(pa.perm_filed_24mo), 0) * 1.0 / MAX(wc.lca_certified_24mo)
                 ELSE NULL END,
            CURRENT_TIMESTAMP
        FROM lca_case l
        LEFT JOIN v_cap_exempt_score vc ON vc.employer_id = l.employer_id
        LEFT JOIN perm_agg pa ON pa.employer_id = l.employer_id
        LEFT JOIN windowed_certified wc ON wc.employer_id = l.employer_id
        GROUP BY l.employer_id
    """)


def export_sponsors_csv(con: duckdb.DuckDBPyConnection, out_path: Path) -> int:
    df = con.execute("""
        SELECT
            e.canonical_name, e.ein, e.hq_state,
            p.lca_target_soc_24mo, p.lca_certified_24mo, p.distinct_soc_count,
            p.median_offered_wage_target_soc, p.off_cycle_ratio, p.off_cycle_n,
            p.last_lca_decision_date, p.h1b_dependent, p.willful_violator
        FROM employer_h1b_profile p
        JOIN employer e ON e.employer_id = p.employer_id
        WHERE p.lca_target_soc_24mo > 0
        ORDER BY p.lca_target_soc_24mo DESC, p.lca_certified_24mo DESC
    """).pl()
    df.write_csv(out_path)
    return df.height


def export_cap_exempt_csv(con: duckdb.DuckDBPyConnection, out_path: Path) -> int:
    """Every employer with any cap-exempt evidence, regardless of target-SOC LCA
    volume — per the Phase 2 universe-construction rule, this is built from the
    registries outward, not filtered to a volume/SOC subset. cap_exempt_score and
    evidence_types are always shown alongside the score; never a bare boolean."""
    df = con.execute("""
        SELECT
            e.canonical_name, e.ein, e.hq_state,
            v.cap_exempt_score, v.evidence_count,
            STRING_AGG(DISTINCT ce.evidence_type, ',' ORDER BY ce.evidence_type) AS evidence_types,
            COALESCE(p.lca_target_soc_24mo, 0) AS lca_target_soc_24mo,
            COALESCE(p.lca_certified_24mo, 0) AS lca_certified_24mo,
            p.off_cycle_ratio, p.off_cycle_n
        FROM v_cap_exempt_score v
        JOIN employer e ON e.employer_id = v.employer_id
        JOIN cap_exempt_evidence ce ON ce.employer_id = v.employer_id
        LEFT JOIN employer_h1b_profile p ON p.employer_id = v.employer_id
        GROUP BY e.canonical_name, e.ein, e.hq_state, v.cap_exempt_score, v.evidence_count,
                 p.lca_target_soc_24mo, p.lca_certified_24mo, p.off_cycle_ratio, p.off_cycle_n
        ORDER BY v.cap_exempt_score DESC, lca_target_soc_24mo DESC
    """).pl()
    df.write_csv(out_path)
    return df.height
