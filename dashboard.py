"""LNG Feedgas + Cross-Border Dashboard — Plotly Dash app.

Run with:
    python dashboard.py

Then open http://localhost:8050 in a browser.

Reads from data/feedgas.db (the SQLite written by `python -m ng_feedgas pull`).
No live scraping happens here — refresh the data by running the pull command.

Terminal nameplates are loaded dynamically from src/ng_feedgas/config/meter_points.yaml
so the dashboard automatically picks up new terminals as you add them to the YAML.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import dash
import dash_bootstrap_components as dbc
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import yaml
from dash import Input, Output, State, dash_table, dcc, html

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "feedgas.db"
YAML_PATH = ROOT / "src" / "ng_feedgas" / "config" / "meter_points.yaml"

# Make the ng_feedgas package importable so we reuse the deviation logic in
# src/ng_feedgas/analysis/changes.py rather than duplicating it here.
sys.path.insert(0, str(ROOT / "src"))


# ---------- terminal taxonomy ----------

def categorize(terminal: str) -> str:
    if terminal.startswith("Mexico"):
        return "Mexico exports"
    if terminal.startswith("Canada"):
        return "Canada border"
    return "U.S. LNG"


def load_terminal_catalog() -> tuple[dict[str, float], list[str], dict[str, str]]:
    """Returns (nameplates, ordered list, category_map)."""
    if not YAML_PATH.exists():
        return {}, [], {}
    raw = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    nameplates: dict[str, float] = {}
    cat_map: dict[str, str] = {}
    order: list[str] = []
    for t in raw.get("terminals", []):
        name = t["name"]
        nameplates[name] = float(t["nameplate_mmcfd"])
        cat_map[name] = categorize(name)
        order.append(name)
    # Stable category order: LNG first, then Mexico, then Canada
    category_rank = {"U.S. LNG": 0, "Mexico exports": 1, "Canada border": 2}
    order.sort(key=lambda n: (category_rank.get(cat_map[n], 9), n))
    return nameplates, order, cat_map


NAMEPLATES, TERMINAL_ORDER, TERMINAL_CATEGORY = load_terminal_catalog()
CATEGORIES = ["U.S. LNG", "Mexico exports", "Canada border"]
ALL_REGIONS = "All regions"
CATEGORY_COLORS = {
    "U.S. LNG":        "#2E86AB",
    "Mexico exports":  "#E76F51",
    "Canada border":   "#2A9D8F",
    ALL_REGIONS:       "#343a40",
}


def _region_mask(frame: pd.DataFrame, category: str) -> pd.Series:
    """Boolean mask selecting rows for a region tab; ALL_REGIONS selects all."""
    if category == ALL_REGIONS:
        return pd.Series(True, index=frame.index)
    return frame["category"] == category


# Per-terminal data-confidence tier. Drives the green/amber/gray outlines.
#
# The distinction is PHYSICAL, not about scraper quality:
#   high    — a single metered flow where that one meter IS the entire volume
#             at the point. True only for border crossings (one pipe, one meter).
#   partial — an LNG terminal fed by MULTIPLE pipes. We sum the pipelines with
#             public EBBs, but private/intrastate feeders may be missing, so the
#             total is a LOWER BOUND, not the true feedgas. (e.g. Sabine's Creole
#             Trail Pipeline is Cheniere-private and carries ~1.5-2 Bcf/d we
#             cannot see — captured total runs ~57% of actual.)
#   none    — no scraper coverage yet; bar will be empty.
CONFIDENCE = {
    # U.S. LNG — ALL amber: multi-fed terminals, captured total is a lower bound.
    "Sabine Pass":    "partial",   # Creole Trail Pipeline (Cheniere) is private
    "Plaquemines":    "partial",   # Gator Express aggregator; TETCO->Gator excluded
    "Freeport LNG":   "partial",   # also fed by TX intrastates (Gulf South etc.)
    "Cove Point":     "partial",   # dominant Transco feed captured; DTI not
    "Elba Island":    "partial",   # EEC captured; other interconnects not verified
    "Cameron LNG":    "partial",   # Cameron Interstate Pipeline (CIP) is private
    "Corpus Christi": "partial",   # Cheniere CCPL (dominant feed) is private
    "Calcasieu Pass": "none",      # Venture Global TransCameron private
    "Golden Pass":    "none",      # no scraper yet (gasnom.com ColdFusion)
    # Mexico exports — each crossing is a single metered border flow = exact.
    "Mexico - Sasabe (Sierrita)":          "high",
    "Mexico - North Baja (EPNG)":          "high",
    "Mexico - Samalayuca (EPNG via IEnova)": "high",
    "Mexico - Cananea (EPNG via Douglas)": "high",
    "Mexico - Willcox (EPNG to CENAGAS)":  "high",
    "Mexico - El Fresnal (EPNG to CFE)":   "high",
    "Mexico - Mendoza Trail (EPNG to KMTP)": "high",
    # Canada border — single metered border crossings = exact.
    "Canada - Niagara (TGP delivery to TC Mainline)": "high",
    "Canada - Sumas (Northwest)":                     "high",
    "Canada - Waddington (Iroquois)":                 "high",
    "Canada - Chippawa (Empire)":                     "none",  # no scraper yet
    "Canada - Emerson (Viking/GreatLakes/NorthernBorder)": "none",  # no scraper yet
}

CONFIDENCE_COLOR = {
    "high":    "#1B998B",   # green  — single metered flow, captures entire volume
    "partial": "#E9C46A",   # amber  — multi-fed LNG terminal, LOWER BOUND only
    "none":    "#ADB5BD",   # gray   — not captured
}

# Terminals whose total IS the true physical flow (single metered crossings).
TRUSTED = {t for t, c in CONFIDENCE.items() if c == "high"}


def short_name(t: str) -> str:
    """Compact label for axis ticks / legends.

    Long names like 'Canada - Emerson (Viking/GreatLakes/NorthernBorder)' are
    unreadable on stacked-bar tick labels; this strips the parenthetical and
    prefixes a 2-letter region code.
    """
    if t.startswith("Mexico - "):
        body = t[len("Mexico - "):].split("(")[0].strip()
        return f"MX-{body}"
    if t.startswith("Canada - "):
        body = t[len("Canada - "):].split("(")[0].strip()
        return f"CA-{body}"
    return t


SHORT = {t: short_name(t) for t in TERMINAL_ORDER}


# ---------- data loading ----------

def load_flows() -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame(columns=[
            "gas_day", "cycle", "terminal", "pipeline", "meter_point",
            "mmcfd", "direction", "scraped_at", "source_url",
        ])
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query("SELECT * FROM flows", conn)
    if not df.empty:
        df["gas_day"] = pd.to_datetime(df["gas_day"]).dt.date
        df["category"] = df["terminal"].map(TERMINAL_CATEGORY).fillna("U.S. LNG")
    return df


def load_eia_weekly() -> pd.DataFrame:
    """Returns EIA monthly LNG export totals (US Bcf/d). Empty if not yet fetched.

    Table is named eia_weekly for historical reasons; data is now monthly since
    EIA retired the legacy weekly series.
    """
    if not DB_PATH.exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            df = pd.read_sql_query(
                "SELECT week_ending, us_lng_bcfd FROM eia_weekly ORDER BY week_ending",
                conn,
            )
        if not df.empty:
            df["week_ending"] = pd.to_datetime(df["week_ending"]).dt.date
        return df
    except Exception:
        return pd.DataFrame()


def load_ais_inference() -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            df = pd.read_sql_query(
                "SELECT gas_day, terminal, ship_at_berth, moored_hours, "
                "est_feedgas_mmcfd, computed_at FROM ais_daily_inference",
                conn,
            )
        if not df.empty:
            df["gas_day"] = pd.to_datetime(df["gas_day"]).dt.date
        return df
    except Exception:
        return pd.DataFrame()


def terminal_totals(df: pd.DataFrame) -> pd.DataFrame:
    """Per-day, per-terminal sum.

    For US LNG + Mexico + Canada exports: only count direction='delivery'.
    For Canada imports (terminal name 'Canada - Sumas...' / 'Canada - Waddington...'
    with rows in direction='receipt'): count those too.
    Effectively: count both directions but separately by terminal/direction.
    """
    if df.empty:
        return df
    # The "useful" flow for a terminal depends on what kind of terminal it is.
    # For now: sum direction='delivery' for LNG + Mexico exports + Canada exports,
    # sum direction='receipt' for Canada imports (Sumas, Waddington).
    delivery_terminals = df["terminal"].apply(
        lambda t: TERMINAL_CATEGORY.get(t, "U.S. LNG") != "Canada border"
        or not _is_canada_import(t)
    )
    is_import = df["terminal"].apply(_is_canada_import)
    keep = (
        (~is_import & (df["direction"] == "delivery"))
        | (is_import & (df["direction"] == "receipt"))
    )
    g = (df[keep]
         .groupby(["gas_day", "cycle", "terminal"], as_index=False)["mmcfd"].sum())
    g["category"] = g["terminal"].map(TERMINAL_CATEGORY).fillna("U.S. LNG")
    return g


def _is_canada_import(terminal: str) -> bool:
    """True for Canada→US receipt-side terminals (shared config set)."""
    from ng_feedgas.config import CANADA_IMPORT_TERMINALS
    return terminal in CANADA_IMPORT_TERMINALS


def category_daily(df: pd.DataFrame, cycle: str) -> pd.DataFrame:
    """Per-day total by category."""
    t = terminal_totals(df)
    if t.empty:
        return pd.DataFrame(columns=["gas_day", "category", "mmcfd"])
    t = t[t["cycle"] == cycle]
    return (t.groupby(["gas_day", "category"], as_index=False)["mmcfd"].sum())


# ---------- chart builders ----------

EMPTY_FIG = go.Figure().update_layout(
    template="plotly_white",
    annotations=[{
        "text": "No data — run <code>python -m ng_feedgas pull</code>",
        "showarrow": False, "font": {"size": 16},
        "xref": "paper", "yref": "paper", "x": 0.5, "y": 0.5,
    }],
    height=300,
)


def _terminal_order_for_category(category: str) -> list[str]:
    if category == ALL_REGIONS:
        return list(TERMINAL_ORDER)
    return [t for t in TERMINAL_ORDER if TERMINAL_CATEGORY.get(t) == category]


def fig_category_totals(df: pd.DataFrame, cycle: str,
                        eia_df: pd.DataFrame | None = None) -> go.Figure:
    """Time series of total flow per category."""
    cd = category_daily(df, cycle)
    if cd.empty:
        return EMPTY_FIG
    fig = go.Figure()
    for cat in CATEGORIES:
        sub = cd[cd["category"] == cat]
        if sub.empty:
            continue
        fig.add_trace(go.Scatter(
            x=sub["gas_day"], y=sub["mmcfd"],
            mode="lines+markers", name=cat,
            line=dict(width=3, color=CATEGORY_COLORS[cat]),
            marker=dict(size=8),
        ))
    if eia_df is not None and not eia_df.empty:
        fig.add_trace(go.Scatter(
            x=eia_df["week_ending"],
            y=eia_df["us_lng_bcfd"] * 1000,
            mode="lines+markers",
            name="EIA monthly LNG (×1000 = MMcf/d)",
            line=dict(width=2, color="#444", dash="dash"),
            marker=dict(size=6, symbol="diamond"),
        ))
    fig.update_layout(
        template="plotly_white", height=380,
        margin=dict(l=40, r=20, t=50, b=40),
        title=f"Total Flow by Region — {cycle.title()} Cycle",
        xaxis_title="Gas Day", yaxis_title="MMcf/d",
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1),
    )
    return fig


def fig_terminal_bars(df: pd.DataFrame, cycle: str, gas_day: date,
                      category: str, ais_df: pd.DataFrame | None = None) -> go.Figure:
    """Terminal-by-terminal bar chart for a single category and gas day."""
    t = terminal_totals(df)
    today = t[(t["cycle"] == cycle) & (t["gas_day"] == gas_day)
              & _region_mask(t, category)]
    order = _terminal_order_for_category(category)
    if today.empty or not order:
        return EMPTY_FIG
    today = today.set_index("terminal").reindex(order).reset_index()
    today["mmcfd"] = today["mmcfd"].fillna(0)
    today["nameplate"] = today["terminal"].map(NAMEPLATES)
    today["pct"] = (today["mmcfd"] / today["nameplate"] * 100).round(1)

    # AIS overlay (only meaningful for LNG terminals)
    ais_map = {}
    if category in ("U.S. LNG", ALL_REGIONS) and ais_df is not None and not ais_df.empty:
        ais_today = ais_df[ais_df["gas_day"] == gas_day]
        ais_map = dict(zip(ais_today["terminal"], ais_today["est_feedgas_mmcfd"]))
    today["ais_est"] = today["terminal"].map(ais_map).fillna(0)

    today["label"] = today["terminal"].map(SHORT)
    today["conf"] = today["terminal"].map(CONFIDENCE).fillna("partial")
    outline_colors = [CONFIDENCE_COLOR[c] for c in today["conf"]]
    outline_widths = [4 if c == "high" else 2.5 for c in today["conf"]]
    # In All-regions view, color each bar by its own region; otherwise one color.
    if category == ALL_REGIONS:
        bar_color = [CATEGORY_COLORS[TERMINAL_CATEGORY.get(t_, "U.S. LNG")]
                     for t_ in today["terminal"]]
    else:
        bar_color = CATEGORY_COLORS[category]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=today["label"], y=today["mmcfd"],
        marker=dict(
            color=bar_color,
            line=dict(color=outline_colors, width=outline_widths),
        ),
        text=[f"{v:,.0f}" if v > 0 else "" for v in today["mmcfd"]],
        textposition="outside",
        cliponaxis=False,
        name="Scraped (EBB)",
        customdata=today[["terminal", "conf"]].values,
        hovertemplate="%{customdata[0]}<br>%{y:,.0f} MMcf/d<br>"
                      "confidence: %{customdata[1]}<extra></extra>",
    ))
    # Legend proxies for the confidence outline colors
    for tier, label in [("high", "Exact (single metered crossing)"),
                        ("partial", "Lower bound (multi-fed terminal)"),
                        ("none", "Not captured")]:
        fig.add_trace(go.Bar(
            x=[None], y=[None], name=label,
            marker=dict(color="rgba(0,0,0,0)",
                        line=dict(color=CONFIDENCE_COLOR[tier], width=3)),
            showlegend=True,
        ))
    if today["ais_est"].sum() > 0:
        fig.add_trace(go.Bar(
            x=today["label"], y=today["ais_est"],
            marker_color="#F4A261",
            text=[f"{v:,.0f}" if v > 0 else "" for v in today["ais_est"]],
            textposition="outside",
            cliponaxis=False,
            name="AIS-inferred",
        ))
    # Nameplate as a dashed line per bar — clearer than scatter markers, no
    # collision with the value labels above each bar.
    for _, r in today.iterrows():
        fig.add_shape(
            type="line",
            x0=r["label"], x1=r["label"], y0=0, y1=r["nameplate"],
            line=dict(color="#E63946", width=2, dash="dot"),
        )
    fig.add_trace(go.Scatter(
        x=[None], y=[None], mode="lines",
        line=dict(color="#E63946", width=2, dash="dot"),
        name="Nameplate / capacity",
    ))
    # Top headroom: 18% above max value so text labels don't collide w/ title
    ymax = max(float(today["mmcfd"].max() or 0),
               float(today["nameplate"].max() or 0)) * 1.18 or 100
    fig.update_layout(
        template="plotly_white", height=460,
        margin=dict(l=40, r=20, t=60, b=170),
        title=f"{category} — {gas_day} {cycle.title()}",
        yaxis_title="MMcf/d", yaxis_range=[0, ymax],
        barmode="group", showlegend=True,
        xaxis=dict(tickangle=-45, automargin=True),
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1),
    )
    return fig


def fig_terminals_stack(df: pd.DataFrame, cycle: str, category: str) -> go.Figure:
    t = terminal_totals(df)
    t = t[(t["cycle"] == cycle) & _region_mask(t, category)].copy()
    order = _terminal_order_for_category(category)
    if t.empty or not order:
        return EMPTY_FIG
    short_order = [SHORT[x] for x in order]
    t["label"] = t["terminal"].map(SHORT)
    t["label"] = pd.Categorical(t["label"], categories=short_order, ordered=True)
    t = t.sort_values(["gas_day", "label"])
    fig = px.area(
        t, x="gas_day", y="mmcfd", color="label",
        category_orders={"label": short_order},
        labels={"gas_day": "Gas Day", "mmcfd": "MMcf/d", "label": "Terminal"},
        title=f"{category} — Per-Terminal Stack ({cycle.title()})",
    )
    fig.update_layout(template="plotly_white", height=420,
                      margin=dict(l=40, r=20, t=50, b=40),
                      legend=dict(orientation="v", yanchor="top", y=1,
                                  xanchor="left", x=1.02, font=dict(size=11)))
    return fig


def fig_pipeline_breakdown(df: pd.DataFrame, cycle: str, gas_day: date,
                            category: str) -> go.Figure:
    today = df[(df["cycle"] == cycle) & (df["gas_day"] == gas_day)
               & _region_mask(df, category)].copy()
    if today.empty:
        return EMPTY_FIG
    today["label"] = today["terminal"].map(SHORT)
    order = _terminal_order_for_category(category)
    short_order = [SHORT[x] for x in order]
    fig = px.bar(
        today, x="label", y="mmcfd", color="pipeline",
        category_orders={"label": short_order},
        labels={"mmcfd": "MMcf/d", "pipeline": "Pipeline", "label": "Terminal"},
        title=f"{category} — Pipeline Contribution ({gas_day} {cycle.title()})",
        hover_data=["terminal", "meter_point", "direction"],
    )
    fig.update_layout(template="plotly_white", height=460,
                      margin=dict(l=40, r=20, t=50, b=180),
                      xaxis=dict(tickangle=-45, automargin=True),
                      legend=dict(orientation="h", yanchor="bottom",
                                  y=-0.55, xanchor="center", x=0.5,
                                  font=dict(size=11)))
    return fig


def fig_utilization(df: pd.DataFrame, cycle: str, gas_day: date,
                    category: str) -> go.Figure:
    t = terminal_totals(df)
    today = t[(t["cycle"] == cycle) & (t["gas_day"] == gas_day)
              & _region_mask(t, category)]
    order = _terminal_order_for_category(category)
    if today.empty or not order:
        return EMPTY_FIG
    today = today.set_index("terminal").reindex(order).reset_index()
    today["mmcfd"] = today["mmcfd"].fillna(0)
    today["nameplate"] = today["terminal"].map(NAMEPLATES)
    today["pct"] = today["mmcfd"] / today["nameplate"] * 100
    today["label"] = today["terminal"].map(SHORT)
    today["conf"] = today["terminal"].map(CONFIDENCE).fillna("partial")
    short_order = [SHORT[x] for x in order]
    max_pct = today["pct"].max() if not today["pct"].empty else 0
    fig = go.Figure(go.Bar(
        x=today["pct"], y=today["label"], orientation="h",
        text=[f"{p:.0f}%" for p in today["pct"]],
        textposition="outside",
        cliponaxis=False,
        marker=dict(
            color=["#2A9D8F" if p >= 80 else "#E9C46A" if p >= 40
                   else "#E76F51" for p in today["pct"]],
            line=dict(
                color=[CONFIDENCE_COLOR[c] for c in today["conf"]],
                width=[4 if c == "high" else 2.5 for c in today["conf"]],
            ),
        ),
        hovertext=today["terminal"],
    ))
    fig.update_layout(
        template="plotly_white", height=460,
        margin=dict(l=160, r=60, t=50, b=40),
        title=f"{category} — % of Nameplate ({gas_day} {cycle.title()})",
        xaxis_title="%",
        xaxis_range=[0, max(125, max_pct * 1.15)],
        yaxis=dict(categoryorder="array", categoryarray=short_order[::-1],
                   automargin=True),
    )
    fig.add_vline(x=100, line_dash="dash", line_color="gray",
                  annotation_text="100%", annotation_position="top")
    return fig


# ---------- deviation alerts (DoD / WoW / MoM) ----------

_SEV_BG = {  # background colors for severity in the table
    "significant": "#f8d7da",  # red
    "notable":     "#fff3cd",  # amber
}
_SEV_FG = {"significant": "#842029"}


def build_changes_table(gas_day: date, cycle: str):
    """Return a dash_table.DataTable of per-terminal DoD/WoW/MoM deviations.

    Reuses ng_feedgas.analysis.changes.compute_changes against the live DB.
    """
    try:
        from ng_feedgas.analysis.changes import compute_changes
    except Exception as exc:  # pragma: no cover
        return html.P(f"Deviation module unavailable: {exc}", className="text-muted")

    if not DB_PATH.exists():
        return html.P("No data yet.", className="text-muted")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = compute_changes(conn, gas_day, cycle)

    def _cell(h) -> str:
        if h.pct_delta is None:
            return "n/a"
        arrow = "▲" if h.pct_delta > 0 else "▼" if h.pct_delta < 0 else "■"
        return f"{arrow} {h.pct_delta:+.1f}%  ({h.abs_delta:+,.0f})"

    records = []
    for tc in rows:
        records.append({
            "terminal": tc.terminal,
            "current": f"{tc.current:,.0f}" if tc.current is not None else "n/a",
            "dod": _cell(tc.dod), "dod_sev": tc.dod.severity,
            "wow": _cell(tc.wow), "wow_sev": tc.wow.severity,
            "mom": _cell(tc.mom), "mom_sev": tc.mom.severity,
        })

    style_cond = []
    for col in ("dod", "wow", "mom"):
        for sev, bg in _SEV_BG.items():
            rule = {"if": {"filter_query": f'{{{col}_sev}} = "{sev}"', "column_id": col},
                    "backgroundColor": bg}
            if sev in _SEV_FG:
                rule["color"] = _SEV_FG[sev]
                rule["fontWeight"] = "bold"
            style_cond.append(rule)

    return dash_table.DataTable(
        data=records,
        columns=[
            {"name": "Terminal", "id": "terminal"},
            {"name": "Current (MMcf/d)", "id": "current"},
            {"name": "Day-over-Day", "id": "dod"},
            {"name": "Week-over-Week", "id": "wow"},
            {"name": "Month-over-Month", "id": "mom"},
        ],
        sort_action="native", filter_action="native",
        style_cell={"padding": "8px", "fontFamily": "system-ui", "fontSize": 13,
                    "textAlign": "left"},
        style_header={"backgroundColor": "#343a40", "color": "white",
                      "fontWeight": "bold"},
        style_data_conditional=style_cond,
        page_size=25,
    )


def build_ais_vessels_table(gas_day: date):
    """Table of the actual vessels AIS detected near terminals on `gas_day`.

    Shows name, size, and a transparent LNG-carrier verdict so you can see why
    a hit counts (e.g. UMM SWAYYAH 295x47 = yes) or is suspect (a 209x23 barge
    flagged only by length = 'maybe — narrow beam').
    """
    if not DB_PATH.exists():
        return None
    q = """
        SELECT o.terminal,
               COALESCE(NULLIF(TRIM(o.ship_name), ''), s.ship_name) AS name,
               COALESCE(o.ship_type, s.ship_type) AS typ,
               s.length_m, s.width_m,
               COALESCE(s.is_lng_carrier, 0) AS is_lng_carrier,
               MIN(o.sog) AS minsog, COUNT(*) AS obs
        FROM ais_observations o
        LEFT JOIN ais_ships s ON s.mmsi = o.mmsi
        WHERE substr(o.captured_at, 1, 10) = ?
        GROUP BY o.mmsi
    """
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(q, conn, params=(gas_day.isoformat(),))
    if df.empty:
        return None

    # Possible-LNG filter: drop vessels we know are small/non-LNG (tugs, pilots,
    # crew boats). Keep large or unknown-size vessels. Mirrors the scanner-side
    # filter so the table shows only plausible carriers, not the harbor fleet.
    from ng_feedgas.calibration.ais import is_known_small, classify_lng_carrier
    df = df[~df.apply(lambda r: is_known_small(r["typ"], r["length_m"]), axis=1)]
    if df.empty:
        return None

    def verdict(r) -> str:
        moored = (r["minsog"] is not None) and (r["minsog"] < 1.0)
        is_carrier = classify_lng_carrier(
            r["typ"], r["length_m"], r["width_m"], bool(r["is_lng_carrier"]))
        length = r["length_m"] or 0
        width = r["width_m"] or 0
        if moored and is_carrier:
            return "yes"
        if moored and length >= 200 and width and width < 35:
            return "maybe (narrow beam)"
        if is_carrier:
            return "transiting"
        return "no"

    df["verdict"] = df.apply(verdict, axis=1)
    df["moored"] = df["minsog"].apply(lambda s: "moored" if (s is not None and s < 1) else f"{s:.0f} kn")
    df["len_disp"] = df["length_m"].apply(lambda v: f"{v:.0f}" if v else "?")
    df["beam_disp"] = df["width_m"].apply(lambda v: f"{v:.0f}" if v else "?")
    # Carriers / candidates first, then by size
    rank = {"yes": 0, "maybe (narrow beam)": 1, "transiting": 2, "no": 3}
    df = df.sort_values(by=["verdict", "length_m"],
                        key=lambda c: c.map(rank) if c.name == "verdict" else c,
                        ascending=[True, False])

    return dash_table.DataTable(
        data=df[["terminal", "name", "typ", "len_disp", "beam_disp",
                 "moored", "verdict", "obs"]].fillna("?").to_dict("records"),
        columns=[
            {"name": "Terminal", "id": "terminal"},
            {"name": "Vessel", "id": "name"},
            {"name": "AIS type", "id": "typ"},
            {"name": "Length (m)", "id": "len_disp"},
            {"name": "Beam (m)", "id": "beam_disp"},
            {"name": "Status", "id": "moored"},
            {"name": "LNG carrier?", "id": "verdict"},
            {"name": "Obs", "id": "obs"},
        ],
        sort_action="native", filter_action="native",
        style_cell={"padding": "6px", "fontFamily": "system-ui", "fontSize": 12},
        style_header={"backgroundColor": "#264653", "color": "white", "fontWeight": "bold"},
        style_data_conditional=[
            {"if": {"filter_query": '{verdict} = "yes"'},
             "backgroundColor": "#d1e7dd", "fontWeight": "bold"},
            {"if": {"filter_query": '{verdict} = "maybe (narrow beam)"'},
             "backgroundColor": "#fff3cd"},
        ],
        page_size=15,
    )


# ---------- KPI computation ----------

def compute_kpis(df: pd.DataFrame, cycle: str, gas_day: date) -> dict:
    cd = category_daily(df, cycle)
    today = cd[cd["gas_day"] == gas_day]
    prior_day = gas_day - timedelta(days=1)
    prior = cd[cd["gas_day"] == prior_day]

    def _val(frame: pd.DataFrame, cat: str) -> float:
        s = frame[frame["category"] == cat]["mmcfd"]
        return float(s.sum()) if not s.empty else 0.0

    out = {}
    for cat in CATEGORIES:
        out[cat] = {
            "today": _val(today, cat),
            "delta": _val(today, cat) - _val(prior, cat) if not prior.empty else None,
        }
    # Total visibility
    out["all"] = {
        "today": float(today["mmcfd"].sum()),
        "delta": (float(today["mmcfd"].sum()) - float(prior["mmcfd"].sum()))
                 if not prior.empty else None,
    }
    # Canada split: imports (receipt) vs exports (delivery). Summing the two as a
    # single positive "net" was misleading — they're opposite flows.
    can = df[(df["category"] == "Canada border") & (df["gas_day"] == gas_day)
             & (df["cycle"] == cycle)]
    out["canada_imports"] = float(can[can["direction"] == "receipt"]["mmcfd"].sum())
    out["canada_exports"] = float(can[can["direction"] == "delivery"]["mmcfd"].sum())
    return out


def kpi_card(title: str, value: str, footer: str = "",
             color: str = "primary") -> dbc.Card:
    return dbc.Card([
        dbc.CardBody([
            html.H6(title, className="card-subtitle text-muted"),
            html.H2(value, className="card-title my-2"),
            html.Small(footer, className="text-muted"),
        ])
    ], color=color, outline=True, className="text-center h-100")


# ---------- app layout ----------

app = dash.Dash(__name__, external_stylesheets=[dbc.themes.FLATLY],
                title="U.S. Cross-Border Gas Flows Dashboard")

app.layout = dbc.Container(fluid=True, children=[
    dbc.Row([
        dbc.Col([
            html.H2("U.S. Natural Gas — Cross-Border & LNG Flows", className="mt-3"),
            html.P([
                "Live scrape from public pipeline EBBs. ",
                html.Br(),
                "Refresh data via ",
                html.Code("python -m ng_feedgas pull --date today --cycle evening --pipeline all"),
                ".",
            ], className="text-muted"),
        ], width=9),
        dbc.Col([
            dbc.Button("↻ Reload Data", id="reload-btn", color="secondary",
                       outline=True, className="float-end mt-3"),
        ], width=3),
    ]),
    html.Hr(),
    dbc.Row([
        dbc.Col([
            html.Label("Gas Day", className="fw-bold"),
            dcc.Dropdown(id="date-picker", clearable=False,
                         placeholder="Select gas day…"),
            html.Small("Days with scraped flow or AIS data. "
                       "'(AIS only)' = vessel data but no pipeline pull yet.",
                       className="text-muted"),
        ], width=3),
        dbc.Col([
            html.Label("Cycle", className="fw-bold"),
            dcc.RadioItems(
                id="cycle-radio",
                options=[
                    {"label": " Timely",    "value": "timely"},
                    {"label": " Evening",   "value": "evening"},
                    {"label": " Confirmed", "value": "confirmed"},
                ],
                value="evening",
                inline=True,
                inputStyle={"marginLeft": "10px", "marginRight": "4px"},
            ),
        ], width=6),
        dbc.Col([
            html.Div(id="last-updated", className="text-muted text-end pt-4"),
        ], width=3),
    ], className="mb-3"),
    # Headline KPI row — all four buckets
    dbc.Row(id="kpi-row", className="mb-3"),
    # Cross-region time series
    dbc.Row([dbc.Col(dcc.Graph(id="category-chart"), width=12)]),
    # Region-specific tabs (default: All regions = show everything)
    dcc.Tabs(id="region-tabs", value=ALL_REGIONS, children=[
        dcc.Tab(label="All regions", value=ALL_REGIONS),
        dcc.Tab(label="U.S. LNG", value="U.S. LNG"),
        dcc.Tab(label="Mexico exports", value="Mexico exports"),
        dcc.Tab(label="Canada border", value="Canada border"),
    ], className="mt-3"),
    # Confidence legend — explains the bar outline / table border colors
    dbc.Row([dbc.Col(html.Div([
        html.Span("Data confidence (bar outline / row border):  ",
                  className="text-muted small fw-bold"),
        html.Span("█ ", style={"color": "#1B998B"}),
        html.Span("Exact — single metered border crossing, captures the whole flow    ",
                  className="small"),
        html.Span("█ ", style={"color": "#E9C46A"}),
        html.Span("Lower bound — multi-fed LNG terminal; private/intrastate feeds "
                  "may be missing (e.g. Sabine ≈ 57% of true; Creole Trail is private)    ",
                  className="small"),
        html.Span("█ ", style={"color": "#ADB5BD"}),
        html.Span("Not captured — no scraper yet", className="small"),
    ], className="mt-2 mb-1"))]),
    dbc.Row([
        dbc.Col(dcc.Graph(id="terminal-bars"), width=6),
        dbc.Col(dcc.Graph(id="utilization-chart"), width=6),
    ], className="mt-2"),
    dbc.Row([
        dbc.Col(dcc.Graph(id="terminals-stack-chart"), width=6),
        dbc.Col(dcc.Graph(id="pipeline-breakdown-chart"), width=6),
    ]),
    # Deviation alerts (all regions, not filtered by tab)
    html.Hr(className="mt-4"),
    html.H4("Deviation Alerts — Day / Week / Month", className="mt-3"),
    html.P([
        "Change in captured flow per terminal: DoD = vs prior gas day; "
        "WoW = trailing 7-day mean vs the prior 7 days; MoM = trailing 30-day "
        "mean vs the prior 30. ",
        html.B("Amber ≥10%, red ≥25%. "),
        "For LNG terminals (lower-bound totals), trust the direction of change, "
        "not the absolute level. WoW/MoM fill in as history accumulates.",
    ], className="text-muted small"),
    html.Div(id="changes-table-container"),
    # AIS vessel-tracking section (LNG only)
    html.Hr(className="mt-4"),
    html.H4("AIS Vessel Tracking (LNG terminals)", className="mt-3"),
    html.P([
        "Inferred LNG-carrier berth occupancy from aisstream.io, collected hourly "
        "by the NG-Feedgas-AIS-Track scheduled task. ",
        "Ships at berth > 0 means an LNG carrier was moored that day — a real "
        "signal the amber (lower-bound) terminal totals can be cross-checked against.",
    ], className="text-muted small"),
    html.Div(id="ais-table-container"),
    html.H4("Raw flows (selected day)", className="mt-4"),
    html.Div(id="flows-table-container"),
    dcc.Store(id="data-store"),
    dcc.Store(id="eia-store"),
    dcc.Store(id="ais-store"),
])


# ---------- callbacks ----------

@app.callback(
    Output("data-store", "data"),
    Output("eia-store", "data"),
    Output("ais-store", "data"),
    Output("date-picker", "options"),
    Output("date-picker", "value"),
    Output("last-updated", "children"),
    Input("reload-btn", "n_clicks"),
    Input("cycle-radio", "value"),
)
def reload_data(_n, cycle):
    df = load_flows()
    eia = load_eia_weekly()
    ais = load_ais_inference()
    if df.empty:
        return ([], [], [], [], None,
                "No data loaded. Run python -m ng_feedgas pull first.")
    # Offer dates that have flow data for the selected cycle, UNIONED with dates
    # that have AIS inference (which is cycle-independent). AIS-only dates are
    # tagged so the user knows the flow charts will be empty but AIS will show.
    flow_days = set(df[df["cycle"] == cycle]["gas_day"].tolist())
    ais_days = set(ais["gas_day"].tolist()) if not ais.empty else set()
    all_days = sorted(flow_days | ais_days, reverse=True)
    options = []
    for d in all_days:
        iso = d.isoformat()
        tag = "" if d in flow_days else "  (AIS only)"
        options.append({"label": iso + tag, "value": iso})
    available = all_days
    selected = available[0].isoformat() if available else None

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    eia_records = (eia.assign(week_ending=eia["week_ending"].astype(str))
                   .to_dict("records") if not eia.empty else [])
    ais_records = (ais.assign(gas_day=ais["gas_day"].astype(str))
                   .to_dict("records") if not ais.empty else [])
    tag_eia = f"EIA {len(eia)} months" if not eia.empty else "EIA: none"
    tag_ais = f"AIS {len(ais)} rows" if not ais.empty else "AIS: none"
    n_days = len(available)
    return (
        df.assign(gas_day=df["gas_day"].astype(str)).to_dict("records"),
        eia_records,
        ais_records,
        options,
        selected,
        f"Loaded {len(df)} flows · {n_days} gas days for cycle={cycle} · "
        f"{tag_eia} · {tag_ais} · refreshed {stamp}",
    )


@app.callback(
    Output("kpi-row", "children"),
    Output("category-chart", "figure"),
    Output("terminal-bars", "figure"),
    Output("terminals-stack-chart", "figure"),
    Output("utilization-chart", "figure"),
    Output("pipeline-breakdown-chart", "figure"),
    Output("flows-table-container", "children"),
    Output("ais-table-container", "children"),
    Output("changes-table-container", "children"),
    Input("data-store", "data"),
    Input("eia-store", "data"),
    Input("ais-store", "data"),
    Input("date-picker", "value"),
    Input("cycle-radio", "value"),
    Input("region-tabs", "value"),
)
def update_dashboard(data, eia_data, ais_data, picked_date, cycle, region):
    if not data or not picked_date:
        empty_kpis = [dbc.Col(kpi_card("--", "--"), width=3) for _ in range(4)]
        empty_table = html.P("No data available.", className="text-muted")
        return (empty_kpis, EMPTY_FIG, EMPTY_FIG, EMPTY_FIG, EMPTY_FIG,
                EMPTY_FIG, empty_table, empty_table, empty_table)

    df = pd.DataFrame(data)
    df["gas_day"] = pd.to_datetime(df["gas_day"]).dt.date
    df["category"] = df["terminal"].map(TERMINAL_CATEGORY).fillna("U.S. LNG")
    gas_day = pd.to_datetime(picked_date).date()

    eia_df = pd.DataFrame(eia_data) if eia_data else pd.DataFrame()
    if not eia_df.empty and "week_ending" in eia_df.columns:
        eia_df["week_ending"] = pd.to_datetime(eia_df["week_ending"]).dt.date
    ais_df = pd.DataFrame(ais_data) if ais_data else pd.DataFrame()
    if not ais_df.empty and "gas_day" in ais_df.columns:
        ais_df["gas_day"] = pd.to_datetime(ais_df["gas_day"]).dt.date

    k = compute_kpis(df, cycle, gas_day)

    def _delta_str(d):
        return f"{d:+,.0f} vs prior day" if d is not None else "no prior day"

    kpis = [
        dbc.Col(kpi_card(
            "Total visibility",
            f"{k['all']['today']:,.0f}",
            _delta_str(k["all"]["delta"]) + "  ·  MMcf/d",
            color="primary"
        ), width=3),
        dbc.Col(kpi_card(
            "U.S. LNG feedgas",
            f"{k['U.S. LNG']['today']:,.0f}",
            _delta_str(k["U.S. LNG"]["delta"]),
            color="info"
        ), width=3),
        dbc.Col(kpi_card(
            "U.S. → Mexico exports",
            f"{k['Mexico exports']['today']:,.0f}",
            _delta_str(k["Mexico exports"]["delta"]),
            color="warning"
        ), width=3),
        dbc.Col(kpi_card(
            "Canada net imports",
            f"{k['canada_imports'] - k['canada_exports']:+,.0f}",
            f"{k['canada_imports']:,.0f} import / {k['canada_exports']:,.0f} export  ·  MMcf/d",
            color="success"
        ), width=3),
    ]

    # Raw flows table for the selected day, filtered by region tab
    day_df = df[(df["gas_day"] == gas_day) & (df["cycle"] == cycle)
                & _region_mask(df, region)].copy()
    day_df["mmcfd"] = day_df["mmcfd"].round(1)
    if day_df.empty:
        table = html.P(f"No {region} rows for {gas_day} / {cycle}.",
                       className="text-muted")
    else:
        table = dash_table.DataTable(
            data=day_df[["terminal", "pipeline", "meter_point",
                         "mmcfd", "direction"]].to_dict("records"),
            columns=[
                {"name": "Terminal", "id": "terminal"},
                {"name": "Pipeline", "id": "pipeline"},
                {"name": "Meter Point", "id": "meter_point"},
                {"name": "MMcf/d", "id": "mmcfd", "type": "numeric",
                 "format": {"specifier": ",.1f"}},
                {"name": "Direction", "id": "direction"},
            ],
            sort_action="native", filter_action="native",
            style_cell={"padding": "8px", "fontFamily": "system-ui",
                        "fontSize": 13},
            style_header={"backgroundColor": CATEGORY_COLORS[region],
                          "color": "white", "fontWeight": "bold"},
            style_data_conditional=(
                # Green left-border + tint for trusted terminals
                [{"if": {"filter_query": f"{{terminal}} = '{t}'"},
                  "borderLeft": "4px solid #1B998B",
                  "backgroundColor": "#EAF7F3"}
                 for t in TRUSTED]
                + [{"if": {"filter_query": f"{{terminal}} = '{t}'"},
                    "borderLeft": "4px solid #E9C46A"}
                   for t, c in CONFIDENCE.items() if c == "partial"]
            ),
            page_size=25,
        )

    # AIS section: per-terminal inference summary + the actual detected vessels
    ais_today = ais_df[ais_df["gas_day"] == gas_day] if not ais_df.empty \
        else pd.DataFrame()
    ais_parts = []
    if ais_today.empty:
        ais_parts.append(html.P(
            "No AIS inference for this gas day. The hourly NG-Feedgas-AIS-Track "
            "task populates this (requires AISSTREAM_API_KEY).",
            className="text-muted"))
    else:
        ais_parts.append(dash_table.DataTable(
            data=ais_today.assign(
                gas_day=ais_today["gas_day"].astype(str),
                est_feedgas_mmcfd=ais_today["est_feedgas_mmcfd"].round(0),
                moored_hours=ais_today["moored_hours"].round(1),
            ).to_dict("records"),
            columns=[
                {"name": "Terminal", "id": "terminal"},
                {"name": "Ships at berth", "id": "ship_at_berth"},
                {"name": "Moored hours", "id": "moored_hours", "type": "numeric"},
                {"name": "Est. feedgas (MMcf/d)",
                 "id": "est_feedgas_mmcfd", "type": "numeric"},
            ],
            sort_action="native",
            style_cell={"padding": "8px", "fontFamily": "system-ui", "fontSize": 13},
            style_header={"backgroundColor": "#F4A261", "color": "white",
                          "fontWeight": "bold"},
            style_data_conditional=[
                {"if": {"filter_query": "{ship_at_berth} > 0"},
                 "backgroundColor": "#FFF7E6"},
            ],
        ))
    # Detected-vessel detail (names + sizes), independent of inference
    vessels = build_ais_vessels_table(gas_day)
    if vessels is not None:
        ais_parts.append(html.H6("Vessels detected near terminals (this gas day)",
                                 className="mt-3"))
        ais_parts.append(vessels)
    ais_table = html.Div(ais_parts)

    changes_table = build_changes_table(gas_day, cycle)

    return (
        kpis,
        fig_category_totals(df, cycle, eia_df),
        fig_terminal_bars(df, cycle, gas_day, region, ais_df),
        fig_terminals_stack(df, cycle, region),
        fig_utilization(df, cycle, gas_day, region),
        fig_pipeline_breakdown(df, cycle, gas_day, region),
        table,
        ais_table,
        changes_table,
    )


if __name__ == "__main__":
    print(f"Loading from: {DB_PATH}")
    print(f"Terminals: {len(TERMINAL_ORDER)} configured across "
          f"{len(set(TERMINAL_CATEGORY.values()))} regions")
    print("Dashboard at: http://localhost:8050")
    app.run(debug=False, host="127.0.0.1", port=8050)
