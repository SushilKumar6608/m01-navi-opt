# Phase 5: custom Airflow image for the NAVI-Opt DAG. The stock
# apache/airflow:2.9.3 image has none of navi_opt's dependencies, and this
# project targets Python 3.11 (pyproject.toml's requires-python) while
# Apache's default tag for that version is 3.8 — the `-python3.11` variant
# tag below is Apache's own published way to pick the interpreter version,
# not something this project is patching in.
#
# NOT verified from the sandbox that wrote this (no Docker there to build
# or pull with) — if `docker compose build` reports this tag doesn't
# exist, check Apache's published tags for the closest available Python
# 3.11 build of 2.9.x and update this line; the requirements.txt install
# step below doesn't depend on the exact patch version.
FROM apache/airflow:2.9.3-python3.11

# Installed as the airflow user (the image's default), matching how the
# base image itself is built — pip installing as root into this image is
# actively discouraged by Apache's own docs and can leave files the
# airflow user can't read.
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt
