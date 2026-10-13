# Public dashboard data

`jobs.parquet`, `evidence.parquet`, `meta.json` are a point-in-time snapshot
read by `app_public.py` (the dashboard deployed to Streamlit Community
Cloud). They are committed to git on purpose — Streamlit Cloud only has
what's in this repo, never the local `data/h1b.duckdb`.

**To refresh what people see:**

```bash
uv run h1b export snapshot
git add public/
git commit -m "refresh public snapshot"
git push
```

Streamlit Cloud auto-redeploys on push. There is no automatic refresh —
run this whenever you want the public view to catch up to local data.
