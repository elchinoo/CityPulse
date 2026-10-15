"""OTLP/HTTP metrics receiver: any OpenTelemetry SDK or Collector can report sensor readings here.

Protobuf or JSON bodies, optionally gzip (bounded inflate). Each gauge or sum data point becomes one reading.
Identity comes from attributes, data point first, then resource:
  sensor.id    the sensor's id (required)
  sensor.type  a catalogue key such as `temperature`; if absent, the metric name is used when it is a key
  district     district id (default `d00`)
The OTel Collector can map any existing attribute to these with its `attributes`/`resource` processors.
"""
import json
import zlib
from datetime import UTC, datetime

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)

from . import gateway
from .catalog import TYPES

MAX_DECOMPRESSED = 8 * 1024 * 1024


class OtlpError(ValueError):
    pass


def decode(body: bytes, content_type: str, encoding: str) -> dict:
    if encoding in ("gzip", "x-gzip"):
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            body = d.decompress(body, MAX_DECOMPRESSED)
        except zlib.error as exc:
            raise OtlpError("body is not valid gzip") from exc
        if d.unconsumed_tail or (not d.eof and len(body) >= MAX_DECOMPRESSED):
            raise OtlpError("decompressed body is too large")
    elif encoding not in ("", "identity"):
        raise OtlpError(f"unsupported Content-Encoding {encoding!r}")
    ct = content_type.split(";")[0].strip().lower()
    if ct == "application/json":
        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise OtlpError("body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise OtlpError("JSON body must be an object")
        return data
    if ct in ("application/x-protobuf", "application/protobuf"):
        msg = ExportMetricsServiceRequest()
        try:
            msg.ParseFromString(body)
        except Exception as exc:  # noqa: BLE001 - protobuf raises DecodeError and others
            raise OtlpError("body is not a valid OTLP protobuf message") from exc
        return MessageToDict(msg)
    raise OtlpError("Content-Type must be application/x-protobuf or application/json")


def _any(v):
    if not isinstance(v, dict):
        return None
    for k in ("stringValue", "doubleValue"):
        if k in v:
            return v[k]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return None
    return None


def _attrs(items) -> dict:
    return {str(kv.get("key", ""))[:60]: _any(kv.get("value", {})) for kv in (items or [])[:60] if isinstance(kv, dict)}


def to_readings(data: dict) -> tuple[list[dict], int]:
    """OTLP JSON structure -> gateway items. Returns (items, data points skipped for lacking an identity)."""
    items, skipped = [], 0
    for rm in data.get("resourceMetrics") or []:
        res = _attrs((rm.get("resource") or {}).get("attributes"))
        for sm in rm.get("scopeMetrics") or []:
            for metric in sm.get("metrics") or []:
                points = (metric.get("gauge") or metric.get("sum") or {}).get("dataPoints") or []
                name = str(metric.get("name", "")).lower().replace(".", "_").replace("-", "_")
                for p in points:
                    attrs = {**res, **_attrs(p.get("attributes"))}
                    sensor = attrs.get("sensor.id")
                    kind = attrs.get("sensor.type") or (name if name in TYPES else None)
                    value = p.get("asDouble", p.get("asInt"))
                    if not sensor or not kind or value is None:
                        skipped += 1
                        continue
                    try:
                        value = float(value)
                        ts = int(p["timeUnixNano"]) / 1e9 if p.get("timeUnixNano") else None
                    except (TypeError, ValueError):
                        skipped += 1
                        continue
                    if ts is not None and not (0 < ts < datetime.now(UTC).timestamp() + 120):
                        ts = None
                    item = {"sensor": str(sensor), "type": kind, "district": str(attrs.get("district") or "d00"),
                            "value": value, "protocol": "otlp"}
                    if ts:
                        item["ts"] = ts
                    items.append(item)
    return items, skipped


def store(data: dict) -> tuple[int, int, str]:
    """Run the points through the gateway. Returns (accepted, rejected, note)."""
    items, skipped = to_readings(data)
    accepted = rejected = 0
    note = ""
    for i in range(0, len(items), gateway.MAX_BATCH):
        res = gateway.accept(items[i:i + gateway.MAX_BATCH])
        accepted += res["accepted"]
        rejected += res["rejected"]
        note = note or "; ".join(res["errors"])
    if skipped:
        note = (note + "; " if note else "") + f"{skipped} data points had no sensor.id / sensor.type"
    return accepted, rejected + skipped, note


def ack(rejected: int, message: str, protobuf: bool) -> tuple[bytes, str]:
    msg = ExportMetricsServiceResponse()
    if rejected:
        msg.partial_success.rejected_data_points = rejected
        msg.partial_success.error_message = message[:200]
    if protobuf:
        return msg.SerializeToString(), "application/x-protobuf"
    return json.dumps(MessageToDict(msg)).encode(), "application/json"
