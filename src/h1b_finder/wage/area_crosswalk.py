import zipfile
from pathlib import Path

import duckdb
import numpy as np
import polars as pl

# Census Gazetteer NAME field is "<place> <LSAD>" (e.g. "Foster City city",
# "Abanda CDP") -- strip the trailing designator to get the bare place name.
_GAZETTEER_LSAD_SUFFIXES = (
    " city", " town", " village", " cdp", " borough", " municipality",
    " county", " government", " corporation", " urbana", " comunidad", " (balance)",
)
_EARTH_RADIUS_KM = 6371.0


def _strip_gazetteer_lsad_suffix(name: str) -> str:
    lowered = name.lower()
    for suffix in _GAZETTEER_LSAD_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)].strip()
    return name.strip()


def _haversine_km(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    lat1r, lon1r, lat2r, lon2r = np.radians(lat1), np.radians(lon1), np.radians(lat2), np.radians(lon2)
    dlat, dlon = lat2r - lat1r, lon2r - lon1r
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1r) * np.cos(lat2r) * np.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def load_ref_area(con: duckdb.DuckDBPyConnection, oflc_wage_zip: Path) -> int:
    """OFLC's Geography.csv (bundled in the wage zip) has one row per (Area,
    county) — an MSA spanning six counties appears six times. Collapse to one
    row per distinct Area code. area_type is read directly off the name ("...
    nonmetropolitan area" is OFLC's own BOS/nonmetro designation) rather than
    inferred from code-digit length, since leading zeros on state-FIPS-prefixed
    nonmetro codes get inconsistently dropped depending on how the exporting
    tool serialized them.
    """
    with zipfile.ZipFile(oflc_wage_zip) as zf, zf.open("Geography.csv") as f:
        df = pl.read_csv(f, infer_schema_length=0)
    # Multi-state MSAs (e.g. "New York-Newark-Jersey City, NY-NJ") appear once per
    # COUNTY, and those counties span multiple states — .unique(subset=["Area"])
    # would otherwise keep an arbitrary county's StateAb (whichever sorts first),
    # silently mislabeling New York's own metro as NJ. The area_name's own state
    # suffix ("NY-NJ" -> first token "NY") is OFLC's "first principal city"
    # convention and the only state label that's actually correct for an MSA.
    # Nonmetro (BOS) areas never straddle states, so StateAb is reliable there.
    areas = (
        df.unique(subset=["Area"])
        .select(
            pl.col("Area").alias("area_code"),
            pl.col("AreaName").alias("area_name"),
            pl.when(pl.col("AreaName").str.contains("(?i)nonmetropolitan"))
            .then(pl.lit("BOS"))
            .otherwise(pl.lit("MSA"))
            .alias("area_type"),
            pl.col("StateAb").alias("_county_state"),
            pl.col("AreaName").str.extract(r",\s*([A-Z]{2})(?:-[A-Z]{2})*\s*$", 1).alias("_msa_state"),
        )
        .with_columns(
            pl.when(pl.col("area_type") == "MSA")
            .then(pl.col("_msa_state").fill_null(pl.col("_county_state")))
            .otherwise(pl.col("_county_state"))
            .alias("state")
        )
        .select("area_code", "area_name", "area_type", "state")
    )
    con.register("areas_stage", areas)
    con.execute("INSERT INTO ref_area SELECT * FROM areas_stage")
    con.unregister("areas_stage")
    return areas.height


def load_ref_county_to_area(con: duckdb.DuckDBPyConnection, oflc_wage_zip: Path) -> int:
    """FIX 1 discovery: a real share of unresolvable postings have a COUNTY
    name in the city field, not a city at all ('Contra Costa, CA', 'Broward,
    FL', 'Wake, NC') — some ATS/aggregator sources give county-level location
    for suburban/rural postings instead of a specific city. Geography.csv
    (same OFLC wage zip already used for ref_area) has a CountyTownName column
    that load_ref_area() discards after using it only to dedupe Area rows —
    re-reading it keyed by (county, state) gives an exact crosswalk for free,
    no new data source needed. Verified 1:1 (zero counties span multiple
    Areas in this file), unlike ZIPs/cities which can straddle boundaries."""
    with zipfile.ZipFile(oflc_wage_zip) as zf, zf.open("Geography.csv") as f:
        df = pl.read_csv(f, infer_schema_length=0)
    counties = (
        df.select(
            pl.col("CountyTownName").str.strip_chars().str.to_uppercase().alias("county"),
            pl.col("StateAb").str.strip_chars().str.to_uppercase().alias("state"),
            pl.col("Area").alias("area_code"),
        )
        .unique(subset=["county", "state"])
    )
    con.register("county_area_stage", counties)
    con.execute("INSERT INTO ref_county_to_area SELECT * FROM county_area_stage")
    con.unregister("county_area_stage")
    return counties.height


def load_ref_zip_to_area(con: duckdb.DuckDBPyConnection, zip_cbsa_xlsx: Path) -> dict[str, int]:
    """HUD's ZIP-CBSA crosswalk: a ZIP straddling multiple CBSAs appears once per
    CBSA with res/bus/oth/tot_ratio columns. Collapse to one row per ZIP, keeping
    the CBSA with the highest tot_ratio, per the explicit instruction — anything
    else double-counts.

    geoid "99999" means "not in any CBSA" (rural). Those ZIPs, plus any geoid that
    doesn't match a published OFLC area, fall back to a single representative
    nonmetro (BOS) area for that state — OFLC publishes several nonmetro regions
    per state (Texas has six), and HUD's crosswalk gives city/state text only, no
    county, so there's no way to pick the *correct* one without a geocoding API.
    This is the "statewide BOS area" fallback the phase plan explicitly sanctions;
    it's a real precision loss for rural ZIPs, not a bug.
    """
    df = pl.read_excel(zip_cbsa_xlsx, infer_schema_length=0)
    df = df.with_columns(pl.col("tot_ratio").cast(pl.Float64))
    collapsed = df.sort("tot_ratio", descending=True).unique(subset=["zip"], keep="first")

    known_areas = set(con.execute("SELECT area_code FROM ref_area").pl()["area_code"])
    nonmetro = con.execute("SELECT area_code, state FROM ref_area WHERE area_type = 'BOS'").pl()
    fallback_by_state = dict(
        nonmetro.sort("area_code").group_by("state", maintain_order=True).first().select("state", "area_code").iter_rows()
    )

    def resolve(geoid: str, state: str) -> str | None:
        if geoid in known_areas:
            return geoid
        return fallback_by_state.get(state)

    collapsed = collapsed.with_columns(
        pl.struct(["geoid", "state"])
        .map_elements(lambda s: resolve(s["geoid"], s["state"]), return_dtype=pl.Utf8)
        .alias("area_code")
    )
    unresolved = collapsed.filter(pl.col("area_code").is_null()).height
    resolved = collapsed.filter(pl.col("area_code").is_not_null())

    stage = resolved.select("zip", "area_code")
    con.register("zip_stage", stage)
    con.execute("INSERT INTO ref_zip_to_area (zip, area_code, county_fips) SELECT zip, area_code, NULL FROM zip_stage")
    con.unregister("zip_stage")
    return {"loaded": stage.height, "unresolved_no_state_fallback": unresolved}


def load_ref_city_to_area(con: duckdb.DuckDBPyConnection, zip_cbsa_xlsx: Path) -> dict:
    """Real city->CBSA crosswalk (FIX 3), built from the SAME ZIP-CBSA file as
    load_ref_zip_to_area -- but that function discards the file's own 'city'
    column (USPS-preferred place name per ZIP) after using it only to resolve
    ZIPs. Re-reading it keyed by (city, state) gives a genuine local
    city->CBSA mapping, unlike find_area_by_city_state()'s old substring match
    against OFLC's own hyphenated principal-city MSA names -- which silently
    failed real, valid non-principal cities ("Palo Alto", "Mountain View" are
    in the San Jose MSA, but neither name is IN that MSA's own display string).

    Multiple ZIPs can share a city name and point at different CBSAs (a city
    straddling a metro boundary) -- sum tot_ratio per (city, state, geoid) and
    keep the dominant geoid, mirroring load_ref_zip_to_area's own
    highest-ratio-wins convention. Falls back to a representative nonmetro
    (BOS) area per state, same as load_ref_zip_to_area, when the dominant
    geoid isn't a recognized OFLC area.
    """
    df = pl.read_excel(zip_cbsa_xlsx, infer_schema_length=0)
    df = df.with_columns(
        pl.col("tot_ratio").cast(pl.Float64),
        pl.col("city").str.strip_chars().str.to_uppercase(),
        pl.col("state").str.strip_chars().str.to_uppercase(),
    )

    known_areas = set(con.execute("SELECT area_code FROM ref_area").pl()["area_code"])
    nonmetro = con.execute("SELECT area_code, state FROM ref_area WHERE area_type = 'BOS'").pl()
    fallback_by_state = dict(
        nonmetro.sort("area_code").group_by("state", maintain_order=True).first().select("state", "area_code").iter_rows()
    )

    dominant = (
        df.group_by(["city", "state", "geoid"])
        .agg(pl.col("tot_ratio").sum().alias("weight"))
        .sort("weight", descending=True)
        .unique(subset=["city", "state"], keep="first")
    )

    def resolve(geoid: str, state: str) -> str | None:
        if geoid in known_areas:
            return geoid
        return fallback_by_state.get(state)

    dominant = dominant.with_columns(
        pl.struct(["geoid", "state"])
        .map_elements(lambda s: resolve(s["geoid"], s["state"]), return_dtype=pl.Utf8)
        .alias("area_code")
    )
    unresolved = dominant.filter(pl.col("area_code").is_null()).height
    resolved = dominant.filter(pl.col("area_code").is_not_null())

    stage = resolved.select("city", "state", "area_code")
    con.register("city_area_stage", stage)
    con.execute("INSERT INTO ref_city_to_area SELECT * FROM city_area_stage")
    con.unregister("city_area_stage")
    return {"loaded": stage.height, "unresolved_no_state_fallback": unresolved}


def load_ref_gazetteer_place_to_area(
    con: duckdb.DuckDBPyConnection,
    gaz_places_txt: Path,
    gaz_zcta_txt: Path,
    zip_cbsa_xlsx: Path,
) -> dict:
    """FIX 1: bottom-of-chain fallback for genuine city/CDP names that neither
    ref_city_to_area (HUD ZIP-CBSA's own city column) nor ref_county_to_area
    cover -- e.g. Foster City, CA is attributed to "SAN MATEO" as its
    USPS-preferred mailing city in HUD's crosswalk (a real, different, small
    Foster City exists in Michigan and IS in that crosswalk under its own
    name), so the real Bay Area Foster City never resolves through it.

    No geocoding API: pure local nearest-neighbor by Haversine distance
    between two downloaded government coordinate sets — the Census Places
    gazetteer's own place centroid (INTPTLAT/INTPTLONG) vs the Census ZCTA
    gazetteer's own ZCTA centroids, bucketed by state to keep the search
    space small (each place only compared against ZCTAs in its own state).
    The winning ZCTA is then looked up in the already-loaded ref_zip_to_area
    for its area_code. distance_km is persisted as an audit trail — a huge
    winning distance would flag a place whose state has few/no covered ZCTAs.
    """
    places = pl.read_csv(gaz_places_txt, separator="|", infer_schema_length=0)
    places = places.with_columns(
        pl.col("NAME").map_elements(_strip_gazetteer_lsad_suffix, return_dtype=pl.Utf8)
        .str.to_uppercase().alias("place"),
        pl.col("USPS").str.to_uppercase().alias("state"),
        pl.col("INTPTLAT").cast(pl.Float64, strict=False),
        pl.col("INTPTLONG").cast(pl.Float64, strict=False),
    ).select("place", "state", "INTPTLAT", "INTPTLONG").drop_nulls()

    zctas = pl.read_csv(gaz_zcta_txt, separator="|", infer_schema_length=0)
    zctas = zctas.with_columns(
        pl.col("GEOID").alias("zcta"),
        pl.col("INTPTLAT").cast(pl.Float64, strict=False).alias("zlat"),
        pl.col("INTPTLONG").cast(pl.Float64, strict=False).alias("zlon"),
    ).select("zcta", "zlat", "zlon").drop_nulls()

    # The ZCTA gazetteer has no state column; the ZIP-CBSA crosswalk (already
    # used elsewhere) has a direct per-ZIP state, cheaper than re-deriving it
    # via ref_zip_to_area -> ref_area.
    zip_state = (
        pl.read_excel(zip_cbsa_xlsx, infer_schema_length=0)
        .select(pl.col("zip").alias("zcta"), pl.col("state").str.to_uppercase().alias("zstate"))
        .unique(subset=["zcta"])
    )
    zctas = zctas.join(zip_state, on="zcta", how="inner")

    known_area_by_zip: dict[str, str] = dict(con.execute("SELECT zip, area_code FROM ref_zip_to_area").fetchall())

    resolved_rows: list[tuple] = []
    unresolved = 0
    for state in places["state"].unique().to_list():
        state_places = places.filter(pl.col("state") == state)
        state_zctas = zctas.filter(pl.col("zstate") == state)
        if state_zctas.height == 0:
            unresolved += state_places.height
            continue

        plat, plon = state_places["INTPTLAT"].to_numpy(), state_places["INTPTLONG"].to_numpy()
        zlat, zlon = state_zctas["zlat"].to_numpy(), state_zctas["zlon"].to_numpy()
        zcta_codes = state_zctas["zcta"].to_list()

        dist = _haversine_km(plat[:, None], plon[:, None], zlat[None, :], zlon[None, :])
        nearest_idx = np.argmin(dist, axis=1)
        nearest_dist = dist[np.arange(len(nearest_idx)), nearest_idx]

        for i, place_name in enumerate(state_places["place"].to_list()):
            zcta = zcta_codes[nearest_idx[i]]
            area_code = known_area_by_zip.get(zcta)
            if area_code:
                resolved_rows.append((place_name, state, area_code, zcta, round(float(nearest_dist[i]), 2)))
            else:
                unresolved += 1

    df = pl.DataFrame(
        resolved_rows, schema=["place", "state", "area_code", "nearest_zcta", "distance_km"], orient="row"
    ).unique(subset=["place", "state"], keep="first")

    con.register("gaz_place_stage", df)
    con.execute("INSERT INTO ref_gazetteer_place_to_area SELECT * FROM gaz_place_stage")
    con.unregister("gaz_place_stage")
    return {"loaded": df.height, "unresolved": unresolved}


def find_area_by_city_state(con: duckdb.DuckDBPyConnection, city: str, state: str) -> str | None:
    """FIX 3/FIX 1 (Gazetteer batch): real city->CBSA lookup first
    (ref_city_to_area, built from HUD's own USPS-preferred city names), then
    the old OFLC hyphenated-MSA-name substring match, then a direct
    county->CBSA match (ref_county_to_area, FIX 1 discovery — some sources
    give a bare county name instead of a city, e.g. 'Contra Costa, CA'), then
    the Census Gazetteer nearest-place-centroid fallback for genuine city/CDP
    names neither crosswalk covers (ref_gazetteer_place_to_area).

    A plain substring match is ambiguous for city names that also appear inside
    a nonmetro region's own descriptive name — "New York, NY" matches the real
    NYC metro AND "Capital/Northern New York nonmetropolitan area" (nonmetro
    regions are often named after the state, not just a city). MSA is what a
    user means by a city name in the overwhelming majority of cases, so prefer
    it; only fall through to BOS if no MSA matches.
    """
    city_norm, state_norm = city.strip().upper(), state.strip().upper()

    row = con.execute(
        "SELECT area_code FROM ref_city_to_area WHERE city = ? AND state = ?",
        [city_norm, state_norm],
    ).fetchone()
    if row:
        return row[0]

    row = con.execute(
        "SELECT area_code FROM ref_area WHERE state = ? AND area_name ILIKE ? "
        "ORDER BY (area_type = 'MSA') DESC, area_code LIMIT 1",
        [state_norm, f"%{city.strip()}%"],
    ).fetchone()
    if row:
        return row[0]

    county_candidate = city_norm if city_norm.endswith(" COUNTY") else f"{city_norm} COUNTY"
    row = con.execute(
        "SELECT area_code FROM ref_county_to_area WHERE county = ? AND state = ?",
        [county_candidate, state_norm],
    ).fetchone()
    if row:
        return row[0]

    row = con.execute(
        "SELECT area_code FROM ref_gazetteer_place_to_area WHERE place = ? AND state = ?",
        [city_norm, state_norm],
    ).fetchone()
    return row[0] if row else None
