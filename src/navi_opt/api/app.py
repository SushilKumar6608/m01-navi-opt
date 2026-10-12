"""
Phase 7b: NAVI-Opt read-only HTTP API.

    uvicorn navi_opt.api.app:app --port 8000      (interactive docs at /docs)

A thin layer over `navi_opt.dashboard.data` — the same tested functions the
Streamlit dashboard uses — so the two cannot disagree about, say, what a
like-for-like reoptimization comparison is. It is READ-ONLY: it reads the
Pareto sweep CSV and snapshot-reads the Kafka topics; it never publishes,
departs voyages or triggers reoptimizations.

Configuration (environment variables, same names as the dashboard):
    NAVI_DATA_DIR   default <repo>/data/processed
    NAVI_KAFKA      default localhost:9092

`create_app()` takes the Kafka reader, graph loader and clock as arguments so
tests run with no broker; the module-level `app` uses the real ones. NOT
verified against a real broker from the sandbox this was written in — see
`findings.md`.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query

from navi_opt.api import schemas as m
from navi_opt.api import service
from navi_opt.dashboard import kafka_reader
from navi_opt.dashboard.data import (
    VoyageView,
    build_voyage_views,
    load_frontier,
    path_coordinates,
    profile_leg_frame,
    summarize_frontier,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GRAPH = "ocean_digraph_rotterdam_ceyhan.pkl"
RECENT_WEATHER_EVENTS = 50
#: Shorter than the dashboard's 15 s: an HTTP client should get its 503 quickly.
API_KAFKA_TIMEOUT_SECONDS = 8.0


def _api_reader(bootstrap: str, topic: str):
    return kafka_reader.read_topic(bootstrap, topic, timeout_seconds=API_KAFKA_TIMEOUT_SECONDS)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _default_graph_loader(path: Path):
    from navi_opt.grid.graph_builder import load_graph  # lazy: heavy imports only when a route is asked for
    return load_graph(path)


def create_app(
    data_dir: Optional[Path] = None,
    bootstrap: Optional[str] = None,
    graph_path: Optional[Path] = None,
    read_topic: Optional[service.ReadTopic] = None,
    graph_loader: Callable[[Path], Any] = _default_graph_loader,
    now: Callable[[], datetime] = _utcnow,
    ttl_seconds: float = 5.0,
) -> FastAPI:
    data_dir = Path(data_dir or os.environ.get("NAVI_DATA_DIR", REPO_ROOT / "data" / "processed"))
    bootstrap = bootstrap or os.environ.get("NAVI_KAFKA", kafka_reader.DEFAULT_BOOTSTRAP_SERVERS)
    graph_path = Path(graph_path or data_dir / DEFAULT_GRAPH)
    store = service.EventStore(read_topic or _api_reader, bootstrap, ttl_seconds)
    graph_cache: Dict[str, Any] = {}

    api = FastAPI(
        title="NAVI-Opt API",
        version="0.1.0",
        description=(
            "Read-only access to NAVI-Opt's Pareto sweep and live voyage state. Vessel fuel curves and "
            "deadlines are synthetic; voyage positions come from an accelerated demo clock, not vessel "
            "tracking; weather is real CMEMS data from a fixed June 2024 window."
        ),
    )

    # ---- helpers ---------------------------------------------------------

    def _read(topic: str):
        events, note = store.get(topic)
        return events, note

    def _views() -> tuple[List[VoyageView], List[str]]:
        notes: List[str] = []
        data = {}
        for topic in (kafka_reader.TOPIC_VOYAGE_EVENTS, kafka_reader.TOPIC_REOPTIMIZATION_RESULTS,
                      kafka_reader.TOPIC_WEATHER_UPDATES):
            events, note = _read(topic)
            if note and note.startswith(kafka_reader.UNREACHABLE_PREFIX):
                raise HTTPException(status_code=503, detail=note)
            if note:
                notes.append(note)
            data[topic] = events
        views = build_voyage_views(
            data[kafka_reader.TOPIC_VOYAGE_EVENTS], data[kafka_reader.TOPIC_REOPTIMIZATION_RESULTS],
            now(), data[kafka_reader.TOPIC_WEATHER_UPDATES],
        )
        return views, notes

    def _view(voyage_id: str) -> VoyageView:
        views, _ = _views()
        for v in views:
            if v.voyage_id == voyage_id:
                return v
        raise HTTPException(status_code=404, detail=f"No voyage '{voyage_id}' on the voyage-events topic.")

    def _graph():
        if not graph_path.exists():
            raise HTTPException(status_code=503, detail=f"No graph cache at {graph_path}; "
                                "run scripts/build_real_ocean_graph.py.")
        key = f"{graph_path}:{graph_path.stat().st_mtime}"
        if key not in graph_cache:
            graph_cache.clear()
            graph_cache[key] = graph_loader(graph_path)
        return graph_cache[key]

    # ---- routes ----------------------------------------------------------

    @api.get("/health", response_model=m.Health, tags=["meta"])
    def health() -> m.Health:
        return m.Health()

    @api.get("/frontier", response_model=m.FrontierResponse, tags=["pareto"])
    def frontier(
        file: Optional[str] = Query(None, description="sweep CSV file name inside the data dir (default: newest pareto_frontier*.csv)"),
        corridor: Optional[int] = Query(None, ge=1, description="only this 1-based corridor"),
        frontier_only: bool = Query(False, description="only non-dominated points"),
    ) -> m.FrontierResponse:
        if file is not None:
            if Path(file).name != file:  # no path traversal out of the data dir
                raise HTTPException(status_code=400, detail="`file` must be a plain file name.")
            path = data_dir / file
        else:
            candidates = sorted(data_dir.glob("pareto_frontier*.csv"), key=lambda p: p.stat().st_mtime)
            if not candidates:
                raise HTTPException(status_code=404, detail=f"No pareto_frontier*.csv in {data_dir}; "
                                    "run scripts/run_pareto_sweep.py.")
            path = candidates[-1]
        try:
            df = load_frontier(path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        summary = summarize_frontier(df)  # of the whole file, whatever the filters below
        if corridor is not None:
            df = df[df["corridor"] == corridor]
        if frontier_only:
            df = df[df["on_frontier"]]
        return m.FrontierResponse(
            source_file=path.name, severe_threshold_m=df.attrs.get("severe_threshold_m"),
            summary=service.frontier_summary(summary), points=service.frontier_points(df),
        )

    @api.get("/voyages", response_model=m.VoyagesResponse, tags=["voyages"])
    def voyages(status: Optional[str] = Query(None, pattern="^(sailing|arrived)$")) -> m.VoyagesResponse:
        views, notes = _views()
        if status:
            views = [v for v in views if v.status == status]
        return m.VoyagesResponse(voyages=[service.voyage_summary(v) for v in views], kafka_notes=notes)

    @api.get("/voyages/{voyage_id}", response_model=m.VoyageDetail, tags=["voyages"])
    def voyage(voyage_id: str) -> m.VoyageDetail:
        return service.voyage_detail(_view(voyage_id))

    @api.get("/voyages/{voyage_id}/profile", response_model=m.ProfileResponse, tags=["voyages"])
    def profile(voyage_id: str, which: str = Query("current", pattern="^(original|current)$")) -> m.ProfileResponse:
        v = _view(voyage_id)
        prof = v.original_profile if which == "original" else v.current_profile
        return m.ProfileResponse(voyage_id=voyage_id, which=which, legs=service.profile_legs(prof))

    @api.get("/voyages/{voyage_id}/route", response_model=m.RouteResponse, tags=["voyages"])
    def route(voyage_id: str) -> m.RouteResponse:
        v = _view(voyage_id)
        graph = _graph()
        try:
            lats, lons = path_coordinates(graph, v.original_profile.path)
            trigger = None
            if v.steps:
                last = v.steps[-1]
                elats, elons = path_coordinates(graph, [last.trigger_u, last.trigger_v])
                trigger = ((elats[0], elons[0]), (elats[1], elons[1]))
        except KeyError as exc:
            raise HTTPException(status_code=409, detail=f"Graph cache does not match this voyage: {exc}")
        return m.RouteResponse(
            voyage_id=voyage_id, lats=lats, lons=lons,
            current_index=len(v.original_profile.path) - len(v.remaining_path), trigger_edge=trigger,
        )

    @api.get("/weather-events", response_model=m.WeatherEventsResponse, tags=["weather"])
    def weather_events(limit: int = Query(RECENT_WEATHER_EVENTS, ge=1, le=1000)) -> m.WeatherEventsResponse:
        events, note = _read(kafka_reader.TOPIC_WEATHER_UPDATES)
        if note and note.startswith(kafka_reader.UNREACHABLE_PREFIX):
            raise HTTPException(status_code=503, detail=note)
        recent = sorted(events, key=lambda e: e.get("sampled_at", ""), reverse=True)[:limit]
        return m.WeatherEventsResponse(events=[
            m.WeatherEvent(u=e["u"], v=e["v"], sampled_at=e.get("sampled_at"),
                           previous_wave_height_m=e.get("previous_wave_height_m"),
                           new_wave_height_m=e.get("new_wave_height_m"), injected=bool(e.get("injected", False)))
            for e in recent
        ])

    return api


app = create_app()
