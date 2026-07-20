# /// script
# requires-python = ">=3.11"
# dependencies = ["polars", "httpx", "rapidfuzz"]
# ///
"""Phase 0.2 spike: attempt public ATS board resolution for the top-500 LCA employers.

Throwaway spike code. Providers polled in parallel; each provider is hard rate-limited
to 2 req/sec. Every HTTP response is cached to disk; reruns hit cache only.

Detection signals (calibrated live before this run):
  greenhouse      GET  boards-api.greenhouse.io/v1/boards/{t}          200+{"name"} vs 404
  lever           GET  api.lever.co/v0/postings/{t}?mode=json&limit=1  200+JSON list vs 404
  ashby           GET  api.ashbyhq.com/posting-api/job-board/{t}       200+{"jobs"} vs 404
  smartrecruiters GET  api.smartrecruiters.com/v1/companies/{t}/postings?limit=1
                       -> always 200; hit iff totalFound > 0 (name verifiable)
  workday         POST {t}.wd{N}.myworkdayjobs.com/wday/cxs/{t}/zzprobe/jobs
                       -> 404 errorCode S21 = tenant EXISTS (bad site); 422 = no tenant
                       (site discovery is Phase 4b work; tenant existence = resolvable here)

Name verification: greenhouse and smartrecruiters return a company name -> fuzzy-checked.
Lever/Ashby/Workday are token-existence only; a first-word-only hit for a multiword
employer is flagged low_confidence (e.g. "fidelity" on Lever could be anyone).
"""

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

import httpx
import polars as pl
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent.parent
SPIKE = ROOT / "data" / "spike"
CACHE = SPIKE / "ats_cache"

UA = "h1b-finder-phase0-spike/0.1 (personal research tool; contact: akhangmu@gmail.com)"
RATE = 2.0  # req/sec per provider (hard rule in CLAUDE.md)
WORKDAY_HOSTS = ["wd1", "wd5", "wd3"]

SUFFIXES = r"\b(inc|llc|corp|corporation|ltd|limited|lp|llp|plc|co|company|holdings|group|usa|us|na)\b"
ABBREV = {"intl": "international", "tech": "technologies", "svcs": "services"}
GENERIC_FIRST_WORDS = {
    "the", "new", "american", "national", "global", "international", "united",
    "university", "trustees", "board", "regents", "state", "first", "general",
    "technologies", "systems", "digital", "data", "advanced",
}


def normalize(name: str) -> str:
    s = name.lower()
    s = re.split(r"\bd/?b/?a\b", s)[-1]  # prefer the trade name when a d/b/a is present
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(SUFFIXES, " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return " ".join(ABBREV.get(w, w) for w in s.split() if w != "com")


def candidates(norm: str) -> list[tuple[str, str]]:
    """Ordered (token, kind) guesses. kind='full' spans the whole name, 'partial' does not."""
    words = norm.split()
    if not words:
        return []
    out: list[tuple[str, str]] = [("".join(words), "full")]
    if len(words) > 1:
        out.append(("-".join(words), "full"))
        if len(words) > 2:
            out.append(("".join(words[:2]), "partial"))  # "wal mart associates" -> "walmart"
        first = words[0]
        if len(first) >= 4 and first not in GENERIC_FIRST_WORDS:
            out.append((first, "partial"))
    seen = set()
    return [(t, k) for t, k in out if not (t in seen or seen.add(t))]


class RateLimiter:
    def __init__(self, rate: float):
        self.min_interval = 1.0 / rate
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.min_interval
        if delay:
            await asyncio.sleep(delay)


async def fetch(client: httpx.AsyncClient, limiter: RateLimiter, provider: str,
                method: str, url: str, **kw) -> dict:
    """Rate-limited request with disk cache and 429 backoff. Returns {status, body}."""
    key = hashlib.sha256(f"{method} {url}".encode()).hexdigest()[:24]
    path = CACHE / provider / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text())

    for attempt in range(4):
        await limiter.wait()
        try:
            r = await client.request(method, url, **kw)
        except httpx.HTTPError as e:
            if attempt == 3:
                rec = {"url": url, "status": -1, "body": f"error: {e!r}"}
                break
            await asyncio.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            retry = float(r.headers.get("retry-after", 2 ** (attempt + 1)))
            await asyncio.sleep(retry)
            continue
        rec = {"url": url, "status": r.status_code, "body": r.text[:200_000]}
        break
    else:
        rec = {"url": url, "status": 429, "body": "rate limited after retries"}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec))
    return rec


def name_score(employer_norm: str, found_name: str) -> float:
    return fuzz.token_set_ratio(employer_norm, normalize(found_name))


# --- per-provider probes: return dict(token, kind, confidence, matched_name) or None ---

async def probe_greenhouse(client, limiter, norm, cands):
    for token, kind in cands:
        rec = await fetch(client, limiter, "greenhouse", "GET",
                          f"https://boards-api.greenhouse.io/v1/boards/{token}")
        if rec["status"] == 200:
            try:
                board = json.loads(rec["body"]).get("name", "")
            except json.JSONDecodeError:
                continue
            score = name_score(norm, board)
            if score >= 60:
                return {"token": token, "kind": kind, "confidence": "verified", "matched_name": board}
    return None


async def probe_lever(client, limiter, norm, cands):
    for token, kind in cands:
        rec = await fetch(client, limiter, "lever", "GET",
                          f"https://api.lever.co/v0/postings/{token}?mode=json&limit=1")
        if rec["status"] == 200:
            try:
                body = json.loads(rec["body"])
            except json.JSONDecodeError:
                continue
            if isinstance(body, list) and body:  # empty board = stale/squatted, not coverage
                conf = "token" if kind == "full" else "low_confidence"
                return {"token": token, "kind": kind, "confidence": conf, "matched_name": ""}
    return None


async def probe_ashby(client, limiter, norm, cands):
    for token, kind in cands:
        rec = await fetch(client, limiter, "ashby", "GET",
                          f"https://api.ashbyhq.com/posting-api/job-board/{token}")
        if rec["status"] == 200:
            try:
                body = json.loads(rec["body"])
            except json.JSONDecodeError:
                continue
            if body.get("jobs"):
                conf = "token" if kind == "full" else "low_confidence"
                return {"token": token, "kind": kind, "confidence": conf, "matched_name": ""}
    return None


async def probe_smartrecruiters(client, limiter, norm, cands):
    for token, kind in cands:
        rec = await fetch(client, limiter, "smartrecruiters", "GET",
                          f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=1")
        if rec["status"] == 200:
            try:
                body = json.loads(rec["body"])
            except json.JSONDecodeError:
                continue
            if body.get("totalFound", 0) > 0:
                comp = (body.get("content") or [{}])[0].get("company", {}).get("name", "")
                score = name_score(norm, comp) if comp else 0
                if score >= 60:
                    return {"token": token, "kind": kind, "confidence": "verified", "matched_name": comp}
    return None


async def probe_workday(client, limiter, norm, cands):
    payload = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
    for token, kind in cands:
        token = token.replace("-", "")  # workday tenants are alnum
        for host in WORKDAY_HOSTS:
            url = f"https://{token}.{host}.myworkdayjobs.com/wday/cxs/{token}/zzprobe/jobs"
            rec = await fetch(client, limiter, "workday", "POST", url, json=payload)
            # tenant exists: 404/S21 (bad site id), or 200 (some tenants, e.g. walmart@wd5,
            # redirect unknown sites to a default page instead). No tenant: 422.
            hit = (rec["status"] == 404 and '"S21"' in rec["body"]) or (
                rec["status"] == 200
                and ("jobPostings" in rec["body"] or rec["body"].lstrip()[:15].lower().startswith("<!doctype"))
            )
            if hit:
                conf = "token" if kind == "full" else "low_confidence"
                return {"token": f"{token}@{host}", "kind": kind, "confidence": conf, "matched_name": ""}
    return None


PROBES = {
    "greenhouse": probe_greenhouse,
    "lever": probe_lever,
    "ashby": probe_ashby,
    "smartrecruiters": probe_smartrecruiters,
    "workday": probe_workday,
}


async def run_provider(provider, probe, employers, results):
    limiter = RateLimiter(RATE)
    timeout = httpx.Timeout(15.0)
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=timeout,
                                 follow_redirects=True) as client:
        for i, (name, norm, cands) in enumerate(employers):
            hit = await probe(client, limiter, norm, cands)
            results[name][provider] = hit
            if i % 50 == 0:
                print(f"[{provider}] {i}/{len(employers)}", flush=True)
    print(f"[{provider}] done", flush=True)


async def main():
    top = pl.read_csv(SPIKE / "top500_employers.csv")
    if limit := int(os.environ.get("LIMIT", "0")):
        top = top.head(limit)
    employers = []
    for name in top["EMPLOYER_NAME"]:
        norm = normalize(name)
        employers.append((name, norm, candidates(norm)))

    results: dict[str, dict] = {name: {} for name, _, _ in employers}
    await asyncio.gather(*(
        run_provider(p, probe, employers, results) for p, probe in PROBES.items()
    ))

    rows = []
    for row in top.iter_rows(named=True):
        name = row["EMPLOYER_NAME"]
        out = {
            "rank": row["rank"],
            "employer_name": name,
            "normalized_name": normalize(name),
            "lca_certified_count": row["lca_certified_count"],
            "employer_state": row["employer_state"],
        }
        for p in PROBES:
            hit = results[name].get(p)
            out[f"{p}_token"] = hit["token"] if hit else ""
            out[f"{p}_confidence"] = hit["confidence"] if hit else ""
            out[f"{p}_matched_name"] = hit["matched_name"] if hit else ""
        rows.append(out)

    pl.DataFrame(rows).write_csv(SPIKE / "ats_resolution.csv")
    print(f"wrote {SPIKE / 'ats_resolution.csv'}")


if __name__ == "__main__":
    asyncio.run(main())
