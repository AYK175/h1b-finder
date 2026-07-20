from pathlib import Path

from h1b_finder.ingest.dol_lca import parse_lca_file

FIXTURE = Path(__file__).parent / "fixtures" / "LCA_Disclosure_Data_FY2025_Q1_sample.xlsx"


def test_fixture_parses_to_500_rows():
    df = parse_lca_file(FIXTURE)
    assert df.height == 500


def test_fixture_has_required_columns():
    df = parse_lca_file(FIXTURE)
    for col in ("CASE_NUMBER", "CASE_STATUS", "SOC_CODE", "EMPLOYER_NAME", "WAGE_UNIT_OF_PAY"):
        assert col in df.columns


def test_fixture_row_hash_is_populated_and_stable():
    df = parse_lca_file(FIXTURE)
    assert df["row_hash"].null_count() == 0
    # re-parsing the same file must be deterministic (dedupe relies on this)
    df2 = parse_lca_file(FIXTURE)
    assert df["row_hash"].to_list() == df2["row_hash"].to_list()


def test_fixture_case_status_values_match_real_data():
    df = parse_lca_file(FIXTURE)
    statuses = set(df["CASE_STATUS"].unique().to_list())
    # not "Certified-Withdrawn" (no spaces) — a real landmine found during Phase 1
    assert statuses <= {"Certified", "Certified - Withdrawn", "Denied", "Withdrawn"}
