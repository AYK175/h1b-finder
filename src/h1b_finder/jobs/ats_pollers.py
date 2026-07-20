import re
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx
import polars as pl

from h1b_finder.config import Config
from h1b_finder.jobs.storage import close_stale_postings, get_or_create_board, mark_poll_result, upsert_posting
from h1b_finder.resolve.matcher import match_existing_employer

USER_AGENT = "h1b-finder/0.1 (personal research tool; contact: akhangmu@gmail.com)"
_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(html: str | None) -> str | None:
    if not html:
        return None
    text = _HTML_TAG.sub(" ", html)
    return re.sub(r"\s+", " ", text).strip() or None


class RateLimiter:
    def __init__(self, rate: float):
        self.min_interval = 1.0 / rate
        self.next_at = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self.next_at:
            time.sleep(self.next_at - now)
        self.next_at = time.monotonic() + self.min_interval


def fetch_greenhouse(client: httpx.Client, limiter: RateLimiter, token: str) -> list[dict]:
    limiter.wait()
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", params={"content": "true"})
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "external_id": str(j["id"]), "title": j.get("title", ""),
            "apply_url": j.get("absolute_url", ""),
            "description": _strip_html(j.get("content")),
            "location_raw": (j.get("location") or {}).get("name"),
            "city": None, "state": None,
            "raw": j,
        })
    return out


def fetch_lever(client: httpx.Client, limiter: RateLimiter, token: str) -> list[dict]:
    limiter.wait()
    r = client.get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
    r.raise_for_status()
    out = []
    for j in r.json():
        desc = " ".join(p for p in (j.get("descriptionPlain"), j.get("additionalPlain")) if p) or None
        out.append({
            "external_id": str(j["id"]), "title": j.get("text", ""),
            "apply_url": j.get("hostedUrl") or j.get("applyUrl") or "",
            "description": desc,
            "location_raw": (j.get("categories") or {}).get("location"),
            "city": None, "state": None,
            "raw": j,
        })
    return out


def fetch_ashby(client: httpx.Client, limiter: RateLimiter, token: str) -> list[dict]:
    limiter.wait()
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "external_id": str(j["id"]), "title": j.get("title", ""),
            "apply_url": j.get("jobUrl") or j.get("applyUrl") or "",
            "description": j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml")),
            "location_raw": j.get("location"),
            "city": None, "state": None,
            "raw": j,
        })
    return out


def fetch_smartrecruiters(client: httpx.Client, limiter: RateLimiter, token: str) -> list[dict]:
    """The list endpoint has no description or apply URL — both need a per-posting
    detail call. Costs one extra request per posting; still rate-limited the same
    as the list call."""
    limiter.wait()
    r = client.get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings", params={"limit": 100})
    r.raise_for_status()
    listing = r.json().get("content", [])

    out = []
    for item in listing:
        limiter.wait()
        try:
            dr = client.get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{item['id']}")
            dr.raise_for_status()
            detail = dr.json()
        except httpx.HTTPError:
            detail = {}
        sections = (detail.get("jobAd") or {}).get("sections", {})
        desc = " ".join(
            sections.get(k, {}).get("text", "") for k in ("jobDescription", "qualifications")
        ).strip()
        loc = item.get("location") or {}
        out.append({
            "external_id": str(item["id"]), "title": item.get("name", ""),
            "apply_url": detail.get("postingUrl") or detail.get("applyUrl") or "",
            "description": _strip_html(desc) if desc else None,
            "location_raw": loc.get("fullLocation"),
            "city": loc.get("city"), "state": loc.get("region"),
            "raw": item,
        })
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
}


def confident_ats_targets(spike_csv: Path, con: duckdb.DuckDBPyConnection) -> list[dict]:
    """The Phase 0.2 spike's cached resolutions (data/spike/ats_resolution.csv).
    Workday hits are present in that file too but explicitly deferred — skipped
    here entirely, not just left unpolled.
    """
    df = pl.read_csv(spike_csv)
    targets = []
    for provider in ("greenhouse", "lever", "ashby", "smartrecruiters"):
        conf_col, token_col = f"{provider}_confidence", f"{provider}_token"
        hits = df.filter(pl.col(conf_col).is_in(["verified", "token"]))
        for row in hits.iter_rows(named=True):
            match = match_existing_employer(con, row["employer_name"], row["employer_state"])
            if not match:
                continue
            targets.append({
                "employer_id": match[0], "provider": provider, "token": row[token_col],
                "employer_name": row["employer_name"],
            })
    return targets


def run_ats_poll(con: duckdb.DuckDBPyConnection, cfg: Config, spike_csv: Path) -> dict:
    targets = confident_ats_targets(spike_csv, con)
    limiters = {p: RateLimiter(cfg.jobs.ats.req_per_sec) for p in FETCHERS}
    failures_limit = cfg.jobs.ats.consecutive_failures_limit

    stats = {"boards_polled": 0, "boards_failed": 0, "postings_new": 0,
              "postings_seen_again": 0, "postings_closed": 0, "employers_with_postings": 0}
    employers_with_hits: set[int] = set()

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True) as client:
        for t in targets:
            board_id = get_or_create_board(
                con, t["employer_id"], t["provider"], t["token"], discovered_via="guess"
            )
            is_active = con.execute("SELECT is_active FROM ats_board WHERE board_id=?", [board_id]).fetchone()[0]
            if not is_active:
                continue

            poll_started_at = datetime.now(UTC).replace(tzinfo=None)
            try:
                jobs = FETCHERS[t["provider"]](client, limiters[t["provider"]], t["token"])
            except httpx.HTTPError as e:
                status = f"error: {e!r}"[:200]
                mark_poll_result(con, board_id, ok=False, status=status,
                                  consecutive_failures_limit=failures_limit)
                stats["boards_failed"] += 1
                continue

            mark_poll_result(con, board_id, ok=True, status="ok",
                              consecutive_failures_limit=failures_limit)
            stats["boards_polled"] += 1

            for job in jobs:
                if not job["apply_url"]:
                    continue
                is_new = upsert_posting(
                    con, employer_id=t["employer_id"], board_id=board_id,
                    external_id=job["external_id"], title=job["title"],
                    description_text=job["description"], apply_url=job["apply_url"],
                    location_raw=job["location_raw"], city=job["city"], state=job["state"],
                    postal_code=None, is_remote=None,
                    salary_min=None, salary_max=None, salary_unit=None,
                    raw_json=job["raw"],
                )
                if is_new:
                    stats["postings_new"] += 1
                else:
                    stats["postings_seen_again"] += 1
                employers_with_hits.add(t["employer_id"])

            stats["postings_closed"] += close_stale_postings(con, board_id, poll_started_at)

    stats["employers_with_postings"] = len(employers_with_hits)
    return stats
