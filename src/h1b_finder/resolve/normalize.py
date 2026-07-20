import re

_DBA_SPLIT = re.compile(r"\bd\s*/?\s*b\s*/?\s*a\b")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_SUFFIXES = re.compile(
    r"\b(inc|llc|corp|corporation|ltd|limited|lp|llp|plc|co|company|holdings|group|usa|us|na)\b"
)
_WHITESPACE = re.compile(r"\s+")

_ABBREVIATIONS = {
    "intl": "international",
    "tech": "technologies",
    "svcs": "services",
}

# "Tech" as a school nickname ("Virginia Tech", "Georgia Tech") is a proper noun,
# not an abbreviation of "Technologies" — expanding it corrupts the name into
# something that will never fuzzy-match the school's real LCA/Adzuna filings.
# Exempt the whole two-word phrase rather than trying to generically detect
# "is this a school name", since a company genuinely named "X Tech Corp" should
# still expand normally.
_TECH_NICKNAME_EXEMPT = {
    "virginia tech", "georgia tech", "texas tech", "louisiana tech",
    "michigan tech", "montana tech", "arkansas tech", "new jersey tech",
}


def normalize_employer_name(name: str) -> str:
    """Canonicalize a raw employer name for exact/fuzzy matching.

    Prefers the trade name over the legal name when a d/b/a is present, strips
    punctuation and common legal suffixes, expands a few abbreviations, and
    collapses whitespace. Pure function — see tests/test_normalize.py.
    """
    if not name:
        return ""
    s = name.lower()
    parts = _DBA_SPLIT.split(s)
    s = parts[-1]  # "Fidelity Brokerage Services LLC d/b/a Fidelity Investments" -> trade name
    s = _NON_ALNUM.sub(" ", s)
    s = _SUFFIXES.sub(" ", s)
    s = _WHITESPACE.sub(" ", s).strip()
    if s in _TECH_NICKNAME_EXEMPT:
        return s
    words = [_ABBREVIATIONS.get(w, w) for w in s.split()]
    return " ".join(words)
