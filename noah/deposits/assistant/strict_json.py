"""Read JSON without duplicate keys or non-finite numeric values."""
import json
from pathlib import Path


def read_json(path):
    return read_json_text(Path(path).read_text())


def read_json_text(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON field: ' + key)
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('non-finite JSON number: ' + value)
    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)

