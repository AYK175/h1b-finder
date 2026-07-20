import json
import re

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# Official OFLC/OEWS title + description for each target SOC (from oes_soc_occs.csv,
# Wage Year 2026-27) — used as the reference text for the tier-2 similarity check.
TARGET_SOC_REFERENCE = {
    "15-2051": "Data Scientists. Develop and implement a set of techniques or analytics "
               "applications to transform raw data into meaningful information using "
               "data-oriented programming languages and visualization software. Apply data "
               "mining, data modeling, natural language processing, and machine learning to "
               "extract and analyze information from large structured and unstructured datasets.",
    "15-1252": "Software Developers. Research, design, and develop computer and network "
               "software or specialized utility programs. Analyze user needs and develop "
               "software solutions, applying principles and techniques of computer science, "
               "engineering, and mathematical analysis.",
    "15-1221": "Computer and Information Research Scientists. Conduct research into "
               "fundamental computer and information science as theorists, designers, or "
               "inventors. Develop solutions to problems in the field of computer hardware "
               "and software.",
    "15-2041": "Statisticians. Develop or apply mathematical or statistical theory and "
               "methods to collect, organize, interpret, and summarize numerical data to "
               "provide usable information. May specialize in biostatistics, agricultural "
               "statistics, business statistics, or economic statistics.",
    "15-1243": "Database Architects. Design strategies for enterprise databases, data "
               "warehouse systems, and multidimensional networks. Set standards for database "
               "operations, programming, query processes, and security. Model, design, and "
               "construct large relational databases or data warehouses.",
}

# Title regex catches the obvious majority — ordered most-specific first so
# "Machine Learning Research Scientist" hits 15-1221 before any looser rule.
_TITLE_REGEX_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bdata scientists?\b", re.I), "15-2051"),
    (re.compile(r"\bstatisticians?\b", re.I), "15-2041"),
    (re.compile(r"\bdatabase architects?\b", re.I), "15-1243"),
    (re.compile(r"\b(machine learning|ml|ai)\s+(research(er)?|scientist)\b", re.I), "15-1221"),
    (re.compile(r"\b(computer|artificial intelligence)\s+research\s+scientists?\b", re.I), "15-1221"),
    (re.compile(r"\b(machine learning|ml)\s+engineers?\b", re.I), "15-1252"),
    (re.compile(r"\b(software|backend|frontend|full[- ]?stack|application|mobile|platform)\s+"
                r"(developer|engineer)s?\b", re.I), "15-1252"),
]


def title_regex_soc(title: str) -> str | None:
    for pattern, soc in _TITLE_REGEX_RULES:
        if pattern.search(title):
            return soc
    return None


def build_similarity_vectorizer():
    """Returns (vectorizer, soc_codes, reference_matrix) for similarity_soc()."""
    socs = list(TARGET_SOC_REFERENCE)
    vectorizer = TfidfVectorizer(stop_words="english")
    ref_matrix = vectorizer.fit_transform([TARGET_SOC_REFERENCE[s] for s in socs])
    return vectorizer, socs, ref_matrix


def similarity_soc(text: str, vectorizer: TfidfVectorizer, socs: list[str], ref_matrix,
                    min_confidence: float) -> tuple[str, float] | None:
    if not text or not text.strip():
        return None
    vec = vectorizer.transform([text])
    sims = cosine_similarity(vec, ref_matrix)[0]
    best_idx = sims.argmax()
    best_score = float(sims[best_idx])
    if best_score >= min_confidence:
        return socs[best_idx], best_score
    return None


HAIKU_PROMPT_TEMPLATE = """You are classifying a job posting title (and short description \
excerpt) into one of these SOC occupation codes, or "none" if it clearly doesn't fit any of them.

{soc_list}

For each posting below, respond with ONLY a JSON array (no other text), one object per \
posting in the same order, each: {{"soc_code": "<code or null>", "confidence": <0-1>, \
"reasoning": "<one short phrase>"}}

Postings:
{postings}"""


def build_haiku_prompt(postings: list[dict]) -> str:
    soc_list = "\n".join(f"- {code}: {desc.split('.')[0]}." for code, desc in TARGET_SOC_REFERENCE.items())
    postings_text = "\n".join(
        f"{i + 1}. Title: {p['title']!r}  Description excerpt: {(p.get('description') or '')[:200]!r}"
        for i, p in enumerate(postings)
    )
    return HAIKU_PROMPT_TEMPLATE.format(soc_list=soc_list, postings=postings_text)


def parse_haiku_response(text: str, expected_n: int) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    parsed = json.loads(text)
    if not isinstance(parsed, list) or len(parsed) != expected_n:
        raise ValueError(f"expected {expected_n} results, got {parsed!r}")
    return parsed
