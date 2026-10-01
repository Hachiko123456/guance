"""Small, dependency-free protobuf/tnetstring primitives used by offline capture readers."""
from __future__ import annotations

import io
from collections import defaultdict
from typing import BinaryIO, Iterator, Any

MAX_RECORD = 64 * 1024 * 1024


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def varint(body: bytes, offset: int = 0) -> tuple[int, int]:
    value = 0
    for index in range(10):
        require(offset < len(body), "truncated protobuf varint")
        byte = body[offset]
        offset += 1
        if index == 9:
            require(byte <= 1, "protobuf varint overflow")
        value |= (byte & 0x7F) << (7 * index)
        if byte < 0x80:
            return value, offset
    raise ValueError("protobuf varint overflow")


def encode_varint(value: int) -> bytes:
    require(isinstance(value, int) and value >= 0, "varint requires a non-negative integer")
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def fields(body: bytes) -> dict[int, list[tuple[int, int | bytes]]]:
    groups: dict[int, list[tuple[int, int | bytes]]] = defaultdict(list)
    offset = 0
    while offset < len(body):
        tag, offset = varint(body, offset)
        number, wire_type = tag >> 3, tag & 7
        require(1 <= number <= 536870911, "invalid protobuf field number")
        if wire_type == 0:
            value, offset = varint(body, offset)
        elif wire_type in (1, 2, 5):
            size = 8 if wire_type == 1 else 4 if wire_type == 5 else None
            if wire_type == 2:
                size, offset = varint(body, offset)
            require(size is not None and offset + size <= len(body), "truncated protobuf field")
            value = body[offset:offset + size]
            offset += size
        else:
            raise ValueError(f"unsupported protobuf wire type: {wire_type}")
        groups[number].append((wire_type, value))
    return dict(groups)


def optional_scalar(
    group: dict[int, list[tuple[int, int | bytes]]], number: int,
) -> tuple[bool, int | None]:
    """Read a scalar without losing protobuf field presence information."""
    values = group.get(number, [])
    if not values:
        return False, None
    require(len(values) == 1 and values[0][0] == 0, f"field {number} is not a scalar")
    value = values[0][1]
    assert isinstance(value, int)
    return True, value


def scalar(group: dict[int, list[tuple[int, int | bytes]]], number: int, default: int = 0) -> int:
    present, value = optional_scalar(group, number)
    return value if present else default


def blobs(group: dict[int, list[tuple[int, int | bytes]]], number: int) -> list[bytes]:
    values = group.get(number, [])
    require(all(wire_type == 2 for wire_type, _ in values), f"field {number} is not bytes")
    return [value for _, value in values if isinstance(value, bytes)]


def one_blob(group: dict[int, list[tuple[int, int | bytes]]], number: int) -> bytes:
    values = blobs(group, number)
    require(len(values) == 1, f"expected exactly one field {number}")
    return values[0]


def packed(body: bytes) -> list[int]:
    values: list[int] = []
    offset = 0
    while offset < len(body):
        value, offset = varint(body, offset)
        values.append(value)
    return values


def packed_field(group: dict[int, list[tuple[int, int | bytes]]], number: int) -> list[int]:
    return packed(one_blob(group, number)) if number in group else []


def tnet_read(stream: BinaryIO, depth: int = 0) -> Any:
    require(depth < 64, "tnetstring nesting too deep")
    digits = bytearray()
    while True:
        char = stream.read(1)
        if not char:
            if not digits:
                raise EOFError
            raise ValueError("truncated tnetstring length")
        if char == b":":
            break
        require(char.isdigit() and len(digits) < 10, "invalid tnetstring length")
        digits.extend(char)
    require(bool(digits), "missing tnetstring length")
    length = int(digits)
    require(length <= MAX_RECORD, "tnetstring record too large")
    body, tag = stream.read(length), stream.read(1)
    require(len(body) == length and len(tag) == 1, "truncated tnetstring")
    if tag == b",":
        return body
    if tag == b";":
        return body.decode("utf-8")
    if tag == b"#":
        return int(body)
    if tag == b"^":
        return float(body)
    if tag == b"!":
        require(body in (b"true", b"false"), "invalid boolean")
        return body == b"true"
    if tag == b"~":
        require(not body, "invalid null")
        return None
    if tag in (b"]", b"}"):
        sub = io.BytesIO(body)
        values = []
        while sub.tell() < length:
            values.append(tnet_read(sub, depth + 1))
        if tag == b"]":
            return values
        require(len(values) % 2 == 0, "invalid tnetstring dictionary")
        return dict(zip(values[::2], values[1::2]))
    raise ValueError(f"unsupported tnetstring tag: {tag!r}")


def mapping_get(mapping: dict[Any, Any], key: str, default: Any = None) -> Any:
    return mapping.get(key, mapping.get(key.encode(), default))


def text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def build_field(number: int, wire_type: int, value: int | bytes) -> bytes:
    result = bytearray(encode_varint((number << 3) | wire_type))
    if wire_type == 0:
        assert isinstance(value, int)
        result.extend(encode_varint(value))
    elif wire_type == 2:
        assert isinstance(value, bytes)
        result.extend(encode_varint(len(value)))
        result.extend(value)
    else:
        raise ValueError("test builder supports only varint and bytes")
    return bytes(result)


def build_message(*parts: bytes) -> bytes:
    return b"".join(parts)
