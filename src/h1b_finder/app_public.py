"""Read-only public dashboard — reads a static snapshot exported by
`uv run h1b export snapshot` (see cli.py), never the live DuckDB. No
secrets, no DB connection, no writes: this is what gets deployed to
Streamlit Community Cloud so other people can look at the ranked list
without touching the maintainer's local data or application tracker.

Refresh is manual: re-run the export command locally and git push whenever
the maintainer wants viewers to see newer data (see public/README.md).
"""

import json
from pathlib import Path

import polars as pl
import streamlit as st

from h1b_finder.dashboard_common import (
    CSS,
    SENIORITY_LABELS,
    apply_filters,
    render_job_card,
    sort_and_cap,
)

SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "public"

st.set_page_config(page_title="H-1B Job Finder", page_icon="🎯", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)


@st.cache_data
def load_snapshot() -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    jobs_path = SNAPSHOT_DIR / "jobs.parquet"
    evidence_path = SNAPSHOT_DIR / "evidence.parquet"
    meta_path = SNAPSHOT_DIR / "meta.json"
    if not jobs_path.exists():
        st.error(
            f"No snapshot found at {jobs_path}. Run `uv run h1b export snapshot` "
            "locally and commit the result before deploying."
        )
        st.stop()
    jobs = pl.read_parquet(jobs_path)
    evidence = pl.read_parquet(evidence_path) if evidence_path.exists() else pl.DataFrame()
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    return jobs, evidence, meta


all_df, evidence_df, meta = load_snapshot()

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
    f"Read-only snapshot · scored {meta.get('scored_at', 'unknown')} · "
    f"LCA data horizon {meta.get('lca_horizon', 'unknown')} · "
    f"snapshot exported {meta.get('exported_at', 'unknown')} · {all_df.height} postings"
)
st.info(
    "This is a shared, read-only view — a point-in-time snapshot, not the live tool. "
    "There's no application tracker here; it updates only when the maintainer re-exports it.",
    icon="ℹ️",
)

filtered = apply_filters(
    all_df, cap_exempt_only, min_tickets, seniority_filter, state_filter, soc_code_filter,
    remote_only, hide_staffing, staffing_threshold, posted_salary_only,
)

cap = None if show_all_per_employer else 4
exempt_df = sort_and_cap(filtered.filter(pl.col("cap_exempt_score_sort").fill_null(0) > 0.5), cap)
subject_df = sort_and_cap(filtered.filter(pl.col("cap_exempt_score_sort").fill_null(0) <= 0.5), cap)

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
        render_job_card(st, row, evidence_df)
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
            render_job_card(st, row, evidence_df)
        if subject_df.height > st.session_state.subject_shown:
            if st.button(f"Show more ({subject_df.height - st.session_state.subject_shown} remaining)", key="more_subject"):
                st.session_state.subject_shown += PAGE_SIZE
                st.rerun()
