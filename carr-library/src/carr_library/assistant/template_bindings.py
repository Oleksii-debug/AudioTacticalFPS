"""Whole-value typed bindings adapted from reviewed donor 5891de9e.

No interpolation, expressions, tools or execution permissions are evaluated.
This module is shared by the one existing catalogue, native review and CLI.
"""
import json
import math
import re
from .models import canonical, text

MAX_TEMPLATE_BYTES = 256 * 1024
MAX_ITEMS = 128
_PARAMETER = re.compile(r'[a-z][a-z0-9_]{0,47}\Z')
PARAMETER_KINDS = ('text', 'integer', 'number', 'boolean', 'path', 'voice')


def _label(value):
    text(value, 'parameter label', maximum=160)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('A parameter label must be a single line.')
    return value


def _json_value(value):
    """Reject deep/opaque inputs before serialization; never consume iterators."""
    remaining = [12000]
    def visit(item, depth):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 20:
            raise ValueError('Template data exceeds its bounded structure allowance.')
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            if abs(item) > 2**53:
                raise ValueError('A template integer exceeds its exact JSON range.')
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError('Template numbers must be finite.')
            return
        if type(item) is str:
            text(item, 'template text', maximum=MAX_TEMPLATE_BYTES, empty=True)
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or len(key) > 128:
                    raise ValueError('Template fields need bounded text names.')
                visit(child, depth + 1)
            return
        raise ValueError('Templates contain plain JSON data only.')
    visit(value, 0)
    encoded = canonical(value)
    if len(encoded.encode('utf-8')) > MAX_TEMPLATE_BYTES:
        raise ValueError('The template exceeds its 256 KiB allowance.')
    return json.loads(encoded)


def _parameter_value(kind, value):
    if kind in ('text', 'path', 'voice'):
        text(value, 'parameter value', maximum=65536 if kind == 'text' else 8192,
             empty=kind == 'text')
    elif kind == 'integer':
        if type(value) is not int or abs(value) > 2**31 - 1:
            raise ValueError('An integer parameter must be within the signed 32-bit range.')
    elif kind == 'number':
        if type(value) not in (int, float) or abs(value) > 10**12 or not math.isfinite(value):
            raise ValueError('A numeric parameter must be finite and bounded.')
    elif kind == 'boolean':
        if type(value) is not bool:
            raise ValueError('A boolean parameter must be true or false.')
    else:
        raise ValueError('Unsupported template parameter type.')
    return value


def _parameters(value):
    if type(value) is not dict or len(value) > 64:
        raise ValueError('Use at most 64 named typed parameters.')
    for name, spec in value.items():
        if (type(name) is not str or not _PARAMETER.fullmatch(name) or type(spec) is not dict
                or set(spec) != {'kind', 'label', 'example'}):
            raise ValueError('Each parameter needs a stable name, kind, label and example.')
        _label(spec['label'])
        _parameter_value(spec['kind'], spec['example'])
    return value


def _replace(value, parameters, values, used):
    if type(value) is dict:
        if '$parameter' in value:
            if (set(value) != {'$parameter'} or type(value['$parameter']) is not str
                    or value['$parameter'] not in parameters):
                raise ValueError('A parameter reference must name exactly one declared parameter.')
            name = value['$parameter']
            used.add(name)
            return _parameter_value(parameters[name]['kind'], values[name])
        return {key: _replace(item, parameters, values, used) for key, item in value.items()}
    if type(value) is list:
        return [_replace(item, parameters, values, used) for item in value]
    return value



def explicit_rows(parameters, values, items=None):
    base = _json_value({} if values is None else values)
    rows = [{}] if items is None else _json_value(items)
    if type(base) is not dict or set(base) - set(parameters):
        raise ValueError('Bindings must name only declared parameters.')
    if (type(rows) is not list or not 1 <= len(rows) <= MAX_ITEMS
            or any(type(row) is not dict or set(row) - set(parameters) for row in rows)):
        raise ValueError('Use from 1 to 128 explicit per-item parameter objects.')
    resolved = []
    for row in rows:
        selected = {**base, **row}
        if set(selected) != set(parameters):
            raise ValueError('Supply every declared parameter explicitly, without extra fields.')
        resolved.append({name: _parameter_value(spec['kind'], selected[name])
                         for name, spec in parameters.items()})
    return resolved
