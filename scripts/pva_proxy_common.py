"""Wire helpers shared by the host PVA proxy and container relay."""

import base64
import json
import socket
import struct

from p4p.wrapper import Type


def _json_value(value):
    if isinstance(value, bytes):
        return {"__pva_proxy_type__": "bytes", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return {"__pva_proxy_type__": "tuple", "items": [_json_value(item) for item in value]}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if hasattr(value, "tolist"):
        return {"__pva_proxy_type__": "array", "items": _json_value(value.tolist())}
    if hasattr(value, "item"):
        return value.item()
    return value


def _python_value(value):
    if isinstance(value, list):
        return [_python_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get("__pva_proxy_type__")
    if kind == "bytes":
        return base64.b64decode(value["data"])
    if kind == "tuple":
        return tuple(_python_value(item) for item in value["items"])
    if kind == "array":
        return _python_value(value["items"])
    return {key: _python_value(item) for key, item in value.items()}


def encode_value(value):
    """Convert a p4p Value into a JSON-safe type/value pair."""
    return {"type": _json_value(value.type().aspy()), "value": _json_value(value.todict())}


def decode_value(payload):
    """Recreate a p4p Value from :func:`encode_value` output."""
    kind, identifier, fields = _python_value(payload["type"])
    if kind != "S":
        raise ValueError("only structure PVA values are supported")
    return Type(fields, id=identifier)(_python_value(payload["value"]))


def send_message(sock, message, lock):
    wire = json.dumps(message, separators=(",", ":")).encode("utf-8")
    with lock:
        sock.sendall(struct.pack("!I", len(wire)) + wire)


def receive_message(sock):
    header = _receive_exact(sock, 4)
    if header is None:
        return None
    size, = struct.unpack("!I", header)
    if size > 16 * 1024 * 1024:
        raise ValueError("proxy message exceeds 16 MiB")
    payload = _receive_exact(sock, size)
    if payload is None:
        return None
    return json.loads(payload.decode("utf-8"))


def _receive_exact(sock, size):
    chunks = bytearray()
    while len(chunks) < size:
        block = sock.recv(size - len(chunks))
        if not block:
            return None
        chunks.extend(block)
    return bytes(chunks)
