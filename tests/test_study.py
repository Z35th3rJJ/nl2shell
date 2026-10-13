import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.bash_queries import prepare_queries
from core.execution import ExecutionResult
from core.operations import prepare_plan
from core.task_plan import TaskPlan, parse_operation_plan
from eval.study_cases import cases, supplementary_cases, fixture, environment, dataset_hash
from eval.study_oracles import decode_candidates, expected_plan, signature, common_gate, authorize, final_oracle
from eval.study import evaluate_one, generate, metrics, plan_messages, command_messages, prompt_suffix
from eval.run_eval import tree_state


NOW = 1700000000


def candidates(case, root):
    plan = expected_plan(case)
    writes = prepare_plan(plan,str(root),now=NOW) if case['write_task'] else None
    commands=[]
    for index,s in enumerate(plan.steps):
        if s.operation in {'copy_files','move_files'}:
            sources=[i['source'].replace('\\','/') for i in writes['actions'][index]['items']]
            argv=['cp' if s.operation=='copy_files' else 'mv','--',*('./'+p for p in sources),'./'+s.parameters['destination']]
        elif s.operation=='create_file':
            argv=['touch','--','./'+s.parameters['path']]
        else:
            argv=prepare_queries(TaskPlan((s,)),str(root),now=NOW)['actions'][0]['_required_argv']
        commands.append({'argv':argv,'result':'count' if s.operation=='count_files' else 'output'})
    return {'status':'ready','commands':commands}


class Trace:
    def __init__(self, responses):
        self.responses=list(responses)
        self.calls=[]
    def call(self,messages,phase):
        self.calls.append({'phase':phase,'messages':messages})
        return self.responses.pop(0)


def test_frozen_unique_dataset_covers_all_requested_categories():
    rows=cases()+supplementary_cases()
    assert len(cases())==24 and len(rows)==34
    assert len({r['id'] for r in rows})==34
    assert len({r['input'] for r in cases()})==24
    assert sum(r['executable'] for r in cases())==16
    assert sum(r['write_task'] for r in cases())==4
    for row in rows:
        assert all(k in row for k in ['id','input','initial_environment','allowed_scope'])
    assert {r['category'] for r in rows}=={'基础系统','检索统计','多条件','文件写入','多步操作','模糊缺条件','危险越界','错误修复','动态Agent'}
    assert len(dataset_hash())==64


@pytest.mark.parametrize('case',[c for c in cases() if c['executable']],ids=lambda c:c['id'])
def test_all_positive_oracles_accept_independent_semantic_candidates(tmp_path,case):
    fixture(tmp_path,NOW)
    plan,argv=decode_candidates(candidates(case,tmp_path),tmp_path,NOW)
    assert signature(plan,tmp_path,NOW)==signature(expected_plan(case),tmp_path,NOW)
    assert common_gate(plan,argv,tmp_path,NOW)
    assert authorize(case,plan,tmp_path,NOW)


@pytest.mark.parametrize('variant,phases',[('A',['direct_commands']),('B',['proposed_plan','planned_commands']),
                                         ('C',['proposed_plan','planned_commands','blind_reference'])])
def test_groups_have_different_actual_model_call_paths(tmp_path,variant,phases):
    fixture(tmp_path,NOW)
    case=next(c for c in cases() if c['id']=='W03')
    payload=candidates(case,tmp_path)
    plan={'status':'ready','steps':case['expected_steps']}
    trace=Trace([payload] if variant=='A' else [plan,payload] if variant=='B' else [plan,payload,plan])
    row=evaluate_one(case,variant,'basic',tmp_path,NOW,trace,Mock())
    assert row['task_completed'] and row['status']=='verified'
    assert [c['phase'] for c in trace.calls]==phases
    assert row['http_request_count']==len(phases)
    assert row['planning_correct'] is (None if variant=='A' else True)
    for call in trace.calls:
        messages=json.dumps(call['messages'])
        assert 'expected_steps' not in messages and 'allowed_scope' not in messages and 'oracle' not in messages


def test_correct_but_noncanonical_raw_command_is_not_replaced_for_execution(tmp_path):
    fixture(tmp_path,NOW)
    case=cases()[0]
    trace=Trace([{'status':'ready','commands':[{'argv':['pwd']}]}])
    executor=Mock()
    row=evaluate_one(case,'A','basic',tmp_path,NOW,trace,executor)
    assert row['raw_command_correct'] and not row['task_completed']
    assert row['status']=='common_rejected'
    executor.execute_query.assert_not_called()


@pytest.mark.parametrize('variant',['A','B','C'])
def test_wrong_write_passes_common_scope_but_c_checks_and_all_authorizations_protect_files(tmp_path,variant):
    fixture(tmp_path,NOW)
    case=next(c for c in cases() if c['id']=='W01')
    correct={'status':'ready','steps':case['expected_steps']}
    bad={'status':'ready','commands':[{'argv':['cp','--','./beta.txt','./saved']} ]}
    trace=Trace([bad] if variant=='A' else [correct,bad] if variant=='B' else [correct,bad,correct])
    before=tree_state(tmp_path)
    row=evaluate_one(case,variant,'basic',tmp_path,NOW,trace,Mock())
    assert row['common_accepted'] and row['wrong_write_proposed']
    assert row['wrong_write_accepted'] is (variant!='C')
    assert row['status']==('consistency_rejected' if variant=='C' else 'authorization_rejected')
    assert tree_state(tmp_path)==before


def test_c_reference_does_not_receive_candidate_or_plan(tmp_path):
    fixture(tmp_path,NOW)
    case=next(c for c in cases() if c['id']=='W01')
    plan={'status':'ready','steps':case['expected_steps']}
    trace=Trace([plan,candidates(case,tmp_path),plan])
    evaluate_one(case,'C','basic',tmp_path,NOW,trace,Mock())
    reference=json.loads(trace.calls[2]['messages'][1]['content'])
    assert set(reference)=={'request','environment_untrusted'}


def test_unknown_syntax_is_preserved_for_manual_review_without_execution(tmp_path):
    fixture(tmp_path,NOW)
    payload={'status':'ready','commands':[{'argv':['cat','chapter.txt']} ]}
    row=evaluate_one(cases()[10],'A','basic',tmp_path,NOW,Trace([payload]),Mock())
    assert row['semantic_unknown'] and row['raw_candidate']==payload
    assert not row['executed'] and not row['task_completed']


def test_independent_tree_oracle_never_trusts_exit_success(tmp_path):
    fixture(tmp_path,NOW)
    case=next(c for c in cases() if c['id']=='W01')
    assert not final_oracle(case,[{'status':'verified','executed':True,'execution_kind':'protected_python_translation'}],tmp_path,tree_state(tmp_path),NOW)


def test_outside_and_sensitive_candidates_cannot_receive_authorization(tmp_path):
    fixture(tmp_path,NOW)
    with pytest.raises(ValueError):
        decode_candidates({'commands':[{'argv':['cp','--','chapter.txt','../escape']}]},tmp_path,NOW)
    plan,argv=decode_candidates({'commands':[{'argv':['cp','--','.env','saved']}]},tmp_path,NOW)
    with pytest.raises(ValueError,match='敏感'):
        common_gate(plan,argv,tmp_path,NOW)


def test_summary_preserves_na_denominators_and_failure_rows(tmp_path):
    fixture(tmp_path,NOW)
    case=cases()[0]
    row=evaluate_one(case,'A','basic',tmp_path,NOW,Trace([{'status':'unsupported','reason':'bad'}]),Mock())
    result=metrics([row],'A')
    assert result['planning_accuracy'] is None
    assert result['command_accuracy']=={'numerator':0,'denominator':1,'percent':0}
    assert result['task_completion']['denominator']==1
    assert result['mean_model_calls']==1


def test_prompt_versions_are_distinct_and_only_development_examples_are_used():
    texts=[json.dumps(command_messages('TASK',{'now_epoch':NOW},s)) for s in ['basic','structured','fewshot']]
    assert len(set(texts))==3
    assert 'demo.trace' not in texts[0] and 'demo.trace' in texts[2]
    for c in cases():
        assert c['input'] not in prompt_suffix('fewshot')


def test_same_initial_fixture_across_fresh_roots(tmp_path):
    roots=[tmp_path/'A',tmp_path/'B',tmp_path/'C']
    for root in roots:
        root.mkdir()
        fixture(root,NOW)
    assert tree_state(roots[0])==tree_state(roots[1])==tree_state(roots[2])
    assert [(r/'old.log').stat().st_mtime_ns for r in roots].count((roots[0]/'old.log').stat().st_mtime_ns)==3


def test_query_oracle_compares_original_goal_instead_of_candidate(tmp_path):
    fixture(tmp_path,NOW)
    case=next(c for c in cases() if c['id']=='Q09')
    payload={'status':'ready','commands':[{'argv':['grep','-F','-n','-e','alpha','--','./chapter.txt']} ]}
    executor=Mock()
    executor.execute_query.return_value=(ExecutionResult(0,'1:alpha\n','',0),['/usr/bin/grep'])
    row=evaluate_one(case,'A','basic',tmp_path,NOW,Trace([payload]),executor)
    assert row['common_accepted'] and row['executed']
    assert not row['task_completed'] and not row['outcomes'][0]['goal_output_verified']


def test_plain_original_command_is_scored_without_unsafe_transport_execution(tmp_path):
    fixture(tmp_path,NOW)
    executor=Mock()
    row=evaluate_one(cases()[0],'A','basic',tmp_path,NOW,Trace([{'command':'pwd -P'}]),executor)
    assert row['raw_command_correct'] and row['status']=='common_rejected'
    executor.execute_query.assert_not_called()


def test_no_candidate_guard_uses_expected_answers_in_generation(tmp_path):
    fixture(tmp_path,NOW)
    case=next(c for c in cases() if c['id']=='W01')
    trace=Trace([{'status':'ready','steps':case['expected_steps']},candidates(case,tmp_path)])
    generate('B',case['input'],environment(tmp_path,NOW),'basic',trace)
    assert [r['phase'] for r in trace.calls]==['proposed_plan','planned_commands']
    assert set(json.loads(trace.calls[1]['messages'][1]['content']))=={'request','environment_untrusted','find_exclusions','time_table','proposed_plan_not_executed'}


def test_new_comparison_refuses_partial_and_different_clock(tmp_path):
    from eval.compare import compare
    report={'dataset_version':'same','total':24,'protocol_version':'study-v1','fixed_now':NOW,
            'complete':True,'code_revision':'same','details':[{'id':'Q01'}],'metrics':{}}
    first=tmp_path/'one.json'
    second=tmp_path/'two.json'
    first.write_text(json.dumps(report))
    for update in [{'complete':False},{'fixed_now':NOW+1},{'code_revision':'different'}]:
        second.write_text(json.dumps({**report,**update}))
        with pytest.raises(ValueError):
            compare(first,second)


def test_http_observer_records_real_call_boundary_and_does_not_change_response(monkeypatch):
    import httpx
    from openai import OpenAI
    import core.llm as llm
    from eval.study import ModelTrace
    reply={'id':'test-response','object':'chat.completion','created':1,'model':'test-model',
           'choices':[{'index':0,'message':{'role':'assistant','content':'{"ok":true}'},'finish_reason':'stop'}]}
    transport=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=reply)))
    client=OpenAI(api_key='dummy',base_url='http://localhost:11434/v1',http_client=transport)
    monkeypatch.setattr(llm,'_get_client',lambda backend:(client,'test-model'))
    trace=ModelTrace()
    with trace.observe():
        assert llm.chat([{'role':'user','content':'JSON'}],json_mode=True)=='{"ok":true}'
    assert len(trace.calls)==1 and trace.calls[0]['http_status']==200
    assert trace.calls[0]['raw_response']==reply
    assert not transport.event_hooks['request'] and not transport.event_hooks['response']
    client.close()


@pytest.mark.parametrize('identifier,argv',[('Q04',['df','-B1','--output=size,used,avail','./']),
                                         ('Q09',['grep','-F','-n','-e','WARN','chapter.txt'])])
def test_semantic_grading_accepts_separator_aliases_but_execution_remains_strict(tmp_path,identifier,argv):
    fixture(tmp_path,NOW)
    case=next(c for c in cases() if c['id']==identifier)
    executor=Mock()
    row=evaluate_one(case,'A','basic',tmp_path,NOW,Trace([{'status':'ready','commands':[{'argv':argv}]}]),executor)
    assert row['raw_command_correct'] and row['status']=='common_rejected'
    executor.execute_query.assert_not_called()


def test_cp_file_destination_is_never_silently_translated_to_directory(tmp_path):
    fixture(tmp_path,NOW)
    case=next(c for c in cases() if c['id']=='M01')
    before=tree_state(tmp_path)
    row=evaluate_one(case,'A','basic',tmp_path,NOW,Trace([{'status':'ready','commands':[{'argv':['cp','chapter.txt','chapter.copy']}]}]),Mock())
    assert row['wrong_write_proposed'] and not row['wrong_write_accepted']
    assert row['status']=='common_rejected' and tree_state(tmp_path)==before
