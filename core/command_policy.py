"""实验黑名单：扩展只读命令在只读沙箱运行，写命令仍绑定已授权文件清单。"""
import json
import os
from pathlib import Path
import re
import time

from .bash_queries import _sensitive
from .llm import chat
from .operations import workspace_path


DENIED = set('sudo su doas rm rmdir dd shred mkfs mount umount shutdown reboot halt poweroff kill killall '
             'pkill chmod chown chgrp chroot nsenter unshare bwrap prlimit env xargs parallel '
             'bash sh dash zsh fish python python3 perl ruby node php lua busybox git '
             'curl wget ssh scp rsync nc ncat socat telnet ftp apt apt-get dpkg pip pip3 '
             'systemctl service crontab at sed awk gawk vim vi ex ed tee truncate '
             'cp mv mkdir touch install ln tar zip unzip'.split())


def review_query(argv, cwd):
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 128 or
            any(not isinstance(v, str) or '\0' in v or '\n' in v or '\r' in v for v in argv) or
            sum(len(v) for v in argv) > 16384 or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_+-]*', argv[0])):
        raise ValueError('黑名单模式只接受单条系统命令的参数数组')
    if argv[0] in DENIED or argv[0].startswith(('mkfs', 'python', 'pip')):
        raise ValueError('黑名单拒绝该命令；文件写入必须使用可校验的文件操作流程')
    if any(v in {'-delete', '-exec', '-execdir', '-ok', '-okdir', '|', '&&', ';', '>', '>>'} or
           v.startswith(('-fprint', '-fprintf', '-fls')) for v in argv[1:]):
        raise ValueError('黑名单拒绝执行、删除或重定向参数')
    for argument in argv[1:]:
        value = argument.split('=', 1)[-1]
        if value.startswith(('/', '~')) or '..' in Path(value).parts:
            raise ValueError('黑名单模式的命令路径必须位于工作目录内，使用相对路径')
    if not Path(cwd).is_dir():
        raise ValueError('工作目录不存在')
    return list(argv)


def generate_candidate(user_input, cwd, clarifications, backend=None):
    system = '''你是实验黑名单模式的 Linux 助手。只输出 JSON，不输出 Markdown。
明确只读查询输出 {"status":"ready","argv":["date","-u","+%Y"],"explanation":"说明"}。
可以使用系统工具，不限于白名单已有的操作；仅单条参数数组，不支持管道、Shell、脚本或网络。
复制、移动、重命名、创建文件或目录、整理、回收和恢复必须输出 {"status":"protected"}，后续由受保护流程解析条件及预览，不生成这些操作的命令。
真正缺少信息输出 {"status":"need_clarification","clarification":"具体问题"}。
危险或不支持的写入、删除、提权、安装及网络输出 {"status":"unsupported","reason":"具体原因"}。
路径使用工作区内相对路径。命令在只读、无网络的隔离 Linux 环境中运行；系统信息反映沙箱视图，不推断物理硬盘数量。'''
    raw = chat([{'role': 'system', 'content': system}, {'role': 'user', 'content': json.dumps(
        {'request': user_input, 'cwd': cwd, 'clarifications': clarifications}, ensure_ascii=False)}],
        backend=backend, json_mode=True)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('黑名单候选必须是 JSON 对象')
    allowed = {'ready': {'status','argv','explanation'}, 'protected': {'status'},
               'need_clarification': {'status','clarification'}, 'unsupported': {'status','reason'}}
    if set(value) != allowed.get(value.get('status')):
        raise ValueError('黑名单候选字段不完整或包含未知字段')
    return value, raw


def sandbox_argv(argv, cwd, *, writable=False, excluded=()):
    if os.name != 'posix' or any(not Path(v).is_file() for v in ('/usr/bin/bwrap','/usr/bin/prlimit')):
        raise RuntimeError('黑名单模式需要 Linux 的 bwrap 和 prlimit；不会降级到主机执行')
    root = Path(cwd).resolve(strict=True)
    if not root.is_dir() or _sensitive(root, root):
        raise ValueError('黑名单工作目录无效或属于敏感路径')
    masks, pending, count = [], [root], 0
    deadline = time.monotonic() + 5
    excluded = [Path(value).resolve() for value in excluded]
    while pending:
        for path in pending.pop().iterdir():
            count += 1
            if count > 10000 or time.monotonic() > deadline:
                raise ValueError('黑名单工作区检查超出预算，请缩小工作目录')
            if path.is_symlink():
                raise ValueError('黑名单工作目录包含符号链接，请选择不含链接的目录')
            if _sensitive(path, root) or path in excluded:
                masks.append((path, path.is_dir()))
            elif path.is_dir():
                pending.append(path)
            elif not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError('黑名单工作目录不接受特殊文件或硬链接')
    binary = Path('/usr/bin') / argv[0]
    if not binary.is_file():
        binary = Path('/bin') / argv[0]
    if not binary.is_file():
        raise RuntimeError('系统未安装对应命令')
    command = ['/usr/bin/prlimit', '--as=536870912', '--cpu=20', '--nproc=256', '--nofile=64',
               '--fsize=' + str(1024**3 if writable else 1024**2), '--', '/usr/bin/bwrap',
               '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL', '--clearenv',
               '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib']
    if Path('/lib64').exists():
        command += ['--ro-bind', '/lib64', '/lib64']
    command += ['--symlink','usr/bin','/bin','--proc','/proc','--dev','/dev','--tmpfs','/tmp',
                '--bind' if writable else '--ro-bind',str(root),'/workspace']
    for path, directory in masks:
        target = '/workspace/' + path.relative_to(root).as_posix()
        command += ['--tmpfs',target] if directory else ['--ro-bind','/dev/null',target]
    return command + ['--chdir','/workspace','--setenv','PATH','/usr/bin:/bin','--setenv','HOME','/tmp',
                      '--setenv','LANG','C.UTF-8','--setenv','LC_ALL','C.UTF-8','--',str(binary),*argv[1:]]


def native_argv(operation, item):
    destination = './' + item['destination']
    if operation == 'create_directory':
        return ['mkdir','--',destination]
    if operation == 'copy_files':
        return ['cp','--update=none','--no-target-directory','--recursive','--no-dereference',
                '--','./' + item['source'],destination]
    if operation in {'move_files','rename'}:
        return ['mv','--update=none','--no-copy','--no-target-directory','--','./' + item['source'],destination]
    return None


def prepare_native(prepared):
    for action in prepared['actions']:
        for item in action['items']:
            argv = native_argv(action['operation'], item)
            if argv is not None:
                for key in ('source','destination'):
                    if key in item:
                        path = workspace_path(Path(prepared['cwd']), item[key])
                        if _sensitive(path, Path(prepared['cwd'])):
                            raise ValueError('黑名单写命令不访问敏感路径')
                sandbox_argv(argv, prepared['cwd'], writable=True, excluded=prepared['excluded'])
                item['native_argv'] = argv
                action['execution_kind'] = 'blacklist_native'
    return prepared
