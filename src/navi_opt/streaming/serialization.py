"""
Phase 6: JSON (de)serialization for the values that cross Kafka topic
boundaries in this project.

Plain JSON, not Avro/Protobuf + a schema registry — deliberately, matching
this project's repeated "don't add complexity a problem this size doesn't
need" standard (same call already made for `pareto_frontier()`'s O(n^2)
filter and the STRtree/joblib question in Phase 2). A single-node demo
Kafka cluster with a handful of message types doesn't need schema
evolution machinery; JSON is trivially inspectable in Kafka UI too, which
matters for a project meant to be demoed and read, not just run.

`SpeedProfileResult`/`LegResult` are `typing.NamedTuple`s, so `._asdict()`
gets most of the way there for free — the only real work is recursing into
`SpeedProfileResult.legs` (a list of `LegResult`, not flat) on the way
both in and out.
"""
from __future__ import annotations

from typing import Any, Dict

from navi_opt.optimization.speed_profile import LegResult, SpeedProfileResult


def leg_result_to_dict(leg: LegResult) -> Dict[str, Any]:
    return leg._asdict()


def leg_result_from_dict(d: Dict[str, Any]) -> LegResult:
    return LegResult(**d)


def speed_profile_result_to_dict(result: SpeedProfileResult) -> Dict[str, Any]:
    d = result._asdict()
    d["legs"] = [leg_result_to_dict(leg) for leg in result.legs]
    return d


def speed_profile_result_from_dict(d: Dict[str, Any]) -> SpeedProfileResult:
    d = dict(d)  # don't mutate the caller's dict
    d["legs"] = [leg_result_from_dict(leg) for leg in d["legs"]]
    return SpeedProfileResult(**d)
