HOURS_PER_YEAR = 2080
WEEKS_PER_YEAR = 52
BIWEEKLY_PER_YEAR = 26
MONTHS_PER_YEAR = 12

_MULTIPLIER = {
    "year": 1,
    "month": MONTHS_PER_YEAR,
    "bi-weekly": BIWEEKLY_PER_YEAR,
    "week": WEEKS_PER_YEAR,
    "hour": HOURS_PER_YEAR,
}


def annualize_wage(amount: float | None, unit: str | None) -> float | None:
    """Convert a WAGE_UNIT_OF_PAY-denominated amount to an annual figure.

    2080 hrs/yr assumed for Hour (40hr week x 52). See tests/test_wage.py for the
    known-value check on every unit in the enum — a missed conversion here poisons
    the whole wage-level engine downstream.
    """
    if amount is None or unit is None:
        return None
    mult = _MULTIPLIER.get(unit.strip().lower())
    if mult is None:
        raise ValueError(f"unknown WAGE_UNIT_OF_PAY: {unit!r}")
    return amount * mult
