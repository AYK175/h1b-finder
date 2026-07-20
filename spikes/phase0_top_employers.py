# /// script
# requires-python = ">=3.11"
# dependencies = ["polars", "fastexcel", "pyyaml"]
# ///
"""Phase 0.1/0.2 spike: top 500 employers by certified LCA count for target SOC codes.

Throwaway spike code. Column names verified against LCA_Record_Layout_FY2026_Q2.pdf
(identical to the FY2025_Q4 layout apart from the cover header).

Files may restate rows across quarterly releases, so we dedupe on CASE_NUMBER,
keeping the row from the latest file (sorted by filename FY/Q order).
"""

from pathlib import Path

import polars as pl
import yaml

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "spike"

COLS = [
    "CASE_NUMBER",
    "CASE_STATUS",
    "VISA_CLASS",
    "SOC_CODE",
    "EMPLOYER_NAME",
    "EMPLOYER_STATE",
]


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    target_socs = set(cfg["target_soc_codes"])
    positive_statuses = cfg["ingest"]["case_statuses_positive"]

    # Both naming variants exist on disk ("Disclosure" and the typo "Dislclosure").
    files = sorted(RAW.glob("LCA_Dis*Data_FY*.xlsx"))
    assert files, f"no LCA xlsx files under {RAW}"

    frames = []
    for i, f in enumerate(files):
        df = pl.read_excel(f, columns=COLS, infer_schema_length=0)
        df = df.with_columns(pl.lit(i).alias("_file_order"), pl.lit(f.name).alias("_file"))
        print(f"{f.name}: {df.height:,} rows")
        frames.append(df)

    all_rows = pl.concat(frames)
    print(f"total raw rows: {all_rows.height:,}")

    deduped = (
        all_rows.sort("_file_order")
        .unique(subset=["CASE_NUMBER"], keep="last")
    )
    print(f"after CASE_NUMBER dedupe: {deduped.height:,} "
          f"({all_rows.height - deduped.height:,} restated/duplicate rows dropped)")

    # SOC_CODE sometimes carries an O*NET-style suffix ("15-2051.00") — match on prefix.
    certified = deduped.filter(
        pl.col("CASE_STATUS").is_in(positive_statuses)
        & (pl.col("VISA_CLASS") == "H-1B")
        & pl.col("SOC_CODE").str.strip_chars().str.slice(0, 7).is_in(target_socs)
    )
    print(f"certified H-1B rows in target SOCs: {certified.height:,}")

    top = (
        certified.group_by("EMPLOYER_NAME")
        .agg(
            pl.len().alias("lca_certified_count"),
            pl.col("EMPLOYER_STATE").mode().first().alias("employer_state"),
        )
        .sort("lca_certified_count", descending=True)
        .head(500)
        .with_row_index("rank", offset=1)
    )

    OUT.mkdir(parents=True, exist_ok=True)
    top.write_csv(OUT / "top500_employers.csv")
    print(f"\nwrote {OUT / 'top500_employers.csv'} ({top.height} employers)")

    # Sanity check from CLAUDE.md: Amazon entities, all SOCs, certified — should be
    # tens of thousands per FY. Off by an order of magnitude = broken parse.
    amazon = deduped.filter(
        pl.col("CASE_STATUS").is_in(positive_statuses)
        & pl.col("EMPLOYER_NAME").str.to_uppercase().str.contains("AMAZON")
    )
    print(f"sanity — Amazon entities, certified, ALL SOCs, all files: {amazon.height:,}")

    print("\ntop 20 by certified LCA count (target SOCs):")
    with pl.Config(tbl_rows=20, fmt_str_lengths=60):
        print(top.head(20))


if __name__ == "__main__":
    main()
