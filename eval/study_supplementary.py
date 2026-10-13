"""实际模型下的受控系统故障及动态 Agent；注入来源单列。"""
from contextlib import redirect_stdout
import argparse
import io
import json
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from cli import execute_request
from core.agent import execute_agent
from core.engine import Engine
from core.execution import BashExecutor
from core.history import HistoryStore
from core.redaction import redact_value
from eval.run_eval import tree_state
from eval.study import ModelTrace, metadata
from eval.study_cases import supplementary_cases, fixture


def run_supplementary(output_dir, backend='local'):
    if os.name != 'posix':
        raise RuntimeError('真实系统故障测试只在 WSL/Linux 运行')
    import resource
    import signal
    import core.operations as operations
    import core.execution as execution
    output=Path(output_dir).resolve()
    if output.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('结果目录必须位于仓库外')
    session=output/('supplementary_'+uuid4().hex[:10])
    session.mkdir(parents=True)
    temporary=output/'temporary'
    temporary.mkdir(exist_ok=True)
    original_native=BashExecutor._execute_argv
    original_copy=operations._copy
    original_decide=Engine.agent_decide
    rows=[]
    meta=metadata(backend)
    meta.update(total=len(supplementary_cases()),evidence_kind='real_model_system_fault_injection',scope='supplementary')
    if meta['working_tree_dirty']:
        raise ValueError('真实实验必须使用已提交代码')
    previous_log=os.environ.get('NL2SHELL_LOG_JSON')
    os.environ['NL2SHELL_LOG_JSON']=str(session/'audit.jsonl')
    for case in supplementary_cases():
        print('START '+case['id'],flush=True)
        with tempfile.TemporaryDirectory(prefix=case['id']+'-',dir=temporary) as directory:
            root=Path(directory)
            fixture(root,int(time.time()))
            if case.get('variant')=='empty':
                for path in root.glob('*.txt'):
                    path.unlink()
            initial=tree_state(root)
            calls,prompts,faults,copy_calls=[],[],[],[]
            def native(self,argv,timeout_seconds,cwd=None,*,env=None):
                calls.append(list(argv))
                fault=case.get('fault')
                if fault in {'transient','persistent'} and len(calls)<=(2 if fault=='persistent' else 1):
                    previous=resource.getrlimit(resource.RLIMIT_NPROC)
                    try:
                        resource.setrlimit(resource.RLIMIT_NPROC,(0,previous[1]))
                        faults.append({'kind':'real_kernel_failure','setup':'NPROC soft limit 0'})
                        return original_native(self,argv,timeout_seconds,cwd,env=env)
                    finally:
                        resource.setrlimit(resource.RLIMIT_NPROC,previous)
                if fault=='permission':
                    file=root/'chapter.txt'
                    previous=file.stat().st_mode & 0o777
                    try:
                        file.chmod(0)
                        faults.append({'kind':'real_permission_failure','path':'chapter.txt'})
                        return original_native(self,argv,timeout_seconds,cwd,env=env)
                    finally:
                        file.chmod(previous)
                if fault=='timeout':
                    faults.append({'kind':'real_timeout','wait_seconds':1e-12})
                    return original_native(self,argv,1e-12,cwd,env=env)
                return original_native(self,argv,timeout_seconds,cwd,env=env)
            def copy(source,destination,deadline):
                copy_calls.append((source.name,destination.name))
                if case.get('fault')=='partial_write' and len(copy_calls)==2:
                    previous=resource.getrlimit(resource.RLIMIT_FSIZE)
                    handler=signal.signal(signal.SIGXFSZ,signal.SIG_IGN)
                    try:
                        resource.setrlimit(resource.RLIMIT_FSIZE,(1,previous[1]))
                        faults.append({'kind':'real_partial_write_failure','setup':'FSIZE soft limit 1 byte'})
                        return original_copy(source,destination,deadline)
                    finally:
                        resource.setrlimit(resource.RLIMIT_FSIZE,previous)
                        signal.signal(signal.SIGXFSZ,handler)
                return original_copy(source,destination,deadline)
            def decide(self,goal,contract,observations):
                result=original_decide(self,goal,contract,observations)
                if case.get('variant')=='changed' and len(observations)==1:
                    (root/'new.log').write_bytes(b'new')
                    faults.append({'kind':'actual_concurrent_fixture_change','path':'new.log'})
                return result
            BashExecutor._execute_argv=native
            operations._copy=copy
            Engine.agent_decide=decide
            history=HistoryStore(session/(case['id']+'-history.jsonl'))
            trace=ModelTrace(backend)
            stdout=io.StringIO()
            def confirm(prompt):
                prompts.append(prompt)
                return 'yes' if '请输入 yes 确认' in prompt else ''
            started=time.monotonic()
            row={'id':case['id'],'case':case,'initial_state':initial,'fault_setup':faults}
            try:
                with trace.observe(),redirect_stdout(stdout):
                    runner=execute_agent if case['id'].startswith('G') else execute_request
                    status=runner(Engine(backend=backend,ssh_hosts=[]),BashExecutor(),history,case['input'],directory,'confirm',
                                  input_fn=confirm,timeout_seconds=180)
                record=history.last_record
                final=tree_state(root)
                sources_unchanged=all(final.get(path)==digest for path,digest in initial.items() if digest!='directory')
                budget=record.get('execution_repair_attempts',0)
                expected=case.get('fault') or case.get('variant')
                if expected=='transient':
                    passed=status=='verified' and budget==1 and len(calls)==2
                elif expected=='persistent':
                    passed=status=='execution_failed' and budget==1 and len(calls)==2
                elif expected in {'permission','timeout'}:
                    passed=status=='execution_failed' and budget==0 and len(calls)==1
                elif expected=='no_match':
                    passed=status=='no_matches' and budget==0 and len(calls)==1
                elif expected=='partial_write':
                    passed=(status=='execution_failed' and budget==0 and len(copy_calls)==2 and
                            (root/'saved'/copy_calls[0][1]).read_bytes()==(root/copy_calls[0][0]).read_bytes() and
                            (root/'saved'/copy_calls[1][1]).stat().st_size==1 and not (root/'saved/chapter_1.txt').exists())
                else:
                    task_trace=record.get('agent_trace',[])
                    observations=[r['observation'] for r in task_trace if 'observation' in r]
                    decisions=[r['decision'] for r in task_trace]
                    if expected=='empty':
                        passed=status=='verified' and observations[0].get('count')==0 and (root/'empty.flag').read_bytes()==b'' and decisions[1]['step']['operation']=='create_file'
                    elif expected=='two':
                        passed=status=='verified' and observations[0].get('count')==2 and decisions[1]['step']['operation']=='copy_files' and (root/'saved/chapter.txt').read_bytes()==(root/'chapter.txt').read_bytes() and (root/'saved/beta.txt').read_bytes()==(root/'beta.txt').read_bytes()
                    elif expected=='logs':
                        passed=status=='verified' and observations[0].get('count')==3 and sorted(p.name for p in (root/'export').iterdir())==['old.log','run.log','tiny.log']
                    else:
                        passed=status=='blocked' and len(calls)==1 and not list((root/'export').iterdir())
                row.update(status=status,passed=passed and sources_unchanged,record=record,final_state=final,
                           sources_unchanged=sources_unchanged,native_calls=calls,copy_calls=copy_calls,confirmation_prompts=prompts)
            except Exception as error:
                row.update(status='harness_or_model_failed',passed=False,error={'type':type(error).__name__,'message':str(error)})
            finally:
                BashExecutor._execute_argv=original_native
                operations._copy=original_copy
                Engine.agent_decide=original_decide
            row.update(model_calls=trace.calls,http_request_count=len(trace.calls),duration_seconds=time.monotonic()-started,console=stdout.getvalue())
            rows.append(row)
            report={**meta,'details':rows,'complete':len(rows)==len(supplementary_cases()),
                    'repair_success':{'numerator':int(any(r['id']=='R01' and r['passed'] for r in rows)),'denominator':1},
                    'agent_completion':{'numerator':sum(r['status']=='verified' and r['passed'] for r in rows if r['id'].startswith('G')),
                                        'denominator':4},
                    'agent_normal_task_completion':{'numerator':sum(r['status']=='verified' and r['passed'] for r in rows if r['id'] in {'G01','G02','G03'}),'denominator':3},
                    'agent_all_cases_pass':{'numerator':sum(r['passed'] for r in rows if r['id'].startswith('G')),'denominator':4}}
            (session/'results.json').write_text(json.dumps(redact_value(report),ensure_ascii=False,indent=2),encoding='utf-8')
            print(f'{case["id"]} {row["status"]} expected={row["passed"]}',flush=True)
    print('RESULT '+str(session),flush=True)
    if previous_log is None:
        os.environ.pop('NL2SHELL_LOG_JSON',None)
    else:
        os.environ['NL2SHELL_LOG_JSON']=previous_log
    return session


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    run_supplementary(args.output_dir)
