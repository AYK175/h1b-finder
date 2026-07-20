import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl

from h1b_finder.db import LCA_RAW_COLUMNS

_FY_Q_RE = re.compile(r"FY(\d{4})_Q(\d)", re.IGNORECASE)

# Column-name variants seen across quarterly releases within the same FY layout.
# FY2025 Q1 predates the addition of LAWFIRM_BUSINESS_FEIN and spells the dependent
# flag with a hyphen; canonicalize both to the Q2+ names before landing.
_RENAME = {"H-1B_DEPENDENT": "H_1B_DEPENDENT"}

_PAYLOAD_HASH_COLS = [c for c in LCA_RAW_COLUMNS if c != "CASE_NUMBER"]


def _fy_quarter_from_filename(path: Path) -> tuple[int, int]:
    m = _FY_Q_RE.search(path.name)
    if not m:
        raise ValueError(f"cannot parse fiscal year/quarter from filename: {path.name}")
    return int(m.group(1)), int(m.group(2))


def parse_lca_file(path: Path) -> pl.DataFrame:
    """Read one LCA quarterly XLSX into a DataFrame shaped for raw_lca.

    All payload columns stay TEXT (infer_schema_length=0) — this is the immutable
    landing zone, no casting or cleaning happens here. row_hash covers every
    payload column except CASE_NUMBER, so a restated row (same case, changed
    status/decision) gets a distinct hash and both versions land, while an exact
    re-release of an already-loaded row is a no-op on insert.
    """
    fiscal_year, fiscal_quarter = _fy_quarter_from_filename(path)
    df = pl.read_excel(path, infer_schema_length=0)
    df = df.rename({k: v for k, v in _RENAME.items() if k in df.columns})

    missing = [c for c in LCA_RAW_COLUMNS if c not in df.columns]
    df = df.with_columns([pl.lit(None, dtype=pl.Utf8).alias(c) for c in missing])
    df = df.select(LCA_RAW_COLUMNS)

    hash_input = pl.concat_str(
        [pl.col(c).fill_null("") for c in _PAYLOAD_HASH_COLS], separator="\x1f"
    )
    df = df.with_columns(
        hash_input.map_elements(
            lambda s: hashlib.sha256(s.encode()).hexdigest(), return_dtype=pl.Utf8
        ).alias("row_hash"),
        pl.lit(path.name).alias("source_file"),
        pl.lit(fiscal_year).alias("fiscal_year"),
        pl.lit(fiscal_quarter).alias("fiscal_quarter"),
        pl.lit(datetime.now(UTC).replace(tzinfo=None)).alias("loaded_at"),
    )
    return df


def load_lca_files(con: duckdb.DuckDBPyConnection, files: list[Path]) -> dict[str, int]:
    """Parse and insert each file into raw_lca. Returns per-file {raw rows, new rows}."""
    next_id = con.execute("SELECT COALESCE(MAX(raw_id), 0) FROM raw_lca").fetchone()[0]
    stats = {}
    cols_sql = ", ".join(f'"{c}"' for c in LCA_RAW_COLUMNS)
    for path in sorted(files):
        df = parse_lca_file(path).unique(subset=["row_hash"], keep="first")  # noqa: F841 (used via duckdb SQL below)
        before = con.execute("SELECT COUNT(*) FROM raw_lca").fetchone()[0]
        con.execute(f"""
            INSERT INTO raw_lca (raw_id, source_file, fiscal_year, fiscal_quarter, loaded_at,
                                  row_hash, {cols_sql})
            SELECT {next_id} + row_number() OVER () , source_file, fiscal_year, fiscal_quarter,
                   loaded_at, row_hash, {cols_sql}
            FROM df
            WHERE row_hash NOT IN (SELECT row_hash FROM raw_lca)
        """)
        after = con.execute("SELECT COUNT(*) FROM raw_lca").fetchone()[0]
        next_id = con.execute("SELECT COALESCE(MAX(raw_id), 0) FROM raw_lca").fetchone()[0]
        stats[path.name] = {"raw_rows": df.height, "new_rows": after - before}
    return stats
