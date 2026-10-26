"""普通任务的耗时与运行环境；不保存消息、凭据或完整环境变量。"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import os
from pathlib import Path
import platform
import subprocess
import time


current = ContextVar('task_measurements', default=None)


def runtime_environment():
    result = {'python':platform.python_version(), 'system':platform.system(),
              'release':platform.release(), 'architecture':platform.machine(),
              'logical_cpu_count':os.cpu_count(), 'cpu_model':None, 'memory_bytes':None,
              'git_sha':None}
    if platform.system() == 'Linux':
        try:
            with Path('/proc/cpuinfo').open() as source:
                lines = source.read(65536).splitlines()
            result['cpu_model'] = next((line.split(':',1)[1].strip() for line in lines
                                        if line.startswith(('model name','Hardware'))),None)
            result['memory_bytes'] = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
        except (OSError, ValueError):
            pass
    try:
        process = subprocess.run(['git','rev-parse','HEAD'], cwd=Path(__file__).resolve().parents[1],
                                 capture_output=True, text=True, timeout=2)
        value = process.stdout.strip()
        if process.returncode == 0 and len(value) == 40 and all(c in '0123456789abcdef' for c in value):
            result['git_sha'] = value
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


@contextmanager
def measured(kind, **fields):
    state = current.get()
    row = {'kind':kind, **fields, 'status':'returned'}
    started = time.monotonic()
    try:
        yield row
    except BaseException as error:
        row.update(status='raised', error_type=type(error).__name__)
        raise
    finally:
        if state is not None:
            row['duration_seconds'] = time.monotonic() - started
            state['calls'].append(row)


def snapshot():
    state = current.get()
    if state is None:
        return None
    elapsed = time.monotonic() - state['started']
    calls = list(state['calls'])
    seconds = {kind:sum(row['duration_seconds'] for row in calls if row['kind'] == kind)
               for kind in ('model','command','input')}
    return {'schema_version':1, 'runtime':state['environment'],
            'elapsed_seconds':elapsed, 'user_input_seconds':seconds['input'],
            'active_seconds':max(0,elapsed-seconds['input']),
            'model_seconds':seconds['model'], 'command_seconds':seconds['command'],
            'model_call_count':sum(row['kind']=='model' for row in calls), 'calls':calls}


def measure_task(function):
    signature = inspect.signature(function)
    @wraps(function)
    def wrapped(*args, **kwargs):
        if current.get() is not None:
            return function(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        original_input = bound.arguments['input_fn']
        def timed_input(prompt):
            with measured('input'):
                return original_input(prompt)
        state = {'started':time.monotonic(), 'calls':[], 'environment':runtime_environment()}
        token = current.set(state)
        bound.arguments['input_fn'] = timed_input
        try:
            return function(*bound.args, **bound.kwargs)
        finally:
            current.reset(token)
    return wrapped


def measure_command(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with measured('command') as row:
            result = function(*args, **kwargs)
            row.update(exit_code=result.exit_code, timed_out=result.timed_out)
            return result
    return wrapped
