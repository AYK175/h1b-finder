import json
import time
from pathlib import Path

import duckdb

from h1b_finder.config import Config, Secrets
from h1b_finder.jobs.soc_predict import build_haiku_prompt, parse_haiku_response

CACHE_DIR = Path("data/cache/soc_llm")


def _cache_path(cache_dir: Path, posting_id: int) -> Path:
    return cache_dir / f"{posting_id}.json"


def _call_haiku_batch(client, cfg: Config, cache_dir: Path, batch: list[tuple[int, str, str]], stats: dict) -> dict[int, dict]:
    """One Haiku call for one batch. Returns whatever succeeded (empty dict on
    failure) — never raises, so a single malformed response can't crash the
    whole run."""
    postings = [{"title": t, "description": d} for _, t, d in batch]
    prompt = build_haiku_prompt(postings)
    try:
        response = client.messages.create(
            model=cfg.jobs.soc_predict.haiku_model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        stats["api_calls"] += 1
        stats["input_tokens"] += response.usage.input_tokens
        stats["output_tokens"] += response.usage.output_tokens
        results = parse_haiku_response(response.content[0].text, len(batch))
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
    """Shared Haiku-calling core for both the tier-2 reclassification and the
    general unclassified pass: disk-cache lookup, batch calls, cache write.
    Same posting_id-keyed cache directory for both — no collision risk since
    posting_id is a single global key regardless of which caller touches it.

    A batch that fails to parse (e.g. a job title/description with a quote
    character Haiku fails to escape, breaking the JSON contract) is retried
    ONE posting at a time so a single poison row can't take out the other
    ~19 postings in its batch — isolates the bad row instead of guessing.
    Postings that still fail after that are left uncached (not silently
    marked "none") so a future re-run retries them for free.

    rows: (posting_id, title, description). Returns (posting_id -> parsed
    Haiku result, {from_cache, api_calls, input_tokens, output_tokens,
    batch_failures, unresolved_after_retry}).
    """
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
            # Isolate the poison row: retry one posting at a time.
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


def reclassify_tier2_via_haiku(
    con: duckdb.DuckDBPyConnection,
    cfg: Config,
    secrets: Secrets,
    cache_dir: Path = CACHE_DIR,
) -> dict:
    """Re-route every soc_method='embedding' posting through Claude Haiku.

    Tier-1 (title_regex) rows are untouched by construction — the WHERE clause
    only ever selects tier-2 rows. The prior tier-2 value is preserved on
    prior_soc_* columns before being overwritten so before/after can be
    compared. Each Haiku response is cached to disk by posting_id so a re-run
    makes zero new API calls for postings already processed.
    """
    if not secrets.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing from .env — cannot run Haiku reclassification")

    import anthropic

    client = anthropic.Anthropic(api_key=secrets.anthropic_api_key)

    rows = con.execute(
        "SELECT posting_id, title, description_text, soc_code_predicted, soc_confidence "
        "FROM job_posting WHERE soc_method = 'embedding'"
    ).fetchall()

    stats = {
        "total_tier2": len(rows),
        "reclassified": 0,
        "flipped_to_none": 0,
        "flipped_to_different_soc": 0,
        "confirmed_same_soc": 0,
        "before_distribution": {},
        "after_distribution": {},
    }

    for _posting_id, _title, _description, prior_soc, _prior_conf in rows:
        key = prior_soc or "none"
        stats["before_distribution"][key] = stats["before_distribution"].get(key, 0) + 1

    call_rows = [(pid, title or "", desc or "") for pid, title, desc, _, _ in rows]
    cached_results, batch_stats = _batch_classify_via_haiku(client, cfg, cache_dir, call_rows)
    stats.update(batch_stats)

    for posting_id, _title, _description, prior_soc, prior_conf in rows:
        if posting_id not in cached_results:
            continue
        result = cached_results[posting_id]
        new_soc = result.get("soc_code")
        new_conf = result.get("confidence")

        after_key = new_soc or "none"
        stats["after_distribution"][after_key] = stats["after_distribution"].get(after_key, 0) + 1

        if prior_soc and not new_soc:
            stats["flipped_to_none"] += 1
        elif prior_soc != new_soc:
            stats["flipped_to_different_soc"] += 1
        else:
            stats["confirmed_same_soc"] += 1

        con.execute(
            "UPDATE job_posting SET prior_soc_method='embedding', prior_soc_code_predicted=?, "
            "prior_soc_confidence=?, soc_code_predicted=?, soc_confidence=?, soc_method='llm' "
            "WHERE posting_id=?",
            [prior_soc, prior_conf, new_soc, new_conf, posting_id],
        )
        stats["reclassified"] += 1

    return stats


def classify_unclassified_via_haiku(
    con: duckdb.DuckDBPyConnection,
    cfg: Config,
    secrets: Secrets,
    cache_dir: Path = CACHE_DIR,
) -> dict:
    """FIX 2: route every posting that never got ANY SOC prediction
    (soc_method IS NULL — title_regex found nothing and the general tier-3
    Haiku pass never ran) through Claude Haiku, same prompt/JSON contract and
    'none / not a target SOC' reject option as the tier-2 reclassification.
    No prior value to preserve here (there was no prior classification), so
    prior_soc_* stay NULL for these rows — an honest "nothing to compare
    against", not a bug. Same disk cache convention, so a re-run is free.
    """
    if not secrets.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing from .env — cannot run Haiku classification")

    import anthropic

    client = anthropic.Anthropic(api_key=secrets.anthropic_api_key)

    rows = con.execute(
        "SELECT posting_id, title, description_text FROM job_posting WHERE soc_method IS NULL"
    ).fetchall()

    stats = {
        "total_unclassified": len(rows),
        "classified": 0,
        "rejected_none": 0,
        "after_distribution": {},
    }

    call_rows = [(pid, title or "", desc or "") for pid, title, desc in rows]
    cached_results, batch_stats = _batch_classify_via_haiku(client, cfg, cache_dir, call_rows)
    stats.update(batch_stats)

    for posting_id, _title, _description in rows:
        if posting_id not in cached_results:
            continue
        result = cached_results[posting_id]
        new_soc = result.get("soc_code")
        new_conf = result.get("confidence")

        after_key = new_soc or "none"
        stats["after_distribution"][after_key] = stats["after_distribution"].get(after_key, 0) + 1
        if new_soc:
            stats["classified"] += 1
        else:
            stats["rejected_none"] += 1

        con.execute(
            "UPDATE job_posting SET soc_code_predicted=?, soc_confidence=?, soc_method='llm' "
            "WHERE posting_id=?",
            [new_soc, new_conf, posting_id],
        )

    return stats
