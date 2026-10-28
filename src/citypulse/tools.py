"""Tool-ready functions: what an AIDB pipeline, an MCP server or any LLM agent can call later. NO agent is built here.

Each tool has a name, a description written for a model, a JSON Schema for its arguments (`input_schema`, the shape MCP and the major LLM
APIs take) and a plain function. `GET /api/tools` lists them; `POST /api/tools/{name}` calls one with `{"arguments": {...}}` and needs the
same permission as the equivalent read route (`city:read`). All three are read-only.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import explain as explain_mod
from . import memory, timeseries
from .catalog import CITY_ID, DISTRICT_ID


class ToolError(ValueError):
    """The arguments do not fit the tool's schema (HTTP 422)."""


class UnknownTool(LookupError):
    pass


CITY = {"type": "string", "pattern": CITY_ID.pattern, "maxLength": 32, "description": "City id, e.g. paris"}
DISTRICT = {"type": "string", "pattern": DISTRICT_ID.pattern, "maxLength": 24, "description": "District id within the city, e.g. d03 or alder-quay"}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    run: Callable[..., Any]
    permission: str = "city:read"

    def listing(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema, "permission": self.permission,
                "read_only": True}


def _explain(conn, city: str, district: str, k: int = 3) -> dict:
    return explain_mod.explain(conn, city, district, k)


def _similar(conn, city: str, district: str, k: int = 5, scope: str = "all") -> dict:
    return memory.similar(conn, city, district, k, scope)


def _series(conn, city: str, district: str, bucket: str = "5m", type: str | None = None, hours: float = 6) -> dict:
    end = datetime.now(UTC)
    return timeseries.series(conn, city, district, bucket, end - timedelta(hours=hours), end, type)


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("explain_district",
         "Everything known about one district in a single database round trip: current sensor status counts (relational), the last hour of "
         "readings from the 5-minute roll-up (time series), which vendor or firmware is over-represented among its problem sensors (JSON), the "
         "nearest healthy sensors and neighbouring districts (geo, where PostGIS exists) and the most similar past episodes with what happened "
         "next (vector, where pgvector exists). Call it first when asked what is wrong with a district and whether it has happened before. "
         "Parts whose extension is missing say so in `available: false` and `reason`.",
         {"type": "object", "properties": {"city": CITY, "district": DISTRICT,
                                           "k": {"type": "integer", "minimum": 1, "maximum": explain_mod.K_MAX, "default": 3,
                                                 "description": "How many similar past episodes to return"}},
          "required": ["city", "district"], "additionalProperties": False},
         _explain),
    Tool("similar_episodes",
         "The k past episodes (a district over a 15-minute window) whose fingerprint is closest to this district's latest one, by cosine distance, "
         "each with the verdict an hour later and how long the bad stretch lasted. Use it to answer 'have we seen this before, and how did it end?'.",
         {"type": "object", "properties": {"city": CITY, "district": DISTRICT,
                                           "k": {"type": "integer", "minimum": 1, "maximum": memory.SIMILAR_MAX, "default": 5},
                                           "scope": {"type": "string", "enum": ["all", "city"], "default": "all",
                                                     "description": "'city' limits the search to this city's own history"}},
          "required": ["city", "district"], "additionalProperties": False},
         _similar),
    Tool("district_series",
         "Roll-up time series of one district: readings, distinct sensors reporting and out-of-range readings per bucket (plus mean, min and max "
         "when a sensor type is given). Empty buckets are absent.",
         {"type": "object", "properties": {"city": CITY, "district": DISTRICT,
                                           "bucket": {"type": "string", "enum": ["5m", "1h"], "default": "5m"},
                                           "hours": {"type": "number", "minimum": 0.25, "maximum": 168, "default": 6,
                                                     "description": "How far back from now"},
                                           "type": {"type": "string", "maxLength": 32, "description": "A sensor type key, e.g. temperature"}},
          "required": ["city", "district"], "additionalProperties": False},
         _series),
]}


def check_arguments(tool: Tool, arguments: dict) -> dict:
    """A small JSON Schema check (type, required, enum, bounds, pattern, no extra properties): enough for these flat schemas."""
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")
    schema = tool.input_schema
    props = schema["properties"]
    extra = sorted(set(arguments) - set(props))
    if extra:
        raise ToolError(f"unknown argument(s): {', '.join(extra)}")
    for name in schema.get("required", []):
        if name not in arguments:
            raise ToolError(f"missing argument: {name}")
    clean = {}
    for name, value in arguments.items():
        spec = props[name]
        kind = spec["type"]
        if kind == "string":
            if not isinstance(value, str) or len(value) > spec.get("maxLength", 256):
                raise ToolError(f"{name} must be a string of at most {spec.get('maxLength', 256)} characters")
            if "pattern" in spec and not re.match(spec["pattern"], value):
                raise ToolError(f"{name} does not match {spec['pattern']}")
            if "enum" in spec and value not in spec["enum"]:
                raise ToolError(f"{name} must be one of {', '.join(spec['enum'])}")
        else:
            ok = isinstance(value, int) if kind == "integer" else isinstance(value, (int, float))
            if isinstance(value, bool) or not ok:
                raise ToolError(f"{name} must be {'an integer' if kind == 'integer' else 'a number'}")
            if not spec["minimum"] <= value <= spec["maximum"]:
                raise ToolError(f"{name} must be between {spec['minimum']} and {spec['maximum']}")
        clean[name] = value
    return clean


def call(conn, name: str, arguments: dict) -> dict:
    """Validate the arguments against the tool's schema and run it. Raises UnknownTool, ToolError, and the tool's own errors
    (explain.NoSuchDistrict, timeseries.BadRange)."""
    tool = TOOLS.get(name)
    if tool is None:
        raise UnknownTool(f"no such tool: {name}")
    return {"tool": name, "result": tool.run(conn, **check_arguments(tool, arguments))}


def listing() -> list[dict]:
    return [t.listing() for t in TOOLS.values()]
