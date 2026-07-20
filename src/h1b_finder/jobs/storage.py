import json
from datetime import UTC, datetime

import duckdb


def _next_id(con: duckdb.DuckDBPyConnection, table: str, pk: str) -> int:
    return con.execute(f"SELECT COALESCE(MAX({pk}), 0) FROM {table}").fetchone()[0] + 1


def get_or_create_board(
    con: duckdb.DuckDBPyConnection, employer_id: int, provider: str, board_token: str,
    board_url: str | None = None, discovered_via: str | None = None,
) -> int:
    row = con.execute(
        "SELECT board_id FROM ats_board WHERE provider = ? AND board_token = ?",
        [provider, board_token],
    ).fetchone()
    if row:
        return row[0]
    board_id = _next_id(con, "ats_board", "board_id")
    con.execute(
        "INSERT INTO ats_board (board_id, employer_id, provider, board_token, board_url, "
        "discovered_via, is_active, last_polled_at, last_poll_status, consecutive_failures) "
        "VALUES (?,?,?,?,?,?,TRUE,NULL,NULL,0)",
        [board_id, employer_id, provider, board_token, board_url, discovered_via],
    )
    return board_id


def mark_poll_result(con: duckdb.DuckDBPyConnection, board_id: int, ok: bool, status: str,
                      consecutive_failures_limit: int) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    if ok:
        con.execute(
            "UPDATE ats_board SET last_polled_at=?, last_poll_status=?, consecutive_failures=0 WHERE board_id=?",
            [now, status, board_id],
        )
    else:
        row = con.execute("SELECT consecutive_failures FROM ats_board WHERE board_id=?", [board_id]).fetchone()
        failures = (row[0] or 0) + 1
        is_active = failures < consecutive_failures_limit
        con.execute(
            "UPDATE ats_board SET last_polled_at=?, last_poll_status=?, consecutive_failures=?, is_active=? "
            "WHERE board_id=?",
            [now, status, failures, is_active, board_id],
        )


def upsert_posting(con: duckdb.DuckDBPyConnection, *, employer_id: int, board_id: int,
                    external_id: str, title: str, description_text: str | None, apply_url: str,
                    location_raw: str | None, city: str | None, state: str | None,
                    postal_code: str | None, is_remote: bool | None,
                    salary_min: float | None, salary_max: float | None, salary_unit: str | None,
                    raw_json: dict) -> bool:
    """Upsert on (board_id, external_id): update last_seen_at (and clear closed_at,
    in case it reappeared) if it exists, insert a fresh row otherwise. Returns True
    if this is a newly-seen posting."""
    now = datetime.now(UTC).replace(tzinfo=None)
    existing = con.execute(
        "SELECT posting_id FROM job_posting WHERE board_id = ? AND external_id = ?",
        [board_id, external_id],
    ).fetchone()
    if existing:
        con.execute(
            "UPDATE job_posting SET last_seen_at = ?, closed_at = NULL WHERE posting_id = ?",
            [now, existing[0]],
        )
        return False

    posting_id = _next_id(con, "job_posting", "posting_id")
    con.execute(
        """INSERT INTO job_posting (
            posting_id, employer_id, board_id, external_id, title, description_text, apply_url,
            location_raw, city, state, postal_code, area_code, is_remote,
            soc_code_predicted, soc_confidence, soc_method,
            salary_min, salary_max, salary_unit, salary_source,
            mentions_no_sponsorship, mentions_clearance, mentions_sponsorship_ok,
            first_seen_at, last_seen_at, closed_at, raw_json
        ) VALUES (?,?,?,?,?,?,?, ?,?,?,?,NULL,?, NULL,NULL,NULL, ?,?,?,NULL, NULL,NULL,NULL, ?,?,NULL,?)""",
        [
            posting_id, employer_id, board_id, external_id, title, description_text, apply_url,
            location_raw, city, state, postal_code, is_remote,
            salary_min, salary_max, salary_unit,
            now, now, json.dumps(raw_json),
        ],
    )
    return True


def close_stale_postings(con: duckdb.DuckDBPyConnection, board_id: int, poll_started_at: datetime) -> int:
    """Any posting on this board not touched during this poll cycle has dropped
    off — soft-delete via closed_at, never a hard delete."""
    n = con.execute(
        "SELECT COUNT(*) FROM job_posting WHERE board_id = ? AND closed_at IS NULL AND last_seen_at < ?",
        [board_id, poll_started_at],
    ).fetchone()[0]
    con.execute(
        "UPDATE job_posting SET closed_at = ? WHERE board_id = ? AND closed_at IS NULL AND last_seen_at < ?",
        [datetime.now(UTC).replace(tzinfo=None), board_id, poll_started_at],
    )
    return n
