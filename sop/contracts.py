"""Trusted task contracts; model proposals can reference but cannot change them."""
from copy import deepcopy

RULES = ['highest_revision', 'first']
LOCAL = ['orders.profile', 'orders.normalize', 'orders.deduplicate', 'orders.summarize']

def task_contracts():
    source = {'type':'artifact','required':True}
    rule = {'type':'string','enum':RULES[:],'required':True}
    clean = {'inputs':{'source_file':dict(source),'rule':dict(rule)},
             'outputs':['cleaned'],'capabilities':LOCAL[1:3]}
    report = {'inputs':{'source_file':dict(source),'rule':dict(rule,required=False)},
              'outputs':['cleaned','summary','quality'],'capabilities':LOCAL[:]}
    prepare = {'inputs':{'source_file':dict(source,role='source_json')},
               'outputs':['prepared','mapping'],'output_roles':{'prepared':'source_file','mapping':'mapping'},
               'capabilities':['orders.prepare']}
    from_json = {'inputs':{'source_file':dict(source,role='source_json'),'rule':dict(rule,required=False)},
                 'outputs':['prepared','mapping','cleaned','summary','quality'],
                 'output_roles':{'prepared':'source_file','mapping':'mapping'},
                 'capabilities':['orders.prepare',*LOCAL]}
    configured = {'inputs': {'source_file': dict(source, role='source_json'),
                             'column_map': {'type': 'object', 'required': True},
                             'chunk_size': {'type': 'integer', 'required': True}},
                  'outputs': ['prepared', 'mapping'],
                  'output_roles': {'prepared': 'source_file', 'mapping': 'mapping'},
                  'capabilities': ['orders.prepare_configured'],
                  'fixed_capability': 'orders.prepare_configured'}
    return deepcopy({'orders.clean': clean, 'orders.report': report, 'orders.prepare': prepare,
                     'orders.from_json': from_json, 'orders.prepare_configured': configured})
