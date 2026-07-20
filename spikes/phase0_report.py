# /// script
# requires-python = ">=3.11"
# dependencies = ["polars"]
# ///
"""Phase 0.2 spike: generate reports/phase0_ats_coverage.md from ats_resolution.csv.

"Confident" = name-verified (greenhouse/smartrecruiters) or a full-name token hit
(lever/ashby/workday). "Low-confidence" = first-word-only token hit for a multiword
employer — the board exists but may belong to someone else.
"""

from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS = ["greenhouse", "lever", "ashby", "workday", "smartrecruiters"]
TIER1 = ["greenhouse", "lever", "ashby"]

# Manual audit of every name-verified / token hit (2026-07-18): these tokens exist on the
# provider but belong to a different company than the LCA employer (e.g. greenhouse board
# "charles" is not Charles Schwab; "PIONEER PLUMBING" is not Pioneer Consulting). Conservative:
# ambiguous cases (kingston, metlife) are excluded too.
AUDITED_FALSE_POSITIVES = {
    ("greenhouse", "charles"),
    ("greenhouse", "system"),
    ("greenhouse", "pioneer"),
    ("greenhouse", "avani"),
    ("greenhouse", "genesis"),
    ("greenhouse", "genuine"),
    ("greenhouse", "kingston"),
    ("lever", "linkedin"),
    ("lever", "metlife"),
    ("lever", "genesis"),
}


def main() -> None:
    df = pl.read_csv(ROOT / "data" / "spike" / "ats_resolution.csv")
    n = df.height
    total_lca = df["lca_certified_count"].sum()

    fp_count = 0
    for prov, token in AUDITED_FALSE_POSITIVES:
        mask = pl.col(f"{prov}_token") == token
        fp_count += df.filter(mask).height
        df = df.with_columns(
            pl.when(mask).then(pl.lit("audited_fp")).otherwise(pl.col(f"{prov}_confidence")).alias(f"{prov}_confidence")
        )

    conf_cols = [pl.col(f"{p}_confidence") for p in PROVIDERS]
    df = df.with_columns(
        pl.any_horizontal(*(c.is_in(["verified", "token"]) for c in conf_cols)).alias("confident"),
        pl.any_horizontal(*(c == "low_confidence" for c in conf_cols)).alias("low_conf_only_raw"),
        pl.any_horizontal(*((pl.col(f"{p}_confidence").is_in(["verified", "token"])) for p in TIER1)).alias("tier1_confident"),
    ).with_columns(
        (pl.col("low_conf_only_raw") & ~pl.col("confident")).alias("low_conf_only"),
    )

    confident = int(df["confident"].sum())
    low_conf = int(df["low_conf_only"].sum())
    unresolved = n - confident - low_conf
    tier1 = int(df["tier1_confident"].sum())
    lca_covered = int(df.filter(pl.col("confident"))["lca_certified_count"].sum())

    lines = [
        "# Phase 0.2 — ATS Coverage Spike",
        "",
        f"Top **{n}** employers by certified H-1B LCA count (FY2025 Q1–Q4 + FY2026 Q2, "
        "deduped on CASE_NUMBER), filtered to target SOC codes in `config.yaml`, "
        "CASE_STATUS in ('Certified','Certified-Withdrawn').",
        "",
        "Resolution = token guessed from normalized employer name, confirmed by a live "
        "request to the provider's public API. Greenhouse and SmartRecruiters hits are "
        "name-verified against the API's returned company name; Lever/Ashby/Workday are "
        "token-existence only. A first-word-only guess for a multiword employer counts as "
        "**low-confidence**, not resolved. Every confident hit was then manually audited; "
        f"{fp_count} hits whose board clearly belongs to a different company were reclassified "
        "as false positives and excluded from all counts below.",
        "",
        "## Headline",
        "",
        "| Bucket | Employers | % of 500 |",
        "|---|---:|---:|",
        f"| Resolved (confident) | {confident} | {confident/n:.1%} |",
        f"| Low-confidence only | {low_conf} | {low_conf/n:.1%} |",
        f"| Unresolved | {unresolved} | {unresolved/n:.1%} |",
        "",
        f"- **Tier 1 (Greenhouse/Lever/Ashby) confident: {tier1} / {n} = {tier1/n:.1%}**",
        f"- LCA-volume-weighted confident coverage: {lca_covered:,} / {total_lca:,} "
        f"certified LCAs = {lca_covered/total_lca:.1%}",
        "",
        "## By provider",
        "",
        "| Provider | Confident | Low-confidence | Total hits |",
        "|---|---:|---:|---:|",
    ]

    for p in PROVIDERS:
        c = df.filter(pl.col(f"{p}_confidence").is_in(["verified", "token"])).height
        l = df.filter(pl.col(f"{p}_confidence") == "low_confidence").height
        lines.append(f"| {p} | {c} | {l} | {c + l} |")

    lines += [
        "",
        "(Employers can hit on multiple providers; provider rows sum to more than the headline.)",
        "",
        "## Top 20 unresolved by LCA volume",
        "",
        "| Rank | Employer | Certified LCAs (target SOCs) | State |",
        "|---:|---|---:|---|",
    ]
    top_unresolved = (
        df.filter(~pl.col("confident") & ~pl.col("low_conf_only"))
        .sort("lca_certified_count", descending=True)
        .head(20)
    )
    for r in top_unresolved.iter_rows(named=True):
        lines.append(f"| {r['rank']} | {r['employer_name']} | {r['lca_certified_count']:,} | {r['employer_state']} |")

    lines += ["", "## Assessment", "", "_(filled in by hand below)_", ""]

    out = ROOT / "reports" / "phase0_ats_coverage.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines))
    print(f"wrote {out}")
    print(f"confident={confident} ({confident/n:.1%}), low_conf={low_conf}, "
          f"unresolved={unresolved}, tier1={tier1} ({tier1/n:.1%})")


if __name__ == "__main__":
    main()
