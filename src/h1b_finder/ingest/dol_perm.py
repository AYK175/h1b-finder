import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl

# Deliberately minimal, unlike raw_lca's "land every column" convention — this
# batch only needs employer name, case status, decision date, and SOC (see
# schema.sql's raw_perm comment). Verified identical across both PERM files
# actually on disk (FY2025_Q4, FY2026_Q2) — no old/new ETA-9089 schema split
# was found between them.
PERM_RAW_COLUMNS = [
    "CASE_NUMBER", "CASE_STATUS", "RECEIVED_DATE", "DECISION_DATE",
    "EMP_BUSINESS_NAME", "EMP_FEIN", "EMP_STATE", "JOB_TITLE",
    "PWD_SOC_CODE", "PRIMARY_WORKSITE_STATE",
]

_FY_Q_RE = re.compile(r"FY(\d{4})_Q(\d)", re.IGNORECASE)

# Unlike parse_lca_file (85+ payload columns, CASE_NUMBER excluded from the
# hash so a restated case gets a new hash), PERM_RAW_COLUMNS is deliberately
# minimal (9 fields). Excluding CASE_NUMBER there collapsed ~24% of real,
# distinct cases: many different PERM filings for the same employer/title/
# status/date/state genuinely share identical values across all 9 fields.
# CASE_NUMBER must be part of the hash here so distinct cases can never
# collide — restatement handling still works via clean_perm_df's later
# QUALIFY-based "latest wins" dedup, which keys on CASE_NUMBER directly, not
# on this hash.
_PAYLOAD_HASH_COLS = PERM_RAW_COLUMNS


def _fiscal_year_from_filename(path: Path) -> int:
    m = _FY_Q_RE.search(path.name)
    if not m:
        raise ValueError(f"cannot parse fiscal year from filename: {path.name}")
    return int(m.group(1))


def parse_perm_file(path: Path) -> pl.DataFrame:
    """Read one PERM disclosure XLSX into a DataFrame shaped for raw_perm.
    All-text landing zone, same convention as parse_lca_file — no casting here."""
    fiscal_year = _fiscal_year_from_filename(path)
    df = pl.read_excel(path, infer_schema_length=0)

    missing = [c for c in PERM_RAW_COLUMNS if c not in df.columns]
    df = df.with_columns([pl.lit(None, dtype=pl.Utf8).alias(c) for c in missing])
    df = df.select(PERM_RAW_COLUMNS)

    hash_input = pl.concat_str(
        [pl.col(c).fill_null("") for c in _PAYLOAD_HASH_COLS], separator="\x1f"
    )
    df = df.with_columns(
        hash_input.map_elements(
            lambda s: hashlib.sha256(s.encode()).hexdigest(), return_dtype=pl.Utf8
        ).alias("row_hash"),
        pl.lit(path.name).alias("source_file"),
        pl.lit(fiscal_year).alias("fiscal_year"),
        pl.lit(datetime.now(UTC).replace(tzinfo=None)).alias("loaded_at"),
    )
    return df


def load_perm_files(con: duckdb.DuckDBPyConnection, files: list[Path]) -> dict[str, dict]:
    """Parse and insert each file into raw_perm. Returns per-file {raw_rows, new_rows}."""
    next_id = con.execute("SELECT COALESCE(MAX(raw_id), 0) FROM raw_perm").fetchone()[0]
    stats = {}
    cols_sql = ", ".join(f'"{c}"' for c in PERM_RAW_COLUMNS)
    for path in sorted(files):
        df = parse_perm_file(path).unique(subset=["row_hash"], keep="first")  # noqa: F841
        before = con.execute("SELECT COUNT(*) FROM raw_perm").fetchone()[0]
        con.execute(f"""
            INSERT INTO raw_perm (raw_id, source_file, fiscal_year, loaded_at, row_hash, {cols_sql})
            SELECT {next_id} + row_number() OVER (), source_file, fiscal_year,
                   loaded_at, row_hash, {cols_sql}
            FROM df
            WHERE row_hash NOT IN (SELECT row_hash FROM raw_perm)
        """)
        after = con.execute("SELECT COUNT(*) FROM raw_perm").fetchone()[0]
        next_id = con.execute("SELECT COALESCE(MAX(raw_id), 0) FROM raw_perm").fetchone()[0]
        stats[path.name] = {"raw_rows": df.height, "new_rows": after - before}
    return stats
