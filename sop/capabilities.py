"""Pinned local capabilities and independent source-based acceptance checks.

No definition can supply code. The registry owns names, algorithms and outputs.
"""
import csv
import io
import re
import stat
from collections import defaultdict
from pathlib import Path

from .common import SopError, digest, file_digest, read_json, write_json
from .contracts import task_contracts
from .input_preparation import (prepare_orders, verify_preparation,
                                prepare_orders_configured, verify_configured_preparation)

SOURCE_COLUMNS = ['order_id', 'customer_id', 'revision', 'gross_cents', 'refund_cents']
CLEAN_COLUMNS = SOURCE_COLUMNS + ['net_cents']
SUMMARY_COLUMNS = ['customer_id', 'order_count', 'gross_cents', 'refund_cents', 'net_cents']
TASK_OUTPUTS = {name: contract['outputs'] for name, contract in task_contracts().items()}
INTEGER = re.compile(r'[+-]?[0-9]+\Z')


def _artifact(path):
    if not isinstance(path, Path):
        raise SopError('invalid_artifact', 'Capabilities require resolved Path artifacts')
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise SopError('invalid_artifact', 'Only regular, non-symlink artifacts are accepted')
    return path


def _table(path, columns):
    try:
        raw = _artifact(path).read_bytes()
        text = raw.decode('utf-8')
        if '\r' in text or not text.endswith('\n'):
            raise SopError('invalid_csv', 'CSV must be UTF-8 with LF line endings and final LF')
        rows = list(csv.reader(io.StringIO(text, newline=''), strict=True))
        if not rows or rows[0] != columns:
            raise SopError('invalid_csv', 'CSV columns must match the declared ordered schema')
        if any(len(row) != len(columns) for row in rows[1:]):
            raise SopError('invalid_csv', 'Every record must have exactly the declared number of fields')
        return [dict(zip(columns, row)) for row in rows[1:]]
    except SopError:
        raise
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        raise SopError('invalid_csv', 'Cannot read a valid CSV artifact') from exc


def _orders(path, *, cleaned=False):
    records = _table(path, CLEAN_COLUMNS if cleaned else SOURCE_COLUMNS)
    for index, row in enumerate(records, 1):
        if not row['order_id'] or not row['customer_id'].strip():
            raise SopError('invalid_identifier', f'Empty identifier in data row {index}')
        row['customer_id'] = row['customer_id'].strip()
        for key in ['revision', 'gross_cents', 'refund_cents'] + (['net_cents'] if cleaned else []):
            if not INTEGER.fullmatch(row[key]):
                raise SopError('invalid_integer', f'{key} in data row {index} must be an integer')
            row[key] = int(row[key])
        if cleaned and row['net_cents'] != row['gross_cents'] - row['refund_cents']:
            raise SopError('invalid_net', f'Incorrect net amount in data row {index}')
    return records


def _select(records, rule):
    if rule not in ('highest_revision', 'first'):
        raise SopError('invalid_rule', 'A declared deduplication rule is required')
    grouped = defaultdict(list)
    for row in records:
        grouped[row['order_id']].append(row)
    chosen = []
    for order_id in sorted(grouped):
        group = grouped[order_id]
        if len({row['customer_id'] for row in group}) != 1:
            raise SopError('customer_conflict', f'Customer differs across revisions of {order_id}')
        candidates = [group[0]] if rule == 'first' else [row for row in group if row['revision'] == max(r['revision'] for r in group)]
        if len({(row['customer_id'], row['gross_cents'], row['refund_cents']) for row in candidates}) != 1:
            raise SopError('conflicting_highest_revision', f'Conflicting highest revision for {order_id}')
        selected = dict(candidates[0])
        selected['net_cents'] = selected['gross_cents'] - selected['refund_cents']
        chosen.append(selected)
    return chosen


def _write_table(path, columns, records):
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator='\n')
        writer.writeheader()
        writer.writerows(records)
    return path


def _reference(source_file, rule):
    """Separate checker implementation: rereads raw source; never invokes producer helpers.

    Keeping this intentionally independent catches selecting the last row, bad group
    totals, dropped source records and producer helper regressions.
    """
    if rule not in ('highest_revision', 'first'):
        raise SopError('invalid_rule', 'Acceptance requires the accepted deduplication rule')
    try:
        raw = _artifact(source_file).read_bytes()
        if b'\r' in raw or not raw.endswith(b'\n'):
            raise SopError('invalid_csv', 'Source must use LF and final LF')
        reader = csv.reader(io.StringIO(raw.decode('utf-8'), newline=''), strict=True)
        if next(reader, None) != SOURCE_COLUMNS:
            raise SopError('invalid_csv', 'Source schema differs from accepted schema')
        owners, winners, revisions = {}, {}, {}
        conflicts = set()
        count = 0
        for values in reader:
            count += 1
            if len(values) != 5:
                raise SopError('invalid_csv', 'Incorrect source record width')
            order, customer, revision, gross, refund = values
            customer = customer.strip()
            if not order or not customer:
                raise SopError('invalid_identifier', 'Source identifiers must be nonempty')
            numeric = []
            for value in (revision, gross, refund):
                digits = value[1:] if value[:1] in ('+', '-') else value
                if not digits or any(character < '0' or character > '9' for character in digits):
                    raise SopError('invalid_integer', 'Source revision and amounts must be integers')
                numeric.append(int(value))
            revision, gross, refund = numeric
            if order in owners and owners[order] != customer:
                raise SopError('customer_conflict', f'Customer differs across revisions of {order}')
            owners[order] = customer
            candidate = [order, customer, str(revision), str(gross), str(refund), str(gross-refund)]
            if order not in winners or (rule == 'highest_revision' and revision > revisions[order]):
                winners[order], revisions[order] = candidate, revision
                conflicts.discard(order)
            elif rule == 'highest_revision' and revision == revisions[order] and candidate != winners[order]:
                conflicts.add(order)
        if conflicts:
            raise SopError('conflicting_highest_revision', 'Source highest revision has conflicting amounts')
    except SopError:
        raise
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        raise SopError('invalid_csv', 'Cannot independently parse source') from exc
    cleaned = [winners[key] for key in sorted(winners)]
    sums = {}
    for _, customer, _, gross, refund, net in cleaned:
        if customer not in sums:
            sums[customer] = [0, 0, 0, 0]
        for index, value in enumerate((1, int(gross), int(refund), int(net))):
            sums[customer][index] += value
    summary = [[customer, *map(str, sums[customer])] for customer in sorted(sums)]
    quality = dict(input_rows=count, retained_orders=len(cleaned), removed_rows=count-len(cleaned),
                   total_gross_cents=sum(int(row[3]) for row in cleaned),
                   total_refund_cents=sum(int(row[4]) for row in cleaned),
                   total_net_cents=sum(int(row[5]) for row in cleaned))
    return cleaned, summary, quality


class Registry:
    def _version(self):
        return digest({'capabilities': file_digest(Path(__file__)),
                       'common': file_digest(Path(__file__).with_name('common.py')),
                       'contracts':file_digest(Path(__file__).with_name('contracts.py')),
                       'preparation':file_digest(Path(__file__).with_name('input_preparation.py')),
                       'parameter_forms': file_digest(Path(__file__).with_name('parameter_forms.py')),
                       'protocol': 'registry/3'})

    def catalog(self):
        artifact = {'type': 'artifact', 'required': True}
        rule = {'type': 'string', 'enum': ['highest_revision', 'first'], 'required': True}
        version = self._version()
        catalog = {name: {'inputs': inputs, 'outputs': outputs, 'version': version, 'effects': 'local_artifact'}
                for name, inputs, outputs in [
                    ('orders.profile', {'source_file': dict(artifact)}, ['profile']),
                    ('orders.normalize', {'source_file': dict(artifact)}, ['normalized']),
                    ('orders.deduplicate', {'normalized': dict(artifact), 'rule': rule}, ['cleaned']),
                    ('orders.summarize', {'cleaned': dict(artifact), 'source_file': dict(artifact)}, ['summary', 'quality'])]}
        catalog['orders.prepare'] = {'inputs': {'source_file': dict(artifact,role='source_json')},
            'outputs':['prepared','mapping'], 'output_roles':{'prepared':'source_file','mapping':'mapping'},
            'version':version,'effects':'local_artifact','result_check':'orders.prepare'}
        configured = self.task_contracts()['orders.prepare_configured']
        catalog['orders.prepare_configured'] = {
            'inputs': configured['inputs'], 'outputs': configured['outputs'],
            'output_roles': configured['output_roles'], 'version': version,
            'effects': 'local_artifact', 'result_check': 'orders.prepare_configured'}
        return catalog

    def parameter_forms(self):
        from .parameter_forms import forms
        return forms()

    def task_contracts(self):
        return task_contracts()

    def checkers(self):
        return {name: self._version() for name in TASK_OUTPUTS}

    def versions(self):
        return {'capabilities': {key: value['version'] for key, value in self.catalog().items()},
                'checkers': self.checkers()}

    def execute(self, capability, inputs, work_dir):
        catalog = self.catalog()
        if capability not in catalog:
            raise SopError('capability_missing', f'Unknown capability: {capability}')
        if set(inputs) != set(catalog[capability]['inputs']):
            raise SopError('invalid_inputs', 'Capability arguments must match the declared schema')
        if capability == 'orders.prepare':
            return prepare_orders(inputs['source_file'], Path(work_dir))
        if capability == 'orders.prepare_configured':
            return prepare_orders_configured(inputs['source_file'], inputs['column_map'],
                                             inputs['chunk_size'], Path(work_dir))
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        if work_dir.is_symlink():
            raise SopError('invalid_artifact', 'Output directory cannot be a symlink')
        for name in catalog[capability]['outputs']:
            target = work_dir / (name + ('.json' if name in ('quality', 'profile') else '.csv'))
            if target.exists() or target.is_symlink():
                raise SopError('output_exists', 'Capability output must be a new artifact')
        if capability == 'orders.profile':
            rows = _orders(inputs['source_file'])
            path = work_dir / 'profile.json'
            counts = defaultdict(int)
            for row in rows:
                counts[row['order_id']] += 1
            write_json(path, {'input_rows': len(rows), 'unique_orders': len(counts),
                              'duplicate_orders': sum(value > 1 for value in counts.values()),
                              'columns': SOURCE_COLUMNS})
            return {'profile': path}
        if capability == 'orders.normalize':
            rows = _orders(inputs['source_file'])
            return {'normalized': _write_table(work_dir / 'normalized.csv', SOURCE_COLUMNS, rows)}
        if capability == 'orders.deduplicate':
            rows = _select(_orders(inputs['normalized']), inputs['rule'])
            return {'cleaned': _write_table(work_dir / 'cleaned.csv', CLEAN_COLUMNS, rows)}
        rows = _orders(inputs['cleaned'], cleaned=True)
        if len({row['order_id'] for row in rows}) != len(rows):
            raise SopError('duplicate_output', 'Cleaned records must contain each order once')
        source_rows = _orders(inputs['source_file'])
        groups = defaultdict(list)
        for row in rows:
            groups[row['customer_id']].append(row)
        summary = [{'customer_id': customer, 'order_count': len(group),
                    **{key: sum(row[key] for row in group) for key in ('gross_cents', 'refund_cents', 'net_cents')}}
                   for customer, group in sorted(groups.items())]
        quality = {'input_rows': len(source_rows), 'retained_orders': len(rows),
                   'removed_rows': len(source_rows)-len(rows),
                   **{'total_'+key: sum(row[key] for row in rows) for key in ('gross_cents', 'refund_cents', 'net_cents')}}
        summary_path = _write_table(work_dir / 'summary.csv', SUMMARY_COLUMNS, summary)
        quality_path = work_dir / 'quality.json'
        write_json(quality_path, quality)
        return {'summary': summary_path, 'quality': quality_path}

    def verify(self, task, inputs, outputs):
        if task not in TASK_OUTPUTS:
            raise SopError('checker_missing', f'Unknown acceptance checker: {task}')
        if task == 'orders.prepare':
            return verify_preparation(inputs['source_file'], outputs)
        if task == 'orders.prepare_configured':
            return verify_configured_preparation(inputs['source_file'], inputs.get('column_map'),
                                                inputs.get('chunk_size'), outputs)
        if task == 'orders.from_json':
            expected=set(TASK_OUTPUTS[task])
            if set(outputs)!=expected:
                return {'passed':False,'diagnostics':[{'code':'output_contract','message':'missing original-source preparation or report outputs'}],'metrics':{}}
            prepared = verify_preparation(inputs['source_file'], {k:outputs[k] for k in ('prepared','mapping')})
            if not prepared['passed']:
                return prepared
            return self.verify('orders.report',{'source_file':outputs['prepared'],'rule':inputs.get('rule')},
                               {k:outputs[k] for k in ('cleaned','summary','quality')})
        diagnostics = []
        metrics = {}
        try:
            if set(outputs) != set(TASK_OUTPUTS[task]):
                raise SopError('output_contract', 'Output names do not match the task contract')
            expected_clean, expected_summary, expected_quality = _reference(inputs['source_file'], inputs.get('rule'))
            actual_clean = _table(outputs['cleaned'], CLEAN_COLUMNS)
            if [list(row.values()) for row in actual_clean] != expected_clean:
                diagnostics.append({'code': 'cleaned_mismatch', 'message': 'Cleaned records differ from independently recomputed source rows'})
            if task == 'orders.report':
                actual_summary = _table(outputs['summary'], SUMMARY_COLUMNS)
                if [list(row.values()) for row in actual_summary] != expected_summary:
                    diagnostics.append({'code': 'summary_mismatch', 'message': 'Customer summary differs from independent source computation'})
                actual_quality = read_json(_artifact(outputs['quality']))
                if (not isinstance(actual_quality, dict) or set(actual_quality) != set(expected_quality)
                        or any(type(value) is not int for value in actual_quality.values())
                        or actual_quality != expected_quality):
                    diagnostics.append({'code': 'quality_mismatch', 'message': 'Quality counts or integer totals differ from independent source computation'})
            metrics = expected_quality
        except SopError as exc:
            diagnostics.append({'code': exc.code, 'message': str(exc)})
        except (KeyError, OSError, TypeError) as exc:
            diagnostics.append({'code': 'invalid_artifact', 'message': 'Missing or invalid source/output artifact'})
        return {'passed': not diagnostics, 'diagnostics': diagnostics, 'metrics': metrics}
