from h1b_finder.wage.wage_level import compute_level

CUTOFFS = {1: 80_000, 2: 100_000, 3: 120_000, 4: 150_000}


def test_below_level_one_is_zero():
    assert compute_level(50_000, CUTOFFS) == 0


def test_exactly_at_level_one_cutoff():
    assert compute_level(80_000, CUTOFFS) == 1


def test_between_levels_takes_lower():
    assert compute_level(95_000, CUTOFFS) == 1


def test_level_two():
    assert compute_level(100_000, CUTOFFS) == 2


def test_level_three():
    assert compute_level(125_000, CUTOFFS) == 3


def test_level_four():
    assert compute_level(150_000, CUTOFFS) == 4


def test_far_above_level_four_stays_at_four():
    assert compute_level(500_000, CUTOFFS) == 4


def test_zero_salary():
    assert compute_level(0, CUTOFFS) == 0
