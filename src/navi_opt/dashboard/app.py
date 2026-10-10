"""
Phase 7: NAVI-Opt Streamlit dashboard.

    streamlit run src/navi_opt/dashboard/app.py

Two views:

1. **Pareto trade-offs** — the Phase 4/5 sweep CSV: fuel vs. duration vs.
   weather risk across the k diverse Rotterdam <-> Ceyhan corridors, with the
   non-dominated frontier marked.
2. **Live voyages** — what Phase 6's Kafka pipeline is doing: each departed
   voyage's simulated position, its current speed plan against the original,
   and the history of weather-triggered reoptimizations.

Everything computed lives in `data.py` / `charts.py` (unit-tested offline);
this file is layout and wiring only. Reading Kafka (`kafka_reader.py`) needs a
real broker and is not covered by the offline tests.

Configuration is by environment variable, with the sidebar able to override:
`NAVI_DATA_DIR` (default `<repo>/data/processed`) and `NAVI_KAFKA` (default
`localhost:9092`).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from navi_opt.dashboard import charts, kafka_reader  # noqa: E402
from navi_opt.dashboard.data import (  # noqa: E402
    VoyageView,
    build_voyage_views,
    demo_clock_note,
    load_frontier,
    path_coordinates,
    profile_leg_frame,
    summarize_frontier,
)

DATA_DIR = Path(os.environ.get("NAVI_DATA_DIR", SRC.parent / "data" / "processed"))
DEFAULT_GRAPH = "ocean_digraph_rotterdam_ceyhan.pkl"
AUTO_REFRESH_SECONDS = 10

st.set_page_config(page_title="NAVI-Opt", layout="wide")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _utcnow() -> datetime:
    # Naive UTC, matching the `datetime.utcnow()` the Phase 6 scripts stamp
    # `demo_departed_at` with — the demo clock subtracts the two.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:  # older Streamlit, or no theme info
        return "light"


def _show(fig) -> None:
    config = {"displaylogo": False}
    try:
        st.plotly_chart(fig, width="stretch", theme=None, config=config)
    except TypeError:  # older Streamlit without width="stretch"
        st.plotly_chart(fig, use_container_width=True, theme=None, config=config)


def _table(df: pd.DataFrame, **kwargs) -> None:
    try:
        st.dataframe(df, hide_index=True, width="stretch", **kwargs)
    except TypeError:
        st.dataframe(df, hide_index=True, use_container_width=True, **kwargs)


def _delta(value: float, unit: str) -> str:
    """Signed delta text; a change that rounds to zero is shown as unchanged, not as '-0.0'."""
    if round(value, 1) == 0:
        return f"unchanged vs previous plan"
    return f"{value:+.1f} {unit} vs previous plan"


def _short(node: str) -> str:
    return node if len(node) <= 10 else node[:8] + "…"


@st.cache_data(show_spinner=False)
def _load_frontier_cached(path: str, mtime: float) -> pd.DataFrame:
    return load_frontier(Path(path))


@st.cache_resource(show_spinner=False)
def _load_graph_cached(path: str, mtime: float):
    from navi_opt.grid.graph_builder import load_graph  # heavy import (geopandas); only when needed

    return load_graph(path=Path(path))


@st.cache_data(ttl=5, show_spinner=False)
def _read_topic_cached(bootstrap: str, topic: str):
    return kafka_reader.read_topic(bootstrap, topic)


# --------------------------------------------------------------------------
# sidebar
# --------------------------------------------------------------------------

with st.sidebar:
    st.header("NAVI-Opt")
    bootstrap = st.text_input("Kafka bootstrap servers", os.environ.get("NAVI_KAFKA", kafka_reader.DEFAULT_BOOTSTRAP_SERVERS))
    sweep_files: List[Path] = sorted(DATA_DIR.glob("pareto_frontier*.csv"))
    sweep_choice: Optional[Path] = None
    if sweep_files:
        sweep_choice = st.selectbox("Pareto sweep file", sweep_files, format_func=lambda p: p.name)
    graph_path = st.text_input("Ocean graph cache", str(DATA_DIR / DEFAULT_GRAPH))
    if st.button("Refresh data"):
        st.cache_data.clear()
    auto_refresh = st.toggle(f"Auto-refresh live voyages ({AUTO_REFRESH_SECONDS} s)", value=False)
    with st.expander("About the data"):
        st.markdown(
            "- **Real:** coastlines, port positions, and the wave/wind fields "
            "(Copernicus Marine, fixed June 2024 window).\n"
            "- **Synthetic:** the tanker's fuel curves (literature-calibrated, "
            "not a real vessel) and the voyage deadline.\n"
            "- **Simulated:** voyage position — a demo clock replays the planned "
            "profile at accelerated speed. This is not vessel tracking.\n"
            "- Fuel and time figures are model outputs, not measurements."
        )

mode = _mode()

st.title("NAVI-Opt")
st.caption("Fuel, duration and weather-risk trade-offs for an MR2 product tanker, Rotterdam to Ceyhan — "
           "and event-driven reoptimization of voyages in progress.")

tab_pareto, tab_live = st.tabs(["Pareto trade-offs", "Live voyages"])


# --------------------------------------------------------------------------
# Pareto tab
# --------------------------------------------------------------------------


def render_pareto() -> None:
    if sweep_choice is None:
        st.info(f"No sweep file found in `{DATA_DIR}`. Run `python scripts/run_pareto_sweep.py` "
                f"(about 45 minutes) or trigger the Airflow DAG.")
        return
    try:
        df = _load_frontier_cached(str(sweep_choice), sweep_choice.stat().st_mtime)
    except (FileNotFoundError, ValueError) as exc:
        st.error(str(exc))
        return

    s = summarize_frontier(df)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sweep points", s.n_points, help="Corridors x deadlines solved by the epsilon-constraint sweep.")
    c2.metric("On the frontier", s.n_frontier,
              help="Non-dominated: no other swept plan is at least as good on fuel, duration and weather risk.")
    c3.metric("Fuel on frontier", f"{s.fuel_range_t[0]:.0f} – {s.fuel_range_t[1]:.0f} t")
    c4.metric("Duration on frontier", f"{s.duration_range_h[0]:.0f} – {s.duration_range_h[1]:.0f} h")

    show_dominated = st.toggle("Show dominated points", value=True)
    order = sorted(df["corridor"].unique())
    plot_df = df if show_dominated else df[df["on_frontier"]]

    left, right = st.columns(2)
    with left:
        st.subheader("Fuel vs. duration")
        _show(charts.pareto_scatter(plot_df, "transit_hours", "fuel_tonnes", "Duration (hours)",
                                    "Fuel (tonnes)", mode, corridor_order=order))
    with right:
        st.subheader("Fuel vs. weather exposure")
        options = {"Weather risk (m·h)": "weather_risk_m_hours"}
        if df["severe_weather_hours"].notna().any():
            threshold = df.attrs.get("severe_threshold_m")
            label = f"Severe-weather hours (waves ≥ {threshold:g} m)" if threshold else "Severe-weather hours"
            options[label] = "severe_weather_hours"
        x_label = st.selectbox("Exposure measure", list(options), label_visibility="collapsed") \
            if len(options) > 1 else next(iter(options))
        _show(charts.pareto_scatter(plot_df, options[x_label], "fuel_tonnes", x_label, "Fuel (tonnes)",
                                    mode, corridor_order=order))

    st.caption("A diamond is a frontier point: no other swept plan beats it on all three objectives. "
               "Slower sailing burns less fuel; the sweep shows what a tighter deadline costs, per corridor.")
    if s.n_unproven:
        st.caption(f"{s.n_unproven} of {s.n_points} points come from solves that stopped at their time or "
                   f"iteration cap — real, usable plans but not proven optimal. They are flagged in the hover "
                   f"text and the table.")
    if df["severe_weather_hours"].isna().all():
        st.caption("This sweep file predates the severe-weather-hours column, so that measure is unavailable.")

    with st.expander("Table view"):
        shown = df.rename(columns={
            "transit_hours": "duration_h", "fuel_tonnes": "fuel_t", "weather_risk_m_hours": "risk_m_h",
            "severe_weather_hours": "severe_h", "success": "solved_to_optimum",
        })
        _table(shown)


# --------------------------------------------------------------------------
# Live tab
# --------------------------------------------------------------------------


def _read_all():
    deps, n1 = _read_topic_cached(bootstrap, kafka_reader.TOPIC_VOYAGE_EVENTS)
    results, n2 = _read_topic_cached(bootstrap, kafka_reader.TOPIC_REOPTIMIZATION_RESULTS)
    weather, n3 = _read_topic_cached(bootstrap, kafka_reader.TOPIC_WEATHER_UPDATES)
    return deps, results, weather, [n for n in (n1, n2, n3) if n]


def _graph_or_none():
    path = Path(graph_path)
    if not path.exists():
        st.caption(f"Route map unavailable: no graph cache at `{path}` (run `scripts/build_real_ocean_graph.py`).")
        return None
    try:
        return _load_graph_cached(str(path), path.stat().st_mtime)
    except Exception as exc:
        st.caption(f"Route map unavailable: could not load the graph cache ({exc}).")
        return None


def render_voyage(view: VoyageView) -> None:
    sailed = view.total_legs - view.legs_remaining
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Status", view.status.capitalize())
    k2.metric("Legs sailed", f"{sailed} / {view.total_legs}")
    k3.metric("Simulated time", f"{view.elapsed_hours:.0f} h",
              help=f"Planned arrival {view.current_profile.arrival_time_hours:.0f} h; deadline {view.deadline_hours:.0f} h.")
    k4.metric("Reoptimizations", len(view.steps))
    st.progress(min(1.0, max(0.0, view.progress)))
    st.caption(demo_clock_note())

    if view.steps:
        last = view.steps[-1]
        st.subheader("Latest reoptimization")
        if last.comparison is not None:
            cmp = last.comparison
            m1, m2, m3 = st.columns(3)
            m1.metric("Remaining fuel", f"{cmp.new_fuel_tonnes:.1f} t",
                      delta=_delta(cmp.fuel_delta_tonnes, "t"), delta_color="inverse")
            m2.metric("Remaining transit", f"{cmp.new_transit_hours:.1f} h",
                      delta=_delta(cmp.transit_delta_hours, "h"), delta_color="inverse")
            m3.metric("Triggered on edge", f"{_short(last.trigger_u)} → {_short(last.trigger_v)}")
            st.caption(f"Like for like: the same {cmp.n_legs} remaining legs, previous plan against new plan. "
                       f"(Whole-voyage totals are deliberately not compared — part of the voyage is already sailed.)")
        else:
            st.warning("This result could not be aligned with the previous plan, so no like-for-like "
                       "comparison is shown.")
        if last.injected:
            st.info("This reoptimization was triggered by a labelled test event "
                    "(`scripts/inject_weather_event.py`), not a real forecast revision. The consumer "
                    "re-fetches real weather when it handles an event, so the change shown is a re-solve "
                    "of the remaining legs, not a response to an injected storm.")
    else:
        st.caption("No reoptimization yet — this voyage is still on its original plan.")

    graph = _graph_or_none()
    if graph is not None:
        try:
            lats, lons = path_coordinates(graph, view.original_profile.path)
            current_index = len(view.original_profile.path) - len(view.remaining_path)
            trigger = None
            if view.steps:
                try:
                    last_step = view.steps[-1]
                    elats, elons = path_coordinates(graph, [last_step.trigger_u, last_step.trigger_v])
                    trigger = ((elats[0], elons[0]), (elats[1], elons[1]))
                except KeyError:
                    trigger = None
            st.subheader("Route")
            _show(charts.corridor_map(lats, lons, current_index, mode, trigger_edge=trigger))
        except KeyError as exc:
            st.caption(f"Route map unavailable: {exc}. The graph cache may not match this voyage.")

    st.subheader("Speed plan")
    _show(charts.speed_profile_chart(
        profile_leg_frame(view.original_profile), profile_leg_frame(view.current_profile),
        view.elapsed_hours, view.deadline_hours, mode,
    ))

    if view.steps:
        st.subheader("Reoptimization history")
        rows = []
        for st_ in view.steps:
            cmp = st_.comparison
            rows.append({
                "at (UTC)": str(st_.reoptimized_at).replace("T", " ")[:19],
                "edge": f"{_short(st_.trigger_u)} → {_short(st_.trigger_v)}",
                "legs re-solved": len(st_.profile.legs),
                "fuel before (t)": round(cmp.old_fuel_tonnes, 1) if cmp else None,
                "fuel after (t)": round(cmp.new_fuel_tonnes, 1) if cmp else round(st_.profile.total_fuel_tonnes, 1),
                "transit before (h)": round(cmp.old_transit_hours, 1) if cmp else None,
                "transit after (h)": round(cmp.new_transit_hours, 1) if cmp else round(st_.profile.total_transit_hours, 1),
                "solved to optimum": st_.profile.success,
                "test event": st_.injected,
            })
        _table(pd.DataFrame(rows))


def render_live() -> None:
    deps, results, weather, notes = _read_all()
    unreachable = [n for n in notes if n.startswith("Cannot reach")]
    if unreachable:
        st.warning(f"{unreachable[0]} Start the stack with `docker compose up -d` from `infra/` "
                   f"and use **Refresh data**.")
        return
    for note in notes:
        st.caption(note)

    if not deps:
        st.info("No voyage has departed yet. Start the consumer, then depart one:")
        st.code("python scripts\\run_reoptimization_consumer.py\npython scripts\\depart_demo_voyage.py", language="bash")
        return

    views = build_voyage_views(deps, results, _utcnow(), weather)
    by_id = {v.voyage_id: v for v in views}
    chosen = st.selectbox("Voyage", list(by_id), format_func=lambda i: f"{i} — {by_id[i].status}")
    render_voyage(by_id[chosen])

    if weather:
        with st.expander(f"Recent weather events ({len(weather)})"):
            recent = sorted(weather, key=lambda e: e.get("sampled_at", ""), reverse=True)[:10]
            _table(pd.DataFrame([{
                "sampled at (UTC)": e.get("sampled_at"), "edge": f"{_short(e['u'])} → {_short(e['v'])}",
                "wave height before (m)": e.get("previous_wave_height_m"),
                "after (m)": e.get("new_wave_height_m"), "test injection": bool(e.get("injected", False)),
            } for e in recent]))


with tab_pareto:
    render_pareto()

with tab_live:
    if auto_refresh:
        st.fragment(run_every=f"{AUTO_REFRESH_SECONDS}s")(render_live)()
    else:
        render_live()
