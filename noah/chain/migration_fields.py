"""Exact-field validation for immutable migration records."""

import hashlib
import json


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name}: integer >= {minimum} required")
    return value


def fields(obj, names, name):
    if not isinstance(obj, dict) or set(obj) != set(names.split()):
        raise ValueError(f"{name}: exact fields required: {names}")


def text(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name}: nonempty string required")
    return value


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
