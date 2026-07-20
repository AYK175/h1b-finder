import duckdb
import pytest

from h1b_finder.resolve.matcher import match_existing_employer


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE employer (
            employer_id BIGINT, canonical_name TEXT, normalized_name TEXT,
            ein TEXT, hq_state TEXT
        )
    """)
    c.execute("""
        INSERT INTO employer VALUES
            (1, 'University of Maryland College Park', 'university of maryland college park', '526002033', 'MD'),
            (2, 'Acme Software Solutions', 'acme software solutions', '123456789', 'CA')
    """)
    return c


def test_normalized_exact_match(con):
    result = match_existing_employer(con, "University of Maryland College Park", "MD")
    assert result == (1, "normalized", 0.95)


def test_fuzzy_match_same_state(con):
    result = match_existing_employer(con, "Acme Softwear Solutions Corp", "CA")
    assert result is not None
    emp_id, method, confidence = result
    assert emp_id == 2
    assert method == "fuzzy"
    assert confidence >= 0.92


def test_no_match_returns_none(con):
    assert match_existing_employer(con, "Totally Unrelated University", "TX") is None


def test_fuzzy_requires_same_state(con):
    # multi-token fuzzy match to employer_id=2, but only registered in CA
    assert match_existing_employer(con, "Acme Softwear Solutions Corp", "NY") is None


def test_single_token_name_does_not_fuzzy_match():
    # a single-token normalized name (post-suffix-strip) should never fuzzy-match
    # via subset inclusion — only exact/normalized match should catch it
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE employer (
            employer_id BIGINT, canonical_name TEXT, normalized_name TEXT,
            ein TEXT, hq_state TEXT
        )
    """)
    c.execute("INSERT INTO employer VALUES (1, 'Ocean Corporation', 'ocean', '999999999', 'TX')")
    # "CEVA Air & Ocean USA Inc." normalizes to a multi-token string containing
    # "ocean" as one token — must NOT match the single-token "ocean" employer
    assert match_existing_employer(c, "CEVA Air & Ocean USA Inc.", "TX") is None
