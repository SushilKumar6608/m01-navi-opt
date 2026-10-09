# M01 — NAVI-Opt

**Dynamic Maritime Voyage Planning & Multi-Objective Route Optimization Engine**

A portfolio project demonstrating mathematical optimization, graph algorithms, and
decision-support modeling for a commercial tanker fleet: discretized H3 ocean-grid
routing, a two-phase time-dependent-A\* + MILP/NLP speed-profile solver under laycan
and ECA constraints, ε-constraint Pareto trade-off analysis (fuel vs. duration vs.
weather risk), an event-driven Kafka reoptimization layer, an Airflow-orchestrated
nightly fleet-planning batch, and a FastAPI + Streamlit/Plotly decision-support
dashboard.

## Data honesty

Coastlines, port coordinates, ECA boundaries, and wave/wind fields (via Copernicus
Marine Service) are **real, public data**. Per-vessel fuel-speed curves and laycan
windows are **synthetic** — no public source of proprietary fleet performance or
commercial charter data exists — but the fuel curves are calibrated to published,
literature-reported ranges for tanker classes (see `config/vessel_profiles.yaml`).
This split, and every other modeling assumption, is documented plainly in
`findings.md` as the project develops.

## Status

Through Phase 6 (Kafka event-driven reoptimization: a voyage departs, real CMEMS
weather is polled and diffed against the previous poll, and a meaningful forecast
change on an edge still ahead of a tracked voyage triggers a real, bounded
re-solve of that voyage's remaining legs) — see `findings.md` for the running log
of design decisions and results, including what's real vs. not-yet-verified at
each phase.

## Repository layout

```
m01-navi-opt/
├── config/                     # vessel fuel-curve profiles, ECA compliance costs
├── data/
│   ├── raw/                    # Natural Earth, CMEMS pulls, ECA polygons (gitignored)
│   └── processed/              # cached H3 graph, resolved weather grids (gitignored)
├── src/navi_opt/
│   ├── grid/                   # H3 ocean partitioning, land masking, edge pruning
│   ├── weather/                # CMEMS ingestion, resistance/fuel-curve model
│   ├── routing/                # time-dependent A*, Yen's k-shortest-path
│   ├── optimization/           # MILP (HiGHS) speed solver, NLP (Pyomo/Ipopt), Pareto sweep
│   ├── streaming/               # Phase 6: voyage state, reoptimization, Kafka producer/consumer logic
│   ├── orchestration/          # Airflow DAG task logic
│   ├── api/                    # FastAPI app
│   └── dashboard/               # Streamlit + Plotly nautical dashboard
├── infra/
│   ├── docker-compose.yml      # Kafka (KRaft) + Kafka UI + Airflow (LocalExecutor) + Postgres
│   └── airflow/
│       ├── Dockerfile           # custom Airflow image — adds navi_opt's own deps (see requirements.txt)
│       ├── requirements.txt     # pip deps for that image (narrower than environment.yml — see file)
│       └── dags/                # Airflow DAG definitions (mounted into the Airflow containers)
├── scripts/
│   ├── build_real_ocean_graph.py
│   ├── run_pareto_sweep.py
│   ├── depart_demo_voyage.py          # Phase 6: solves + publishes one voyage's initial profile
│   ├── run_weather_producer.py        # Phase 6: polls real CMEMS, publishes weather-change events
│   ├── run_reoptimization_consumer.py # Phase 6: tracks voyages, reoptimizes on weather events
│   └── inject_weather_event.py        # Phase 6: labelled test event for demos
├── tests/
├── notebooks/
├── environment.yml             # conda environment spec (confluent-kafka already included)
└── .env.example                # copy to .env and fill in CMEMS credentials
```

## Setup

See the Phase 0 setup instructions (conda environment + Docker Desktop) in the
project chat / findings.md. Quick reference:

```
conda env create -f environment.yml
conda activate m01-navi-opt
copy .env.example .env
```

Then edit `.env` with your free Copernicus Marine credentials
(register at https://data.marine.copernicus.eu/register). **Do this before the
next step** — as of Phase 5, `docker-compose.yml` loads `../.env` directly into
the Airflow containers (`env_file:` on `x-airflow-common`), so `docker compose up`
fails at that step if `.env` doesn't exist yet, even if you don't care about
Airflow's CMEMS access yet.

To bring up Kafka + Airflow locally (requires Docker Desktop):

```
cd infra
docker compose up -d --build
```

`--build` is required the first time (and after any change to
`infra/airflow/Dockerfile`/`requirements.txt`) since Airflow's containers now run
a custom image with `navi_opt`'s own dependencies installed, not the stock
`apache/airflow` image — see `infra/airflow/requirements.txt` for what's in it and
why. Subsequent `docker compose up -d` calls reuse the built image without
`--build` unless those files changed.

Airflow UI: http://localhost:8080 (admin/admin are the image's initial defaults —
change them, and remember to use whatever you changed them to afterward)
Kafka UI: http://localhost:8081

The `pareto_sweep_rotterdam_ceyhan` DAG (Phase 5, `infra/airflow/dags/pareto_sweep_dag.py`)
loads paused by default (`AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION`) — see that
file's own docstring before enabling it, including why it deliberately doesn't
retry on failure and what it costs to run (~45 minutes, real CMEMS network calls).

## Running Phase 6 (Kafka event-driven reoptimization)

Requires the Kafka broker from `infra/docker-compose.yml` up (`docker compose up
-d` from `infra/` — `confluent-kafka` is already in `environment.yml`, so no
separate image/build should be needed here the way Airflow required one, though
that hasn't been confirmed against a real run yet — see `findings.md`'s Phase 6
entry), `.env` filled in with real CMEMS credentials, and
`scripts/build_real_ocean_graph.py` already run once (these scripts reuse its
cached digraph). Kafka UI (http://localhost:8081) is the easiest way to watch the
three topics (`voyage-events`, `weather-updates`, `reoptimization-results`) fill
up in real time.

In one terminal, start the consumer first so it's listening before anything is
published:

```
python scripts/run_reoptimization_consumer.py
```

In a second terminal, depart one (or more, with distinct `--voyage-id`s) demo
voyages:

```
python scripts/depart_demo_voyage.py
```

In a third terminal, start the weather producer, which polls real CMEMS on a
real (documented, overridable) interval and publishes an event whenever a
forecast has meaningfully revised:

```
python scripts/run_weather_producer.py --poll-interval-seconds 60
```

The June 2024 weather window rarely revises, so to exercise the path without
waiting, `python scripts/inject_weather_event.py` publishes one clearly labelled
test event on an edge still ahead of the voyage (it proves the plumbing, not that
the optimizer reacts to a storm — see `findings.md`).

When a weather-update event lands on an edge still ahead of a departed voyage,
the consumer re-fetches real weather and reoptimizes that voyage's remaining
legs, printing the fuel/transit before-and-after and publishing the full result
to `reoptimization-results`. See `findings.md`'s Phase 6 entry for the full
design (the three-clock demo-time model, the event-as-trigger-not-ground-truth
pattern, and the in-memory-registry/real-CMEMS-polling scope decisions) and what
hasn't yet been verified against a real broker.
