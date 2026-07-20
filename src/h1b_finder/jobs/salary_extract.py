import re

import duckdb

_MONEY = r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s?([kK])?"
_RANGE = re.compile(rf"{_MONEY}\s*(?:-|to|–|—)\s*{_MONEY}")
_HOURLY_CONTEXT = re.compile(r"per\s+hour|/\s?hr\b|hourly|an\s+hour", re.I)


def extract_posted_salary(text: str | None) -> tuple[float, float, str] | None:
    """Parse a posted salary range out of free text. Pure function — a real
    range needs two dollar amounts joined by a dash/"to"; anything looser is
    too easy to false-positive on (a phone number, a headcount, a year)."""
    if not text:
        return None
    m = _RANGE.search(text)
    if not m:
        return None
    lo_raw, lo_k, hi_raw, hi_k = m.groups()
    lo = float(lo_raw.replace(",", "")) * (1000 if lo_k else 1)
    hi = float(hi_raw.replace(",", "")) * (1000 if hi_k else 1)
    if lo > hi:
        lo, hi = hi, lo

    window = text[max(0, m.start() - 30): m.end() + 30]
    unit = "hour" if _HOURLY_CONTEXT.search(window) else "year"

    # A "$X-$Y" with no k-suffix and no hourly context, both under ~$1,000, is
    # more likely a headcount/phone-number/misc than an unmarked hourly rate —
    # too ambiguous to trust, skip rather than guess.
    if unit == "year" and hi < 1000:
        return None
    return (lo, hi, unit)


def median_wage_for_employer_soc(con: duckdb.DuckDBPyConnection, employer_id: int, soc_code: str) -> float | None:
    """Employer's own historical median LCA wage for this SOC — a genuinely
    good prior (BUILD_PLAN 4e): it's what they actually paid someone in this
    role, not a guess."""
    row = con.execute(
        "SELECT MEDIAN(wage_from_annual) FROM lca_case "
        "WHERE employer_id = ? AND soc_code = ? AND wage_from_annual IS NOT NULL",
        [employer_id, soc_code],
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None
