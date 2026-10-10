"""
Phase 7: Plotly figure builders for the dashboard. Pure functions that take
frames/lists and return a `plotly.graph_objects.Figure` — no Streamlit, no
I/O — so they are unit-testable.

Colour follows the data-viz method this project adopted for the dashboard
(see findings.md's Phase 7 entry), and the choices are deliberate:

- **Corridors are categorical identity**, so they use the first three slots
  of the validated palette (blue, orange, aqua). A scatter is an all-pairs
  form, for which only the first three slots validate in both modes
  (worst-pair CVD ΔE 9.2 light / 9.4 dark, normal-vision floor ≥ 20.9), so a
  fourth corridor is folded into a gray "Other corridors" series rather than
  given a new hue. Never a generated colour.
- **Plan comparison is emphasis, not categorical**: the plan being compared
  AGAINST (the original) is the de-emphasis gray, the current plan the single
  accent hue. Two equal colours would suggest two equal series.
- **Identity is never colour alone**: the Pareto frontier is a diamond marker
  and dominated points are circles; the legend explains both. (Aqua is below
  3:1 on the light surface — the relief rule — which the legend, the marker
  shapes and the table view in the app together cover.)
- Marks are thin (2px lines), markers ≥ 8px, with a 2px surface ring where
  they can overlap; grid lines are hairlines; axis text is the muted ink.

Both light and dark palettes are the same hues stepped for each surface, not
an automatic flip. `mode` is "light" or "dark" (the app reads Streamlit's
active theme).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd
import plotly.graph_objects as go

#: Validated with the data-viz skill's `validate_palette.js` (--pairs all,
#: first three categorical slots) for each mode against its own surface.
PALETTES: Dict[str, Dict[str, object]] = {
    "light": {
        "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
        "grid": "#e1e0d9", "axis": "#c3c2b7", "land": "#e1e0d9",
        "series": ["#2a78d6", "#eb6834", "#1baf7a"],
    },
    "dark": {
        "surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "axis": "#383835", "land": "#2c2c2a",
        "series": ["#3987e5", "#d95926", "#199e70"],
    },
}
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
MAX_CATEGORICAL = 3  # all-pairs cap for scatter-type forms (see module docstring)


def palette(mode: str) -> Dict[str, object]:
    if mode not in PALETTES:
        raise ValueError(f"mode must be 'light' or 'dark', got {mode!r}")
    return PALETTES[mode]


def _style(fig: go.Figure, mode: str, height: int) -> go.Figure:
    p = palette(mode)
    fig.update_layout(
        height=height,
        paper_bgcolor=p["surface"], plot_bgcolor=p["surface"],
        font=dict(family=FONT, color=p["ink2"], size=12),
        margin=dict(l=56, r=20, t=44, b=48),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0,
                    font=dict(color=p["ink2"]), bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(bgcolor=p["surface"], bordercolor=p["axis"],
                        font=dict(family=FONT, color=p["ink"])),
    )
    return fig


def _axes(fig: go.Figure, mode: str, x_title: str, y_title: str) -> None:
    p = palette(mode)
    common = dict(gridcolor=p["grid"], gridwidth=1, zeroline=False, linecolor=p["axis"],
                  tickfont=dict(color=p["muted"]), ticks="")
    fig.update_xaxes(title=dict(text=x_title, font=dict(color=p["ink2"])), **common)
    fig.update_yaxes(title=dict(text=y_title, font=dict(color=p["ink2"])), **common)


def _empty(fig: go.Figure, mode: str, message: str) -> go.Figure:
    p = palette(mode)
    fig.add_annotation(text=message, showarrow=False, font=dict(color=p["ink2"], size=14),
                       xref="paper", yref="paper", x=0.5, y=0.5)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


# --------------------------------------------------------------------------
# Pareto scatter
# --------------------------------------------------------------------------


def _hover(row: pd.Series) -> str:
    lines = [
        f"<b>Corridor {int(row['corridor'])}</b>",
        f"Duration: {row['transit_hours']:.1f} h",
        f"Fuel: {row['fuel_tonnes']:.1f} t",
        f"Weather risk: {row['weather_risk_m_hours']:.0f} m·h",
    ]
    if pd.notna(row.get("severe_weather_hours", float("nan"))):
        lines.append(f"Severe-weather time: {row['severe_weather_hours']:.1f} h")
    lines.append("Frontier point" if row["on_frontier"] else "Dominated by another point")
    if not row["success"]:
        lines.append("Solve stopped at its time/iteration cap (unproven-optimal)")
    return "<br>".join(lines)


def pareto_scatter(
    df: pd.DataFrame, x: str, y: str, x_title: str, y_title: str, mode: str = "light",
    height: int = 420, corridor_order: Optional[Sequence[int]] = None,
) -> go.Figure:
    """Scatter of one objective against another, coloured by corridor.

    Frontier points are filled diamonds, dominated points smaller circles at
    reduced opacity. `x`/`y` are column names of `df` (as produced by
    `data.load_frontier`). With more than three corridors, the rest are
    folded into one gray "Other corridors" series (see module docstring).

    `corridor_order` fixes which corridor gets which colour. Pass the order
    derived from the UNFILTERED data whenever `df` is a filtered subset: the
    colour belongs to the corridor, never to its rank within what happens to
    be on screen, so hiding points must not repaint the survivors.
    """
    fig = go.Figure()
    p = palette(mode)
    if df.empty:
        return _style(_empty(fig, mode, "No sweep points to show"), mode, height)

    corridors = list(corridor_order) if corridor_order is not None else sorted(df["corridor"].unique())
    colour_of: Dict[object, str] = {}
    for i, c in enumerate(corridors):
        colour_of[c] = p["series"][i] if i < MAX_CATEGORICAL else p["muted"]
    folded = [c for c in corridors if colour_of[c] == p["muted"]]
    label_of = {c: (f"Corridor {int(c)}" if c not in folded else "Other corridors") for c in corridors}

    seen_labels = set()
    for c in corridors:
        sub = df[df["corridor"] == c]
        label = label_of[c]
        for frontier in (False, True):
            part = sub[sub["on_frontier"] == frontier]
            if part.empty:
                continue
            first = label not in seen_labels and frontier  # legend entry on the frontier trace
            if first:
                seen_labels.add(label)
            fig.add_trace(go.Scatter(
                x=part[x], y=part[y], mode="markers", name=label, legendgroup=label,
                showlegend=first,
                marker=dict(
                    color=colour_of[c], symbol="diamond" if frontier else "circle",
                    size=13 if frontier else 9, opacity=1.0 if frontier else 0.45,
                    line=dict(width=2, color=p["surface"]),
                ),
                text=[_hover(r) for _, r in part.iterrows()], hoverinfo="text",
            ))
    # A corridor with only dominated points still needs a legend entry.
    for c in corridors:
        if label_of[c] not in seen_labels:
            seen_labels.add(label_of[c])
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="markers", name=label_of[c], legendgroup=label_of[c],
                marker=dict(color=colour_of[c], symbol="circle", size=9),
            ))
    # Shape key, so the diamond/circle encoding is explained in the legend itself.
    for name, symbol in (("Frontier (non-dominated)", "diamond"), ("Dominated", "circle")):
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode="markers", name=name,
            marker=dict(color=p["muted"], symbol=symbol, size=11 if symbol == "diamond" else 9),
        ))

    _axes(fig, mode, x_title, y_title)
    return _style(fig, mode, height)


# --------------------------------------------------------------------------
# Corridor map
# --------------------------------------------------------------------------


def corridor_map(
    lats: Sequence[float], lons: Sequence[float], current_index: int, mode: str = "light",
    trigger_edge: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None,
    height: int = 520,
) -> go.Figure:
    """The voyage's route: sailed part (muted), remaining part (accent), the
    simulated current position, and — if given — the edge a weather event
    fired on. `current_index` indexes the path's node list. `trigger_edge` is
    ((lat_u, lon_u), (lat_v, lon_v)).

    Land/ocean shapes come from Plotly's bundled topojson, which the browser
    fetches from Plotly's CDN, so the basemap needs internet access in the
    viewer's browser (the route itself does not).
    """
    p = palette(mode)
    fig = go.Figure()
    n = len(lats)
    if n == 0 or n != len(lons):
        return _style(_empty(fig, mode, "No route to show"), mode, height)
    current_index = max(0, min(current_index, n - 1))

    if current_index > 0:
        fig.add_trace(go.Scattergeo(
            lat=lats[: current_index + 1], lon=lons[: current_index + 1], mode="lines",
            name="Sailed", line=dict(width=2, color=p["muted"]), hoverinfo="skip",
        ))
    if current_index < n - 1:
        fig.add_trace(go.Scattergeo(
            lat=lats[current_index:], lon=lons[current_index:], mode="lines",
            name="Remaining route", line=dict(width=2, color=p["series"][0]), hoverinfo="skip",
        ))
    if trigger_edge is not None:
        (la0, lo0), (la1, lo1) = trigger_edge
        fig.add_trace(go.Scattergeo(
            lat=[la0, la1], lon=[lo0, lo1], mode="lines+markers", name="Weather-event edge",
            line=dict(width=4, color=p["series"][1]),
            marker=dict(size=8, color=p["series"][1], line=dict(width=2, color=p["surface"])),
            hoverinfo="skip",
        ))
    fig.add_trace(go.Scattergeo(
        lat=[lats[0], lats[-1]], lon=[lons[0], lons[-1]], mode="markers+text",
        text=["Origin", "Destination"], textposition=["top right", "top left"],
        textfont=dict(color=p["ink"], size=12), name="Ports",
        marker=dict(size=9, color=p["ink2"], line=dict(width=2, color=p["surface"])),
        hoverinfo="skip", showlegend=False,
    ))
    fig.add_trace(go.Scattergeo(
        lat=[lats[current_index]], lon=[lons[current_index]], mode="markers",
        name="Simulated position",
        marker=dict(size=14, symbol="diamond", color=p["ink"], line=dict(width=2, color=p["surface"])),
        hovertemplate="Simulated position<extra></extra>",
    ))

    # Explicit padded ranges instead of `fitbounds`: the latter gave an offset,
    # partly empty frame in a wide Streamlit container.
    pad_lon = max((max(lons) - min(lons)) * 0.08, 2.0)
    pad_lat = max((max(lats) - min(lats)) * 0.15, 2.0)
    lon_lo, lon_hi = min(lons) - pad_lon, max(lons) + pad_lon
    lat_lo, lat_hi = min(lats) - pad_lat, max(lats) + pad_lat
    # Plotly keeps the geo frame's aspect ratio fixed, so a route that is much
    # taller than wide-screen proportions leaves empty bands at the sides. Widen
    # the longitude span (centred) to roughly a wide-screen width:height ratio (narrower windows get slack above/below instead, which is harmless).
    target_ratio = 4.2
    need = (lat_hi - lat_lo) * target_ratio
    if lon_hi - lon_lo < need:
        mid = (lon_lo + lon_hi) / 2
        lon_lo, lon_hi = mid - need / 2, mid + need / 2
    fig.update_geos(
        projection_type="equirectangular", showland=True, landcolor=p["land"],
        showocean=True, oceancolor=p["surface"], showcountries=False,
        coastlinecolor=p["axis"], coastlinewidth=1, showframe=False, bgcolor=p["surface"],
        lonaxis=dict(range=[lon_lo, lon_hi]), lataxis=dict(range=[lat_lo, lat_hi]),
    )
    _style(fig, mode, height)
    fig.update_layout(margin=dict(l=0, r=0, t=40, b=0))
    return fig


# --------------------------------------------------------------------------
# Speed-profile comparison (emphasis form)
# --------------------------------------------------------------------------


def _step_xy(frame: pd.DataFrame) -> Tuple[List[float], List[float]]:
    xs = list(frame["start_hours"]) + [float(frame["end_hours"].iloc[-1])]
    ys = list(frame["speed_knots"]) + [float(frame["speed_knots"].iloc[-1])]
    return xs, ys


def speed_profile_chart(
    original: pd.DataFrame, current: pd.DataFrame, elapsed_hours: float, deadline_hours: float,
    mode: str = "light", height: int = 380,
) -> go.Figure:
    """Planned speed over voyage time: original plan in the de-emphasis gray,
    current (reoptimized) plan in the single accent hue, with vertical
    reference lines at "now" (the simulated clock) and the deadline. When the
    voyage hasn't been reoptimized, `original` and `current` are the same
    frame and only one series is drawn."""
    p = palette(mode)
    fig = go.Figure()
    if original.empty:
        return _style(_empty(fig, mode, "No speed profile to show"), mode, height)

    same = original.equals(current)
    ox, oy = _step_xy(original)
    if not same:
        fig.add_trace(go.Scatter(
            x=ox, y=oy, mode="lines", name="Original plan", line=dict(width=2, color=p["muted"], shape="hv"),
            hovertemplate="Original plan<br>%{x:.0f} h: %{y:.2f} kn<extra></extra>",
        ))
    if not current.empty:
        cx, cy = _step_xy(current)
        fig.add_trace(go.Scatter(
            x=cx, y=cy, mode="lines", name="Current plan" if not same else "Planned speed",
            line=dict(width=2, color=p["series"][0], shape="hv"),
            hovertemplate="Current plan<br>%{x:.0f} h: %{y:.2f} kn<extra></extra>",
        ))

    # The plan, not the wall clock, sets the x-range: a voyage that finished long
    # ago would otherwise squash the whole plan into a corner. "now" is drawn only
    # while it falls inside the plan; after that a note says so instead.
    plan_end = max(float(original["end_hours"].iloc[-1]),
                   float(current["end_hours"].iloc[-1]) if not current.empty else 0.0,
                   float(deadline_hours))
    x_max = plan_end * 1.04
    marks = [(deadline_hours, "deadline")]
    if elapsed_hours <= x_max:
        marks.insert(0, (elapsed_hours, "now"))
    for x_val, label in marks:
        fig.add_vline(x=x_val, line=dict(width=1, dash="dot", color=p["ink2"]),
                      annotation=dict(text=label, font=dict(color=p["ink2"], size=11)),
                      annotation_position="top")
    fig.update_xaxes(range=[0, x_max])
    if elapsed_hours > x_max:
        fig.add_annotation(text="Voyage complete (simulated clock is past the plan)", xref="paper", yref="paper",
                           x=0, y=1.0, xanchor="left", yanchor="bottom", showarrow=False,
                           font=dict(color=p["ink2"], size=11))
    _axes(fig, mode, "Voyage time (hours)", "Speed (knots)")
    # Keep the y-axis at least ~2 knots tall: auto-zoom would stretch a 0.3 kn
    # difference to fill the whole panel and make a minor re-plan look dramatic.
    speeds = list(oy if not same else []) + list(cy if not current.empty else [])
    if speeds:
        lo, hi = min(speeds), max(speeds)
        pad = max((2.0 - (hi - lo)) / 2.0, 0.25)
        fig.update_yaxes(range=[lo - pad, hi + pad])
    styled = _style(fig, mode, height)
    styled.update_layout(legend=dict(orientation="h", y=-0.28, x=0))
    return styled
