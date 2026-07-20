import re
from dataclasses import dataclass, field

import duckdb
from rapidfuzz import fuzz, process

from h1b_finder.resolve.normalize import normalize_employer_name

FUZZY_AUTOLINK_THRESHOLD = 92
FUZZY_REVIEW_LOW = 85
FUZZY_REVIEW_HIGH = 92

_EIN_DIGITS = re.compile(r"\D")


def clean_ein(raw: str | None) -> str | None:
    """Digit-only EIN, or None if it isn't exactly 9 digits (malformed/blank)."""
    if not raw:
        return None
    digits = _EIN_DIGITS.sub("", raw)
    return digits if len(digits) == 9 else None


@dataclass
class ResolvedEmployer:
    employer_id: int
    canonical_name: str
    normalized_name: str
    ein: str | None
    hq_state: str | None


@dataclass
class AliasAssignment:
    raw_name: str
    employer_id: int
    normalized_name: str
    match_method: str  # ein | normalized | fuzzy | new
    confidence: float
    review_candidate: dict | None = None  # set when this alias also needs human review


@dataclass
class ResolutionResult:
    employers: list[ResolvedEmployer] = field(default_factory=list)
    aliases: list[AliasAssignment] = field(default_factory=list)


def resolve_employers(distinct_employers: list[dict]) -> ResolutionResult:
    """BUILD_PLAN 1.3 cascade, stop at first hit, processed highest-volume first
    so common name variants match against the canonical (biggest) entity.

    distinct_employers: [{raw_name, state, fein, n_cases}, ...]
    """
    ordered = sorted(distinct_employers, key=lambda r: r["n_cases"], reverse=True)

    result = ResolutionResult()
    ein_index: dict[str, int] = {}
    name_index: dict[str, int] = {}
    state_candidates: dict[str, list[str]] = {}  # state -> [normalized_name, ...] (parallel to state_emp_ids)
    state_emp_ids: dict[str, list[int]] = {}
    next_id = 1

    def register(emp: ResolvedEmployer) -> None:
        if emp.ein:
            ein_index[emp.ein] = emp.employer_id
        name_index[emp.normalized_name] = emp.employer_id
        if emp.hq_state:
            state_candidates.setdefault(emp.hq_state, []).append(emp.normalized_name)
            state_emp_ids.setdefault(emp.hq_state, []).append(emp.employer_id)

    for row in ordered:
        raw_name, state, fein = row["raw_name"], row.get("state"), clean_ein(row.get("fein"))
        normalized = normalize_employer_name(raw_name)

        if fein and fein in ein_index:
            emp_id = ein_index[fein]
            result.aliases.append(AliasAssignment(raw_name, emp_id, normalized, "ein", 1.0))
            continue

        if normalized in name_index:
            emp_id = name_index[normalized]
            result.aliases.append(AliasAssignment(raw_name, emp_id, normalized, "normalized", 0.95))
            continue

        best_emp_id, best_score = None, 0.0
        if state and state_candidates.get(state):
            match = process.extractOne(
                normalized, state_candidates[state], scorer=fuzz.token_set_ratio
            )
            if match:
                _, best_score, idx = match
                best_emp_id = state_emp_ids[state][idx]

        if best_score >= FUZZY_AUTOLINK_THRESHOLD:
            result.aliases.append(
                AliasAssignment(raw_name, best_emp_id, normalized, "fuzzy", best_score / 100)
            )
            continue

        # No confident match: this raw name gets its own employer row so its LCA
        # cases have somewhere to live. If it was a near-miss (85-92), also flag
        # it for human review — do not silently auto-merge.
        emp = ResolvedEmployer(next_id, raw_name, normalized, fein, state)
        review = None
        if FUZZY_REVIEW_LOW <= best_score < FUZZY_REVIEW_HIGH:
            review = {
                "candidate_employer_id": best_emp_id,
                "candidate_score": round(best_score, 1),
            }
        result.employers.append(emp)
        result.aliases.append(
            AliasAssignment(raw_name, emp.employer_id, normalized, "new", 1.0, review)
        )
        register(emp)
        next_id += 1

    return result


def resolve_against_existing_employers(
    con: duckdb.DuckDBPyConnection, distinct_employers: list[dict]
) -> tuple[dict[str, int], list[dict], dict]:
    """Same cascade as resolve_employers() (EIN -> exact normalized name ->
    fuzzy >=92, state-scoped) but attributes to the ALREADY-PERSISTED employer
    table instead of minting new employer rows. Used for PERM: this project's
    sponsor universe is built from LCA, so a PERM case that can't be matched to
    an existing employer has no LCA denominator to enrich and is simply
    unusable here — it is dropped, not given a new employer_id.

    distinct_employers: [{raw_name, state, fein, n_cases}, ...]
    Returns (raw_name -> employer_id map for auto-linked rows only,
             review candidates for the 85-92 near-miss band,
             stats: {ein, normalized, fuzzy, review, dropped} counts).
    """
    existing = con.execute(
        "SELECT employer_id, normalized_name, ein, hq_state FROM employer"
    ).fetchall()

    ein_index = {ein: emp_id for emp_id, _, ein, _ in existing if ein}
    name_index = {name: emp_id for emp_id, name, _, _ in existing}
    state_candidates: dict[str, list[str]] = {}
    state_emp_ids: dict[str, list[int]] = {}
    for emp_id, name, _, state in existing:
        if state:
            state_candidates.setdefault(state, []).append(name)
            state_emp_ids.setdefault(state, []).append(emp_id)

    name_to_employer_id: dict[str, int] = {}
    review_candidates: list[dict] = []
    stats = {"ein": 0, "normalized": 0, "fuzzy": 0, "review": 0, "dropped": 0}

    for row in distinct_employers:
        raw_name, state, fein = row["raw_name"], row.get("state"), clean_ein(row.get("fein"))
        normalized = normalize_employer_name(raw_name)
        if not normalized:
            stats["dropped"] += 1
            continue

        if fein and fein in ein_index:
            name_to_employer_id[raw_name] = ein_index[fein]
            stats["ein"] += 1
            continue

        if normalized in name_index:
            name_to_employer_id[raw_name] = name_index[normalized]
            stats["normalized"] += 1
            continue

        best_emp_id, best_score = None, 0.0
        if state and len(normalized.split()) >= 2 and state_candidates.get(state):
            filtered = [
                (n, i) for n, i in zip(state_candidates[state], state_emp_ids[state])
                if len(n.split()) >= 2
            ]
            if filtered:
                filtered_names = [n for n, _ in filtered]
                match = process.extractOne(normalized, filtered_names, scorer=fuzz.token_set_ratio)
                if match:
                    _, best_score, idx = match
                    best_emp_id = filtered[idx][1]

        if best_score >= FUZZY_AUTOLINK_THRESHOLD:
            name_to_employer_id[raw_name] = best_emp_id
            stats["fuzzy"] += 1
        elif FUZZY_REVIEW_LOW <= best_score < FUZZY_REVIEW_HIGH:
            review_candidates.append({
                "raw_name": raw_name, "state": state,
                "candidate_employer_id": best_emp_id, "candidate_score": round(best_score, 1),
            })
            stats["review"] += 1
        else:
            stats["dropped"] += 1

    return name_to_employer_id, review_candidates, stats


def match_existing_employer(
    con: duckdb.DuckDBPyConnection, raw_name: str, state: str | None = None
) -> tuple[int, str, float] | None:
    """Same cascade as resolve_employers(), but against the ALREADY-PERSISTED
    employer table — for attributing an external record (an Adzuna posting's
    company name, say) to one of our known employers rather than bulk-resolving
    a fresh batch. Returns (employer_id, method, confidence) or None if nothing
    clears the bar; callers should not guess past that point (e.g. the
    querying employer_id is not a substitute for a real match — Adzuna's own
    company= filter does its own loose matching and can return a different,
    unrelated company).
    """
    normalized = normalize_employer_name(raw_name)
    if not normalized:
        return None

    row = con.execute(
        "SELECT employer_id FROM employer WHERE normalized_name = ?", [normalized]
    ).fetchone()
    if row:
        return (row[0], "normalized", 0.95)

    # token_set_ratio scores ~100 whenever one name is a token *subset* of the
    # other — a single-token normalized name ("Ocean Corp" -> "ocean") matches
    # almost anything containing that word (see Phase 2's cap-exempt matcher
    # bug). Require at least 2 tokens on both sides before trusting fuzzy.
    if state and len(normalized.split()) >= 2:
        candidates = con.execute(
            "SELECT employer_id, normalized_name FROM employer WHERE hq_state = ?", [state.strip().upper()]
        ).fetchall()
        candidates = [c for c in candidates if len(c[1].split()) >= 2]
        if candidates:
            names = [c[1] for c in candidates]
            match = process.extractOne(normalized, names, scorer=fuzz.token_set_ratio)
            if match and match[1] >= FUZZY_AUTOLINK_THRESHOLD:
                return (candidates[match[2]][0], "fuzzy", match[1] / 100)

    return None
