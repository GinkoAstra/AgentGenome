"""Independent acceptance for JSON source -> CSV handoff, without business calls."""
import copy
import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sop.common import SopError
from sop.input_preparation import prepare_orders, verify_preparation

FIXTURES = Path(__file__).resolve().parents[2] / 'tests/fixtures/input-handoff'
COLUMNS = ['order_id', 'customer_id', 'revision', 'gross_cents', 'refund_cents']


class InputPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sop-input-preparation-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = FIXTURES / 'source.json'

    def source_file(self, records):
        path = self.root / 'source.json'
        path.write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
        return path

    def candidate(self, source, csv_bytes):
        """Fixture driver makes provenance from original input, never producer."""
        records = json.loads(source.read_text(encoding='utf-8'))
        rows = list(csv.reader(io.StringIO(csv_bytes.decode('utf-8'), newline='')))[1:]
        output = self.root / 'candidate.csv'
        output.write_bytes(csv_bytes)
        mapping = self.root / 'mapping.json'
        mapping.write_text(json.dumps({
            'schema': 'orders-input-mapping/1',
            'method': {'id': 'orders.prepare', 'version': 'orders-json-csv/1'},
            'source': {'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'format': 'json-array', 'rows': len(records)},
            'prepared': {'sha256': hashlib.sha256(csv_bytes).hexdigest(), 'format': 'csv', 'rows': len(rows)},
            'fields': {key: key for key in COLUMNS},
            'records': [{'source_index': index, 'csv_row': index + 1} for index in range(len(records))],
            'unresolved': [],
        }), encoding='utf-8')
        return {'prepared': output, 'mapping': mapping}

    def test_producer_matches_seven_original_records_and_keeps_source_unchanged(self):
        original = self.source.read_bytes()
        outputs = prepare_orders(self.source, self.root / 'prepared')
        self.assertEqual(outputs['prepared'].read_bytes(), (FIXTURES / 'expected.csv').read_bytes())
        self.assertEqual(self.source.read_bytes(), original)
        mapping = json.loads(outputs['mapping'].read_text())
        self.assertEqual(mapping['records'], [{'source_index': i, 'csv_row': i + 1} for i in range(7)])
        result = verify_preparation(self.source, outputs)
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['metrics']['source_rows'], 7)
        self.assertEqual(result['metrics']['prepared_rows'], 7)
        self.assertEqual(result['metrics']['compared_fields'], 35)

    def test_three_authoritative_candidates_are_checked_independently(self):
        for filename, accepted, code in (
            ('expected.csv', True, None),
            ('rejected-dedup.csv', False, 'row_count_mismatch'),
            ('rejected-values.csv', False, 'prepared_value_mismatch'),
        ):
            with self.subTest(candidate=filename):
                outputs = self.candidate(self.source, (FIXTURES / filename).read_bytes())
                with patch('sop.input_preparation.prepare_orders', side_effect=AssertionError('producer called')), \
                     patch('sop.input_preparation._source_records', side_effect=AssertionError('producer parser called')):
                    result = verify_preparation(self.source, outputs)
                self.assertEqual(result['passed'], accepted, result)
                if code:
                    self.assertIn(code, [item['code'] for item in result['diagnostics']])
        # The two altered amounts cancel. Acceptance must still locate both fields.
        value_errors = [item for item in result['diagnostics'] if item['code'] == 'prepared_value_mismatch']
        self.assertEqual(len(value_errors), 2)
        self.assertTrue(all('gross_cents' in item['message'] for item in value_errors))

    def test_reuse_accepts_different_row_counts_and_preserves_strings_and_integer_values(self):
        records = [
            {'order_id': ' O,1 ', 'customer_id': ' 客户 "a"\n ', 'revision': -1, 'gross_cents': 10**80 + 17, 'refund_cents': -3},
            {'order_id': ' O,1 ', 'customer_id': ' 客户 "a"\n ', 'revision': 2, 'gross_cents': 0, 'refund_cents': 17},
        ]
        source = self.source_file(records)
        outputs = prepare_orders(source, self.root / 'different')
        rows = list(csv.DictReader(io.StringIO(outputs['prepared'].read_text(), newline='')))
        self.assertEqual(len(rows), 2)
        for actual, original in zip(rows, records):
            self.assertEqual(actual, {key: str(value) for key, value in original.items()})
        result = verify_preparation(source, outputs)
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['metrics']['source_rows'], 2)

    def test_empty_array_preserves_zero_records_without_fabrication(self):
        source = self.source_file([])
        outputs = prepare_orders(source, self.root / 'empty')
        self.assertEqual(outputs['prepared'].read_text(), ','.join(COLUMNS) + '\n')
        self.assertTrue(verify_preparation(source, outputs)['passed'])
        self.assertEqual(json.loads(outputs['mapping'].read_text())['records'], [])

    def test_invalid_source_types_are_rejected_by_producer_and_independent_checker(self):
        valid = json.loads(self.source.read_text())[0]
        cases = []
        for field in COLUMNS[2:]:
            for value in (True, False, 1.0, '1', None):
                cases.append([{**valid, field: value}])
        for field in COLUMNS[:2]:
            for value in (True, 1, None, ''):
                cases.append([{**valid, field: value}])
        cases.extend([
            [{key: value for key, value in valid.items() if key != 'revision'}],
            [{**valid, 'net_cents': 10000}],
            [{**valid, 'customer_id': '   '}],
            [{**valid, 'order_id': 'O\r1'}],
            {'records': [valid]}, [None],
        ])
        for index, records in enumerate(cases):
            with self.subTest(source=records):
                source = self.source_file(records)
                outputs = self.candidate(source, (FIXTURES / 'expected.csv').read_bytes())
                with self.assertRaises(SopError):
                    prepare_orders(source, self.root / f'bad-{index}')
                self.assertFalse((self.root / f'bad-{index}').exists())
                self.assertFalse(verify_preparation(source, outputs)['passed'])

    def test_duplicate_keys_and_nonstandard_json_are_rejected(self):
        outputs = self.candidate(self.source, (FIXTURES / 'expected.csv').read_bytes())
        for index, raw in enumerate((
            b'[{"order_id":"A","order_id":"B","customer_id":"C","revision":1,"gross_cents":0,"refund_cents":0}]',
            b'[{"order_id":"A","customer_id":"C","revision":1,"gross_cents":NaN,"refund_cents":0}]',
            b'\xff', b'[',
        )):
            with self.subTest(raw=raw):
                source = self.root / 'invalid.json'
                source.write_bytes(raw)
                with self.assertRaises(SopError):
                    prepare_orders(source, self.root / f'invalid-{index}')
                self.assertFalse(verify_preparation(source, outputs)['passed'])

    def test_reordered_trimmed_and_forged_numeric_candidates_are_rejected(self):
        original = (FIXTURES / 'expected.csv').read_text().splitlines(keepends=True)
        variants = [
            ''.join([original[0], original[2], original[1], *original[3:]]),
            ''.join(original).replace('O1001, C001 ,1', 'O1001,C001,1', 1),
            ''.join(original).replace('O1001, C001 ,1', 'O9999, C001 ,1', 1),
            ''.join(original).replace('1,10000,0', '1,10000.0,0', 1),
            ''.join(original).replace('1,10000,0', '1,010000,0', 1),
        ]
        for candidate in variants:
            with self.subTest(candidate=candidate.splitlines()[1]):
                result = verify_preparation(self.source, self.candidate(self.source, candidate.encode()))
                self.assertFalse(result['passed'])
                self.assertIn('prepared_value_mismatch', [item['code'] for item in result['diagnostics']])

    def test_mapping_is_a_checked_claim_not_a_trusted_conversion_record(self):
        outputs = self.candidate(self.source, (FIXTURES / 'expected.csv').read_bytes())
        original = json.loads(outputs['mapping'].read_text())
        modifications = [
            lambda m: m['records'].pop(),
            lambda m: m['records'].reverse(),
            lambda m: m['records'][0].update(source_index=-1),
            lambda m: m['records'][0].update(source_index=False),
            lambda m: m['records'][0].update(source_index=0.0),
            lambda m: m['records'][0].update(csv_row=True),
            lambda m: m['records'][1].update(source_index=0),
            lambda m: m['records'][0].update(csv_row=0),
            lambda m: m['fields'].update(gross_cents='refund_cents'),
            lambda m: m['source'].update(rows=True),
            lambda m: m['source'].update(sha256='0' * 64),
            lambda m: m['prepared'].update(sha256='0' * 64),
            lambda m: m['method'].update(version='new-unchecked-method'),
            lambda m: m['unresolved'].append('unknown amount units'),
            lambda m: m.update(passed=True),
        ]
        for index, modify in enumerate(modifications):
            with self.subTest(modification=index):
                altered = copy.deepcopy(original)
                modify(altered)
                outputs['mapping'].write_text(json.dumps(altered))
                result = verify_preparation(self.source, outputs)
                self.assertFalse(result['passed'])
                self.assertIn('mapping_mismatch', [item['code'] for item in result['diagnostics']])

    def test_duplicate_mapping_json_keys_are_rejected(self):
        outputs = self.candidate(self.source, (FIXTURES / 'expected.csv').read_bytes())
        raw = outputs['mapping'].read_text()
        outputs['mapping'].write_text(raw.replace('"unresolved": []', '"unresolved": ["missing"], "unresolved": []'))
        result = verify_preparation(self.source, outputs)
        self.assertFalse(result['passed'])
        self.assertEqual(result['diagnostics'][0]['code'], 'invalid_json')

    def test_changed_source_or_candidate_invalidates_previous_mapping(self):
        source = self.source_file(json.loads(self.source.read_text()))
        outputs = prepare_orders(source, self.root / 'prepared')
        source.write_text(source.read_text() + '\n')
        result = verify_preparation(source, outputs)
        self.assertFalse(result['passed'])
        self.assertEqual(result['diagnostics'][0]['code'], 'mapping_mismatch')
        source.write_text(source.read_text()[:-1])
        outputs['prepared'].write_text(outputs['prepared'].read_text().replace('10000', '10100'))
        self.assertFalse(verify_preparation(source, outputs)['passed'])

    def test_malformed_schema_line_endings_and_record_width_are_rejected(self):
        good = (FIXTURES / 'expected.csv').read_bytes()
        outputs = self.candidate(self.source, good)
        for data in (
            good.replace(b'\n', b'\r\n'), good[:-1],
            good.replace(b'order_id,customer_id', b'customer_id,order_id', 1),
            good.replace(b'1,10000,0', b'1,10000,0,extra', 1),
            b'\xff', b'"unterminated\n',
        ):
            with self.subTest(data=data[:50]):
                outputs['prepared'].write_bytes(data)
                self.assertFalse(verify_preparation(self.source, outputs)['passed'])

    def test_missing_extra_and_unreadable_artifacts_are_rejected(self):
        outputs = self.candidate(self.source, (FIXTURES / 'expected.csv').read_bytes())
        for candidate in ({}, {'prepared': outputs['prepared']}, {**outputs, 'approval': True},
                          {**outputs, 'prepared': str(outputs['prepared'])},
                          {**outputs, 'prepared': self.root / 'absent'},
                          {**outputs, 'mapping': self.root}):
            with self.subTest(candidate=candidate):
                self.assertFalse(verify_preparation(self.source, candidate)['passed'])

    def test_symlinks_and_existing_outputs_are_not_written_through(self):
        linked = self.root / 'linked.json'
        linked.symlink_to(self.source)
        with self.assertRaises(SopError):
            prepare_orders(linked, self.root / 'symlink-source')
        outputs = prepare_orders(self.source, self.root / 'prepared')
        before = {name: path.read_bytes() for name, path in outputs.items()}
        with self.assertRaises(SopError) as caught:
            prepare_orders(self.source, self.root / 'prepared')
        self.assertEqual(caught.exception.code, 'output_exists')
        self.assertEqual(before, {name: path.read_bytes() for name, path in outputs.items()})
        alias = self.root / 'alias'
        alias.symlink_to(self.root / 'prepared', target_is_directory=True)
        with self.assertRaises(SopError):
            prepare_orders(self.source, alias / 'nested')
        self.assertFalse((self.root / 'prepared' / 'nested').exists())
        self.assertFalse(verify_preparation(linked, outputs)['passed'])
        linked_output = self.root / 'linked-output.csv'
        linked_output.symlink_to(outputs['prepared'])
        self.assertFalse(verify_preparation(self.source, {**outputs, 'prepared': linked_output})['passed'])


if __name__ == '__main__':
    unittest.main()
