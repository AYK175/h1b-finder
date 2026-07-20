from h1b_finder.jobs.salary_extract import extract_posted_salary


def test_basic_range():
    assert extract_posted_salary("Salary: $120,000 - $150,000 per year") == (120000.0, 150000.0, "year")


def test_k_suffix():
    assert extract_posted_salary("Pay range $120k-$150k") == (120000.0, 150000.0, "year")


def test_hourly_range():
    result = extract_posted_salary("Compensation: $45 - $60 per hour")
    assert result == (45.0, 60.0, "hour")


def test_reversed_order_normalized():
    assert extract_posted_salary("Salary: $150,000 - $120,000 per year") == (120000.0, 150000.0, "year")


def test_no_range_returns_none():
    assert extract_posted_salary("We are a fast-growing startup in San Francisco") is None


def test_none_input():
    assert extract_posted_salary(None) is None


def test_ambiguous_small_numbers_without_hourly_context_skipped():
    # small dollar amounts with no k-suffix or hourly cue are too ambiguous to trust
    assert extract_posted_salary("Get $5 - $10 off your first order") is None
