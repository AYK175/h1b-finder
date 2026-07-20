import re

_NO_SPONSORSHIP = re.compile(
    r"without\s+sponsorship|not\s+able\s+to\s+sponsor|no\s+visa\s+sponsorship|"
    r"must\s+be\s+authorized\s+to\s+work.{0,40}without|"
    r"will\s+not\s+sponsor|unable\s+to\s+sponsor|does\s+not\s+sponsor",
    re.I,
)
_CLEARANCE = re.compile(
    r"security\s+clearance|\bts\s*/\s*sci\b|\btop\s+secret\b|"
    r"must\s+be\s+a?\s*u\.?s\.?\s+citizen|us\s+citizenship\s+required",
    re.I,
)
_SPONSORSHIP_OK = re.compile(
    r"will\s+sponsor|sponsorship\s+(is\s+)?available|sponsorship\s+(is\s+)?offered|"
    r"visa\s+sponsorship\s+(is\s+)?provided",
    re.I,
)


def detect_sponsorship_flags(description: str | None) -> dict[str, bool]:
    """Pure regex scan of a posting description. Every flag defaults False when
    there's no description text — absence of language is not evidence of
    absence of the policy, just untested."""
    text = description or ""
    return {
        "mentions_no_sponsorship": bool(_NO_SPONSORSHIP.search(text)),
        "mentions_clearance": bool(_CLEARANCE.search(text)),
        "mentions_sponsorship_ok": bool(_SPONSORSHIP_OK.search(text)),
    }
