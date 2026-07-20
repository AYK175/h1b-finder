import pytest

from h1b_finder.resolve.wage import annualize_wage


def test_year_passthrough():
    assert annualize_wage(120_000, "Year") == 120_000


def test_hour_uses_2080():
    assert annualize_wage(50, "Hour") == 50 * 2080 == 104_000


def test_month():
    assert annualize_wage(10_000, "Month") == 120_000


def test_week():
    assert annualize_wage(2_000, "Week") == 104_000


def test_bi_weekly():
    assert annualize_wage(4_000, "Bi-Weekly") == 104_000


def test_case_insensitive_unit():
    assert annualize_wage(50, "hour") == annualize_wage(50, "HOUR")


def test_none_amount_or_unit_passes_through_none():
    assert annualize_wage(None, "Year") is None
    assert annualize_wage(100, None) is None


def test_unknown_unit_raises():
    with pytest.raises(ValueError):
        annualize_wage(100, "Fortnight")
