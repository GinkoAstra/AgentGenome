"""Parameter preparation probes for SPEC 13–15 and R01/R02/R03.

These exercise pure candidate checks; runtime consumption/recovery/permissions
are tested by runtime integration, not implied by this module's success.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import unittest

import sop.parameter_forms as parameter_forms
from sop.parameter_forms import forms, validate_form

TASK = 'orders.prepare_configured'
COLUMNS = ['order_id', 'customer_id', 'revision', 'gross_cents', 'refund_cents']
IDENTITY = {name: name for name in COLUMNS}
POLICY = {'max_chunk_size': 64}
SOURCE = {'kind': 'input', 'hash': 'a' * 64, 'bytes': 123}


class ParameterFormTests(unittest.TestCase):
    def validate(self, current, patch, **kwargs):
        return validate_form(TASK, current, patch, POLICY, **kwargs)

    def test_registry_schema_is_detached_and_version_pins_validator_bytes(self):
        catalog = forms()
        self.assertEqual(set(catalog), {TASK})
        self.assertEqual(catalog[TASK]['fields'], {'column_map': {'type': 'object', 'required': True},
                                                 'chunk_size': {'type': 'integer', 'required': True}})
        self.assertEqual(catalog[TASK]['version'], hashlib.sha256(Path(parameter_forms.__file__).read_bytes()).hexdigest())
        catalog[TASK]['fields']['chunk_size']['required'] = False
        self.assertTrue(forms()[TASK]['fields']['chunk_size']['required'])

    def test_r01_missing_parameters_are_explicit_and_partial_map_is_accepted(self):
        current = {'source_file': deepcopy(SOURCE)}
        initial = self.validate(current, {})
        self.assertTrue(initial['passed'])
        self.assertEqual(initial['missing'], ['column_map', 'chunk_size'])
        mapped = self.validate(current, {'column_map': IDENTITY})
        self.assertTrue(mapped['passed'])
        self.assertEqual(mapped['missing'], ['chunk_size'])
        self.assertEqual(mapped['bindings']['source_file'], SOURCE)
        self.assertEqual(mapped['bindings']['column_map'], IDENTITY)
        self.assertEqual(mapped['invalid_fields'], [])
        self.assertEqual(current, {'source_file': SOURCE})

    def test_r02_invalid_chunk_after_accepted_map_does_not_change_bindings(self):
        current = {'source_file': deepcopy(SOURCE), 'column_map': deepcopy(IDENTITY)}
        rejected = self.validate(current, {'chunk_size': 0})
        self.assertFalse(rejected['passed'])
        self.assertEqual(rejected['bindings'], current)
        self.assertEqual(rejected['missing'], ['chunk_size'])
        self.assertEqual(rejected['invalid_fields'], ['chunk_size'])
        self.assertEqual(rejected['diagnostics'][0]['code'], 'invalid_value')
        self.assertEqual(rejected['diagnostics'][0]['field'], 'chunk_size')
        corrected = self.validate(rejected['bindings'], {'chunk_size': 32}, allowed_fields=['chunk_size'])
        self.assertTrue(corrected['passed'])
        self.assertEqual(corrected['missing'], [])
        self.assertEqual(corrected['bindings']['column_map'], IDENTITY)

    def test_r03_source_template_permission_and_observed_fields_reject_entire_patch(self):
        current = {'source_file': deepcopy(SOURCE), 'column_map': deepcopy(IDENTITY), 'chunk_size': 8}
        for field, value in {
            'source_ref': SOURCE, 'source_file': SOURCE, 'template_ref': 'new-template',
            'script_entry': 'replacement.py', 'output_ref': '/tmp/result', 'rule': 'first',
            'permissions': ['network'], 'max_chunk_size': 999, 'policy': {'max_chunk_size': 999},
            'node_status': 'succeeded', 'source_rows': 7, 'prepared_rows': 7,
            'record_values_and_order_preserved': True, 'target_format': 'json', 'code': 'print(1)',
        }.items():
            with self.subTest(field=field):
                patch = {'chunk_size': 16, field: value}
                result = self.validate(current, patch, allowed_fields=['chunk_size'])
                self.assertFalse(result['passed'])
                self.assertEqual(result['bindings'], current)
                self.assertEqual(result['invalid_fields'], [field])
                self.assertIn('unknown_field', {item['code'] for item in result['diagnostics']})

    def test_chunk_size_requires_nonboolean_integer_and_bounded_range(self):
        current = {'column_map': IDENTITY}
        for value in (True, False, 1.0, 4.5, '8', None, [], {}, -1, 0, 65, 10 ** 100):
            with self.subTest(value=value):
                result = self.validate(current, {'chunk_size': value})
                self.assertFalse(result['passed'])
                self.assertEqual(result['invalid_fields'], ['chunk_size'])
                self.assertEqual(result['bindings'], current)
        for value in (1, 2, 63, 64):
            with self.subTest(value=value):
                result = self.validate(current, {'chunk_size': value})
                self.assertTrue(result['passed'])
                self.assertEqual(result['missing'], [])
                self.assertEqual(result['bindings']['chunk_size'], value)

    def test_identity_mapping_must_be_complete_and_cannot_guess_semantics(self):
        renamed = {**IDENTITY, 'gross_cents': 'amount'}
        swapped = {**IDENTITY, 'gross_cents': 'refund_cents', 'refund_cents': 'gross_cents'}
        missing = dict(IDENTITY)
        missing.pop('revision')
        for value in (None, [], 'identity', {}, missing, renamed, swapped,
                      {**IDENTITY, 'extra': 'extra'}, {**IDENTITY, 'revision': 1},
                      {**IDENTITY, 'order_id': 'customer_id'}):
            with self.subTest(value=value):
                result = self.validate({'chunk_size': 4}, {'column_map': value})
                self.assertFalse(result['passed'])
                self.assertEqual(result['invalid_fields'], ['column_map'])
                self.assertEqual(result['bindings'], {'chunk_size': 4})

    def test_mixed_valid_and_invalid_fields_are_not_partially_accepted(self):
        current = {'source_file': SOURCE, 'chunk_size': 8}
        result = self.validate(current, {'column_map': IDENTITY, 'chunk_size': 0})
        self.assertFalse(result['passed'])
        self.assertEqual(result['bindings'], current)
        self.assertNotIn('column_map', result['bindings'])
        self.assertEqual(result['missing'], ['column_map'])

    def test_allowed_fields_can_only_narrow_and_excluded_field_even_same_value_is_rejected(self):
        current = {'column_map': IDENTITY, 'chunk_size': 8}
        rejected = self.validate(current, {'column_map': IDENTITY, 'chunk_size': 16}, allowed_fields=['chunk_size'])
        self.assertFalse(rejected['passed'])
        self.assertEqual(rejected['bindings'], current)
        self.assertEqual(rejected['invalid_fields'], ['column_map'])
        self.assertEqual(rejected['diagnostics'][0]['code'], 'field_not_editable')
        for scope in ('chunk_size', ['source_file'], ['chunk_size', 'chunk_size'], [None]):
            with self.subTest(scope=scope):
                result = self.validate(current, {'chunk_size': 16}, allowed_fields=scope)
                self.assertFalse(result['passed'])
                self.assertEqual(result['diagnostics'][0]['code'], 'invalid_allowed_fields')
                self.assertEqual(result['bindings'], current)
        self.assertTrue(self.validate(current, {}, allowed_fields=[])['passed'])
        self.assertFalse(self.validate(current, {'chunk_size': 16}, allowed_fields=[])['passed'])

    def test_bad_host_policy_never_implies_a_default_or_candidate_grant(self):
        current = {'column_map': IDENTITY}
        for policy in ({}, None, {'max_chunk_size': True}, {'max_chunk_size': 1.0},
                       {'max_chunk_size': 0}, {'max_chunk_size': -1}, {'max_chunk_size': 8, 'other': 1}):
            with self.subTest(policy=policy):
                result = validate_form(TASK, current, {'chunk_size': 1}, policy)
                self.assertFalse(result['passed'])
                self.assertEqual(result['bindings'], current)
                self.assertEqual(result['diagnostics'][0]['code'], 'invalid_policy')
                self.assertEqual(result['invalid_fields'], [])
        self.assertTrue(validate_form(TASK, current, {'chunk_size': 1}, {'max_chunk_size': 1})['passed'])

    def test_inputs_and_outputs_do_not_share_mutable_state(self):
        current = {'source_file': deepcopy(SOURCE), 'notes': {'labels': ['run-owned']}}
        patch = {'column_map': deepcopy(IDENTITY), 'chunk_size': 16}
        current_copy, patch_copy = deepcopy(current), deepcopy(patch)
        result = self.validate(current, patch)
        self.assertEqual(current, current_copy)
        self.assertEqual(patch, patch_copy)
        result['bindings']['source_file']['hash'] = 'b' * 64
        result['bindings']['notes']['labels'].append('changed')
        result['bindings']['column_map']['order_id'] = 'wrong'
        self.assertEqual(current, current_copy)
        self.assertEqual(patch, patch_copy)
        rejected = self.validate(current, {'chunk_size': 0})
        rejected['bindings']['notes']['labels'].append('also changed')
        self.assertEqual(current, current_copy)

    def test_existing_invalid_value_blocks_readiness_until_valid_correction(self):
        current = {'source_file': SOURCE, 'column_map': IDENTITY, 'chunk_size': 0}
        result = self.validate(current, {})
        self.assertFalse(result['passed'])
        self.assertEqual(result['invalid_fields'], ['chunk_size'])
        corrected = self.validate(current, {'chunk_size': 1}, allowed_fields=['chunk_size'])
        self.assertTrue(corrected['passed'])
        self.assertEqual(corrected['missing'], [])
        self.assertEqual(current['chunk_size'], 0)

    def test_unbound_null_is_missing_but_null_patch_is_invalid(self):
        current = {'column_map': None, 'chunk_size': None, 'source_file': SOURCE}
        result = self.validate(current, {})
        self.assertTrue(result['passed'])
        self.assertEqual(result['missing'], ['column_map', 'chunk_size'])
        result = self.validate(current, {'column_map': None})
        self.assertFalse(result['passed'])
        self.assertEqual(result['invalid_fields'], ['column_map'])

    def test_unknown_task_and_malformed_messages_fail_closed(self):
        self.assertEqual(validate_form('unknown', {}, {}, POLICY)['diagnostics'][0]['code'], 'unknown_form')
        for current, patch, code in (([], {}, 'invalid_current'), ({1: 'x'}, {}, 'invalid_current'),
                                     ({}, [], 'invalid_patch'), ({}, {1: 'x'}, 'invalid_patch')):
            result = validate_form(TASK, current, patch, POLICY)
            self.assertFalse(result['passed'])
            self.assertEqual(result['diagnostics'][0]['code'], code)


if __name__ == '__main__':
    unittest.main()
