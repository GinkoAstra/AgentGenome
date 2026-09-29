"""Command line entry for the recursive SOP release."""
import argparse
import json
from pathlib import Path
import sys

from .common import SopError, read_json, write_json


def parser():
    result = argparse.ArgumentParser(prog='agentgenome-sop')
    result.add_argument('--data', default='.data', help='isolated authoring and run data directory')
    sub = result.add_subparsers(dest='command', required=True)
    author = sub.add_parser('author')
    author.add_argument('skill')
    author.add_argument('--proposal')
    author.add_argument('--backend', choices=['none','pi'], default='none')
    author.add_argument('--max-model-calls', type=int, default=12)
    da = sub.add_parser('draft-answer')
    da.add_argument('draft_id')
    da.add_argument('request_id')
    da.add_argument('value', choices=['highest_revision', 'first'])
    da.add_argument('--message-id', required=True)
    da.add_argument('--revision', required=True, type=int)
    da.add_argument('--backend',choices=['none','pi'],default='none')
    dr=sub.add_parser('draft-resume')
    dr.add_argument('draft_id')
    dr.add_argument('--backend',choices=['none','pi'],default='none')
    dc=sub.add_parser('draft-cancel')
    dc.add_argument('draft_id')
    dc.add_argument('--message-id',required=True)
    dc.add_argument('--revision',required=True,type=int)
    revise=sub.add_parser('draft-revise')
    revise.add_argument('draft_id')
    revise.add_argument('--message-id',required=True)
    revise.add_argument('--revision',required=True,type=int)
    revise.add_argument('--backend',choices=['none','pi'],default='none')
    revision_input=revise.add_mutually_exclusive_group(required=True)
    revision_input.add_argument('--proposal')
    revision_input.add_argument('--request-model',action='store_true')
    draft = sub.add_parser('draft')
    draft.add_argument('draft_id')
    check = sub.add_parser('check')
    check.add_argument('definition')
    demo = sub.add_parser('definition')
    demo.add_argument('--mode', choices=['pi', 'fixed', 'clean', 'prepare', 'prepare-configured', 'json-pi', 'json-fixed'], default='pi')
    demo.add_argument('--output', required=True)
    for name in ('run', 'resume'):
        command = sub.add_parser(name)
        if name == 'run':
            command.add_argument('--definition')
            command.add_argument('--draft')
            command.add_argument('--input', required=True)
            command.add_argument('--rule', choices=['highest_revision', 'first'])
            command.add_argument('--grant', nargs='+', required=True, help='explicit allowed registry capability IDs')
            command.add_argument('--max-actions', type=int, default=100)
            command.add_argument('--max-model-calls', type=int, default=20)
            command.add_argument('--max-depth', type=int, default=8)
            command.add_argument('--max-chunk-size',type=int,default=1024)
        else:
            command.add_argument('run_id')
        command.add_argument('--backend', choices=['none', 'scripted', 'pi'], default='none')
        command.add_argument('--library', choices=['existing', 'generated'], default='existing')
        command.add_argument('--candidate', help='explicit saved candidate definition JSON')
    for name in ('status', 'events', 'cancel', 'export'):
        command = sub.add_parser(name)
        command.add_argument('run_id')
        if name == 'export':
            command.add_argument('--output', required=True)
    parameters = sub.add_parser('parameters')
    parameters.add_argument('run_id')
    parameters.add_argument('task_id')
    parameters.add_argument('--values',required=True,help='JSON object containing only editable form fields')
    parameters.add_argument('--message-id',required=True)
    parameters.add_argument('--revision',required=True,type=int,help='task parameter_revision, not run revision')
    answer = sub.add_parser('answer')
    answer.add_argument('run_id')
    answer.add_argument('request_id')
    answer.add_argument('value', choices=['highest_revision', 'first'])
    answer.add_argument('--message-id', required=True)
    answer.add_argument('--revision', required=True, type=int)
    answer.add_argument('--owner')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    from .authoring import Authoring, Library, clean_definition, root_definition, prepare_definition, configured_prepare_definition, json_report_definition, validate_definition
    from .capabilities import Registry
    from .service import application
    from .agents import PiAgent, PiPlanner, ScriptedAgent, ScriptedPlanner
    from .store import Store
    registry = Registry()
    try:
        if args.command in ('author', 'draft-answer', 'draft', 'draft-resume', 'draft-cancel', 'draft-revise'):
            if getattr(args,'backend','none')=='pi':
                from .agents import PiAuthor, PiSemanticReviewer
                from .skill_authoring import SemanticAuthoring
                author=SemanticAuthoring(Path(args.data),registry,PiAuthor(),PiSemanticReviewer(),getattr(args,'max_model_calls',12))
            elif args.command in ('draft-cancel','draft-revise'):
                from .skill_authoring import SemanticAuthoring
                author=SemanticAuthoring(Path(args.data),registry)
            else:
                author = Authoring(Path(args.data), registry)
            if args.command == 'author':
                result = author.submit(Path(args.skill), proposal=read_json(args.proposal) if args.proposal else None)
            elif args.command == 'draft-answer':
                result = author.answer(args.draft_id, args.request_id, args.value, args.message_id, args.revision)
            elif args.command == 'draft-cancel':
                result=author.cancel(args.draft_id,message_id=args.message_id,revision=args.revision)
            elif args.command == 'draft-revise':
                result=author.revise(args.draft_id,message_id=args.message_id,revision=args.revision,
                                     proposal=read_json(args.proposal) if args.proposal else None,
                                     request_model=args.request_model)
            elif args.command == 'draft-resume':
                if not hasattr(author,'resume'):
                    raise SopError('backend_unavailable','draft-resume requires an explicit semantic backend')
                result=author.resume(args.draft_id)
            else:
                result = author.get(args.draft_id)
        elif args.command == 'check':
            result = validate_definition(read_json(args.definition), registry)
        elif args.command == 'definition':
            definition = (configured_prepare_definition() if args.mode=='prepare-configured' else clean_definition() if args.mode=='clean' else prepare_definition() if args.mode=='prepare'
                          else json_report_definition(args.mode[5:]) if args.mode.startswith('json-') else root_definition(args.mode))
            write_json(args.output, definition)
            result = {'definition': str(Path(args.output).absolute()), 'check': validate_definition(definition, registry)}
        elif args.command in ('status', 'events', 'export'):
            store = Store(args.data)
            result = store.events(args.run_id) if args.command == 'events' else store.load(args.run_id)
            if args.command == 'export':
                if result['state'] != 'succeeded':
                    raise SopError('not_succeeded', 'only accepted root outputs can be exported')
                output = Path(args.output)
                output.mkdir(parents=True, exist_ok=True)
                exported = {}
                for name, ref in result['tasks'][result['root']]['outputs'].items():
                    target = output / (name + ('.json' if name in ('quality','mapping') else '.csv'))
                    with target.open('xb') as handle:
                        handle.write(store.path(ref).read_bytes())
                    exported[name] = str(target.absolute())
                result = exported
        else:
            mode = getattr(args, 'backend', 'none')
            agent, planner = (PiAgent(), PiPlanner()) if mode == 'pi' else (
                (ScriptedAgent(), ScriptedPlanner()) if mode == 'scripted' else (None, None))
            definitions = [prepare_definition(), configured_prepare_definition(), clean_definition('first')]
            if getattr(args, 'library', 'existing') == 'existing':
                definitions.append(clean_definition())
            library = Library(definitions)
            if getattr(args, 'candidate', None):
                library.add(read_json(args.candidate), origin='candidate_reuse')
            runtime = application(args.data, registry=registry, library=library, agent=agent, planner=planner)
            if args.command == 'run':
                if bool(args.definition) == bool(args.draft):
                    raise SopError('invalid_arguments', 'choose exactly one of --definition or --draft')
                provenance = None
                if args.draft:
                    draft = Authoring(Path(args.data), registry).get(args.draft)
                    if draft['state'] != 'delivered':
                        raise SopError('draft_not_delivered', 'draft has not passed current definition checks')
                    definition = draft['definition']
                    provenance = {'draft_id': args.draft, 'revision': draft['revision'],
                                  'report': draft['report'], 'answers': draft.get('answers', [])}
                    bound_rule = draft.get('answers', {}).get('rule', {}).get('value')
                    if not bound_rule:
                        answers = draft.get('answers', {})
                        if answers:
                            last = list(answers.values())[-1]
                            bound_rule = last.get('value') if isinstance(last, dict) else None
                else:
                    definition = read_json(args.definition)
                    bound_rule = None
                inputs = {'source_file': args.input}
                rule = args.rule or bound_rule
                if rule:
                    inputs['rule'] = rule
                result = runtime.start(definition, inputs, capabilities=args.grant,
                                       limits={'max_actions': args.max_actions, 'max_model_calls': args.max_model_calls,
                                               'max_depth': args.max_depth}, authoring=provenance,
                                       parameter_policy={'max_chunk_size':args.max_chunk_size})
                result = runtime.advance(result['id'])
            elif args.command == 'resume':
                result = runtime.advance(args.run_id)
            elif args.command == 'parameters':
                result=runtime.propose_parameters(args.run_id,args.task_id,read_json(args.values),
                                                   message_id=args.message_id,revision=args.revision)
            elif args.command == 'answer':
                result = runtime.answer(args.run_id, args.request_id, args.value, message_id=args.message_id,
                                        revision=args.revision, owner=args.owner)
            else:
                result = runtime.cancel(args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and (result.get('state') in {'failed', 'effect_unknown', 'budget_exhausted', 'stopped'} or result.get('passed') is False or result.get('accepted') is False) else 0
    except (SopError, OSError) as exc:
        print(json.dumps({'error': getattr(exc, 'code', 'io_error'), 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
