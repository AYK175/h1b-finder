from h1b_finder.jobs.soc_predict import (
    build_similarity_vectorizer,
    similarity_soc,
    title_regex_soc,
)


def test_title_regex_data_scientist():
    assert title_regex_soc("Senior Data Scientist") == "15-2051"


def test_title_regex_statistician():
    assert title_regex_soc("Statistician II") == "15-2041"


def test_title_regex_database_architect():
    assert title_regex_soc("Database Architect") == "15-1243"
    # a plain administrator is a different, non-target SOC — must not match
    assert title_regex_soc("Database Administrator") is None


def test_title_regex_ml_research_scientist_before_engineer_pattern():
    assert title_regex_soc("Machine Learning Research Scientist") == "15-1221"


def test_title_regex_ml_engineer():
    assert title_regex_soc("Machine Learning Engineer") == "15-1252"


def test_title_regex_software_engineer():
    assert title_regex_soc("Senior Backend Software Engineer") == "15-1252"


def test_title_regex_no_match():
    assert title_regex_soc("HR Business Partner") is None


def test_similarity_soc_finds_data_scientist():
    vectorizer, socs, ref_matrix = build_similarity_vectorizer()
    text = "Analyze large structured and unstructured datasets using machine learning and data mining to extract insights"
    match = similarity_soc(text, vectorizer, socs, ref_matrix, min_confidence=0.05)
    assert match is not None
    soc, score = match
    assert soc == "15-2051"


def test_similarity_soc_none_below_threshold():
    vectorizer, socs, ref_matrix = build_similarity_vectorizer()
    match = similarity_soc("Warehouse forklift operator needed nights", vectorizer, socs, ref_matrix, min_confidence=0.55)
    assert match is None


def test_similarity_soc_empty_text():
    vectorizer, socs, ref_matrix = build_similarity_vectorizer()
    assert similarity_soc("", vectorizer, socs, ref_matrix, min_confidence=0.1) is None
