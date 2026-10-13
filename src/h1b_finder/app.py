"""Phase 6 — the dashboard. Read-only against scoring; the only writes this
file makes are to `application` (the tracker). Reads job_posting/job_score/
employer_h1b_profile directly (the same tables and WHERE/ORDER-BY logic as
v_ranked_jobs) rather than the view itself, because the view doesn't expose
posting_id/employer_id — needed here for the tracker and per-employer cap.

Local-only: connects to the live DuckDB and allows tracker writes. The
read-only snapshot shared with others is app_public.py.
"""

from datetime import UTC, datetime

import polars as pl
import streamlit as st

from h1b_finder.config import load_config
from h1b_finder.dashboard_common import (
    BASE_QUERY,
    CSS,
    SENIORITY_LABELS,
    SCORING_VERSION,
    apply_filters,
    render_job_card,
    sort_and_cap,
)
from h1b_finder.db import connect

st.set_page_config(page_title="H-1B Job Finder", page_icon="🎯", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)


@st.cache_resource
def get_connection():
    cfg = load_config()
    return connect(cfg.db_path)


con = get_connection()


@st.cache_data(ttl=600)
def fetch_all() -> pl.DataFrame:
    return con.execute(BASE_QUERY).pl()


@st.cache_data(ttl=600)
def fetch_evidence(employer_ids: tuple[int, ...]) -> pl.DataFrame:
    if not employer_ids:
        return pl.DataFrame()
    placeholders = ",".join("?" * len(employer_ids))
    return con.execute(
        f"""SELECT employer_id, evidence_type, confidence, detail
            FROM cap_exempt_evidence WHERE employer_id IN ({placeholders})
            ORDER BY confidence DESC""",
        list(employer_ids),
    ).pl()


@st.cache_data(ttl=600)
def fetch_meta() -> dict:
    scored_at = con.execute(
        f"SELECT MAX(scored_at) FROM job_score WHERE scoring_version = '{SCORING_VERSION}'"
    ).fetchone()[0]
    horizon = con.execute("SELECT MAX(decision_date) FROM lca_case").fetchone()[0]
    return {"scored_at": scored_at, "lca_horizon": horizon}


def application_statuses() -> dict[int, str]:
    rows = con.execute("SELECT posting_id, status FROM application").fetchall()
    return dict(rows)


def set_status(posting_id: int, employer_id: int, status: str) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    existing = con.execute(
        "SELECT application_id FROM application WHERE posting_id = ?", [posting_id]
    ).fetchone()
    if existing:
        con.execute(
            "UPDATE application SET status = ?, updated_at = ? WHERE application_id = ?",
            [status, now, existing[0]],
        )
    else:
        next_id = con.execute("SELECT COALESCE(MAX(application_id), 0) + 1 FROM application").fetchone()[0]
        con.execute(
            "INSERT INTO application (application_id, posting_id, employer_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [next_id, posting_id, employer_id, status, now, now],
        )


# --- Sidebar filters ---
all_df = fetch_all()
meta = fetch_meta()

st.sidebar.header("Filters")
cap_exempt_only = st.sidebar.checkbox("Cap-exempt only", value=False)
min_tickets = st.sidebar.select_slider(
    "Minimum lottery tickets", options=[0, 1, 2, 3, 4], value=0,
    help="0 = no minimum, keeps not-yet-scored roles visible. Above 0 excludes them. "
         "Independent of the seniority filter below — combine freely.",
)
seniority_options = ["entry", "mid", "senior", "staff_plus"]
seniority_filter = st.sidebar.multiselect(
    "Seniority", options=seniority_options,
    default=["entry", "mid"],
    format_func=lambda t: SENIORITY_LABELS[t],
    help="Display/filter only — derived from title (regex) with Haiku for ambiguous titles. "
         "Does not affect lottery tickets, cap-exempt status, or ranking.",
)
states = ["All"] + sorted(all_df["state"].drop_nulls().unique().to_list())
state_filter = st.sidebar.selectbox("State (resolved location only)", states)
soc_options = ["All"] + all_df.select("soc_code_predicted", "soc_title").drop_nulls().unique().sort("soc_title")["soc_title"].to_list()
soc_title_filter = st.sidebar.selectbox("Predicted role (SOC)", soc_options)
soc_code_filter = "All"
if soc_title_filter != "All":
    match = all_df.filter(pl.col("soc_title") == soc_title_filter).select("soc_code_predicted").row(0)
    soc_code_filter = match[0]
remote_only = st.sidebar.checkbox("Remote only", value=False, help="Text-matched on the posting's location string — there is no structured remote flag in the source data.")
hide_staffing = st.sidebar.checkbox("Hide staffing firms", value=False)
staffing_threshold = 0.6
if hide_staffing:
    staffing_threshold = st.sidebar.slider("Staffing-score threshold", 0.0, 1.0, 0.6, 0.05)
posted_salary_only = st.sidebar.checkbox("Posted salary only", value=False)

st.sidebar.divider()
show_all_per_employer = st.sidebar.checkbox("Show all postings per employer (lifts the cap of 4)", value=False)

# --- Header ---
st.title("\U0001f3af H-1B Job Finder")
st.caption(
    f"Scored {meta['scored_at']:%Y-%m-%d %H:%M} · LCA data horizon {meta['lca_horizon']} "
    f"· {all_df.height} postings in the ranked universe"
)

statuses = application_statuses()
with st.expander(f"\U0001f4cb My tracker ({sum(1 for s in statuses.values() if s in ('interested', 'applied', 'rejected'))} marked)"):
    if not statuses:
        st.caption("Nothing marked yet — use the buttons on a posting below.")
    else:
        tracked_ids = tuple(statuses.keys())
        tracked_df = all_df.filter(pl.col("posting_id").is_in(tracked_ids))
        for status_key, status_label in [("interested", "Interested"), ("applied", "Applied"), ("rejected", "Rejected")]:
            ids = [pid for pid, s in statuses.items() if s == status_key]
            if not ids:
                continue
            st.markdown(f"**{status_label}**")
            for r in tracked_df.filter(pl.col("posting_id").is_in(ids)).iter_rows(named=True):
                st.markdown(f"- {r['canonical_name']} — {r['title']}")

filtered = apply_filters(
    all_df, cap_exempt_only, min_tickets, seniority_filter, state_filter, soc_code_filter,
    remote_only, hide_staffing, staffing_threshold, posted_salary_only,
)

cap = None if show_all_per_employer else 4
exempt_df = sort_and_cap(filtered.filter(pl.col("cap_exempt_score_sort").fill_null(0) > 0.5), cap)
subject_df = sort_and_cap(filtered.filter(pl.col("cap_exempt_score_sort").fill_null(0) <= 0.5), cap)

evidence_employer_ids = tuple(
    filtered.filter(pl.col("cap_exempt_score").fill_null(0) > 0)["employer_id"].unique().to_list()
)
evidence_df = fetch_evidence(evidence_employer_ids)

PAGE_SIZE = 25
if "exempt_shown" not in st.session_state:
    st.session_state.exempt_shown = PAGE_SIZE
if "subject_shown" not in st.session_state:
    st.session_state.subject_shown = PAGE_SIZE

st.markdown(
    f'<div class="section-header-exempt"><strong>\U0001f393 Cap-Exempt — No Lottery</strong> '
    f"({exempt_df.height} postings)</div>",
    unsafe_allow_html=True,
)
if exempt_df.height == 0:
    st.caption("No cap-exempt postings match the current filters.")
else:
    for row in exempt_df.head(st.session_state.exempt_shown).iter_rows(named=True):
        render_job_card(st, row, evidence_df, statuses.get(row["posting_id"]), set_status)
    if exempt_df.height > st.session_state.exempt_shown:
        if st.button(f"Show more ({exempt_df.height - st.session_state.exempt_shown} remaining)", key="more_exempt"):
            st.session_state.exempt_shown += PAGE_SIZE
            st.rerun()

if not cap_exempt_only:
    st.markdown(
        f'<div class="section-header-subject"><strong>\U0001f3ab Cap-Subject — Lottery Odds</strong> '
        f"({subject_df.height} postings)</div>",
        unsafe_allow_html=True,
    )
    if subject_df.height == 0:
        st.caption("No cap-subject postings match the current filters.")
    else:
        for row in subject_df.head(st.session_state.subject_shown).iter_rows(named=True):
            render_job_card(st, row, evidence_df, statuses.get(row["posting_id"]), set_status)
        if subject_df.height > st.session_state.subject_shown:
            if st.button(f"Show more ({subject_df.height - st.session_state.subject_shown} remaining)", key="more_subject"):
                st.session_state.subject_shown += PAGE_SIZE
                st.rerun()
