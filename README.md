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

Phase 0 — repository and infrastructure scaffold. See `findings.md` (created once
Phase 1 begins) for the running log of design decisions and results.

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
│   ├── streaming/               # Kafka producer/consumer for live reoptimization
│   ├── orchestration/          # Airflow DAG task logic
│   ├── api/                    # FastAPI app
│   └── dashboard/               # Streamlit + Plotly nautical dashboard
├── infra/
│   ├── docker-compose.yml      # Kafka (KRaft) + Kafka UI + Airflow (LocalExecutor) + Postgres
│   └── airflow/dags/           # Airflow DAG definitions (mounted into the Airflow containers)
├── tests/
├── notebooks/
├── environment.yml             # conda environment spec
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
(register at https://data.marine.copernicus.eu/register).

To bring up Kafka + Airflow locally (requires Docker Desktop):

```
cd infra
docker compose up -d
```

Airflow UI: http://localhost:8080 (admin/admin)
Kafka UI: http://localhost:8081
