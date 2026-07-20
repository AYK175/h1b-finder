import json
from datetime import UTC, datetime

import duckdb

from h1b_finder.config import Config, Secrets
from h1b_finder.jobs.salary_extract import extract_posted_salary, median_wage_for_employer_soc
from h1b_finder.jobs.soc_predict import (
    build_haiku_prompt,
    build_similarity_vectorizer,
    parse_haiku_response,
    similarity_soc,
    title_regex_soc,
)
from h1b_finder.jobs.text_flags import detect_sponsorship_flags


def _predict_soc_batch(con: duckdb.DuckDBPyConnection, cfg: Config, secrets: Secrets) -> dict:
    """Title regex -> TF-IDF similarity -> Haiku, in that order, only for
    postings not yet classified. Vectorized where possible — this runs over
    thousands of rows per poll cycle, not one at a time."""
    rows = con.execute(
        "SELECT posting_id, title, description_text FROM job_posting WHERE soc_method IS NULL"
    ).fetchall()

    stats = {"title_regex": 0, "embedding": 0, "llm": 0, "unclassified": 0, "total": len(rows)}
    tier2_candidates: list[tuple[int, str]] = []

    for posting_id, title, description in rows:
        soc = title_regex_soc(title or "")
        if soc:
            con.execute(
                "UPDATE job_posting SET soc_code_predicted=?, soc_confidence=?, soc_method='title_regex' "
                "WHERE posting_id=?",
                [soc, 1.0, posting_id],
            )
            stats["title_regex"] += 1
        else:
            text = f"{title or ''} {description or ''}".strip()
            tier2_candidates.append((posting_id, text))

    if tier2_candidates:
        vectorizer, socs, ref_matrix = build_similarity_vectorizer()
        tier3_candidates: list[tuple[int, str]] = []
        for posting_id, text in tier2_candidates:
            match = similarity_soc(text, vectorizer, socs, ref_matrix, cfg.jobs.soc_predict.embedding_min_confidence)
            if match:
                soc, score = match
                con.execute(
                    "UPDATE job_posting SET soc_code_predicted=?, soc_confidence=?, soc_method='embedding' "
                    "WHERE posting_id=?",
                    [soc, score, posting_id],
                )
                stats["embedding"] += 1
            else:
                tier3_candidates.append((posting_id, text))

        if tier3_candidates and secrets.anthropic_api_key:
            import anthropic

            client = anthropic.Anthropic(api_key=secrets.anthropic_api_key)
            batch_size = cfg.jobs.soc_predict.haiku_batch_size
            for i in range(0, len(tier3_candidates), batch_size):
                batch = tier3_candidates[i:i + batch_size]
                postings = [{"title": t, "description": t} for _, t in batch]
                prompt = build_haiku_prompt(postings)
                try:
                    response = client.messages.create(
                        model=cfg.jobs.soc_predict.haiku_model,
                        max_tokens=2000,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    results = parse_haiku_response(response.content[0].text, len(batch))
                except Exception:
                    stats["unclassified"] += len(batch)
                    continue
                for (posting_id, _), result in zip(batch, results, strict=True):
                    soc = result.get("soc_code")
                    conf = result.get("confidence")
                    if soc:
                        con.execute(
                            "UPDATE job_posting SET soc_code_predicted=?, soc_confidence=?, soc_method='llm' "
                            "WHERE posting_id=?",
                            [soc, conf, posting_id],
                        )
                        stats["llm"] += 1
                    else:
                        con.execute(
                            "UPDATE job_posting SET soc_confidence=?, soc_method='llm' WHERE posting_id=?",
                            [conf, posting_id],
                        )
                        stats["unclassified"] += 1
        else:
            stats["unclassified"] += len(tier3_candidates)

    return stats


def _fill_salary_and_flags(con: duckdb.DuckDBPyConnection) -> dict:
    rows = con.execute(
        "SELECT posting_id, employer_id, description_text, salary_min, salary_max, "
        "soc_code_predicted FROM job_posting WHERE salary_source IS NULL"
    ).fetchall()

    stats = {"posted": 0, "inferred_lca": 0, "unknown": 0, "clearance_flagged": 0}
    for posting_id, employer_id, description, salary_min, salary_max, soc in rows:
        flags = detect_sponsorship_flags(description)
        if flags["mentions_clearance"]:
            stats["clearance_flagged"] += 1

        if salary_min is not None or salary_max is not None:
            source = "posted"
            stats["posted"] += 1
            con.execute(
                "UPDATE job_posting SET salary_source=?, mentions_no_sponsorship=?, "
                "mentions_clearance=?, mentions_sponsorship_ok=? WHERE posting_id=?",
                [source, flags["mentions_no_sponsorship"], flags["mentions_clearance"],
                 flags["mentions_sponsorship_ok"], posting_id],
            )
            continue

        extracted = extract_posted_salary(description)
        if extracted:
            lo, hi, unit = extracted
            stats["posted"] += 1
            con.execute(
                "UPDATE job_posting SET salary_min=?, salary_max=?, salary_unit=?, salary_source='posted', "
                "mentions_no_sponsorship=?, mentions_clearance=?, mentions_sponsorship_ok=? WHERE posting_id=?",
                [lo, hi, unit, flags["mentions_no_sponsorship"], flags["mentions_clearance"],
                 flags["mentions_sponsorship_ok"], posting_id],
            )
            continue

        if soc:
            median = median_wage_for_employer_soc(con, employer_id, soc)
            if median:
                stats["inferred_lca"] += 1
                con.execute(
                    "UPDATE job_posting SET salary_min=?, salary_max=?, salary_unit='year', "
                    "salary_source='inferred_lca', mentions_no_sponsorship=?, mentions_clearance=?, "
                    "mentions_sponsorship_ok=? WHERE posting_id=?",
                    [median, median, flags["mentions_no_sponsorship"], flags["mentions_clearance"],
                     flags["mentions_sponsorship_ok"], posting_id],
                )
                continue

        stats["unknown"] += 1
        con.execute(
            "UPDATE job_posting SET salary_source='unknown', mentions_no_sponsorship=?, "
            "mentions_clearance=?, mentions_sponsorship_ok=? WHERE posting_id=?",
            [flags["mentions_no_sponsorship"], flags["mentions_clearance"],
             flags["mentions_sponsorship_ok"], posting_id],
        )

    return stats


def run_enrichment(con: duckdb.DuckDBPyConnection, cfg: Config, secrets: Secrets) -> dict:
    soc_stats = _predict_soc_batch(con, cfg, secrets)
    salary_stats = _fill_salary_and_flags(con)
    return {"soc_prediction": soc_stats, "salary_and_flags": salary_stats}
