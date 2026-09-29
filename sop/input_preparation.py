"""Lossless order input preparation and independently implemented acceptance.

Only representation changes: JSON array positions identify source records, and
``csv_row`` is a one-based CSV *data record* (the header is excluded). Newlines
inside quoted string fields do not create extra records. No business operation
or model is called here; the caller controls whether a verified artifact is used.
"""
import csv
import hashlib
import io
import json
import stat
from pathlib import Path

from .common import SopError, canonical

COLUMNS = ['order_id', 'customer_id', 'revision', 'gross_cents', 'refund_cents']
METHOD_VERSION = 'orders-json-csv/1'
MAPPING_SCHEMA = 'orders-input-mapping/1'


def _artifact_bytes(path):
    """Shared file I/O only; not source interpretation or conversion."""
    if not isinstance(path, Path):
        raise SopError('invalid_artifact', 'Input preparation requires resolved Path artifacts')
    try:
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            raise SopError('invalid_artifact', 'Artifacts must be regular, non-symlink files')
        return path.read_bytes()
    except OSError as exc:
        raise SopError('invalid_artifact', 'Cannot read input preparation artifact') from exc


def _source_records(raw):
    """Producer-only input parsing. The checker must not call this function."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SopError('invalid_json', 'Duplicate source JSON object key')
            result[key] = value
        return result

    def invalid_constant(value):
        raise SopError('invalid_json', 'Source JSON must contain only finite standard values')

    try:
        records = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                             parse_constant=invalid_constant)
    except (ValueError, UnicodeError) as exc:
        raise SopError('invalid_json', 'Source must be a UTF-8 JSON array') from exc
    if type(records) is not list:
        raise SopError('invalid_source', 'Source must be an array of order revision records')
    for index, record in enumerate(records):
        if type(record) is not dict or set(record) != set(COLUMNS):
            raise SopError('invalid_source', f'Source record {index} must contain exactly the five declared fields')
        for key in COLUMNS[:2]:
            if type(record[key]) is not str:
                raise SopError('invalid_identifier', f'{key} in source record {index} must be a string')
            if '\r' in record[key]:
                raise SopError('invalid_source', 'Carriage returns cannot satisfy the fixed CSV consumer contract')
            try:
                record[key].encode('utf-8')
            except UnicodeError as exc:
                raise SopError('invalid_source', 'Source strings must be valid UTF-8 characters') from exc
        if not record['order_id'] or not record['customer_id'].strip():
            raise SopError('invalid_identifier', f'Source record {index} contains an empty identifier')
        for key in COLUMNS[2:]:
            if type(record[key]) is not int:
                raise SopError('invalid_integer', f'{key} in source record {index} must be a JSON integer, not a boolean or float')
    return records


def _prepare_orders(source_file: Path, work_dir: Path, *, column_map=None, chunk_size=None) -> dict:
    """Convert a declared JSON order array to new CSV and provenance artifacts.

    Missing/extra fields, numeric coercions, and unsupported source values fail
    before creating outputs. Existing output files are never replaced.
    """
    source_raw = _artifact_bytes(source_file)
    records = _source_records(source_raw)
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(COLUMNS)
    # Source validation precedes all writes. CSV serialization processes bounded
    # record batches; this is not a streaming JSON parser or a memory bound for
    # the full source snapshot, which still must be independently validated.
    field_map = column_map if column_map is not None else {key: key for key in COLUMNS}
    batch_size = chunk_size if chunk_size is not None else max(1, len(records))
    for start in range(0, len(records), batch_size):
        writer.writerows([record[field_map[key]] for key in COLUMNS]
                         for record in records[start:start + batch_size])
    prepared_raw = buffer.getvalue().encode('utf-8')
    mapping = {
        'schema': MAPPING_SCHEMA,
        'method': {'id': 'orders.prepare', 'version': METHOD_VERSION},
        'source': {'sha256': hashlib.sha256(source_raw).hexdigest(), 'format': 'json-array', 'rows': len(records)},
        'prepared': {'sha256': hashlib.sha256(prepared_raw).hexdigest(), 'format': 'csv', 'rows': len(records)},
        'fields': {key: key for key in COLUMNS},
        'records': [{'source_index': index, 'csv_row': index + 1} for index in range(len(records))],
        'unresolved': [],
    }
    if not isinstance(work_dir, Path):
        raise SopError('invalid_artifact', 'Preparation output directory must be a resolved Path')
    if work_dir.is_symlink() or any(parent.is_symlink() for parent in work_dir.parents):
        raise SopError('invalid_artifact', 'Preparation output directory cannot traverse a symlink')
    outputs = {'prepared': work_dir / 'prepared.csv', 'mapping': work_dir / 'mapping.json'}
    if any(path.exists() or path.is_symlink() for path in outputs.values()):
        raise SopError('output_exists', 'Input preparation outputs must be new artifacts')
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        with outputs['prepared'].open('xb') as handle:
            handle.write(prepared_raw)
        with outputs['mapping'].open('xb') as handle:
            handle.write((canonical(mapping) + '\n').encode('utf-8'))
    except OSError as exc:
        raise SopError('invalid_artifact', 'Cannot create input preparation outputs') from exc
    return outputs


def prepare_orders(source_file: Path, work_dir: Path) -> dict:
    return _prepare_orders(source_file, work_dir)


def prepare_orders_configured(source_file: Path, column_map, chunk_size, work_dir: Path) -> dict:
    """Execute the declared identity conversion with a real CSV batch bound."""
    if type(column_map) is not dict or column_map != {key: key for key in COLUMNS}:
        raise SopError('invalid_value', 'column_map must contain exactly the five identity field mappings')
    if type(chunk_size) is not int or chunk_size < 1:
        raise SopError('invalid_value', 'chunk_size must be a positive integer, not a boolean or float')
    return _prepare_orders(source_file, work_dir, column_map=column_map, chunk_size=chunk_size)


def verify_configured_preparation(source_file: Path, column_map, chunk_size, outputs: dict) -> dict:
    """Check configuration independently, then use the independent source checker."""
    diagnostics = []
    # Deliberately separate from producer validation and parameter proposal code.
    expected_fields = ('order_id', 'customer_id', 'revision', 'gross_cents', 'refund_cents')
    if (type(column_map) is not dict or len(column_map) != len(expected_fields) or
            any(key not in column_map or type(column_map[key]) is not str or column_map[key] != key
                for key in expected_fields)):
        diagnostics.append({'code': 'invalid_value', 'field': 'column_map',
                            'message': 'Acceptance requires the exact five identity field mappings'})
    if type(chunk_size) is not int or chunk_size <= 0:
        diagnostics.append({'code': 'invalid_value', 'field': 'chunk_size',
                            'message': 'Acceptance requires a positive integer chunk size'})
    if diagnostics:
        return {'passed': False, 'diagnostics': diagnostics, 'metrics': {}}
    result = verify_preparation(source_file, outputs)
    result['metrics']['chunk_size'] = chunk_size
    return result


def verify_preparation(source_file: Path, outputs: dict) -> dict:
    """Check source, candidate, and mapping without calling producer helpers.

    Recomputes source types and every cell in array order. Source/target content
    identities and all mapping fields are also checked; producer row counts or
    totals never stand in for the content comparison.
    """
    diagnostics, metrics = [], {}
    try:
        if type(outputs) is not dict or set(outputs) != {'prepared', 'mapping'}:
            raise SopError('output_contract', 'Preparation requires exactly prepared and mapping artifacts')
        source_raw = _artifact_bytes(source_file)
        prepared_raw = _artifact_bytes(outputs['prepared'])
        mapping_raw = _artifact_bytes(outputs['mapping'])

        # Independent parser and validation, intentionally not _source_records.
        def checked_object(pairs):
            keys = [key for key, _ in pairs]
            if len(set(keys)) != len(keys):
                raise SopError('invalid_json', 'Duplicate key in source or mapping JSON')
            return dict(pairs)

        def reject_constant(value):
            raise SopError('invalid_json', 'Nonstandard JSON constant in source or mapping')

        source = json.loads(source_raw.decode('utf-8'), object_pairs_hook=checked_object,
                            parse_constant=reject_constant)
        mapping = json.loads(mapping_raw.decode('utf-8'), object_pairs_hook=checked_object,
                             parse_constant=reject_constant)
        if type(source) is not list:
            raise SopError('invalid_source', 'Acceptance requires a JSON record array')
        metrics['source_rows'] = len(source)
        source_cells = []
        for index, record in enumerate(source):
            if type(record) is not dict or sorted(record) != sorted(COLUMNS):
                raise SopError('invalid_source', f'Source record {index} has a different field set')
            order, customer = record['order_id'], record['customer_id']
            if type(order) is not str or type(customer) is not str or not order or not customer.strip():
                raise SopError('invalid_identifier', f'Source record {index} has an invalid identifier')
            if '\r' in order or '\r' in customer:
                raise SopError('invalid_source', 'Source string cannot satisfy the LF CSV contract')
            order.encode('utf-8')
            customer.encode('utf-8')
            integers = [record['revision'], record['gross_cents'], record['refund_cents']]
            if any(type(value) is not int for value in integers):
                raise SopError('invalid_integer', f'Source record {index} requires integer revision and cents')
            source_cells.append([order, customer, *(str(value) for value in integers)])

        text = prepared_raw.decode('utf-8')
        if '\r' in text or not text.endswith('\n'):
            raise SopError('invalid_csv', 'Prepared CSV requires LF line endings and a final LF')
        reader = csv.reader(io.StringIO(text, newline=''), strict=True)
        if next(reader, None) != COLUMNS:
            raise SopError('invalid_csv', 'Prepared CSV must have the exact declared ordered columns')
        rows = list(reader)
        metrics['prepared_rows'] = len(rows)
        if any(len(row) != len(COLUMNS) for row in rows):
            raise SopError('invalid_csv', 'Prepared CSV has an incorrect record width')
        if len(rows) != len(source_cells):
            diagnostics.append({'code': 'row_count_mismatch', 'message': 'Prepared record count differs from original JSON array'})
        for index, (expected, actual) in enumerate(zip(source_cells, rows)):
            for column, before, after in zip(COLUMNS, expected, actual):
                if before != after:
                    diagnostics.append({'code': 'prepared_value_mismatch',
                                        'message': f'Source record {index}, CSV data row {index + 1}, field {column} differs'})
        expected_mapping = {
            'schema': MAPPING_SCHEMA,
            'method': {'id': 'orders.prepare', 'version': METHOD_VERSION},
            'source': {'sha256': hashlib.sha256(source_raw).hexdigest(), 'format': 'json-array', 'rows': len(source)},
            'prepared': {'sha256': hashlib.sha256(prepared_raw).hexdigest(), 'format': 'csv', 'rows': len(rows)},
            'fields': {column: column for column in COLUMNS},
            'records': [{'source_index': index, 'csv_row': index + 1} for index in range(len(source))],
            'unresolved': [],
        }
        # Canonical JSON retains bool/int distinctions unlike Python dict equality.
        if canonical(mapping) != canonical(expected_mapping):
            diagnostics.append({'code': 'mapping_mismatch', 'message': 'Source/target identity, method, fields, or ordered record mapping is invalid'})
        metrics['compared_fields'] = min(len(rows), len(source)) * len(COLUMNS)
        metrics['source_sha256'] = hashlib.sha256(source_raw).hexdigest()
        metrics['prepared_sha256'] = hashlib.sha256(prepared_raw).hexdigest()
    except SopError as exc:
        diagnostics.append({'code': exc.code, 'message': str(exc)})
    except (OSError, TypeError, ValueError, UnicodeError, csv.Error) as exc:
        diagnostics.append({'code': 'invalid_artifact', 'message': 'Cannot independently parse valid preparation artifacts'})
    return {'passed': not diagnostics, 'diagnostics': diagnostics, 'metrics': metrics}
