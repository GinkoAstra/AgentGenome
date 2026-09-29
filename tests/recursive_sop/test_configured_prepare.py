"""Registered configured conversion, parameter gate, and independent checks."""
from copy import deepcopy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sop.authoring import configured_prepare_definition, root_definition, validate_definition
from sop.capabilities import Registry
from sop.common import SopError, read_json, write_json
from sop.input_preparation import COLUMNS, prepare_orders, prepare_orders_configured, verify_configured_preparation

FIXTURE = Path(__file__).resolve().parents[2]/'tests/fixtures/input-handoff'
IDENTITY = {name: name for name in COLUMNS}


def repair_rule():
    return {'id': 'correct-chunk', 'phase': 'parameter_validation', 'code': 'invalid_value',
            'action': 'request_parameter_proposal', 'fields': ['chunk_size'], 'max_attempts': 2}


class ConfiguredPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = Registry()

    def test_registered_factory_is_fixed_and_exposes_only_declared_parameters(self):
        definition = configured_prepare_definition()
        report = validate_definition(definition, self.registry)
        self.assertTrue(report['passed'], report)
        self.assertEqual(set(definition['params']), {'source_file', 'column_map', 'chunk_size'})
        self.assertEqual(definition['checks'], ['orders.prepare_configured'])
        self.assertEqual(definition['steps'][0]['kind'], 'fixed')
        self.assertEqual(definition['steps'][0]['inputs'], {name: '$input.'+name for name in definition['params']})
        self.assertEqual(self.registry.catalog()['orders.prepare_configured']['result_check'], 'orders.prepare_configured')
        self.assertEqual(set(self.registry.parameter_forms()['orders.prepare_configured']['fields']), {'column_map', 'chunk_size'})
        changed = deepcopy(self.registry.parameter_forms())
        changed['orders.prepare_configured']['fields'].clear()
        self.assertEqual(len(self.registry.parameter_forms()['orders.prepare_configured']['fields']), 2)

    def test_real_chunk_sizes_control_csv_batches_with_identical_outputs(self):
        baseline = prepare_orders(FIXTURE/'source.json', self.root/'baseline')
        original_writer = csv.writer
        for size, expected in ((1, [1]*7), (2, [2, 2, 2, 1]), (3, [3, 3, 1]), (100, [7])):
            with self.subTest(chunk_size=size):
                batches = []
                def observed_writer(*args, **kwargs):
                    real = original_writer(*args, **kwargs)
                    class Observed:
                        def writerow(self, row):
                            return real.writerow(row)
                        def writerows(self, rows):
                            rows = list(rows)
                            batches.append(len(rows))
                            return real.writerows(rows)
                    return Observed()
                with patch('sop.input_preparation.csv.writer', side_effect=observed_writer):
                    outputs = prepare_orders_configured(FIXTURE/'source.json', IDENTITY, size, self.root/str(size))
                self.assertEqual(batches, expected)
                self.assertEqual(outputs['prepared'].read_bytes(), (FIXTURE/'expected.csv').read_bytes())
                for name in ('prepared', 'mapping'):
                    self.assertEqual(outputs[name].read_bytes(), baseline[name].read_bytes())
                checked = self.registry.verify('orders.prepare_configured',
                    {'source_file': FIXTURE/'source.json', 'column_map': IDENTITY, 'chunk_size': size}, outputs)
                self.assertTrue(checked['passed'], checked)
                self.assertEqual(checked['metrics']['compared_fields'], 35)

    def test_invalid_values_fail_before_creating_artifacts(self):
        invalid = [(IDENTITY, value) for value in (0, -1, True, False, 1.0, '2', None)]
        invalid += [(None, 2), ({}, 2), ({**IDENTITY, 'extra': 'extra'}, 2),
                    ({**IDENTITY, 'gross_cents': 'refund_cents'}, 2)]
        for index, (mapping, size) in enumerate(invalid):
            with self.subTest(mapping=mapping, chunk_size=size):
                output = self.root/str(index)
                with self.assertRaises(SopError) as error:
                    self.registry.execute('orders.prepare_configured',
                        {'source_file': FIXTURE/'source.json', 'column_map': mapping, 'chunk_size': size}, output)
                self.assertEqual(error.exception.code, 'invalid_value')
                self.assertFalse(output.exists())

    def test_checker_validates_parameters_and_original_values_independently(self):
        outputs = prepare_orders_configured(FIXTURE/'source.json', IDENTITY, 2, self.root/'valid')
        with patch('sop.input_preparation._prepare_orders', side_effect=AssertionError('producer called')), \
             patch('sop.input_preparation._source_records', side_effect=AssertionError('producer parser called')):
            self.assertTrue(verify_configured_preparation(FIXTURE/'source.json', IDENTITY, 2, outputs)['passed'])
        for mapping, size in (({}, 2), ({**IDENTITY, 'revision': 'gross_cents'}, 2), (IDENTITY, True), (IDENTITY, 0)):
            checked = verify_configured_preparation(FIXTURE/'source.json', mapping, size, outputs)
            self.assertFalse(checked['passed'])
            self.assertEqual(checked['diagnostics'][0]['code'], 'invalid_value')
        # Refresh the producer-reported checksum: source-based validation must
        # still catch the changed cell rather than relying only on the mapping.
        raw = outputs['prepared'].read_bytes().replace(b'12000', b'12100', 1)
        outputs['prepared'].write_bytes(raw)
        import hashlib
        mapping = read_json(outputs['mapping'])
        mapping['prepared']['sha256'] = hashlib.sha256(raw).hexdigest()
        write_json(outputs['mapping'], mapping)
        checked = verify_configured_preparation(FIXTURE/'source.json', IDENTITY, 2, outputs)
        self.assertFalse(checked['passed'])
        self.assertIn('prepared_value_mismatch', {item['code'] for item in checked['diagnostics']})

    def test_configured_graph_cannot_switch_to_pi_or_override_bound_configuration(self):
        candidates = []
        definition = configured_prepare_definition()
        definition['steps'] = [{'id': 'convert', 'kind': 'pi', 'task': 'orders.prepare_configured',
                                'inputs': dict(definition['steps'][0]['inputs'])}]
        candidates.append(definition)
        for field, value in (('column_map', IDENTITY), ('chunk_size', 1)):
            definition = configured_prepare_definition()
            definition['steps'][0]['inputs'][field] = value
            candidates.append(definition)
        for definition in candidates:
            with self.subTest(definition=definition):
                self.assertFalse(validate_definition(definition, self.registry)['passed'])

    def test_object_schema_cannot_escape_the_registered_contract(self):
        candidates = []
        for name in ('rule', 'source_file'):
            definition = root_definition()
            definition['params'][name]['type'] = 'object'
            candidates.append(definition)
        for extra in ({'properties': {'command': {'type': 'string'}}}, {'enum': [IDENTITY]}, {'additionalProperties': True}):
            definition = configured_prepare_definition()
            definition['params']['column_map'].update(extra)
            candidates.append(definition)
        definition = configured_prepare_definition()
        definition['params']['shell'] = {'type': 'object', 'required': True}
        candidates.append(definition)
        for definition in candidates:
            with self.subTest(params=definition['params']):
                self.assertFalse(validate_definition(definition, self.registry)['passed'])

    def test_parameter_recovery_is_exact_registered_chunk_size_only(self):
        definition = configured_prepare_definition()
        definition['recovery'] = [repair_rule()]
        self.assertTrue(validate_definition(definition, self.registry)['passed'])
        variants = [{'fields': fields} for fields in ([], ['column_map'], ['source_file'], ['chunk_size', 'column_map'], ['chunk_size', 'chunk_size'])]
        variants += [{'code': 'permission_denied'}, {'code': 'temporary_read'}, {'action': 'retry'},
                     {'max_attempts': True}, {'max_attempts': 0}, {'max_attempts': 1.0},
                     {'permissions': ['shell']}, {'checks': []}]
        for mutation in variants:
            with self.subTest(mutation=mutation):
                bad = deepcopy(definition)
                bad['recovery'][0].update(mutation)
                self.assertFalse(validate_definition(bad, self.registry)['passed'])
        missing = deepcopy(definition)
        del missing['recovery'][0]['fields']
        self.assertFalse(validate_definition(missing, self.registry)['passed'])
        duplicate = deepcopy(definition)
        duplicate['recovery'].append(dict(repair_rule(), id='second-selector'))
        self.assertFalse(validate_definition(duplicate, self.registry)['passed'])
        unregistered = root_definition()
        unregistered['recovery'] = [repair_rule()]
        self.assertFalse(validate_definition(unregistered, self.registry)['passed'])
        incompatible = configured_prepare_definition()
        incompatible['recovery'] = [{'id': 'execution', 'phase': 'execution', 'code': 'temporary_read',
                                     'action': 'retry', 'fields': ['chunk_size'], 'max_attempts': 1}]
        self.assertFalse(validate_definition(incompatible, self.registry)['passed'])


if __name__ == '__main__':
    unittest.main()
