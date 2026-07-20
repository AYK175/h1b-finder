import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl

from h1b_finder.geo import STATE_ABBR as _STATE_ABBR
from h1b_finder.resolve.normalize import normalize_employer_name

_EIN_DIGITS = re.compile(r"\D")


def clean_ein(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = _EIN_DIGITS.sub("", raw)
    return digits if len(digits) == 9 else None


def _next_id(con: duckdb.DuckDBPyConnection, table: str, pk: str) -> int:
    return con.execute(f"SELECT COALESCE(MAX({pk}), 0) FROM {table}").fetchone()[0] + 1


def load_ipeds(con: duckdb.DuckDBPyConnection, zip_path: Path) -> int:
    """IPEDS HD2024: one row per accredited US postsecondary institution.
    Ships with a clean 9-digit EIN for every row — the primary join key, per
    CLAUDE.md's "EIN is the best join key" rule. Loads raw_institution (full
    payload, for provenance/reparse) and ref_institution (normalized)."""
    with zipfile.ZipFile(zip_path) as zf:
        csv_name = next(n for n in zf.namelist() if n.endswith(".csv"))
        with zf.open(csv_name) as f:
            df = pl.read_csv(f, infer_schema_length=0, encoding="latin1")
    df = df.rename({df.columns[0]: "UNITID"})  # BOM-prefixed first header

    now = datetime.now(UTC).replace(tzinfo=None)
    raw_id = _next_id(con, "raw_institution", "raw_id")
    con.executemany(
        "INSERT INTO raw_institution (raw_id, source, source_file, loaded_at, payload) VALUES (?,?,?,?,?)",
        [
            (raw_id + i, "IPEDS", zip_path.name, now, row_json)
            for i, row_json in enumerate(df.write_ndjson().splitlines())
        ],
    )

    inst_id = _next_id(con, "ref_institution", "institution_id")
    rows = []
    for i, r in enumerate(df.iter_rows(named=True)):
        rows.append((
            inst_id + i, "IPEDS", r["UNITID"], r["INSTNM"],
            normalize_employer_name(r["INSTNM"]), clean_ein(r["EIN"]),
            r["CITY"], r["STABBR"], None, "higher_ed",
        ))
    con.executemany(
        "INSERT INTO ref_institution "
        "(institution_id, source, source_key, name, normalized_name, ein, city, state, ntee_code, exempt_basis) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def load_irs_bmf(con: duckdb.DuckDBPyConnection, csv_paths: list[Path]) -> dict:
    """IRS Exempt Organizations Business Master File (4 regional extracts —
    together they cover the full US + international). FIX 1 (2026-07-19):
    no codebook (Publication 5926) was present in data/raw/ this session —
    verified column meanings directly against the CSVs' own header row
    (EIN, NAME, ..., SUBSECTION, ..., NTEE_CD, SORT_NAME) instead, cross-
    checked against the standard IRS EO BMF / NTEE taxonomy layout, which
    (unlike DOL's LCA/PERM data — CLAUDE.md landmine #1) is a long-stable
    federal format, not one that shifts year to year.

    SUBSECTION '03' = 501(c)(3). NTEE_CD's first character is the NTEE major
    group: 'U' = Science & Technology Research Institutes, 'H' = Medical
    Research — both matched into evidence_type='irs_research_nonprofit'.
    'E20'/'E21'/'E22' = Hospital (General/Specialty/Rehabilitation), within
    major group E (Health Care) — matched separately into
    evidence_type='coth_match', approximating teaching hospitals via BMF
    since no clean free COTH list exists (KNOWN_ISSUES.md's documented
    decision). NTEE is noisy by design (a 'U99' catch-all swept in a
    railroad-heritage trust and a fruit-growers' society during a spot-check)
    — that's exactly why v_cap_exempt_score already discounts
    irs_research_nonprofit at 0.60 (schema.sql, untouched this batch). This
    loader stores the REAL match confidence (1.0 EIN / 0.95 name-or-fuzzy),
    not a hardcoded 0.60 — the view's existing per-type multiplier is what
    does the discounting, same as every other evidence type.
    """
    frames = []
    for path in csv_paths:
        frame = (
            pl.scan_csv(path, infer_schema_length=0)
            .filter(pl.col("SUBSECTION") == "03")
            .filter(
                pl.col("NTEE_CD").str.starts_with("U")
                | pl.col("NTEE_CD").str.starts_with("H")
                | pl.col("NTEE_CD").is_in(["E20", "E21", "E22"])
            )
            .select("EIN", "NAME", "CITY", "STATE", "NTEE_CD")
            .collect()
        )
        frames.append(frame)
    combined = pl.concat(frames).unique(subset=["EIN"], keep="first")

    is_hospital = pl.col("NTEE_CD").is_in(["E20", "E21", "E22"])
    combined = combined.with_columns(
        pl.when(is_hospital).then(pl.lit("IRS_BMF_HOSPITAL")).otherwise(pl.lit("IRS_BMF_RESEARCH")).alias("_source"),
        pl.when(is_hospital).then(pl.lit("hospital")).otherwise(pl.lit("nonprofit_research")).alias("_exempt_basis"),
    )

    now = datetime.now(UTC).replace(tzinfo=None)
    raw_id = _next_id(con, "raw_institution", "raw_id")
    con.executemany(
        "INSERT INTO raw_institution (raw_id, source, source_file, loaded_at, payload) VALUES (?,?,?,?,?)",
        [
            (raw_id + i, "IRS_BMF", ",".join(p.name for p in csv_paths), now, row_json)
            for i, row_json in enumerate(combined.write_ndjson().splitlines())
        ],
    )

    inst_id = _next_id(con, "ref_institution", "institution_id")
    rows = []
    for i, r in enumerate(combined.iter_rows(named=True)):
        rows.append((
            inst_id + i, r["_source"], r["EIN"], r["NAME"],
            normalize_employer_name(r["NAME"]), clean_ein(r["EIN"]),
            r["CITY"], r["STATE"], r["NTEE_CD"], r["_exempt_basis"],
        ))
    con.executemany(
        "INSERT INTO ref_institution "
        "(institution_id, source, source_key, name, normalized_name, ein, city, state, ntee_code, exempt_basis) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return {
        "total_loaded": len(rows),
        "research": sum(1 for r in rows if r[1] == "IRS_BMF_RESEARCH"),
        "hospital": sum(1 for r in rows if r[1] == "IRS_BMF_HOSPITAL"),
    }


def load_ffrdc(con: duckdb.DuckDBPyConnection, xlsx_path: Path) -> int:
    """NSF Master FFRDC list. The FFRDC sheet has a 3-row merged-cell title
    block before the real header; fastexcel can't detect that automatically.

    LCA filings show the *administering* entity's name, not the lab's (MIT
    files as "Massachusetts Institute of Technology", not "Lincoln
    Laboratory") — so Admin_Name is what actually gets matched against
    employers. FFRDC_Name is kept as the evidence detail (which lab, exactly)
    and as a secondary match candidate in case an employer files directly
    under the lab's own name.
    """
    raw = pl.read_excel(xlsx_path, sheet_name="FFRDC", infer_schema_length=0, has_header=False)
    header = raw.row(2)
    data = raw[3:]
    data.columns = [h if h else f"col{i}" for i, h in enumerate(header)]
    data = data.filter(pl.col("FFRDC_Name").is_not_null() & pl.col("Admin_Name").is_not_null())

    now = datetime.now(UTC).replace(tzinfo=None)
    raw_id = _next_id(con, "raw_institution", "raw_id")
    con.executemany(
        "INSERT INTO raw_institution (raw_id, source, source_file, loaded_at, payload) VALUES (?,?,?,?,?)",
        [
            (raw_id + i, "NSF_FFRDC", xlsx_path.name, now, row_json)
            for i, row_json in enumerate(data.write_ndjson().splitlines())
        ],
    )

    inst_id = _next_id(con, "ref_institution", "institution_id")
    rows = []
    for i, r in enumerate(data.iter_rows(named=True)):
        state = _STATE_ABBR.get((r.get("Loc_State") or "").strip())
        rows.append((
            inst_id + i, "NSF_FFRDC", r["FFRDC_Name"], r["Admin_Name"],
            normalize_employer_name(r["Admin_Name"]), None, None, state, None, "govt_research",
        ))
    con.executemany(
        "INSERT INTO ref_institution "
        "(institution_id, source, source_key, name, normalized_name, ein, city, state, ntee_code, exempt_basis) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)
