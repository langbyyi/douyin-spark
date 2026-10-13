"""Minimal protobuf wire-format reader.

Only what is needed to inspect IM responses without their .proto schema: every
length-delimited field is kept as raw bytes, so the caller decides whether it is
a nested message, a UTF-8 string or opaque data.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntEnum


class WireType(IntEnum):
    VARINT = 0
    FIXED64 = 1
    LENGTH = 2
    FIXED32 = 5


class WireError(ValueError):
    """Raised when bytes are not a well-formed protobuf message."""


@dataclass(frozen=True, slots=True)
class Field:
    number: int
    wire_type: WireType
    value: int | bytes


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a base-128 varint at ``offset``; return ``(value, next_offset)``."""
    result = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise WireError("truncated varint")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, offset
    raise WireError("varint longer than 10 bytes")


def iter_fields(data: bytes) -> Iterator[Field]:
    """Yield the top-level fields of ``data``; raise :class:`WireError` if malformed."""
    offset, end = 0, len(data)
    while offset < end:
        key, offset = read_varint(data, offset)
        number, raw_type = key >> 3, key & 0x7
        if number == 0:
            raise WireError("field number 0")
        try:
            wire_type = WireType(raw_type)
        except ValueError as exc:
            raise WireError(f"unsupported wire type {raw_type}") from exc

        if wire_type is WireType.VARINT:
            value, offset = read_varint(data, offset)
            yield Field(number, wire_type, value)
            continue

        size = {WireType.FIXED64: 8, WireType.FIXED32: 4}.get(wire_type)
        if size is None:
            size, offset = read_varint(data, offset)
        if offset + size > end:
            raise WireError("field runs past end of buffer")
        chunk = bytes(data[offset : offset + size])
        offset += size
        if wire_type is WireType.LENGTH:
            yield Field(number, wire_type, chunk)
        else:
            yield Field(number, wire_type, int.from_bytes(chunk, "little"))


class Message:
    """Read-only view over one decoded message level, indexed by field number."""

    def __init__(self, data: bytes) -> None:
        self.fields: dict[int, list[Field]] = {}
        for field in iter_fields(data):
            self.fields.setdefault(field.number, []).append(field)

    @classmethod
    def try_parse(cls, data: bytes | None) -> Message | None:
        if not data:
            return None
        try:
            return cls(data)
        except WireError:
            return None

    def int(self, number: int) -> int | None:
        for field in self.fields.get(number, ()):
            if field.wire_type is WireType.VARINT:
                return int(field.value)
        return None

    def raw(self, number: int) -> bytes | None:
        for field in self.fields.get(number, ()):
            if field.wire_type is WireType.LENGTH:
                return bytes(field.value)  # type: ignore[arg-type]
        return None

    def text(self, number: int) -> str | None:
        raw = self.raw(number)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def child(self, number: int) -> Message | None:
        return Message.try_parse(self.raw(number))


def printable_strings(data: bytes, depth: int = 4) -> list[str]:
    """Collect every UTF-8 string found in nested length-delimited fields.

    Used as a safety net to spot risk-control markers whose exact field position
    is not known.
    """
    found: list[str] = []
    message = Message.try_parse(data)
    if message is None or depth <= 0:
        return found
    for fields in message.fields.values():
        for field in fields:
            if field.wire_type is not WireType.LENGTH:
                continue
            chunk = bytes(field.value)  # type: ignore[arg-type]
            try:
                text = chunk.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            if text and "".join(text.split()).isprintable():  # allow newlines / tabs
                found.append(text)
            found.extend(printable_strings(chunk, depth - 1))
    return found
