"""Trusted parameter forms and atomic, data-only candidate validation.

This module does not execute capabilities, inspect source data, or alter runtime
state. A passed partial patch may still have missing fields: the runtime must
require both ``passed`` and an empty ``missing`` list before consuming bindings.
Host policy and editable-field scope are inputs from the trusted runtime, never
fields that a candidate can grant to itself.
"""
from copy import deepcopy
import hashlib
from pathlib import Path


_TASK = 'orders.prepare_configured'
_COLUMNS = ('order_id', 'customer_id', 'revision', 'gross_cents', 'refund_cents')
_FIELDS = {'column_map': {'type': 'object', 'required': True},
           'chunk_size': {'type': 'integer', 'required': True}}


def forms():
    """Return detached trusted schemas pinned to this validator implementation."""
    version = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return {_TASK: {'fields': deepcopy(_FIELDS), 'version': version}}


def validate_form(task, current, patch, policy, allowed_fields=None):
    """Validate an entire patch, retaining the old bindings on any rejection.

    ``current`` may also contain runtime-owned inputs such as ``source_file``;
    those are preserved, but never become editable. Missing current values may
    be absent or ``None``. Explicitly patching a field to ``None`` is invalid.
    Mapping semantics are intentionally limited to the declared identity map.
    Input availability/units, consumed revisions, authorisation and scheduling
    remain separate runtime checks; this validator does not infer them.
    """
    current_ok = type(current) is dict and all(type(key) is str for key in current)
    original = deepcopy(current) if current_ok else {}
    result = {'passed': False, 'diagnostics': [], 'bindings': original,
              'missing': [], 'invalid_fields': []}

    def reject(code, field, message):
        result['diagnostics'].append({'code': code, 'field': field, 'message': message})
        if field in _FIELDS or (type(field) is str and code in ('unknown_field', 'field_not_editable')):
            if field not in result['invalid_fields']:
                result['invalid_fields'].append(field)

    def missing(bindings):
        return [name for name in _FIELDS if name not in bindings or bindings[name] is None]

    result['missing'] = missing(original)
    if task != _TASK:
        reject('unknown_form', None, 'No trusted parameter form is registered for this task')
        return result
    if not current_ok:
        reject('invalid_current', None, 'Current bindings must be an object with string field names')
        return result
    if type(patch) is not dict or any(type(key) is not str for key in patch):
        reject('invalid_patch', None, 'Parameter patch must be an object with string field names')
        return result
    if (type(policy) is not dict or set(policy) != {'max_chunk_size'} or
            type(policy['max_chunk_size']) is not int or policy['max_chunk_size'] < 1):
        reject('invalid_policy', None, 'Host policy must declare one positive integer max_chunk_size')
        return result
    if allowed_fields is None:
        allowed = set(_FIELDS)
    elif (not isinstance(allowed_fields, (list, tuple, set, frozenset)) or
            any(type(name) is not str or name not in _FIELDS for name in allowed_fields) or
            len(set(allowed_fields)) != len(allowed_fields)):
        reject('invalid_allowed_fields', None, 'Editable fields may only narrow the declared form')
        return result
    else:
        allowed = set(allowed_fields)
    for name in sorted(patch):
        if name not in _FIELDS:
            reject('unknown_field', name, 'Only declared form parameters can be proposed')
        elif name not in allowed:
            reject('field_not_editable', name, 'This field is not editable at the current boundary')
    candidate = deepcopy(original)
    for name in _FIELDS:
        if name in patch:
            candidate[name] = deepcopy(patch[name])
    for name in _FIELDS:
        if name not in candidate or (candidate[name] is None and name not in patch):
            continue
        value = candidate[name]
        if name == 'chunk_size':
            if type(value) is not int or not 1 <= value <= policy['max_chunk_size']:
                reject('invalid_value', name, 'chunk_size must be an integer from 1 through the host maximum')
        elif (type(value) is not dict or set(value) != set(_COLUMNS) or
                any(type(value[column]) is not str or value[column] != column for column in _COLUMNS)):
            reject('invalid_value', name, 'column_map must map all five declared targets to their identical source field names')
    if result['diagnostics']:
        return result
    result['passed'] = True
    result['bindings'] = candidate
    result['missing'] = missing(candidate)
    return result
