import json

import duckdb

from h1b_finder.config import Config
from h1b_finder.wage.area_crosswalk import find_area_by_city_state
from h1b_finder.wage.wage_level import compute_level, get_wage_cutoffs

HOURS_PER_YEAR = 2080
NEGATIVE_CASE_STATUSES = ("Denied", "Withdrawn")
RECENCY_HALF_LIFE_DAYS = 365.0

# Staffing-firm normalization ceilings — not "weights" in the CLAUDE.md sense
# (those are the top-level scoring.weights in config.yaml, all 1.0). Structural
# constants for the 4-leg BUILD_PLAN formula (high LCA volume + low PERM +
# NAICS 5613 + many worksites), first-pass and unreviewed like every other
# subscore constant here — a human looks at real output before any of these
# get tuned, same as the top-level weights.
STAFFING_VOLUME_CEILING = 500
STAFFING_WORKSITE_BREADTH_CEILING = 10
STAFFING_PERM_RATIO_CEILING = 0.3  # ratio at/above which "low PERM" stops being a signal

# green_card_score: perm_to_lca_ratio saturates to 1.0 at this ratio (a strong
# green-card-follow-through signal, not a body-shop tell).
GREEN_CARD_RATIO_CEILING = 0.5

# Simple, legible keyword overlap against the profile (CS BS, MS Data Analytics
# Engineering, targeting AI/ML/DS). Saturates — a posting doesn't need to hit
# every keyword to be a strong fit.
FIT_KEYWORDS = [
    "data scientist", "data science", "machine learning", "deep learning",
    "artificial intelligence", " ai ", "ai/ml", "nlp",
    "natural language processing", "computer vision", "data engineer",
    "data analytics", "data analyst", "predictive model", "statistical model",
    "llm", "generative ai", "python", " sql ", "analytics engineer",
    "research scientist",
]
FIT_SATURATION = 4

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR",
}

# FIX 1 discovery: a large share of "no candidate at all" postings weren't a
# Gazetteer problem — they spell the state out in full ("San Francisco,
# California", "Seattle, Washington, United States") and the old regex only
# ever accepted a 2-letter code, so these never even reached the existing
# (already-correct) ref_city_to_area lookup. Purely a parsing fix; no new
# data source needed.
US_STATE_NAME_TO_ABBREV = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC", "puerto rico": "PR",
}


def recency_decay(days_since: float, half_life_days: float = RECENCY_HALF_LIFE_DAYS) -> float:
    """0-1, exponential decay with the given half-life. A filing today scores
    1.0; one half-life ago scores 0.5; three half-lives (~3yr at the default)
    scores 0.125 — 'last quarter >> three years ago' per CLAUDE.md/BUILD_PLAN."""
    days_since = max(0.0, days_since)
    return 0.5 ** (days_since / half_life_days)


def sponsor_score_for(days_since_filing: float | None) -> float | None:
    """None means the employer has never filed for this exact SOC — a real,
    known 0, not a missing-data gap, EXCEPT when the SOC itself is unknown
    (caller passes None with soc_known=False in that case — see run_scoring)."""
    if days_since_filing is None:
        return 0.0
    return round(recency_decay(days_since_filing), 4)


def compute_fit_score(title: str, description: str | None) -> tuple[float, list[str]]:
    text = f"{title or ''} {description or ''}".lower()
    matched = [kw for kw in FIT_KEYWORDS if kw.strip() in text]
    score = min(1.0, len(matched) / FIT_SATURATION)
    return round(score, 4), matched


def compute_staffing_firm_score(
    lca_certified_24mo: int | None,
    distinct_worksite_states: int | None,
    perm_to_lca_ratio: float | None,
    primary_naics: str | None,
) -> tuple[float, dict]:
    """FIX 4: the real 4-leg BUILD_PLAN formula — high LCA volume + low PERM +
    NAICS 5613 + many distinct worksite states — now that PERM (FIX 1) and
    NAICS (FIX 4) are both wired in. Simple unweighted average of the 4
    components, same 'no internal tuning either' posture as the partial
    3-component version this replaces.

    perm_to_lca_ratio is inverted (LOW ratio = HIGH staffing-firm signal) and
    saturates at STAFFING_PERM_RATIO_CEILING. A None ratio (no LCA denominator
    at all — should be rare; every employer here has >=1 lca_case row) is
    treated as neutral (0.5), not evidence either way. primary_naics missing
    is treated as 0 (not confirmed 5613), the conservative default."""
    volume_component = min(1.0, (lca_certified_24mo or 0) / STAFFING_VOLUME_CEILING)
    worksite_component = min(1.0, (distinct_worksite_states or 0) / STAFFING_WORKSITE_BREADTH_CEILING)
    naics_component = 1.0 if (primary_naics or "").startswith("5613") else 0.0
    if perm_to_lca_ratio is None:
        perm_component = 0.5
    else:
        perm_component = max(0.0, 1.0 - min(1.0, perm_to_lca_ratio / STAFFING_PERM_RATIO_CEILING))

    score = round((volume_component + perm_component + naics_component + worksite_component) / 4, 4)
    detail = {
        "lca_certified_24mo": lca_certified_24mo,
        "distinct_worksite_states": distinct_worksite_states,
        "perm_to_lca_ratio": round(perm_to_lca_ratio, 4) if perm_to_lca_ratio is not None else None,
        "primary_naics": primary_naics,
        "volume_component": round(volume_component, 4),
        "perm_component": round(perm_component, 4),
        "naics_component": round(naics_component, 4),
        "worksite_breadth_component": round(worksite_component, 4),
        "naics_signal_available": primary_naics is not None,
        "perm_signal_available": perm_to_lca_ratio is not None,
    }
    return score, detail


def compute_green_card_score(perm_to_lca_ratio: float | None) -> tuple[float | None, dict]:
    """FIX 1: perm_to_lca_ratio, higher is better here (they follow through
    to a green card) — the inverse direction from staffing_firm_score's use
    of the same raw ratio. Saturates at GREEN_CARD_RATIO_CEILING."""
    if perm_to_lca_ratio is None:
        return None, {"value": None, "reason": "no_perm_data_for_employer"}
    score = round(min(1.0, perm_to_lca_ratio / GREEN_CARD_RATIO_CEILING), 4)
    return score, {"value": score, "perm_to_lca_ratio": round(perm_to_lca_ratio, 4)}


def salary_to_annual(salary_min: float | None, salary_max: float | None, salary_unit: str | None) -> float | None:
    values = [float(v) for v in (salary_min, salary_max) if v is not None]
    if not values:
        return None
    avg = sum(values) / len(values)
    if salary_unit == "hour":
        return avg * HOURS_PER_YEAR
    return avg


def parse_state_from_location_raw(location_raw: str | None) -> tuple[str, str] | None:
    """Best-effort 'City, ST[, ...]' extraction for postings where the ATS
    provider never parsed city/state into their own columns (Greenhouse/Lever/
    Ashby all leave those NULL — see phase4-job-ingestion-findings memory).
    Only the first ';'- or '|'-separated location wins on multi-location
    postings. The second comma-segment must be either a real 2-letter state
    code OR a full state name (both accepted — 'San Francisco, California'
    is just as common in raw ATS data as 'San Francisco, CA'); anything else
    ('Dublin, IE', 'Berlin, hybrid', 'Toronto, ON, CA') correctly fails to
    match rather than resolving to a bogus US area. Only looks at the segment
    immediately after the city — 'Toronto, ON, CA' must not treat the trailing
    country-code 'CA' as California."""
    if not location_raw:
        return None
    first = location_raw.split(";")[0].split("|")[0]
    parts = [p.strip() for p in first.split(",")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    city = parts[0]
    token = parts[1].replace(".", "").strip()
    state = token.upper() if token.upper() in US_STATES else US_STATE_NAME_TO_ABBREV.get(token.lower())
    if not state:
        return None
    return city, state


# FIX 3: Adzuna's own location hierarchy is frequently 'Neighborhood, City[,
# County]' with the state never repeated in the string (e.g. 'SoMa, San
# Francisco', 'Old Sixth Ward, Houston') — the first segment is what Adzuna's
# own poller stored as job_posting.city, but it's a neighborhood, not a place
# ref_city_to_area can look up. The second segment usually IS the real city;
# Adzuna already gives a real state via its own `state` column, so this only
# needs the second segment plus a county/parish/borough suffix strip.
_NEIGHBORHOOD_STRIP_SUFFIXES = (" county", " parish", " borough")

# A handful of colloquial place names that don't match their canonical
# USPS/CBSA-crosswalk spelling — narrow, explicit aliases, not a general
# heuristic (a blanket "strip trailing City" would break real places whose
# correct name ends in "City": Foster City, Jersey City, Kansas City are all
# correctly spelled that way in ref_city_to_area already).
CITY_ALIASES = {
    ("new york city", "NY"): "new york",
    ("washington", "DC"): "washington",
    # Manhattan is a NYC borough, not its own incorporated place/CDP — doesn't
    # exist as a distinct entry in the Census Places gazetteer either.
    ("manhattan", "NY"): "new york",
}


def parse_neighborhood_city_from_location_raw(
    location_raw: str | None, known_state: str | None
) -> tuple[str, str] | None:
    """known_state must be validated against real US states — Adzuna's own
    `state` column carries foreign region codes verbatim for non-US postings
    (e.g. 'Karnataka', 'KA', 'NSW', 'BC'), and without this check those would
    silently be treated as valid US state candidates downstream."""
    if not location_raw or not known_state or known_state.strip().upper() not in US_STATES:
        return None
    first = location_raw.split(";")[0]
    parts = [p.strip() for p in first.split(",")]
    if len(parts) < 2 or not parts[1]:
        return None
    candidate = parts[1]
    lowered = candidate.lower()
    for suffix in _NEIGHBORHOOD_STRIP_SUFFIXES:
        if lowered.endswith(suffix):
            candidate = candidate[: -len(suffix)].strip()
            break
    if not candidate:
        return None
    return candidate, known_state.strip().upper()


def _resolve_area_code(
    con: duckdb.DuckDBPyConnection,
    city: str | None,
    state: str | None,
    location_raw: str | None,
    cache: dict[tuple[str, str], str | None],
    postal_code: str | None = None,
) -> str | None:
    """Priority order: real ZIP (most precise, FIX 3) -> city/state as given ->
    location_raw 'City, ST' regex -> the neighborhood/second-segment fallback
    (FIX 3, only fires once the direct city/state candidate has already
    failed). Every candidate is cache-keyed and tried in turn; the first hit
    wins."""
    if postal_code:
        zip_key = ("__zip__", postal_code.strip())
        if zip_key not in cache:
            row = con.execute(
                "SELECT area_code FROM ref_zip_to_area WHERE zip = ?", [postal_code.strip()]
            ).fetchone()
            cache[zip_key] = row[0] if row else None
        if cache[zip_key] is not None:
            return cache[zip_key]

    candidates: list[tuple[str, str]] = []
    if city and state:
        candidates.append((city.strip().lower(), state.strip().upper()))
    else:
        parsed = parse_state_from_location_raw(location_raw)
        if parsed:
            candidates.append((parsed[0].strip().lower(), parsed[1]))

    alt = parse_neighborhood_city_from_location_raw(location_raw, state)
    if alt:
        candidates.append((alt[0].strip().lower(), alt[1]))

    for key in candidates:
        lookup_key = (CITY_ALIASES.get(key, key[0]), key[1])
        if lookup_key in cache:
            if cache[lookup_key] is not None:
                return cache[lookup_key]
            continue
        area_code = find_area_by_city_state(con, lookup_key[0], lookup_key[1])
        cache[lookup_key] = area_code
        if area_code is not None:
            return area_code
    return None


def run_scoring(con: duckdb.DuckDBPyConnection, cfg: Config) -> dict:
    """Populate job_score for every open posting at cfg.scoring.version.
    All weights ship at 1.0 (BUILD_PLAN Phase 5 — do not tune before a human
    has looked at real ranked output). Idempotent: re-running the same
    scoring_version overwrites its rows cleanly.

    As of the FIX 1-4 batch (2026-07-19): green_card_score and
    staffing_firm_score both use real perm_to_lca_ratio (PERM ingestion,
    FIX 1) and staffing_firm_score also uses real primary_naics (FIX 4) — see
    compute_green_card_score / compute_staffing_firm_score. Both can still be
    None/neutral for the rare employer with no LCA denominator or no NAICS on
    file; that remains an honest gap, not a fabricated value.

    wage_level_predicted / lottery_tickets are NULL (not 0) whenever SOC,
    salary, or area can't be resolved for a posting — 0 tickets is reserved
    for a posting we CAN price and it's genuinely below Level I. Area
    resolution now uses the real city->CBSA crosswalk (FIX 3,
    ref_city_to_area) via the fixed find_area_by_city_state().
    """
    version = cfg.scoring.version
    weights = cfg.scoring.weights
    wage_year = cfg.wage.year
    # FIX 1 audit finding: this must anchor on the data's own horizon, not
    # wall-clock "today" — LCA data lags real time (currently maxes out
    # 2026-03-31 while the real date is later), so date.today() overstated
    # every days_since_filing by however far the data has fallen behind,
    # systematically under-scoring sponsor_score's recency decay for every
    # employer. sponsor_score does NOT read lca_certified_24mo directly (it
    # computes its own per-(employer,soc) MAX(received_date) below) — this
    # "today" anchor was the actual bug in this area, not the _24mo column.
    today = con.execute("SELECT MAX(received_date) FROM lca_case").fetchone()[0]

    sponsor_recency: dict[tuple[int, str], float] = {}
    for employer_id, soc_code, last_date in con.execute(
        "SELECT employer_id, soc_code, MAX(received_date) FROM lca_case "
        f"WHERE case_status NOT IN {NEGATIVE_CASE_STATUSES!r} AND soc_code IS NOT NULL "
        "GROUP BY employer_id, soc_code"
    ).fetchall():
        sponsor_recency[(employer_id, soc_code)] = (today - last_date).days

    cap_exempt_by_employer: dict[int, tuple[float, int]] = {
        employer_id: (float(score) if score is not None else None, evidence_count)
        for employer_id, score, evidence_count in con.execute(
            "SELECT employer_id, cap_exempt_score, evidence_count FROM v_cap_exempt_score"
        ).fetchall()
    }

    worksite_breadth_by_employer: dict[int, int] = dict(
        con.execute(
            "SELECT employer_id, COUNT(DISTINCT worksite_state) FROM lca_case GROUP BY employer_id"
        ).fetchall()
    )

    profile_by_employer: dict[int, tuple[int | None, float | None]] = {
        employer_id: (lca_certified_24mo, float(perm_to_lca_ratio) if perm_to_lca_ratio is not None else None)
        for employer_id, lca_certified_24mo, perm_to_lca_ratio in con.execute(
            "SELECT employer_id, lca_certified_24mo, perm_to_lca_ratio FROM employer_h1b_profile"
        ).fetchall()
    }

    naics_by_employer: dict[int, str | None] = dict(
        con.execute("SELECT employer_id, primary_naics FROM employer").fetchall()
    )

    postings = con.execute(
        "SELECT posting_id, employer_id, title, description_text, city, state, location_raw, "
        "postal_code, soc_code_predicted, salary_min, salary_max, salary_unit, salary_source "
        "FROM job_posting WHERE closed_at IS NULL"
    ).fetchall()

    area_cache: dict[tuple[str, str], str | None] = {}
    rows_to_insert: list[tuple] = []
    stats = {
        "total_scored": 0,
        "wage_level_computed": 0,
        "wage_level_null_soc_unknown": 0,
        "wage_level_null_salary_unknown": 0,
        "wage_level_null_area_unresolved": 0,
        "wage_level_null_no_wage_data": 0,
        "sponsor_score_null_soc_unknown": 0,
    }

    for (posting_id, employer_id, title, description, city, state, location_raw, postal_code,
         soc_code, salary_min, salary_max, salary_unit, salary_source) in postings:

        # --- sponsor_score ---
        if soc_code is None:
            sponsor_score = None
            sponsor_detail = {"value": None, "reason": "soc_unknown"}
            stats["sponsor_score_null_soc_unknown"] += 1
        else:
            days_since = sponsor_recency.get((employer_id, soc_code))
            sponsor_score = sponsor_score_for(days_since)
            sponsor_detail = {
                "value": sponsor_score,
                "employer_id": employer_id,
                "soc_code": soc_code,
                "days_since_last_filing": days_since,
                "half_life_days": RECENCY_HALF_LIFE_DAYS,
            }

        # --- cap_exempt_score ---
        cap_score, cap_evidence_n = cap_exempt_by_employer.get(employer_id, (0.0, 0))
        cap_score = cap_score if cap_score is not None else 0.0
        cap_detail = {"value": round(cap_score, 4), "evidence_count": cap_evidence_n}

        # --- wage_level_predicted / lottery_tickets ---
        wage_level = None
        wage_detail: dict = {}
        if soc_code is None:
            wage_detail = {"value": None, "reason": "soc_unknown"}
            stats["wage_level_null_soc_unknown"] += 1
        elif salary_source not in ("posted", "inferred_lca"):
            wage_detail = {"value": None, "reason": "salary_unknown", "salary_source": salary_source}
            stats["wage_level_null_salary_unknown"] += 1
        else:
            salary_annual = salary_to_annual(salary_min, salary_max, salary_unit)
            if salary_annual is None:
                wage_detail = {"value": None, "reason": "salary_unknown", "salary_source": salary_source}
                stats["wage_level_null_salary_unknown"] += 1
            else:
                area_code = _resolve_area_code(con, city, state, location_raw, area_cache, postal_code)
                if area_code is None:
                    wage_detail = {"value": None, "reason": "area_unresolved", "location_raw": location_raw}
                    stats["wage_level_null_area_unresolved"] += 1
                else:
                    cutoffs = get_wage_cutoffs(con, area_code, soc_code, wage_year)
                    if cutoffs is None:
                        wage_detail = {
                            "value": None, "reason": "no_wage_data_for_area_soc",
                            "area_code": area_code, "soc_code": soc_code, "wage_year": wage_year,
                        }
                        stats["wage_level_null_no_wage_data"] += 1
                    else:
                        wage_level = compute_level(salary_annual, cutoffs)
                        wage_detail = {
                            "value": wage_level,
                            "lottery_tickets": wage_level,
                            "area_code": area_code,
                            "soc_code": soc_code,
                            "wage_year": wage_year,
                            "salary_annual_used": round(salary_annual, 2),
                            "salary_source": salary_source,
                            "cutoffs": {k: float(v) for k, v in cutoffs.items()},
                        }
                        stats["wage_level_computed"] += 1

        # --- fit_score ---
        fit_score, matched_keywords = compute_fit_score(title, description)
        fit_detail = {"value": fit_score, "matched_keywords": matched_keywords}

        lca_24mo, perm_ratio = profile_by_employer.get(employer_id, (None, None))
        primary_naics = naics_by_employer.get(employer_id)
        worksite_breadth = worksite_breadth_by_employer.get(employer_id)

        # --- green_card_score ---
        green_card_score, green_card_detail = compute_green_card_score(perm_ratio)

        # --- staffing_firm_score ---
        staffing_score, staffing_detail_body = compute_staffing_firm_score(
            lca_24mo, worksite_breadth, perm_ratio, primary_naics
        )
        staffing_detail = {"value": staffing_score, **staffing_detail_body}

        # --- total_score: simple weighted sum, all weights 1.0 (config.yaml, do not tune) ---
        total_score = round(
            (sponsor_score or 0.0) * weights.get("sponsor", 1.0)
            + cap_score * weights.get("cap_exempt", 1.0)
            + (wage_level / 4.0 if wage_level is not None else 0.0) * weights.get("lottery_tickets", 1.0)
            + fit_score * weights.get("fit", 1.0)
            + (green_card_score or 0.0) * weights.get("green_card", 1.0)
            - staffing_score * weights.get("staffing_firm", 1.0),
            4,
        )

        explanation = {
            "scoring_version": version,
            "sponsor_score": sponsor_detail,
            "cap_exempt_score": cap_detail,
            "wage_level": wage_detail,
            "fit_score": fit_detail,
            "green_card_score": green_card_detail,
            "staffing_firm_score": staffing_detail,
            "total_score": total_score,
            "weights": weights,
        }

        rows_to_insert.append((
            posting_id, version, sponsor_score, cap_score, wage_level, wage_level,
            fit_score, green_card_score, staffing_score, total_score, json.dumps(explanation),
        ))
        stats["total_scored"] += 1

    con.execute("DELETE FROM job_score WHERE scoring_version = ?", [version])
    con.executemany(
        "INSERT INTO job_score (posting_id, scoring_version, sponsor_score, cap_exempt_score, "
        "wage_level_predicted, lottery_tickets, fit_score, green_card_score, staffing_firm_score, "
        "total_score, explanation, scored_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)",
        rows_to_insert,
    )
    return stats
