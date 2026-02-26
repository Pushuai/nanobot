"""Raw protobuf decoder for blobs without .proto schema."""

from __future__ import annotations

import re
from typing import Any


class ProtobufDecodeError(ValueError):
    """Raised when protobuf wire data is truncated or malformed."""


_WIRE_NAME = {
    0: "varint",
    1: "fixed64",
    2: "length_delimited",
    3: "start_group",
    4: "end_group",
    5: "fixed32",
}


def decode_protobuf_blob(data: bytes, max_depth: int = 3) -> list[dict[str, Any]]:
    """Decode protobuf wire data without schema information."""
    max_depth = max(1, int(max_depth))
    fields, end = _parse_message(data, 0, depth=0, max_depth=max_depth, strict=False)
    if end < len(data):
        fields.append({
            "wire": "trailing_bytes",
            "offset": end,
            "length": len(data) - end,
            "bytes_hex": _hex_preview(data[end:]),
        })
    return fields


def format_decoded_fields(fields: list[dict[str, Any]]) -> str:
    """Render decoded fields as readable text."""
    if not fields:
        return "(no decodable protobuf fields)"
    lines: list[str] = []
    _append_fields(lines, fields, indent=0)
    return "\n".join(lines)


def extract_printable_strings(data: bytes, min_len: int = 8, limit: int = 120) -> list[str]:
    """Extract printable ASCII strings as a fallback when wire decode is not useful."""
    pattern = re.compile(rb"[ -~]{%d,}" % max(1, min_len))
    out: list[str] = []
    for raw in pattern.findall(data):
        out.append(raw.decode("utf-8", errors="ignore"))
        if len(out) >= limit:
            break
    return out


def _append_fields(lines: list[str], fields: list[dict[str, Any]], indent: int) -> None:
    pad = "  " * indent
    for field in fields:
        wire = field.get("wire", "unknown")
        if wire == "trailing_bytes":
            lines.append(
                f"{pad}- trailing bytes at offset={field['offset']}, "
                f"len={field['length']}, hex={field['bytes_hex']}"
            )
            continue

        number = field.get("field")
        offset = field.get("offset")
        lines.append(f"{pad}- field {number} ({wire}) @ offset {offset}")

        if "value" in field:
            lines.append(f"{pad}  value: {field['value']}")
        if "length" in field:
            lines.append(f"{pad}  len: {field['length']}")
        if "text_preview" in field:
            preview = field["text_preview"].replace("\n", "\\n")
            lines.append(f"{pad}  text: {preview!r}")
        if "bytes_hex" in field:
            lines.append(f"{pad}  hex: {field['bytes_hex']}")
        nested = field.get("nested")
        if nested:
            lines.append(f"{pad}  nested:")
            _append_fields(lines, nested, indent + 2)


def _parse_message(
    data: bytes,
    offset: int,
    *,
    depth: int,
    max_depth: int,
    strict: bool,
) -> tuple[list[dict[str, Any]], int]:
    fields: list[dict[str, Any]] = []
    i = offset
    while i < len(data):
        start = i
        try:
            key, i = _read_varint(data, i)
        except ProtobufDecodeError:
            if strict:
                raise
            break

        if key == 0:
            if strict:
                raise ProtobufDecodeError("invalid field key 0")
            break

        field_no = key >> 3
        wire_type = key & 0x07
        wire_name = _WIRE_NAME.get(wire_type, f"wire_{wire_type}")

        item: dict[str, Any] = {
            "field": field_no,
            "wire_type": wire_type,
            "wire": wire_name,
            "offset": start,
        }

        if wire_type == 0:
            value, i = _read_varint(data, i)
            item["value"] = value
            fields.append(item)
            continue

        if wire_type == 1:
            if i + 8 > len(data):
                if strict:
                    raise ProtobufDecodeError("truncated fixed64")
                break
            chunk = data[i:i + 8]
            i += 8
            item["value"] = int.from_bytes(chunk, "little", signed=False)
            item["bytes_hex"] = chunk.hex()
            fields.append(item)
            continue

        if wire_type == 2:
            length, i = _read_varint(data, i)
            end = i + length
            if end > len(data):
                if strict:
                    raise ProtobufDecodeError("truncated length-delimited field")
                chunk = data[i:]
                i = len(data)
            else:
                chunk = data[i:end]
                i = end

            item["length"] = len(chunk)
            item["bytes_hex"] = _hex_preview(chunk)
            text = _printable_utf8(chunk)
            if text is not None:
                item["text_preview"] = _trim_text(text, max_chars=240)
            nested = _try_nested_decode(chunk, depth=depth, max_depth=max_depth)
            if nested:
                item["nested"] = nested
            fields.append(item)
            continue

        if wire_type == 5:
            if i + 4 > len(data):
                if strict:
                    raise ProtobufDecodeError("truncated fixed32")
                break
            chunk = data[i:i + 4]
            i += 4
            item["value"] = int.from_bytes(chunk, "little", signed=False)
            item["bytes_hex"] = chunk.hex()
            fields.append(item)
            continue

        if strict:
            raise ProtobufDecodeError(f"unsupported wire type {wire_type}")
        break

    return fields, i


def _try_nested_decode(
    chunk: bytes,
    *,
    depth: int,
    max_depth: int,
) -> list[dict[str, Any]] | None:
    if not chunk or (depth + 1) > max_depth:
        return None
    try:
        nested, end = _parse_message(
            chunk,
            0,
            depth=depth + 1,
            max_depth=max_depth,
            strict=True,
        )
    except ProtobufDecodeError:
        return None
    if end != len(chunk) or not nested:
        return None
    return nested


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    i = offset
    while i < len(data):
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift >= 70:
            raise ProtobufDecodeError("varint too long")
    raise ProtobufDecodeError("truncated varint")


def _printable_utf8(data: bytes) -> str | None:
    if not data:
        return ""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    if printable / max(1, len(text)) < 0.9:
        return None
    return text


def _trim_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...(truncated, total {len(text)} chars)"


def _hex_preview(data: bytes, limit: int = 64) -> str:
    if len(data) <= limit:
        return data.hex()
    return f"{data[:limit].hex()}...(truncated, total {len(data)} bytes)"
