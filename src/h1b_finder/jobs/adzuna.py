import re
import time

import duckdb
import httpx

from h1b_finder.config import Config, Secrets
from h1b_finder.geo import STATE_ABBR
from h1b_finder.jobs.storage import get_or_create_board, upsert_posting
from h1b_finder.resolve.matcher import match_existing_employer

ADZUNA_SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/us/search/1"

_LEGAL_PREFIXES = re.compile(
    r"^(the\s+)?(trustees?|regents?|board\s+of\s+(trustees|regents))\s+of\s+(the\s+)?",
    re.I,
)
_LEGAL_SUFFIXES = re.compile(
    r",?\s+(inc\.?|llc\.?|l\.l\.c\.?|corp\.?|corporation|ltd\.?|limited|l\.?p\.?|"
    r"l\.?l\.?p\.?|co\.?|company|holdings)\.?\s*$",
    re.I,
)
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")
_DBA_SPLIT = re.compile(r"\bd\s*/?\s*b\s*/?\s*a\b", re.I)


def clean_query_name(name: str) -> str:
    """Adzuna's company= filter 400s outright on a name it doesn't recognize at
    all, rather than returning an empty result — confirmed against the real
    poll run (1,365 errors = exactly 455 employers x 3 role queries, i.e. every
    query for those employers failed identically). Legal-entity formatting is a
    common, fixable cause: "TRUSTEES OF THE UNIVERSITY OF PENNSYLVANIA" 400s,
    "University of Pennsylvania" doesn't. This won't fix everything — some
    employers (WGU Corporation, UT-Battelle) aren't recognized under any
    variant tried — but it's a real, bounded improvement, kept human-readable
    (title-cased) rather than flattened like normalize_employer_name(), since
    it's constructing an API query string, not a join key.
    """
    s = name.strip()
    s = _DBA_SPLIT.split(s)[-1].strip()  # prefer the trade name over the legal name, same as normalize_employer_name()
    s = _PARENTHETICAL.sub("", s)
    s = _LEGAL_PREFIXES.sub("", s)
    s = _LEGAL_SUFFIXES.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.")
    return s or name.strip()


def search(app_id: str, app_key: str, company: str, what: str, results_per_page: int = 20) -> dict:
    """company= is the real employer filter; what= is a loose keyword search over
    title/description. Combining a company name INTO what= gives false zeros —
    confirmed in the Stop 1 sub-spike. Never do that again."""
    params = {
        "app_id": app_id, "app_key": app_key, "company": company, "what": what,
        "results_per_page": results_per_page,
    }
    r = httpx.get(ADZUNA_SEARCH_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def target_employers(con: duckdb.DuckDBPyConnection, cfg: Config) -> list[tuple[int, str, str | None]]:
    """Restricted to the resolved sponsor + cap-exempt sets, per the Phase 0.2
    architecture decision — never the full ~24k sponsor universe (that's an
    unbounded amount of aggregator quota for the long tail's marginal value).
    """
    top_sponsors = con.execute(
        """
        SELECT e.employer_id, e.canonical_name, e.hq_state
        FROM employer_h1b_profile p JOIN employer e ON e.employer_id = p.employer_id
        WHERE p.lca_target_soc_24mo > 0
        ORDER BY p.lca_target_soc_24mo DESC
        LIMIT ?
        """,
        [cfg.jobs.adzuna.top_sponsors_n],
    ).fetchall()

    cap_exempt = con.execute(
        """
        SELECT e.employer_id, e.canonical_name, e.hq_state
        FROM v_cap_exempt_score v
        JOIN employer e ON e.employer_id = v.employer_id
        JOIN employer_h1b_profile p ON p.employer_id = v.employer_id
        WHERE v.cap_exempt_score > ? AND p.lca_target_soc_24mo > 0
        """,
        [cfg.jobs.adzuna.cap_exempt_min_score],
    ).fetchall()

    dedup: dict[int, tuple[str, str | None]] = {}
    for employer_id, name, state in [*top_sponsors, *cap_exempt]:
        dedup[employer_id] = (name, state)
    return [(emp_id, name, state) for emp_id, (name, state) in dedup.items()]


def run_adzuna_poll(con: duckdb.DuckDBPyConnection, cfg: Config, secrets: Secrets) -> dict:
    if not secrets.adzuna_app_id or not secrets.adzuna_app_key:
        raise RuntimeError("Adzuna credentials missing from .env")

    employers = target_employers(con, cfg)
    role_queries = cfg.jobs.adzuna.role_queries
    min_interval = 1.0 / cfg.jobs.adzuna.req_per_sec
    next_call_at = 0.0

    stats = {
        "employers_queried": len(employers), "employers_with_postings": 0,
        "postings_new": 0, "postings_seen_again": 0,
        "join_unresolved": 0, "company_not_recognized_400": 0, "other_http_errors": 0,
    }
    employers_with_hits: set[int] = set()
    unrecognized_employers: set[int] = set()

    for employer_id, canonical_name, hq_state in employers:
        queried_board_id = get_or_create_board(
            con, employer_id, "adzuna", str(employer_id), discovered_via="aggregator"
        )
        seen_external_ids: set[str] = set()
        query_name = clean_query_name(canonical_name)

        for role in role_queries:
            now = time.monotonic()
            if now < next_call_at:
                time.sleep(next_call_at - now)
            next_call_at = time.monotonic() + min_interval

            try:
                data = search(secrets.adzuna_app_id, secrets.adzuna_app_key, query_name, role)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400:
                    stats["company_not_recognized_400"] += 1
                    unrecognized_employers.add(employer_id)
                else:
                    stats["other_http_errors"] += 1
                continue
            except httpx.HTTPError:
                stats["other_http_errors"] += 1
                continue

            for result in data.get("results", []):
                ext_id = str(result.get("id"))
                if not ext_id or ext_id in seen_external_ids:
                    continue
                seen_external_ids.add(ext_id)

                company_name = (result.get("company") or {}).get("display_name", "")
                loc = result.get("location") or {}
                area = loc.get("area") or []
                state_full = area[1] if len(area) > 1 else None
                state_abbr = STATE_ABBR.get(state_full) if state_full else None

                match = match_existing_employer(con, company_name, state_abbr)
                if not match:
                    stats["join_unresolved"] += 1
                    continue
                resolved_employer_id = match[0]

                board_id = (
                    queried_board_id if resolved_employer_id == employer_id
                    else get_or_create_board(con, resolved_employer_id, "adzuna",
                                              str(resolved_employer_id), discovered_via="aggregator")
                )

                is_new = upsert_posting(
                    con, employer_id=resolved_employer_id, board_id=board_id,
                    external_id=ext_id, title=result.get("title") or "",
                    description_text=result.get("description"),
                    apply_url=result.get("redirect_url") or "",
                    location_raw=loc.get("display_name"),
                    city=area[-1] if area else None, state=state_abbr, postal_code=None,
                    is_remote=None,
                    salary_min=result.get("salary_min"), salary_max=result.get("salary_max"),
                    salary_unit="year",
                    raw_json=result,
                )
                if is_new:
                    stats["postings_new"] += 1
                    employers_with_hits.add(resolved_employer_id)
                else:
                    stats["postings_seen_again"] += 1
                    employers_with_hits.add(resolved_employer_id)

    stats["employers_with_postings"] = len(employers_with_hits)
    stats["employers_not_recognized_by_adzuna"] = len(unrecognized_employers)
    return stats
