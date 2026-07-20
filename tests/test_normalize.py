from h1b_finder.resolve.normalize import normalize_employer_name


def test_basic_suffix_strip():
    assert normalize_employer_name("Google LLC") == "google"
    assert normalize_employer_name("Apple Inc.") == "apple"
    assert normalize_employer_name("Deloitte Consulting LLP") == "deloitte consulting"


def test_case_insensitive_and_punctuation():
    assert normalize_employer_name("AMAZON.COM SERVICES LLC") == normalize_employer_name(
        "Amazon.com Services LLC"
    )
    assert normalize_employer_name("Wal-Mart Associates, Inc.") == "wal mart associates"


def test_multiple_suffixes():
    assert normalize_employer_name("Foo Holdings Group Corp") == "foo"


def test_dba_prefers_trade_name():
    assert normalize_employer_name(
        "Fidelity Brokerage Services LLC d/b/a Fidelity Investments"
    ) == "fidelity investments"
    assert normalize_employer_name("Foo Bar Inc DBA Baz Corp") == "baz"


def test_abbreviation_expansion():
    assert normalize_employer_name("Acme Intl Tech Svcs") == "acme international technologies services"


def test_whitespace_collapse():
    assert normalize_employer_name("  Foo    Bar   LLC  ") == "foo bar"


def test_empty_and_none():
    assert normalize_employer_name("") == ""


def test_state_suffix_only_stripped_as_whole_word():
    # "us" / "na" / "co" are only stripped when they are standalone words,
    # not when they are substrings of a real name token.
    assert normalize_employer_name("Cognizant Technology Solutions Corp") == "cognizant technology solutions"
    assert normalize_employer_name("Coca Cola Company") == "coca cola"


def test_identical_normalization_for_review_queue_pair():
    # distinct legal suffixes on an otherwise-identical name still collapse
    assert normalize_employer_name("Synechron, Inc.") == normalize_employer_name("SYNECHRON INC")


def test_school_tech_nicknames_not_expanded():
    # "Tech" here is a proper-noun nickname, not an abbreviation of "Technologies"
    assert normalize_employer_name("Virginia Tech") == "virginia tech"
    assert normalize_employer_name("Georgia Tech") == "georgia tech"
    assert normalize_employer_name("Texas Tech") == "texas tech"
    assert normalize_employer_name("Louisiana Tech") == "louisiana tech"


def test_generic_tech_still_expands():
    # a real company literally named "X Tech Corp" should still expand normally
    assert normalize_employer_name("Acme Tech Corp") == "acme technologies"
