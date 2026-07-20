"""Seniority tier — a DISPLAY/FILTER axis only, orthogonal to SOC and to wage-
level lottery tickets by design. Never read by scoring.py or any ranking sort.

Same title-regex-first, Haiku-for-ambiguous-cases pattern as soc_predict.py /
soc_reclassify.py, but a separate implementation (different prompt schema,
different cache dir) — two occurrences of the pattern doesn't yet justify a
shared abstraction per CLAUDE.md's "no abstraction until the third occurrence."
"""

import json
import re
import time
from pathlib import Path

import duckdb

from h1b_finder.config import Config, Secrets

CACHE_DIR = Path("data/cache/seniority_llm")

# Checked in this order — most-specific/highest-precedence tier wins when a
# title matches more than one set (e.g. "Senior Staff Engineer" -> staff_plus).
_STAFF_PLUS_RE = re.compile(
    r"\b(staff|principal|distinguished|lead|director|head\s+of|vp|vice\s+president|fellow)\b",
    re.I,
)
_SENIOR_RE = re.compile(r"\b(senior|sr\.?)\b", re.I)
# Roman-numeral level suffixes are case-sensitive by convention ("Engineer III",
# never "engineer iii") — IGNORECASE would risk matching stray lowercase text.
_SENIOR_LEVEL_RE = re.compile(r"\b(III|IV)\b")
_ENTRY_RE = re.compile(
    r"\b(junior|jr\.?|associate|new\s+grad(uate)?|entry[- ]level|entry|"
    r"intern[- ]to[- ]fte|university\s+grad(uate)?|early\s+career|graduate)\b",
    re.I,
)
# Standalone trailing "I" as a level suffix ("Software Engineer I") — anchored
# to end-of-string so it can't match mid-title tokens or fuse with adjacent
# letters ("UI", "AI" don't have a preceding space before a bare "I").
_ENTRY_LEVEL_RE = re.compile(r"\bI\s*$")


def regex_tier(text: str) -> str | None:
    """Highest-precedence tier found in text, or None if no marker present."""
    if not text:
        return None
    if _STAFF_PLUS_RE.search(text):
        return "staff_plus"
    if _SENIOR_RE.search(text) or _SENIOR_LEVEL_RE.search(text):
        return "senior"
    if _ENTRY_RE.search(text) or _ENTRY_LEVEL_RE.search(text.strip()):
        return "entry"
    return None


def classify_regex(title: str, description: str | None) -> tuple[str | None, str, float | None]:
    """Title first, description as a CONFLICT CHECK only (not a freestanding
    source of truth) — returns (tier, sub_method, confidence). tier is None
    (sub_method 'conflict' or 'no_marker') when the posting needs Haiku.

    Naive regex against free-text description is too easy to fool with a
    marker word describing someone OTHER than the role itself — e.g. "Data
    Scientist, Search, Multimodal"'s description says "...mentoring more
    junior team members", which matched the entry pattern on "junior" even
    though the surrounding text (a leveling-ladder blurb literally labeled
    "Mid") describes a MID-level role mentoring juniors, not a junior role.
    So a description marker is only trusted to CONFIRM or CONTRADICT a marker
    already found in the title (contradiction -> ambiguous, Haiku decides);
    when the title has no marker at all, description regex alone is not
    trusted either — that case always goes to Haiku, which has the contextual
    judgment naive regex doesn't."""
    title_tier = regex_tier(title)
    if title_tier is not None:
        desc_tier = regex_tier((description or "")[:500])
        if desc_tier is None or desc_tier == title_tier:
            return title_tier, "title", 0.95
        return None, "conflict", None

    return None, "no_marker", None


HAIKU_SENIORITY_PROMPT = """You are classifying the seniority level of a job posting into exactly \
one of: "entry", "mid", "senior", "staff_plus".

Guidance:
- staff_plus: Staff, Principal, Distinguished, Lead, Director, Head of, VP, Fellow, or clearly an \
IC-track-beyond-senior / management-scope role.
- senior: Senior/Sr in title, or a role clearly requiring 5+ years / deep independent ownership.
- entry: Junior, Associate, New Grad, Entry-level, Early Career, University Grad, Intern-to-FTE, or \
a role explicitly for candidates with 0-2 years experience / recent graduates.
- mid: the default for a normal individual-contributor role with no explicit seniority signal \
(typically 2-5 years, no special modifier).

For each posting below, respond with ONLY a JSON array (no other text), one object per posting in \
the same order, each: {{"seniority_tier": "<entry|mid|senior|staff_plus>", "confidence": <0-1>, \
"reasoning": "<one short phrase>"}}

Postings:
{postings}"""


def build_seniority_prompt(postings: list[dict]) -> str:
    postings_text = "\n".join(
        f"{i + 1}. Title: {p['title']!r}  Description excerpt: {(p.get('description') or '')[:300]!r}"
        for i, p in enumerate(postings)
    )
    return HAIKU_SENIORITY_PROMPT.format(postings=postings_text)


def parse_seniority_response(text: str, expected_n: int) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    parsed = json.loads(text)
    if not isinstance(parsed, list) or len(parsed) != expected_n:
        raise ValueError(f"expected {expected_n} results, got {parsed!r}")
    return parsed


def _cache_path(cache_dir: Path, posting_id: int) -> Path:
    return cache_dir / f"{posting_id}.json"


def _call_haiku_batch(
    client, cfg: Config, cache_dir: Path, batch: list[tuple[int, str, str]], stats: dict
) -> dict[int, dict]:
    postings = [{"title": t, "description": d} for _, t, d in batch]
    prompt = build_seniority_prompt(postings)
    try:
        response = client.messages.create(
            model=cfg.jobs.soc_predict.haiku_model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        stats["api_calls"] += 1
        stats["input_tokens"] += response.usage.input_tokens
        stats["output_tokens"] += response.usage.output_tokens
        results = parse_seniority_response(response.content[0].text, len(batch))
    except Exception:
        stats["batch_failures"] += 1
        return {}

    out = {}
    for (posting_id, _, _), result in zip(batch, results, strict=True):
        _cache_path(cache_dir, posting_id).write_text(json.dumps(result))
        out[posting_id] = result
    return out


def _batch_classify_via_haiku(
    client, cfg: Config, cache_dir: Path, rows: list[tuple[int, str, str]]
) -> tuple[dict[int, dict], dict]:
    """Disk-cache lookup, batch calls, cache write. A batch that fails to parse
    is retried one posting at a time so a single poison row can't take out the
    rest of its batch. Postings still unresolved after that are left uncached
    (not guessed) so a future re-run retries them for free."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    stats = {
        "from_cache": 0, "api_calls": 0, "input_tokens": 0, "output_tokens": 0,
        "batch_failures": 0, "unresolved_after_retry": 0,
    }
    to_call: list[tuple[int, str, str]] = []
    cached_results: dict[int, dict] = {}

    for posting_id, title, description in rows:
        cache_file = _cache_path(cache_dir, posting_id)
        if cache_file.exists():
            cached_results[posting_id] = json.loads(cache_file.read_text())
            stats["from_cache"] += 1
        else:
            to_call.append((posting_id, title, description))

    batch_size = cfg.jobs.soc_predict.haiku_batch_size
    for i in range(0, len(to_call), batch_size):
        batch = to_call[i : i + batch_size]
        result = _call_haiku_batch(client, cfg, cache_dir, batch, stats)
        if result:
            cached_results.update(result)
        else:
            for row in batch:
                single = _call_haiku_batch(client, cfg, cache_dir, [row], stats)
                if single:
                    cached_results.update(single)
                else:
                    stats["unresolved_after_retry"] += 1
                time.sleep(0.2)
        if i + batch_size < len(to_call):
            time.sleep(0.5)

    return cached_results, stats


VALID_TIERS = {"entry", "mid", "senior", "staff_plus"}


def classify_seniority(
    con: duckdb.DuckDBPyConnection, cfg: Config, secrets: Secrets, cache_dir: Path = CACHE_DIR
) -> dict:
    """Classify every job_posting row with seniority_tier IS NULL. Regex-
    resolved rows are written immediately (method='regex'); everything
    ambiguous (no marker anywhere, or a title/description conflict) is
    batched through Haiku (method='llm'), same caching convention as the SOC
    classifier so a re-run makes zero new API calls for postings already
    processed."""
    rows = con.execute(
        "SELECT posting_id, title, description_text FROM job_posting WHERE seniority_tier IS NULL"
    ).fetchall()

    stats = {
        "total": len(rows),
        "regex_title": 0,
        "regex_description_tiebreak": 0,
        "ambiguous_to_llm": 0,
        "distribution": {},
    }

    ambiguous_rows: list[tuple[int, str, str]] = []
    for posting_id, title, description in rows:
        tier, sub_method, confidence = classify_regex(title or "", description)
        if tier is not None:
            con.execute(
                "UPDATE job_posting SET seniority_tier=?, seniority_method='regex', "
                "seniority_confidence=? WHERE posting_id=?",
                [tier, confidence, posting_id],
            )
            stats["distribution"][tier] = stats["distribution"].get(tier, 0) + 1
            if sub_method == "title":
                stats["regex_title"] += 1
            else:
                stats["regex_description_tiebreak"] += 1
        else:
            ambiguous_rows.append((posting_id, title or "", description or ""))

    stats["ambiguous_to_llm"] = len(ambiguous_rows)

    if ambiguous_rows:
        if not secrets.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY missing from .env — cannot classify ambiguous seniority via Haiku")

        import anthropic

        # Explicit timeout: a hung request should fail fast into the existing
        # single-row retry path, not block the whole batch run indefinitely.
        client = anthropic.Anthropic(api_key=secrets.anthropic_api_key, timeout=60.0)
        cached_results, batch_stats = _batch_classify_via_haiku(client, cfg, cache_dir, ambiguous_rows)
        stats.update({f"llm_{k}": v for k, v in batch_stats.items()})

        for posting_id, _title, _description in ambiguous_rows:
            result = cached_results.get(posting_id)
            if result is None:
                continue
            # Haiku occasionally typos the key ("sentiority_tier", "seriority_tier",
            # ...) — match by VALUE among all fields rather than chasing each typo,
            # so any misspelled-but-otherwise-valid response still gets used.
            tier = result.get("seniority_tier")
            if tier not in VALID_TIERS:
                tier = next((v for v in result.values() if v in VALID_TIERS), None)
            confidence = result.get("confidence")
            if tier not in VALID_TIERS:
                continue
            con.execute(
                "UPDATE job_posting SET seniority_tier=?, seniority_method='llm', "
                "seniority_confidence=? WHERE posting_id=?",
                [tier, confidence, posting_id],
            )
            stats["distribution"][tier] = stats["distribution"].get(tier, 0) + 1

    return stats
