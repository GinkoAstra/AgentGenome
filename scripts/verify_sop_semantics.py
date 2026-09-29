#!/usr/bin/env python3
"""Frozen source-based semantic acceptance. Default: offline preparation only.

The development cases are not an algorithm-benefit holdout. Labels are consumed
by the host oracle; the model sees only the original sources and a scoped answer.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sop.agents import PiAuthor, PiSemanticReviewer
from sop.capabilities import Registry
from sop.common import SopError, digest, file_digest, read_json, write_json
from sop.skill_authoring import SemanticAuthoring, bundle_hash
from sop.sources import collect_sources

SUITE = ROOT / 'tests/fixtures/semantic-authoring-v1'
RULES = {'first', 'highest_revision'}
INFRASTRUCTURE = {'backend_unavailable', 'backend_version', 'backend_configuration',
                  'backend_auth', 'backend_timeout', 'backend_error', 'model_failure',
                  'storage_failure', 'source_changed', 'evaluation_changed'}


def load_suite(suite):
    """Fail before dispatch if any frozen label, source or reference graph changed."""
    suite = Path(suite).resolve()
    manifest = read_json(suite / 'manifest.json')
    labels_path = suite / 'labels.json'
    if (manifest.get('schema') != 'semantic-authoring-manifest/1' or
            file_digest(labels_path) != manifest.get('labels_sha256')):
        raise SopError('evaluation_changed', 'frozen labels or manifest schema changed')
    labels = read_json(labels_path)
    if labels.get('schema') != 'semantic-authoring-labels/1':
        raise SopError('evaluation_schema', 'invalid label schema')
    cases, seen = [], set()
    for label in labels['cases']:
        case_id = label['id']
        if not re.fullmatch(r'case-\d{2}', case_id) or case_id in seen:
            raise SopError('evaluation_schema', 'duplicate or unsafe case identity')
        seen.add(case_id)
        if (label['initial_state'] not in ('delivered', 'waiting_info', 'stopped') or
                label['mode'] not in ('fixed', 'pi') or
                label['rule_status'] not in ('known', 'unknown', 'runtime') or
                label['rule_value'] not in RULES | {None} or label['answer'] not in RULES | {None} or
                (label['initial_state'] == 'waiting_info') != (label['answer'] is not None) or
                (label['initial_state'] == 'stopped') != bool(label['reject_spans'])):
            raise SopError('evaluation_schema', 'inconsistent semantic label')
        source = suite / 'cases' / case_id / 'SKILL.md'
        bundle = collect_sources(source)
        observed = {f['relative_path']: f['sha256'] for f in bundle['files']}
        expected = manifest['cases'].get(case_id)
        if (bundle['diagnostics'] or expected is None or observed != expected['files'] or
                bundle_hash(bundle) != expected['bundle_hash']):
            raise SopError('evaluation_changed', f'frozen sources changed: {case_id}')
        cases.append((label, source, bundle))
    if seen != set(manifest['cases']):
        raise SopError('evaluation_schema', 'manifest and label case lists differ')
    return manifest, cases


def codes(draft):
    return {item.get('code') for item in draft.get('diagnostics', [])}


def reject_locations(draft):
    locations = []
    for item in draft.get('diagnostics', []):
        if item.get('code') == 'unsupported_requirement':
            for span in item.get('source_spans', []):
                annotated = draft.get('analysis', {}).get('annotations', [])
                index = span.get('annotation_index')
                reason = annotated[index].get('reason') if type(index) is int and 0 <= index < len(annotated) else None
                locations.append({**span, 'reason': reason})
    for review in draft.get('semantic_reviews', []):
        if review.get('verdict') == 'reject':
            locations.extend(item for item in review.get('findings', []) if isinstance(item, dict))
    return locations


def located_rejection(draft, label, bundle):
    if not codes(draft) & {'unsupported_requirement', 'semantic_review_rejected'}:
        return False
    lengths = {f['id']: len(f['text'].splitlines()) for f in bundle['files']}
    # These frozen cases each isolate the unsupported requirement on one line.
    # A whole-file or out-of-range rejection cannot prove that it was identified.
    return all(any(span.get('source_id') == expected['file'] and
                   type(span.get('start_line')) is int and type(span.get('end_line')) is int and
                   1 <= span['start_line'] == expected['line'] == span['end_line'] <= lengths.get(expected['file'], 0) and
                   isinstance(span.get('reason'), str) and bool(span['reason'].strip())
                   for span in reject_locations(draft)) for expected in label['reject_spans'])


def question_for_answer(draft):
    questions = [q for q in draft.get('questions', []) if q.get('status') == 'open']
    if len(questions) != 1:
        return None
    question = questions[0]
    options = {
        'highest_revision': 'Keep greatest integer revision, not last line; identical highest ties collapse; conflicting highest ties reject.',
        'first': 'Keep first input record per order, after checking customer consistency across all revisions.',
    }
    if (question.get('options') != options or question.get('field') != 'rule' or question.get('schema', {}).get('type') != 'string' or
            set(question.get('schema', {}).get('enum', [])) != RULES or not question.get('requirements')):
        return None
    return question


def assess(label, bundle, initial, final):
    """Host rubric; no generator self-score and no arbitrary-stop success."""
    errors = []
    if any(d.get('bundle_hash') != bundle_hash(bundle) for d in (initial, final)):
        errors.append('frozen_source_identity_mismatch')
    if initial['state'] != label['initial_state']:
        errors.append('initial_state_mismatch')
    if any(d.get('publication') != 'unpublished' or d.get('business_calls') != 0 for d in (initial, final)):
        errors.append('authoring_side_effect_or_publication')
    if label['initial_state'] == 'stopped':
        if not located_rejection(initial, label, bundle):
            errors.append('missing_located_semantic_rejection')
        if (final.get('report') or {}).get('passed') or final.get('state') != 'stopped':
            errors.append('unsupported_request_delivered')
        return errors
    analysis = initial.get('analysis') or {}
    if analysis.get('mode') != label['mode']:
        errors.append('execution_mode_mismatch')
    if analysis.get('rule') != {'status': label['rule_status'], 'value': label['rule_value']}:
        errors.append('retention_interpretation_mismatch')
    # Independently count source coverage, including text mislabelled as background.
    # The frozen semantic cases constrain the interpretation; this check alone is
    # intentionally not described as proving arbitrary natural-language meaning.
    expected = {(f['id'], n) for f in bundle['files']
                for n, line in enumerate(f['text'].splitlines(), 1) if line.strip()}
    covered = Counter()
    for annotation in analysis.get('annotations', []):
        first, last = annotation.get('start_line'), annotation.get('end_line')
        if type(first) is int and type(last) is int and 0 < first <= last <= 100000:
            covered.update((annotation.get('source_id'), n) for n in range(first, last + 1))
    if any(covered[key] != 1 for key in expected):
        errors.append('original_source_coverage_mismatch')
    if label['initial_state'] == 'waiting_info':
        if question_for_answer(initial) is None:
            errors.append('missing_scoped_rule_question')
        answer = final.get('answers', {}).get('rule', {})
        if answer.get('source') != 'answer' or answer.get('value') != label['answer']:
            errors.append('accepted_answer_not_preserved')
    if final.get('state') != 'delivered' or not (final.get('report') or {}).get('passed'):
        errors.append('definition_not_delivered')
        return errors
    definition = final['definition']
    expected_rules = RULES if label['rule_status'] == 'runtime' else {label['answer'] or label['rule_value']}
    parameter = definition.get('params', {}).get('rule', {})
    if set(parameter.get('enum', [])) != expected_rules or 'default' in parameter:
        errors.append('compiled_rule_domain_mismatch')
    if (definition.get('task') != 'orders.report' or definition.get('checks') != ['orders.report'] or
            definition.get('params', {}).get('source_file', {}).get('type') != 'artifact'):
        errors.append('compiled_parent_contract_mismatch')
    if label['mode'] == 'fixed' and any(s.get('kind') != 'fixed' for s in definition.get('steps', [])):
        errors.append('fixed_implementation_replaced')
    requirements = final.get('requirements', [])
    if not requirements or any(not r.get('targets') or not final.get('requirement_map', {}).get('forward', {}).get(r['id']) for r in requirements):
        errors.append('requirement_correspondence_incomplete')
    if not final.get('semantic_reviews') or final['semantic_reviews'][-1].get('verdict') != 'pass':
        errors.append('independent_review_not_completed')
    return errors


def verify(output, *, suite=SUITE, backend='plan', max_model_calls=12, ports_factory=None):
    if backend not in ('plan', 'pi', 'test_double') or (backend == 'test_double') != (ports_factory is not None):
        raise SopError('evaluation_backend', 'only explicit test injection or real Pi is permitted')
    if type(max_model_calls) is not int or not 1 <= max_model_calls <= 12:
        raise SopError('evaluation_budget', 'per-case model reservation limit must be 1..12')
    pinned_manifest_sha = file_digest(Path(suite) / 'manifest.json')
    manifest, cases = load_suite(suite)
    if pinned_manifest_sha != file_digest(Path(suite) / 'manifest.json'):
        raise SopError('evaluation_changed', 'manifest changed during preparation')
    output = Path(output).absolute()
    if output.exists() and any(output.iterdir()):
        raise SopError('output_exists', 'use a new empty evidence directory')
    output.mkdir(parents=True, exist_ok=True)
    # Freeze inputs before the first call. These host-only records never enter a
    # port context. Storage cannot be reused by another case.
    write_json(output / 'suite-manifest.json', manifest)
    result = {'schema': 'semantic-authoring-evidence/1', 'created_at': datetime.now(timezone.utc).isoformat(),
              'backend': backend, 'status': 'not_run' if backend == 'plan' else 'running',
              'case_count': len(cases), 'cases': [], 'complete': False,
              'real_model': backend == 'pi', 'algorithm_benefit': 'not_measured',
              'scope': 'development-constructed semantic acceptance, not algorithm holdout',
              'limits': {'max_model_calls_per_case': max_model_calls,
                         'max_total_model_reservations': len(cases) * max_model_calls,
                         'max_output_tokens_per_request': 4096, 'max_turns_per_port': 3},
              'suite_manifest_sha256': pinned_manifest_sha,
              'implementation_hashes': {str(p.relative_to(ROOT)): file_digest(p)
                                        for p in sorted((ROOT / 'sop').glob('*')) if p.is_file()},
              'driver_sha256': file_digest(Path(__file__))}
    write_json(output / 'summary.json', result)
    if backend == 'plan':
        result['preparation_valid'] = True
        result['model_validation'] = 'not performed; no provider request'
        write_json(output / 'summary.json', result)
        return result
    for label, source, bundle in cases:
        # Verify the whole frozen suite again to catch drift between cases.
        try:
            current_manifest, _ = load_suite(suite)
            if (pinned_manifest_sha != file_digest(Path(suite) / 'manifest.json') or
                    digest(current_manifest) != digest(manifest)):
                raise SopError('evaluation_changed', 'manifest was replaced during the batch')
        except (SopError, OSError) as exc:
            result.update(status='blocked', error={'code': getattr(exc, 'code', 'io_error'), 'case': label['id']})
            break
        case = output / label['id']
        case.mkdir()
        author, reviewer = ports_factory() if ports_factory else (PiAuthor(), PiSemanticReviewer())
        service = SemanticAuthoring(case / '.data', Registry(), author, reviewer, max_model_calls)
        initial = final = None
        try:
            initial = service.submit(source)
            write_json(case / 'initial.json', initial)
            final = initial
            if label['answer'] is not None and initial['state'] == 'waiting_info':
                question = question_for_answer(initial)
                if question:
                    receipt = {'request_id': question['request_id'], 'value': label['answer'],
                               'message_id': 'frozen-rule-answer', 'revision': initial['revision']}
                    write_json(case / 'answer-message.json', receipt)
                    final = service.answer(initial['draft_id'], **receipt)
            write_json(case / 'final.json', final)
            errors = assess(label, bundle, initial, final)
            infrastructure = sorted(codes(final) & INFRASTRUCTURE)
            if 'frozen_source_identity_mismatch' in errors:
                infrastructure.append('evaluation_changed')
            if pinned_manifest_sha != file_digest(Path(suite) / 'manifest.json'):
                infrastructure.append('evaluation_changed')
            entry = {'case': label['id'], 'initial_state': initial['state'], 'final_state': final['state'],
                     'passed': not errors and not infrastructure, 'validation_errors': errors,
                     'infrastructure_errors': infrastructure, 'diagnostic_codes': sorted(codes(final)),
                     'usage': final['usage'], 'model_attempts': final['model_attempts'],
                     'draft_id': final['draft_id'], 'publication': final['publication']}
        except (SopError, OSError) as exc:
            # Keep earlier snapshots and durable authoring records; do not silently
            # skip the case or substitute a scripted proposal.
            entry = {'case': label['id'], 'passed': False, 'validation_errors': ['driver_or_storage_failure'],
                     'infrastructure_errors': [getattr(exc, 'code', 'io_error')]}
        write_json(case / 'assessment.json', entry)
        result['cases'].append(entry)
        if entry['infrastructure_errors']:
            result['status'] = 'blocked'
        write_json(output / 'summary.json', result)
        if result['status'] == 'blocked':
            break
    result['complete'] = len(result['cases']) == len(cases) and all(c['passed'] for c in result['cases'])
    if result['status'] != 'blocked':
        result['status'] = 'passed' if result['complete'] else 'failed'
    result['not_run_cases'] = [label['id'] for label, _, _ in cases][len(result['cases']):]
    result['model_validation'] = 'real Pi requests; see each assessment' if backend == 'pi' else 'program test doubles only'
    write_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--suite', type=Path, default=SUITE)
    parser.add_argument('--backend', choices=['plan', 'pi'], default='plan')
    parser.add_argument('--max-model-calls', type=int, default=12)
    args = parser.parse_args()
    try:
        result = verify(args.output, suite=args.suite, backend=args.backend, max_model_calls=args.max_model_calls)
        print(f"status={result['status']}; cases={len(result['cases'])}/{result['case_count']}; evidence={Path(args.output).absolute()}")
        return 0 if result.get('preparation_valid') or result['complete'] else 1
    except (SopError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f'{getattr(exc, "code", "evaluation_error")}: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
