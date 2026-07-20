import json
from datetime import UTC, datetime

import duckdb
import polars as pl
from rapidfuzz import fuzz, process

FUZZY_THRESHOLD = 95  # token_set_ratio; same-state agreement always required alongside this
MIN_FUZZY_TOKENS = 2  # institution names below this are excluded from the fuzzy pool entirely


def _next_id(con: duckdb.DuckDBPyConnection, table: str, pk: str) -> int:
    return con.execute(f"SELECT COALESCE(MAX({pk}), 0) FROM {table}").fetchone()[0] + 1


def _emit(con, evidence_id, rows) -> int:
    con.executemany(
        "INSERT INTO cap_exempt_evidence "
        "(evidence_id, employer_id, evidence_type, institution_id, match_method, confidence, detail, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def match_registry(con: duckdb.DuckDBPyConnection, source: str, evidence_type: str,
                    require_state: bool = True, enable_fuzzy: bool = True) -> dict[str, int]:
    """Cascade an institution registry (already loaded into ref_institution) against
    every distinct employer: EIN (if the registry has one) -> exact normalized name
    (+ same state) -> fuzzy token_set_ratio >= FUZZY_THRESHOLD (+ same state, blocked
    by state to keep the search tractable). Every employer is in scope — this is
    matched against the full universe, never a volume-filtered subset.

    enable_fuzzy=False skips step 3 entirely. IRS BMF (FIX 1, 2026-07-19) needed
    this: at BMF's scale (15k+ institutions vs IPEDS/FFRDC's few thousand),
    token_set_ratio's subset-match behavior fires on coincidental word overlap
    between totally unrelated entities far more than the single-token guard alone
    catches — e.g. "People.ai Inc." (a real for-profit AI company) scored 100
    against "Partnership on AI to Benefit People and Society" (an unrelated
    nonprofit), and this wasn't an isolated case: a same-state audit of the BMF
    fuzzy tier found 30 of 133 matches were clear for-profit/unrelated-entity
    false positives pushing cap_exempt_score > 0.5. A secondary plain-ratio
    check was tried and didn't cleanly separate true matches (e.g. "Kennedy
    Krieger Institute" vs its own research arm scored LOWER on fuzz.ratio than
    several confirmed false positives) — so per CLAUDE.md's human-in-the-loop
    principle, BMF only trusts EIN + exact-name; fuzzy candidates go to a human
    review list instead of live evidence.
    """
    employers = con.execute(
        "SELECT employer_id, normalized_name, ein, hq_state FROM employer"
    ).pl()
    inst = con.execute(
        "SELECT institution_id, name, normalized_name, ein, state FROM ref_institution WHERE source = ?",
        [source],
    ).pl()

    now = datetime.now(UTC).replace(tzinfo=None)
    evidence_id = _next_id(con, "cap_exempt_evidence", "evidence_id")
    rows = []
    matched_employer_ids: set[int] = set()

    def unmatched(df: pl.DataFrame) -> pl.DataFrame:
        if not matched_employer_ids:
            return df
        return df.filter(~pl.col("employer_id").is_in(list(matched_employer_ids)))

    # 1. EIN (only meaningful when the registry actually carries one, e.g. IPEDS)
    ein_map = {r["ein"]: r for r in inst.filter(pl.col("ein").is_not_null()).iter_rows(named=True)}
    if ein_map:
        for e in employers.filter(pl.col("ein").is_in(list(ein_map))).iter_rows(named=True):
            i = ein_map[e["ein"]]
            rows.append((evidence_id + len(rows), e["employer_id"], evidence_type, i["institution_id"],
                         "ein", 1.0, json.dumps({"institution_name": i["name"]}), now))
            matched_employer_ids.add(e["employer_id"])

    remaining = unmatched(employers)

    # 2. Exact normalized-name match, same state
    name_state_map = {
        (r["normalized_name"], r["state"]): r for r in inst.iter_rows(named=True) if r["state"]
    }
    name_only_map: dict[str, list] = {}
    for r in inst.iter_rows(named=True):
        name_only_map.setdefault(r["normalized_name"], []).append(r)

    for e in remaining.iter_rows(named=True):
        key = (e["normalized_name"], e["hq_state"])
        i = name_state_map.get(key)
        if i is None and not require_state:
            cands = name_only_map.get(e["normalized_name"])
            i = cands[0] if cands else None
        if i:
            rows.append((evidence_id + len(rows), e["employer_id"], evidence_type, i["institution_id"],
                         "normalized", 0.95, json.dumps({"institution_name": i["name"]}), now))
            matched_employer_ids.add(e["employer_id"])

    remaining = unmatched(employers)

    if not enable_fuzzy:
        n = _emit(con, evidence_id, rows)
        return {"evidence_rows": n, "distinct_employers": len(matched_employer_ids)}

    # 3. Fuzzy, blocked by state to keep the search tractable and avoid cross-state
    # coincidental-name false positives (the primary risk with this registry type).
    # token_set_ratio treats a short name as "matched" whenever it's a token subset
    # of the longer one, symmetrically on EITHER side — "RAND Corp." normalizes to
    # the single token "rand" (~100 against "Ingersoll-Rand Industrial"), and just
    # as bad going the other way: "Stryker Corporation" -> "stryker" scores ~100
    # against "...Homer Stryker M.D. School of Medicine" even though Stryker Corp
    # (NYSE: SYK) has nothing to do with that med school's donor namesake.
    # Excluding single-token names from the fuzzy pool on BOTH sides (exact-match
    # is still available to them) closes both directions of that hole.
    fuzzy_inst = inst.filter(pl.col("normalized_name").str.split(" ").list.len() >= MIN_FUZZY_TOKENS)
    remaining = remaining.filter(pl.col("normalized_name").str.split(" ").list.len() >= MIN_FUZZY_TOKENS)
    for state in remaining["hq_state"].unique().drop_nulls().to_list():
        e_block = remaining.filter(pl.col("hq_state") == state)
        i_block = fuzzy_inst.filter(pl.col("state") == state) if require_state else fuzzy_inst
        if e_block.height == 0 or i_block.height == 0:
            continue
        e_names = e_block["normalized_name"].to_list()
        i_names = i_block["normalized_name"].to_list()
        scores = process.cdist(e_names, i_names, scorer=fuzz.token_set_ratio)
        for row_idx, e in enumerate(e_block.iter_rows(named=True)):
            best_idx = scores[row_idx].argmax()
            best_score = float(scores[row_idx][best_idx])
            if best_score >= FUZZY_THRESHOLD:
                i = i_block.row(int(best_idx), named=True)
                rows.append((evidence_id + len(rows), e["employer_id"], evidence_type, i["institution_id"],
                             "fuzzy", round(best_score / 100, 3),
                             json.dumps({"institution_name": i["name"], "fuzzy_score": float(best_score)}), now))
                matched_employer_ids.add(e["employer_id"])

    n = _emit(con, evidence_id, rows)
    return {"evidence_rows": n, "distinct_employers": len(matched_employer_ids)}


def emit_off_cycle_evidence(con: duckdb.DuckDBPyConnection, min_n: int, min_ratio: float) -> int:
    """Corroborating-only signal (weight 0.45 in v_cap_exempt_score) — an employer
    whose LCA filings scatter off the Oct-1 cap-subject cycle. Reuses
    employer_h1b_profile.off_cycle_ratio/off_cycle_n from Phase 1; the n>=10 guard
    is required precisely because plenty of cap-subject employers file off-cycle
    transfers too (CLAUDE.md landmine #7).
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    evidence_id = _next_id(con, "cap_exempt_evidence", "evidence_id")
    candidates = con.execute(
        "SELECT employer_id, off_cycle_ratio, off_cycle_n FROM employer_h1b_profile "
        "WHERE off_cycle_n >= ? AND off_cycle_ratio > ?",
        [min_n, min_ratio],
    ).pl()
    rows = [
        (evidence_id + i, r["employer_id"], "off_cycle_filings", None, "off_cycle_ratio",
         round(float(r["off_cycle_ratio"]), 3),
         json.dumps({"off_cycle_ratio": float(r["off_cycle_ratio"]), "off_cycle_n": r["off_cycle_n"]}), now)
        for i, r in enumerate(candidates.iter_rows(named=True))
    ]
    return _emit(con, evidence_id, rows)
