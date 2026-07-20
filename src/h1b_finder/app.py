"""Phase 6 — the dashboard. Read-only against scoring; the only writes this
file makes are to `application` (the tracker). Reads job_posting/job_score/
employer_h1b_profile directly (the same tables and WHERE/ORDER-BY logic as
v_ranked_jobs) rather than the view itself, because the view doesn't expose
posting_id/employer_id — needed here for the tracker and per-employer cap.
"""

import json
from datetime import UTC, datetime

import polars as pl
import streamlit as st

from h1b_finder.config import load_config
from h1b_finder.db import connect

SCORING_VERSION = "v0.1.0"

WAGE_NULL_REASONS = {
    "soc_unknown": "SOC not identified",
    "salary_unknown": "no salary data",
    "area_unresolved": "location not resolved",
    "no_wage_data": "no wage table for this SOC/area",
}

SENIORITY_LABELS = {
    "entry": "Entry",
    "mid": "Mid",
    "senior": "Senior",
    "staff_plus": "Staff+",
}

EVIDENCE_LABELS = {
    "ipeds_match": "IPEDS (accredited institution)",
    "ffrdc_match": "FFRDC (federally funded R&D center)",
    "coth_match": "IRS BMF hospital (E20-E22)",
    "irs_research_nonprofit": "IRS BMF research nonprofit (NTEE U/H)",
    "manual_confirmed": "manually confirmed",
    "off_cycle_filings": "off-cycle filing pattern",
    "naics_signal": "NAICS signal",
}

st.set_page_config(page_title="H-1B Job Finder", page_icon="🎯", layout="wide")

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

html, body, [class*="css"] { font-family: 'Inter', -apple-system, sans-serif; }
.mono { font-family: 'JetBrains Mono', monospace; }

h1, h2, h3 { font-weight: 600; letter-spacing: -0.01em; }

.section-header-exempt {
    background: linear-gradient(90deg, rgba(16,90,58,0.12), rgba(16,90,58,0.02));
    border-left: 4px solid #1f7a4d;
    padding: 0.6rem 1rem;
    border-radius: 6px;
    margin: 1.2rem 0 0.8rem 0;
}
.section-header-subject {
    background: rgba(127,127,127,0.06);
    border-left: 4px solid #888;
    padding: 0.6rem 1rem;
    border-radius: 6px;
    margin: 1.2rem 0 0.8rem 0;
}

.job-card {
    border: 1px solid rgba(127,127,127,0.25);
    border-radius: 10px;
    padding: 0.9rem 1.2rem 0.6rem 1.2rem;
    margin-bottom: 0.6rem;
    background: rgba(127,127,127,0.025);
}
.job-employer { font-size: 0.82rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; opacity: 0.75; }
.job-title { font-size: 1.08rem; font-weight: 600; margin: 0.1rem 0 0.45rem 0; }

.badge {
    display: inline-block;
    font-size: 0.76rem;
    font-weight: 600;
    padding: 0.15rem 0.55rem;
    border-radius: 20px;
    margin-right: 0.35rem;
    margin-bottom: 0.25rem;
}
.badge-exempt { background: rgba(31,122,77,0.15); color: #1f7a4d; }
.badge-tickets-4 { background: rgba(31,122,77,0.15); color: #1f7a4d; }
.badge-tickets-3 { background: rgba(60,110,160,0.15); color: #2c5f8a; }
.badge-tickets-2 { background: rgba(150,120,20,0.15); color: #8a6d1a; }
.badge-tickets-1 { background: rgba(150,90,40,0.15); color: #8a5228; }
.badge-none { background: rgba(127,127,127,0.13); color: #777; font-weight: 500; font-style: italic; }
.badge-salary-posted { background: rgba(31,122,77,0.12); color: #1f7a4d; }
.badge-salary-inferred { background: rgba(127,127,127,0.1); color: #777; border: 1px dashed rgba(127,127,127,0.4); }
.badge-staffing { background: rgba(160,60,40,0.12); color: #a03c28; }
.badge-seniority { background: rgba(90,90,140,0.12); color: #4a4a8a; }

.note-muted { font-size: 0.8rem; opacity: 0.65; margin-top: 0.15rem; }
.note-offcycle { font-size: 0.78rem; opacity: 0.7; font-style: italic; margin-top: 0.2rem; }
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_resource
def get_connection():
    cfg = load_config()
    return connect(cfg.db_path)


con = get_connection()

BASE_QUERY = f"""
    SELECT
        jp.posting_id, jp.employer_id, e.canonical_name,
        jp.title, jp.location_raw, jp.state, jp.apply_url,
        jp.salary_min, jp.salary_max, jp.salary_unit, jp.salary_source,
        jp.soc_code_predicted, rs.soc_title,
        jp.seniority_tier,
        js.lottery_tickets, js.fit_score, js.total_score,
        js.staffing_firm_score, js.explanation,
        p.cap_exempt_score, p.cap_exempt_score_sort, p.perm_to_lca_ratio,
        jp.first_seen_at
    FROM job_posting jp
    JOIN employer e             ON e.employer_id = jp.employer_id
    JOIN job_score js            ON js.posting_id = jp.posting_id AND js.scoring_version = '{SCORING_VERSION}'
    LEFT JOIN employer_h1b_profile p ON p.employer_id = jp.employer_id
    LEFT JOIN ref_soc rs         ON rs.soc_code = jp.soc_code_predicted
    WHERE jp.closed_at IS NULL
      AND jp.mentions_no_sponsorship IS NOT TRUE
      AND jp.mentions_clearance      IS NOT TRUE
      AND (p.cap_exempt_score > 0.5 OR p.lca_target_soc_24mo > 0)
"""


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


def format_salary(row: dict) -> tuple[str, str]:
    lo, hi, unit, source = row["salary_min"], row["salary_max"], row["salary_unit"], row["salary_source"]
    if lo is None and hi is None:
        return "no salary data", "badge-salary-inferred"
    unit_label = "/hr" if unit == "hour" else "/yr"
    if lo is not None and hi is not None and lo != hi:
        text = f"${lo:,.0f}–${hi:,.0f}{unit_label}"
    else:
        text = f"${(lo or hi):,.0f}{unit_label}"
    if source == "posted":
        return f"{text} · posted", "badge-salary-posted"
    label = {"inferred_lca": "inferred from LCA", "inferred_model": "inferred (model)"}.get(source, "inferred")
    return f"{text} · {label}", "badge-salary-inferred"


def format_tickets(row: dict) -> tuple[str, str]:
    tickets = row["lottery_tickets"]
    if tickets is None:
        explanation = json.loads(row["explanation"]) if row["explanation"] else {}
        reason_code = explanation.get("wage_level", {}).get("reason")
        reason = WAGE_NULL_REASONS.get(reason_code, "insufficient data")
        return f"not scored — {reason}", "badge-none"
    level_roman = {1: "I", 2: "II", 3: "III", 4: "IV"}.get(tickets, str(tickets))
    plural = "ticket" if tickets == 1 else "tickets"
    return f"Level {level_roman} · {tickets} {plural}", f"badge-tickets-{tickets}"


def is_real_cap_exempt(row: dict) -> bool:
    sort_val = row["cap_exempt_score_sort"]
    return sort_val is not None and sort_val > 0.5


def has_offcycle_only(row: dict) -> bool:
    display = row["cap_exempt_score"] or 0
    sort_val = row["cap_exempt_score_sort"] or 0
    return display > 0 and sort_val <= 0.5


def render_evidence(employer_id: int, evidence_df: pl.DataFrame) -> None:
    rows = evidence_df.filter(pl.col("employer_id") == employer_id)
    if rows.height == 0:
        st.caption("No cap-exempt evidence on file.")
        return
    for r in rows.iter_rows(named=True):
        label = EVIDENCE_LABELS.get(r["evidence_type"], r["evidence_type"])
        detail = json.loads(r["detail"]) if r["detail"] else {}
        inst = detail.get("institution_name")
        extra = f" — matched: {inst}" if inst else ""
        st.caption(f"**{label}** (confidence {r['confidence']:.2f}){extra}")


def render_job_card(row: dict, evidence_df: pl.DataFrame, statuses: dict[int, str]) -> None:
    posting_id = row["posting_id"]
    employer_id = row["employer_id"]

    with st.container(border=False):
        st.markdown('<div class="job-card">', unsafe_allow_html=True)

        badges = []
        real_exempt = is_real_cap_exempt(row)
        if real_exempt:
            badges.append('<span class="badge badge-exempt">\U0001f393 Cap-Exempt — No Lottery</span>')

        tickets_text, tickets_class = format_tickets(row)
        badges.append(f'<span class="badge {tickets_class}">{tickets_text}</span>')

        salary_text, salary_class = format_salary(row)
        badges.append(f'<span class="badge {salary_class}">{salary_text}</span>')

        if row["soc_title"]:
            badges.append(f'<span class="badge badge-none">{row["soc_title"]}</span>')

        seniority_label = SENIORITY_LABELS.get(row["seniority_tier"])
        if seniority_label:
            badges.append(f'<span class="badge badge-seniority">{seniority_label}</span>')

        staffing = row["staffing_firm_score"]
        if staffing is not None and staffing >= 0.5:
            badges.append(f'<span class="badge badge-staffing">staffing-adjacent ({staffing:.2f})</span>')

        loc = row["location_raw"] or "location unknown"

        st.markdown(f'<div class="job-employer">{row["canonical_name"]}</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="job-title">{row["title"]}</div>', unsafe_allow_html=True)
        st.markdown(" ".join(badges), unsafe_allow_html=True)
        st.markdown(f'<div class="note-muted">{loc}</div>', unsafe_allow_html=True)

        if has_offcycle_only(row):
            st.markdown(
                '<div class="note-offcycle">Off-cycle LCA filing pattern detected for this employer — '
                "weak, corroborating-only signal. Not treated as cap-exempt on that basis alone.</div>",
                unsafe_allow_html=True,
            )

        ratio = row["perm_to_lca_ratio"]
        if ratio is None or ratio == 0:
            gc_text = "Green card follow-through: thin/no PERM data (not evidence they won't sponsor — our PERM window is only ~18 months)"
        else:
            gc_text = f"Green card follow-through: {ratio:.0%} of LCA volume also has PERM filings"
        st.markdown(f'<div class="note-muted">{gc_text}</div>', unsafe_allow_html=True)

        st.markdown("</div>", unsafe_allow_html=True)

        cols = st.columns([1.3, 1, 1, 1, 2])
        with cols[0]:
            st.link_button("Apply ↗", row["apply_url"], use_container_width=True)

        current_status = statuses.get(posting_id)
        status_options = [("interested", "Interested"), ("applied", "Applied"), ("rejected", "Rejected")]
        for i, (status_key, status_label) in enumerate(status_options):
            with cols[1 + i]:
                is_active = current_status == status_key
                if st.button(
                    ("✓ " if is_active else "") + status_label,
                    key=f"{status_key}_{posting_id}",
                    type="primary" if is_active else "secondary",
                    use_container_width=True,
                ):
                    set_status(posting_id, employer_id, status_key)
                    st.rerun()

        if real_exempt or (row["cap_exempt_score"] or 0) > 0:
            with st.expander("Why cap-exempt?"):
                render_evidence(employer_id, evidence_df)


def apply_filters(
    df: pl.DataFrame,
    cap_exempt_only: bool,
    min_tickets: int,
    seniority_tiers: list[str],
    state: str,
    soc_code: str,
    remote_only: bool,
    hide_staffing: bool,
    staffing_threshold: float,
    posted_salary_only: bool,
) -> pl.DataFrame:
    if cap_exempt_only:
        df = df.filter(pl.col("cap_exempt_score_sort").fill_null(0) > 0.5)
    if min_tickets > 0:
        df = df.filter(pl.col("lottery_tickets").fill_null(-1) >= min_tickets)
    # No "empty means no filter" special case: an empty selection honestly
    # means zero rows, same as any other multiselect.
    df = df.filter(pl.col("seniority_tier").is_in(seniority_tiers))
    if state != "All":
        df = df.filter(pl.col("state") == state)
    if soc_code != "All":
        df = df.filter(pl.col("soc_code_predicted") == soc_code)
    if remote_only:
        df = df.filter(pl.col("location_raw").str.to_lowercase().str.contains("remote").fill_null(False))
    if hide_staffing:
        df = df.filter(pl.col("staffing_firm_score").fill_null(0) < staffing_threshold)
    if posted_salary_only:
        df = df.filter(pl.col("salary_source") == "posted")
    return df


def sort_and_cap(df: pl.DataFrame, cap: int | None) -> pl.DataFrame:
    df = df.sort(
        by=["cap_exempt_score_sort", "lottery_tickets", "fit_score", "staffing_firm_score"],
        descending=[True, True, True, False],
        nulls_last=True,
    )
    if cap is not None:
        df = df.with_columns(pl.col("employer_id").cum_count().over("employer_id").alias("_emp_seq"))
        df = df.filter(pl.col("_emp_seq") <= cap)
    return df


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
        render_job_card(row, evidence_df, statuses)
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
            render_job_card(row, evidence_df, statuses)
        if subject_df.height > st.session_state.subject_shown:
            if st.button(f"Show more ({subject_df.height - st.session_state.subject_shown} remaining)", key="more_subject"):
                st.session_state.subject_shown += PAGE_SIZE
                st.rerun()
