"""Accepted definition constraints remain binding during parameter preparation.

The second probe deliberately changes trusted persisted state to exercise the
consumption guard. It does not claim protection against arbitrary database writes.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sop.authoring import configured_prepare_definition, validate_definition
from sop.capabilities import Registry
from sop.service import application

SOURCE = Path(__file__).resolve().parents[2] / 'tests/fixtures/input-handoff/source.json'
IDENTITY = {name: name for name in ('order_id', 'customer_id', 'revision', 'gross_cents', 'refund_cents')}


class RecordingRegistry(Registry):
    def __init__(self):
        self.calls = []

    def execute(self, capability, inputs, work_dir):
        self.calls.append((capability, deepcopy(inputs)))
        return super().execute(capability, inputs, work_dir)


class ParameterDefinitionConstraintTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sop-parameter-definition-')
        self.addCleanup(temporary.cleanup)
        self.registry = RecordingRegistry()
        self.runtime = application(Path(temporary.name) / 'data', registry=self.registry)
        self.definition = configured_prepare_definition()
        self.definition['params']['chunk_size']['enum'] = [1]
        self.definition['recovery'] = [{'id': 'correct-declared-chunk',
            'phase': 'parameter_validation', 'code': 'invalid_value',
            'action': 'request_parameter_proposal', 'fields': ['chunk_size'], 'max_attempts': 1}]
        self.assertTrue(validate_definition(self.definition, self.registry)['passed'])

    def start(self):
        run = self.runtime.start(self.definition, {'source_file': SOURCE, 'column_map': IDENTITY},
                                 capabilities=self.definition['capabilities'],
                                 parameter_policy={'max_chunk_size': 8})
        waiting = self.runtime.advance(run['id'])
        self.assertEqual(waiting['state'], 'waiting_info')
        self.assertEqual(waiting['operations'], {})
        self.assertEqual(self.registry.calls, [])
        return waiting

    def propose(self, run, value, message):
        current = self.runtime.store.load(run['id'])
        task = current['tasks'][current['root']]
        return self.runtime.propose_parameters(run['id'], task['id'], {'chunk_size': value},
                                               message_id=message, revision=task['parameter_revision'])

    def test_tighter_definition_enum_rejects_chunk_two_then_declared_correction_accepts_one(self):
        run = self.start()
        original = deepcopy(run['tasks'][run['root']]['inputs'])
        rejected = self.propose(run, 2, 'outside-definition')
        self.assertFalse(rejected['accepted'], rejected)
        self.assertTrue(any(item['code'] == 'invalid_value' and item.get('field') == 'chunk_size'
                            for item in rejected['diagnostics']), rejected)
        waiting = self.runtime.advance(run['id'])
        self.assertEqual(waiting['state'], 'waiting_info')
        task = waiting['tasks'][waiting['root']]
        self.assertEqual(task['inputs'], original)
        self.assertEqual(task['parameter_revision'], 0)
        self.assertEqual(task['recovery_counts'], {'correct-declared-chunk': 1})
        self.assertEqual(waiting['operations'], {})
        self.assertEqual(self.registry.calls, [])
        accepted = self.propose(run, 1, 'inside-definition')
        self.assertTrue(accepted['accepted'], accepted)
        self.assertEqual(accepted['missing'], [])
        self.assertEqual(self.registry.calls, [])
        done = self.runtime.advance(run['id'])
        self.assertEqual(done['state'], 'succeeded', done.get('error'))
        self.assertEqual(len(self.registry.calls), 1)
        self.assertEqual(self.registry.calls[0][1]['chunk_size'], 1)
        operation = next(iter(done['operations'].values()))
        self.assertEqual(operation['inputs']['chunk_size'], 1)
        self.assertEqual(operation['parameter_revision'], accepted['parameter_revision'])
        self.assertTrue(done['tasks'][done['root']]['verification']['passed'])

    def test_consumption_rechecks_definition_after_trusted_test_changes_saved_binding(self):
        run = self.start()
        accepted = self.propose(run, 1, 'valid-before-corruption')
        self.assertTrue(accepted['accepted'])
        saved = self.runtime.store.load(run['id'])
        task = saved['tasks'][saved['root']]
        self.assertEqual(task['inputs']['chunk_size'], 1)
        self.assertEqual(task['state'], 'ready')
        # Trusted fault injection into this isolated test's state, not a product
        # proposal endpoint and not an assertion about resisting a malicious DB.
        task['inputs']['chunk_size'] = 2
        self.runtime.store.commit(saved, 'trusted_test_binding_corruption', {'field': 'chunk_size'})
        done = self.runtime.advance(run['id'])
        self.assertEqual(done['state'], 'failed', done)
        self.assertEqual(done['error']['code'], 'invalid_parameter')
        self.assertEqual(done['operations'], {})
        self.assertEqual(self.registry.calls, [])
        self.assertEqual(done['tasks'][done['root']]['parameter_revision'], accepted['parameter_revision'])
        self.assertEqual(done['tasks'][done['root']]['parameter_history'][-1]['bindings']['chunk_size'], 1)


if __name__ == '__main__':
    unittest.main()
