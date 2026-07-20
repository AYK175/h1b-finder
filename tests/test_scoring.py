from h1b_finder.scoring import (
    US_STATES,
    compute_fit_score,
    compute_green_card_score,
    compute_staffing_firm_score,
    parse_neighborhood_city_from_location_raw,
    parse_state_from_location_raw,
    recency_decay,
    salary_to_annual,
    sponsor_score_for,
)


def test_recency_decay_at_zero_days_is_one():
    assert recency_decay(0) == 1.0


def test_recency_decay_at_one_half_life_is_half():
    assert abs(recency_decay(365) - 0.5) < 1e-9


def test_recency_decay_three_years_much_lower_than_last_quarter():
    last_quarter = recency_decay(90)
    three_years = recency_decay(365 * 3)
    assert last_quarter > three_years
    assert three_years < 0.15


def test_recency_decay_negative_days_clamped_to_one():
    assert recency_decay(-10) == 1.0


def test_sponsor_score_for_no_filing_is_zero_not_none():
    assert sponsor_score_for(None) == 0.0


def test_sponsor_score_for_recent_filing_is_high():
    assert sponsor_score_for(30) > 0.9


def test_compute_fit_score_saturates():
    title = "Data Scientist, Machine Learning, NLP, Deep Learning, AI"
    score, matched = compute_fit_score(title, None)
    assert score == 1.0
    assert len(matched) >= 4


def test_compute_fit_score_no_keywords_is_zero():
    score, matched = compute_fit_score("Regional Sales Director", "Grow our EMEA book of business")
    assert score == 0.0
    assert matched == []


def test_compute_fit_score_partial_match():
    score, matched = compute_fit_score("Data Analyst", None)
    assert 0 < score < 1.0


def test_staffing_firm_score_real_staffing_firm_scores_high():
    # high volume, low PERM follow-through, NAICS 5613, many worksites
    score, detail = compute_staffing_firm_score(
        lca_certified_24mo=1500, distinct_worksite_states=10,
        perm_to_lca_ratio=0.01, primary_naics="561320",
    )
    assert score > 0.8
    assert detail["naics_signal_available"] is True
    assert detail["perm_signal_available"] is True


def test_staffing_firm_score_large_non_staffing_employer_is_lower():
    # ByteDance-shaped: huge volume and worksite spread, but NOT NAICS 5613
    # and healthy PERM follow-through — should land well below the real
    # staffing firm above even though volume/worksites are similarly large.
    score, detail = compute_staffing_firm_score(
        lca_certified_24mo=1500, distinct_worksite_states=10,
        perm_to_lca_ratio=0.3, primary_naics="518210",
    )
    assert score < 0.6
    assert detail["naics_component"] == 0.0


def test_staffing_firm_score_small_focused_employer_is_low():
    score, _ = compute_staffing_firm_score(
        lca_certified_24mo=5, distinct_worksite_states=1,
        perm_to_lca_ratio=0.3, primary_naics="541511",
    )
    assert score < 0.15


def test_staffing_firm_score_handles_none_inputs():
    score, detail = compute_staffing_firm_score(None, None, None, None)
    assert 0.0 < score < 1.0  # neutral perm_component (0.5) keeps it mid-range
    assert detail["lca_certified_24mo"] is None
    assert detail["naics_signal_available"] is False
    assert detail["perm_signal_available"] is False


def test_green_card_score_none_ratio_is_none_not_zero():
    score, detail = compute_green_card_score(None)
    assert score is None
    assert detail["reason"] == "no_perm_data_for_employer"


def test_green_card_score_saturates_at_ceiling():
    score, _ = compute_green_card_score(0.5)
    assert score == 1.0


def test_green_card_score_zero_ratio_is_real_zero():
    score, _ = compute_green_card_score(0.0)
    assert score == 0.0


def test_green_card_score_partial_ratio():
    score, _ = compute_green_card_score(0.25)
    assert 0.0 < score < 1.0


def test_salary_to_annual_hourly_conversion():
    assert salary_to_annual(50.0, 50.0, "hour") == 50.0 * 2080


def test_salary_to_annual_yearly_passthrough():
    assert salary_to_annual(100_000, 120_000, "year") == 110_000


def test_salary_to_annual_single_value():
    assert salary_to_annual(100_000, None, "year") == 100_000


def test_salary_to_annual_both_none_is_none():
    assert salary_to_annual(None, None, "year") is None


def test_parse_state_from_location_raw_simple():
    assert parse_state_from_location_raw("Chicago, IL, US") == ("Chicago", "IL")


def test_parse_state_from_location_raw_multi_location_takes_first():
    assert parse_state_from_location_raw("Seattle, WA, US; Remote, US") == ("Seattle", "WA")


def test_parse_state_from_location_raw_rejects_non_us():
    assert parse_state_from_location_raw("Dublin, IE") is None


def test_parse_state_from_location_raw_rejects_no_state_pattern():
    assert parse_state_from_location_raw("Berlin, hybrid") is None


def test_parse_state_from_location_raw_none_input():
    assert parse_state_from_location_raw(None) is None


def test_parse_state_from_location_raw_full_state_name():
    assert parse_state_from_location_raw("San Francisco, California") == ("San Francisco", "CA")


def test_parse_state_from_location_raw_full_state_name_with_country_suffix():
    assert parse_state_from_location_raw("Seattle, Washington, United States") == ("Seattle", "WA")


def test_parse_state_from_location_raw_dc_with_periods():
    assert parse_state_from_location_raw("Washington, D.C.") == ("Washington", "DC")


def test_parse_state_from_location_raw_does_not_treat_country_code_as_state():
    # 'Toronto, ON, CA' -- the trailing 'CA' is Canada's country code, not
    # California; must reject via the second segment (ON), not accept via the third.
    assert parse_state_from_location_raw("Toronto, ON, CA") is None


def test_parse_state_from_location_raw_pipe_separator():
    assert parse_state_from_location_raw("Austin, TX | Remote") == ("Austin", "TX")


def test_us_states_set_has_50_states_plus_dc_and_pr():
    assert len(US_STATES) == 52  # 50 states + DC + PR


def test_neighborhood_fallback_extracts_second_segment():
    assert parse_neighborhood_city_from_location_raw("SoMa, San Francisco", "CA") == ("San Francisco", "CA")


def test_neighborhood_fallback_strips_county_suffix():
    assert parse_neighborhood_city_from_location_raw(
        "Monte Vista, Santa Clara County", "CA"
    ) == ("Santa Clara", "CA")


def test_neighborhood_fallback_multi_location_takes_first():
    assert parse_neighborhood_city_from_location_raw(
        "Old Sixth Ward, Houston; Remote, US", "TX"
    ) == ("Houston", "TX")


def test_neighborhood_fallback_requires_known_state():
    assert parse_neighborhood_city_from_location_raw("SoMa, San Francisco", None) is None


def test_neighborhood_fallback_requires_a_second_segment():
    assert parse_neighborhood_city_from_location_raw("US", "CA") is None


def test_neighborhood_fallback_none_input():
    assert parse_neighborhood_city_from_location_raw(None, "CA") is None


def test_neighborhood_fallback_rejects_foreign_region_as_state():
    # Adzuna's own `state` column carries foreign region values verbatim for
    # non-US postings (e.g. Indian state names/2-letter codes) — must not be
    # treated as a valid US state candidate.
    assert parse_neighborhood_city_from_location_raw("Koramangala, Bengaluru", "Karnataka") is None
    assert parse_neighborhood_city_from_location_raw("Koramangala, Bengaluru", "KA") is None
