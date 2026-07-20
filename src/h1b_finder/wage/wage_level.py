import zipfile
from pathlib import Path

import duckdb
import polars as pl

HOURS_PER_YEAR = 2080
LEVELS = (1, 2, 3, 4)


def compute_level(salary: float, cutoffs: dict[int, float]) -> int:
    """Highest L in {4,3,2,1} where salary >= cutoffs[L]; 0 means below Level I
    (0 lottery tickets — not merely "Level I", genuinely below the wage floor).
    Pure function, no I/O — this is the whole FY2027+ registration determination
    per CLAUDE.md landmine #3: never substitute the LCA's own PW_WAGE_LEVEL here.
    """
    level = 0
    for level_n in LEVELS:
        if salary >= cutoffs[level_n]:
            level = level_n
    return level


def load_ref_prevailing_wage(con: duckdb.DuckDBPyConnection, oflc_wage_zip: Path, wage_year: str) -> int:
    """OFLC's ALC_Export.csv (bundled in the wage zip) is the standard "all
    industries combined" OEWS wage table — as opposed to EDC_Export.csv, the
    ACWIA Higher-Education table used when an employer invokes the separate
    higher-ed prevailing-wage exemption (not loaded this session; see the
    Phase 3 report for why that matters specifically for cap-exempt employers).
    Levels are hourly; annualize at 2080 hrs/yr, the same convention as the LCA
    wage conversion in Phase 1. Scoped to (soc_code, area_code) pairs already
    known to this DB — both columns are FKs, and ALC covers every BLS SOC code
    in existence, most of which are irrelevant here.
    """
    with zipfile.ZipFile(oflc_wage_zip) as zf, zf.open("ALC_Export.csv") as f:
        df = pl.read_csv(f, infer_schema_length=0)
    known_socs = set(con.execute("SELECT soc_code FROM ref_soc").pl()["soc_code"])
    known_areas = set(con.execute("SELECT area_code FROM ref_area").pl()["area_code"])
    df = df.filter(pl.col("SocCode").is_in(known_socs) & pl.col("Area").is_in(known_areas))

    df = df.with_columns(
        pl.col("Area").alias("area_code"),
        pl.col("SocCode").alias("soc_code"),
        pl.lit(wage_year).alias("wage_year"),
        (pl.col("Level1").cast(pl.Float64, strict=False) * HOURS_PER_YEAR).alias("level_1_annual"),
        (pl.col("Level2").cast(pl.Float64, strict=False) * HOURS_PER_YEAR).alias("level_2_annual"),
        (pl.col("Level3").cast(pl.Float64, strict=False) * HOURS_PER_YEAR).alias("level_3_annual"),
        (pl.col("Level4").cast(pl.Float64, strict=False) * HOURS_PER_YEAR).alias("level_4_annual"),
    ).unique(subset=["area_code", "soc_code"])

    stage = df.select(
        "area_code", "soc_code", "wage_year",
        "level_1_annual", "level_2_annual", "level_3_annual", "level_4_annual",
    )
    con.register("wage_stage", stage)
    con.execute("INSERT INTO ref_prevailing_wage SELECT * FROM wage_stage")
    con.unregister("wage_stage")
    return stage.height


def get_wage_cutoffs(
    con: duckdb.DuckDBPyConnection, area_code: str, soc_code: str, wage_year: str
) -> dict[int, float] | None:
    row = con.execute(
        "SELECT level_1_annual, level_2_annual, level_3_annual, level_4_annual "
        "FROM ref_prevailing_wage WHERE area_code = ? AND soc_code = ? AND wage_year = ?",
        [area_code, soc_code, wage_year],
    ).fetchone()
    if row is None:
        return None
    return {1: row[0], 2: row[1], 3: row[2], 4: row[3]}
