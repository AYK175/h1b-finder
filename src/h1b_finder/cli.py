import json
from datetime import UTC, datetime
from pathlib import Path

import typer

from h1b_finder import pipeline
from h1b_finder.capexempt import evidence as capexempt_evidence
from h1b_finder.config import ROOT, Secrets, load_config
from h1b_finder.dashboard_common import BASE_QUERY, SCORING_VERSION
from h1b_finder.db import connect
from h1b_finder.ingest import institutions
from h1b_finder.jobs import adzuna as adzuna_mod
from h1b_finder.jobs import ats_pollers
from h1b_finder.jobs import enrich as enrich_mod
from h1b_finder.resolve.matcher import resolve_employers
from h1b_finder.scoring import run_scoring
from h1b_finder.wage import area_crosswalk, wage_level as wage_level_mod

app = typer.Typer()
ingest_app = typer.Typer()
resolve_app = typer.Typer()
export_app = typer.Typer()
capexempt_app = typer.Typer()
jobs_app = typer.Typer()
app.add_typer(ingest_app, name="ingest")
app.add_typer(resolve_app, name="resolve")
app.add_typer(export_app, name="export")
app.add_typer(capexempt_app, name="capexempt")
app.add_typer(jobs_app, name="jobs")

LEVEL_NAMES = {0: "below Level I", 1: "I", 2: "II", 3: "III", 4: "IV"}


@ingest_app.command("lca")
def ingest_lca():
    cfg = load_config()
    con = connect(cfg.db_path)
    stats = pipeline.ingest(cfg, con)
    for name, s in stats.items():
        typer.echo(f"{name}: {s['raw_rows']} raw rows, {s['new_rows']} new")
    overlap = pipeline.cumulative_overlap_report(con)
    for k, v in overlap.items():
        typer.echo(f"{k}: {v['contained_in_max_q']}/{v['q_case_count']} ({v['pct_contained']}%) contained")


@ingest_app.command("perm")
def ingest_perm_cmd():
    """FIX 1: PERM disclosure ingestion — green_card_score + staffing_firm_score
    inputs. Requires employers to already be resolved (run `resolve employers`
    first)."""
    cfg = load_config()
    con = connect(cfg.db_path)

    stats = pipeline.ingest_perm(cfg, con)
    for name, s in stats.items():
        typer.echo(f"{name}: {s['raw_rows']} raw rows, {s['new_rows']} new")

    result = pipeline.build_perm_case(con)
    for k, v in result.items():
        typer.echo(f"  {k}: {v}")

    n_naics = pipeline.populate_employer_naics(con)
    typer.echo(f"employer.primary_naics populated for {n_naics} employers")

    pipeline.build_employer_profile(con, cfg)
    typer.echo("employer_h1b_profile rebuilt (perm_to_lca_ratio, cap_exempt_score, naics-dependent fields refreshed)")


@resolve_app.command("employers")
def resolve_employers_cmd():
    cfg = load_config()
    con = connect(cfg.db_path)
    clean = pipeline.clean_cases_df(con)
    typer.echo(f"clean cases (deduped on CASE_NUMBER): {clean.height}")

    employers = pipeline.distinct_employers(clean)
    typer.echo(f"distinct raw employer names: {len(employers)}")

    result = resolve_employers(employers)
    info = pipeline.write_entity_resolution(con, result)
    typer.echo(f"resolved employers: {info['n_employers']}")
    typer.echo(f"review queue: {info['n_review_queue']} ({100 * info['n_review_queue'] / len(employers):.1f}% of distinct raw names)")

    lca_df, bad_wage = pipeline.build_lca_case_frame(clean, info["alias_map"], cfg)
    typer.echo(f"wage-out-of-bounds rows nulled: {bad_wage}")
    n_new_soc = pipeline.populate_ref_soc(con, lca_df, cfg)
    typer.echo(f"ref_soc: {n_new_soc} new SOC codes added")
    pipeline.insert_lca_case(con, lca_df)
    pipeline.build_employer_profile(con, cfg)
    typer.echo("employer_h1b_profile rebuilt")


@export_app.command("sponsors")
def export_sponsors():
    cfg = load_config()
    con = connect(cfg.db_path)
    out = ROOT / "sponsors.csv"
    n = pipeline.export_sponsors_csv(con, out)
    typer.echo(f"wrote {out} ({n} employers)")


@export_app.command("snapshot")
def export_snapshot():
    """Export the current ranked view to public/*.parquet + meta.json for
    app_public.py (the shared read-only dashboard). Commit + push the
    public/ dir afterward to refresh what viewers see — this does not run
    automatically."""
    cfg = load_config()
    con = connect(cfg.db_path)

    out_dir = ROOT / "public"
    out_dir.mkdir(exist_ok=True)

    jobs = con.execute(BASE_QUERY).pl()
    jobs.write_parquet(out_dir / "jobs.parquet")
    typer.echo(f"wrote public/jobs.parquet ({jobs.height} postings)")

    employer_ids = jobs.filter(jobs["cap_exempt_score"].fill_null(0) > 0)["employer_id"].unique().to_list()
    if employer_ids:
        placeholders = ",".join("?" * len(employer_ids))
        evidence = con.execute(
            f"""SELECT employer_id, evidence_type, confidence, detail
                FROM cap_exempt_evidence WHERE employer_id IN ({placeholders})
                ORDER BY confidence DESC""",
            employer_ids,
        ).pl()
    else:
        evidence = con.execute(
            "SELECT employer_id, evidence_type, confidence, detail FROM cap_exempt_evidence WHERE FALSE"
        ).pl()
    evidence.write_parquet(out_dir / "evidence.parquet")
    typer.echo(f"wrote public/evidence.parquet ({evidence.height} rows)")

    scored_at = con.execute(
        f"SELECT MAX(scored_at) FROM job_score WHERE scoring_version = '{SCORING_VERSION}'"
    ).fetchone()[0]
    horizon = con.execute("SELECT MAX(decision_date) FROM lca_case").fetchone()[0]
    meta = {
        "scored_at": str(scored_at),
        "lca_horizon": str(horizon),
        "exported_at": datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="minutes"),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    typer.echo(f"wrote public/meta.json ({meta})")
    typer.echo("Now: git add public/ && git commit && git push to refresh the public dashboard.")


@capexempt_app.command("build")
def capexempt_build():
    cfg = load_config()
    con = connect(cfg.db_path)

    if con.execute("SELECT COUNT(*) FROM ref_institution WHERE source='IPEDS'").fetchone()[0] == 0:
        n = institutions.load_ipeds(con, cfg.raw_dir / "hd2024.zip")
        typer.echo(f"IPEDS: {n} institutions loaded into ref_institution")
    else:
        typer.echo("IPEDS: already loaded, skipping")

    if con.execute("SELECT COUNT(*) FROM ref_institution WHERE source='NSF_FFRDC'").fetchone()[0] == 0:
        n = institutions.load_ffrdc(con, cfg.raw_dir / "ffrdc-2026.xlsx")
        typer.echo(f"NSF FFRDC: {n} entries loaded into ref_institution")
    else:
        typer.echo("NSF FFRDC: already loaded, skipping")

    typer.echo("Deferred this session (files not downloaded): IRS BMF (nonprofit_research), AAMC/COTH (teaching hospitals)")

    ipeds_stats = capexempt_evidence.match_registry(con, "IPEDS", "ipeds_match", require_state=True)
    typer.echo(f"ipeds_match: {ipeds_stats['evidence_rows']} evidence rows, {ipeds_stats['distinct_employers']} distinct employers")

    ffrdc_stats = capexempt_evidence.match_registry(con, "NSF_FFRDC", "ffrdc_match", require_state=False)
    typer.echo(f"ffrdc_match: {ffrdc_stats['evidence_rows']} evidence rows, {ffrdc_stats['distinct_employers']} distinct employers")

    n_off = capexempt_evidence.emit_off_cycle_evidence(
        con, cfg.capexempt.off_cycle_min_n, cfg.capexempt.off_cycle_min_ratio
    )
    typer.echo(f"off_cycle_filings: {n_off} evidence rows")

    out = ROOT / "cap_exempt_sponsors.csv"
    n = pipeline.export_cap_exempt_csv(con, out)
    typer.echo(f"wrote {out} ({n} employers)")


def _ensure_wage_data_loaded(con, cfg) -> None:
    if con.execute("SELECT COUNT(*) FROM ref_area").fetchone()[0] == 0:
        n = area_crosswalk.load_ref_area(con, cfg.raw_dir / "OFLC_Wages_2026-27.zip")
        typer.echo(f"ref_area: {n} areas loaded")
    if con.execute("SELECT COUNT(*) FROM ref_zip_to_area").fetchone()[0] == 0:
        stats = area_crosswalk.load_ref_zip_to_area(con, cfg.raw_dir / "ZIP-CBSA_032026.xlsx")
        typer.echo(f"ref_zip_to_area: {stats['loaded']} zips loaded, "
                   f"{stats['unresolved_no_state_fallback']} unresolved (no in-state BOS area, e.g. territories)")
    if con.execute("SELECT COUNT(*) FROM ref_city_to_area").fetchone()[0] == 0:
        stats = area_crosswalk.load_ref_city_to_area(con, cfg.raw_dir / "ZIP-CBSA_032026.xlsx")
        typer.echo(f"ref_city_to_area: {stats['loaded']} cities loaded, "
                   f"{stats['unresolved_no_state_fallback']} unresolved (FIX 3)")
    if con.execute("SELECT COUNT(*) FROM ref_county_to_area").fetchone()[0] == 0:
        n = area_crosswalk.load_ref_county_to_area(con, cfg.raw_dir / "OFLC_Wages_2026-27.zip")
        typer.echo(f"ref_county_to_area: {n} counties loaded (Gazetteer batch FIX 1)")
    if con.execute("SELECT COUNT(*) FROM ref_gazetteer_place_to_area").fetchone()[0] == 0:
        stats = area_crosswalk.load_ref_gazetteer_place_to_area(
            con, cfg.raw_dir / "2025_Gaz_place_national.txt",
            cfg.raw_dir / "2025_Gaz_zcta_national.txt", cfg.raw_dir / "ZIP-CBSA_032026.xlsx",
        )
        typer.echo(f"ref_gazetteer_place_to_area: {stats['loaded']} places loaded, "
                   f"{stats['unresolved']} unresolved (Gazetteer batch FIX 1)")
    if con.execute("SELECT COUNT(*) FROM ref_prevailing_wage").fetchone()[0] == 0:
        n = wage_level_mod.load_ref_prevailing_wage(con, cfg.raw_dir / "OFLC_Wages_2026-27.zip", cfg.wage.year)
        typer.echo(f"ref_prevailing_wage: {n} (area, soc) wage rows loaded for wage year {cfg.wage.year}")


@app.command("wage-level")
def wage_level_cmd(
    salary: float = typer.Option(..., help="Annual offered salary in USD"),
    soc: str = typer.Option(..., help="SOC code, e.g. 15-2051"),
    city: str = typer.Option(..., help='City and state, e.g. "Austin, TX"'),
):
    cfg = load_config()
    con = connect(cfg.db_path)
    _ensure_wage_data_loaded(con, cfg)

    if "," not in city:
        typer.echo(f"Expected 'City, ST' — got '{city}'")
        raise typer.Exit(1)
    city_name, _, state = city.rpartition(",")
    area_code = area_crosswalk.find_area_by_city_state(con, city_name, state)
    if area_code is None:
        typer.echo(f"Could not resolve an OFLC area for '{city}'.")
        raise typer.Exit(1)

    cutoffs = wage_level_mod.get_wage_cutoffs(con, area_code, soc, cfg.wage.year)
    if cutoffs is None:
        typer.echo(f"No wage data for SOC {soc} in area {area_code} ({city}), wage year {cfg.wage.year}.")
        raise typer.Exit(1)

    level = wage_level_mod.compute_level(salary, cutoffs)
    area_name = con.execute("SELECT area_name FROM ref_area WHERE area_code = ?", [area_code]).fetchone()[0]

    typer.echo(f"Area: {area_name} ({area_code})")
    typer.echo(
        f"Cutoffs — I: ${cutoffs[1]:,.0f}  II: ${cutoffs[2]:,.0f}  "
        f"III: ${cutoffs[3]:,.0f}  IV: ${cutoffs[4]:,.0f}"
    )
    if level == 0:
        typer.echo(f"${salary:,.0f} is below Level I (${cutoffs[1]:,.0f}) — 0 tickets, ineligible at this wage.")
    else:
        typer.echo(f"Level {LEVEL_NAMES[level]}, {level} ticket(s)")


@jobs_app.command("poll")
def jobs_poll(
    provider: str = typer.Option("all", help="'adzuna' | 'ats' | 'all'"),
):
    cfg = load_config()
    con = connect(cfg.db_path)
    secrets = Secrets()

    if provider in ("adzuna", "all"):
        typer.echo("--- Adzuna (aggregator, primary breadth) ---")
        stats = adzuna_mod.run_adzuna_poll(con, cfg, secrets)
        for k, v in stats.items():
            typer.echo(f"  {k}: {v}")

    if provider in ("ats", "all"):
        typer.echo("--- Direct ATS (Greenhouse/Lever/Ashby/SmartRecruiters, quality) ---")
        stats = ats_pollers.run_ats_poll(con, cfg, ROOT / "data" / "spike" / "ats_resolution.csv")
        for k, v in stats.items():
            typer.echo(f"  {k}: {v}")

    typer.echo("--- Enrichment (SOC prediction, salary, sponsorship flags) ---")
    stats = enrich_mod.run_enrichment(con, cfg, secrets)
    typer.echo(f"  soc_prediction: {stats['soc_prediction']}")
    typer.echo(f"  salary_and_flags: {stats['salary_and_flags']}")


@app.command("backfill-areas")
def backfill_areas_cmd():
    """FIX 3: persist area_code on lca_case and job_posting using the real
    city->CBSA crosswalk (ref_city_to_area), replacing the old
    principal-city-substring match. Re-run after loading new job postings or
    LCA data."""
    cfg = load_config()
    con = connect(cfg.db_path)
    _ensure_wage_data_loaded(con, cfg)

    lca_stats = pipeline.backfill_lca_case_area_codes(con)
    typer.echo(f"lca_case: {lca_stats['resolved_pairs']}/{lca_stats['distinct_pairs']} distinct worksite pairs resolved")

    posting_stats = pipeline.backfill_job_posting_area_codes(con)
    typer.echo(f"job_posting: {posting_stats['resolved']}/{posting_stats['total_postings']} postings resolved")


@app.command("score")
def score_cmd(
    version: str = typer.Option(None, help="Overrides scoring.version from config.yaml"),
):
    cfg = load_config()
    if version:
        cfg.scoring.version = version
    con = connect(cfg.db_path)
    _ensure_wage_data_loaded(con, cfg)
    stats = run_scoring(con, cfg)
    for k, v in stats.items():
        typer.echo(f"  {k}: {v}")


if __name__ == "__main__":
    app()
