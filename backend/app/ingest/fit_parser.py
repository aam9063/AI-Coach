"""FIT file parsing with fitdecode (ODD task ING-4, PROJECT_BRIEF §5.2, §6).

Pure functions over ``bytes``: no I/O beyond the input, no engine imports,
suitable for reuse after raw FIT storage (ING-6). Extracts:

- per-second record streams mapped to our stream_type strings
  (``power``/``hr``/``speed``/``cadence``/``altitude``/``distance`` plus
  ``time`` as Unix epoch seconds), with ``None`` for missing values;
- swim ``length`` messages as ordered :class:`SwimLength` records.

The extraction logic works over fitdecode-like message dicts (``Message``)
so the pure transformation is unit-testable without a FIT fixture; the
module-level entry points adapt fitdecode frames to that shape. Unknown
message types are ignored. Corrupt or empty input raises ``ValueError``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict

import fitdecode
from fitdecode import FitDataMessage, FitError

# Our stream_type strings (app stream naming) in a fixed order.
STREAM_KEYS: tuple[str, ...] = (
    "time",
    "power",
    "hr",
    "speed",
    "cadence",
    "altitude",
    "distance",
)

# FIT field name(s) -> stream_type, preferring enhanced variants.
_ENHANCED_PREFERENCE: dict[str, tuple[str, ...]] = {
    "speed": ("enhanced_speed", "speed"),
    "altitude": ("enhanced_altitude", "altitude"),
}

_FIELD_TO_STREAM: dict[str, str] = {
    "timestamp": "time",
    "power": "power",
    "heart_rate": "hr",
    "speed": "speed",
    "enhanced_speed": "speed",
    "cadence": "cadence",
    "altitude": "altitude",
    "enhanced_altitude": "altitude",
    "distance": "distance",
}

# FIT swim_stroke enum -> our string names.
_SWIM_STROKES: dict[int, str] = {
    0: "freestyle",
    1: "backstroke",
    2: "breaststroke",
    3: "butterfly",
    4: "drill",
    5: "mixed",
}


class Message(TypedDict):
    """Fitdecode-like data message (duck-typed shape for the pure logic)."""

    name: str
    fields: dict[str, object]


@dataclass(frozen=True)
class SwimLength:
    """One swim length from a FIT ``length`` message."""

    start_time: float | None
    total_timer_time: float | None
    total_elapsed_time: float | None
    swim_stroke: str | None


def _frame_to_message(frame: fitdecode.FitDataMessage) -> Message:
    """Convert a fitdecode frame to the plain ``Message`` dict shape."""
    fields: dict[str, object] = {}
    for field in frame.fields:
        value: object = field.value
        if isinstance(value, datetime):
            value = value.timestamp()
        fields[field.name] = value
    return Message(name=frame.name, fields=fields)


def _iter_data_messages(data: bytes) -> Iterable[Message]:
    """Yield data messages from raw FIT bytes, mapping frames to ``Message``."""
    with fitdecode.FitReader(data) as reader:
        for frame in reader:
            if isinstance(frame, FitDataMessage):
                yield _frame_to_message(frame)


def messages_to_stream_lists(messages: Iterable[Message]) -> dict[str, list[float | None]]:
    """Pure: fold record-like messages into per-stream value arrays.

    Only ``record`` messages are consumed; everything else is ignored.
    Missing fields (or fields absent from the whole file) yield ``None``
    entries so all streams stay aligned per record.
    """
    streams: dict[str, list[float | None]] = {key: [] for key in STREAM_KEYS}
    for message in messages:
        if message["name"] != "record":
            continue
        fields = message["fields"]
        for key in STREAM_KEYS:
            value = _field_value(fields, key)
            streams[key].append(None if value is None else float(value))
    return streams


def _field_value(fields: dict[str, object], stream_key: str) -> float | None:
    """Resolve one stream value from a record's fields, honoring enhanced
    field preferences (e.g. ``enhanced_speed`` over ``speed``)."""
    if stream_key in _ENHANCED_PREFERENCE:
        for candidate in _ENHANCED_PREFERENCE[stream_key]:
            if candidate in fields and fields[candidate] is not None:
                return _numeric(fields[candidate])
        return None
    for field_name, mapped in _FIELD_TO_STREAM.items():
        if mapped == stream_key and field_name in fields:
            return _numeric(fields[field_name])
    return None


def _numeric(value: object) -> float | None:
    """Coerce a field value to float, returning None for non-numeric values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_fit_streams(data: bytes) -> dict[str, list[float | None]]:
    """Parse raw FIT bytes into per-second stream arrays keyed by stream_type.

    Raises ``ValueError`` when ``data`` is not a readable FIT file.
    """
    if not data:
        raise ValueError("not a valid FIT file: empty input")
    try:
        return messages_to_stream_lists(_iter_data_messages(data))
    except (FitError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"not a valid FIT file: {exc}") from exc


def messages_to_swim_lengths(messages: Iterable[Message]) -> list[SwimLength]:
    """Pure: extract swim lengths from ``length`` messages, in order.

    ``swim_stroke`` numeric enum values are mapped to names; unknown numeric
    values map to ``None``; string values pass through.
    """
    lengths: list[SwimLength] = []
    for message in messages:
        if message["name"] != "length":
            continue
        fields = message["fields"]
        stroke = fields.get("swim_stroke")
        if isinstance(stroke, bool) or not isinstance(stroke, int):
            stroke_name = stroke if isinstance(stroke, str) else None
        else:
            stroke_name = _SWIM_STROKES.get(stroke)
        lengths.append(
            SwimLength(
                start_time=_optional_float(fields.get("start_time")),
                total_timer_time=_optional_float(fields.get("total_timer_time")),
                total_elapsed_time=_optional_float(fields.get("total_elapsed_time")),
                swim_stroke=stroke_name,
            )
        )
    return lengths


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_fit_swim_lengths(data: bytes) -> list[SwimLength]:
    """Parse raw FIT bytes into ordered swim length records (possibly empty)."""
    if not data:
        raise ValueError("not a valid FIT file: empty input")
    try:
        return messages_to_swim_lengths(_iter_data_messages(data))
    except (FitError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"not a valid FIT file: {exc}") from exc
