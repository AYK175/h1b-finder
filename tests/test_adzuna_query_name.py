from h1b_finder.jobs.adzuna import clean_query_name


def test_strips_trustees_prefix():
    assert clean_query_name("TRUSTEES OF THE UNIVERSITY OF PENNSYLVANIA") == "UNIVERSITY OF PENNSYLVANIA"


def test_strips_legal_suffix():
    assert clean_query_name("Acme Corporation") == "Acme"
    assert clean_query_name("Acme, LLC") == "Acme"
    assert clean_query_name("Acme Inc.") == "Acme"


def test_strips_parenthetical():
    assert clean_query_name("UT-Battelle, LLC (Oak Ridge National Laboratory)") == "UT-Battelle"


def test_prefers_trade_name_over_dba():
    assert clean_query_name("Fidelity Brokerage Services LLC d/b/a Fidelity Investments") == "Fidelity Investments"


def test_leaves_clean_names_alone():
    assert clean_query_name("Google") == "Google"
    assert clean_query_name("Johns Hopkins University") == "Johns Hopkins University"


def test_never_returns_empty():
    # a name that fully collapses under stripping should fall back to the original
    assert clean_query_name("LLC") == "LLC"
