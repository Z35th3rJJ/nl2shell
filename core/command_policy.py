"""实验黑名单：扩展只读命令在只读沙箱运行，写命令仍绑定已授权文件清单。"""
import json
import os
from pathlib import Path
import re
import time

from .bash_queries import _sensitive
from .bash_queries import SENSITIVE
import fnmatch
from .llm import chat
from .operations import TRASH, workspace_path
from .system_view import SYSTEM_VIEW


DENIED = set('sudo su doas rm rmdir dd shred mkfs umount shutdown reboot halt poweroff kill killall '
             'pkill chmod chown chgrp chroot nsenter unshare bwrap prlimit env xargs parallel '
             'bash sh dash zsh fish python python3 perl ruby node php lua busybox git '
             'curl wget ssh scp rsync nc ncat socat telnet ftp apt apt-get dpkg pip pip3 '
             'systemctl service crontab at vim vi ex ed tee truncate '
             'cp mv mkdir touch install ln tar zip unzip'.split())


def _bind_view(argv, options, path):
    values = []
    for index, value in enumerate(argv[1:], 1):
        if value in options:
            if index+1 >= len(argv):
                raise ValueError('系统查询缺少元数据来源')
            values.append(argv[index+1])
        elif any(value.startswith(option+'=') for option in options):
            values.append(value.split('=',1)[1])
    if values:
        if values != [path]:
            raise ValueError('系统查询只能绑定当前 Linux 的只读元数据，不能切换到其他根目录')
        return argv
    index = argv.index('--') if '--' in argv else len(argv)
    return argv[:index]+[options[0],path]+argv[index:]


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
    if argv[0] == 'mount' and argv[1:] not in ([], ['-l'], ['--show-labels']):
        raise ValueError('允许列出挂载信息，不允许挂载、重新挂载或修改挂载状态')
    if argv[0] == 'sed' and ('--sandbox' not in argv or any(
            value.startswith(('-i','--in-place')) or value in {'-f','--file'} for value in argv[1:])):
        raise ValueError('sed 过滤需使用 --sandbox，不能修改文件或加载外部脚本')
    if argv[0] in {'awk','gawk','mawk'} and any(
            value.startswith(('-f','--file','--include','--load','-l','-W')) or re.search(r'\bsystem\s*\(|[|>]',value)
            for value in argv[1:]):
        raise ValueError('允许 awk 文本过滤，不允许程序执行、外部脚本或文件重定向')
    for argument in argv[1:]:
        value = argument.split('=', 1)[-1]
        virtual_system = value.startswith('/') and any(
            value == prefix or value.startswith(prefix+'/') for prefix in ('/proc','/sys',SYSTEM_VIEW))
        mount_selector = value == '/' and argv[0] == 'findmnt'
        if '..' in Path(value).parts or (value.startswith(('/', '~')) and not virtual_system and not mount_selector):
            raise ValueError('黑名单模式的命令路径必须位于工作目录内，使用相对路径')
    if not Path(cwd).is_dir():
        raise ValueError('工作目录不存在')
    argv = list(argv)
    if argv[0] == 'lsblk':
        argv = _bind_view(argv, ('--sysroot',), SYSTEM_VIEW)
    elif argv[0] == 'findmnt':
        if any(value.startswith(('-N','--task')) for value in argv):
            raise ValueError('不切换到其他进程的挂载命名空间')
        argv = _bind_view(argv, ('--tab-file','-F'), SYSTEM_VIEW+'/proc/self/mountinfo')
    return argv


def generate_candidate(user_input, cwd, clarifications, backend=None):
    system = '''你是实验黑名单模式的 Linux 助手。只输出 JSON，不输出 Markdown。
明确只读查询输出 {"status":"ready","argv":["date","-u","+%Y"],"explanation":"说明"}。
可以使用系统工具，不限于白名单已有的操作；仅单条参数数组，不支持管道、Shell、脚本或网络。
目录大小 du、文件类型 file、校验值 sha256sum、日期 date 都是允许的只读查询，不要因旧操作表没有这些类型而拒绝。
系统挂载、块设备、CPU、内存等元数据也是允许的低风险查询，不限固定操作表。未指定其他环境时，默认查询当前运行的 Linux/WSL，不因“硬盘”的措辞反复追问；说明可见设备不等于主机物理硬盘。
区分询问现状和修改状态：“我们挂载了几个硬盘”“现在有哪些挂载点”是在查询现状，应输出 lsblk 或 findmnt 的 ready 参数数组；“挂载一个硬盘”“重新挂载”是在修改状态，必须拒绝。不要把疑问句中的“挂载了”当成执行挂载操作。
挂载与设备可用 findmnt、lsblk、mount（仅列出信息）。程序会绑定当前 Linux 的只读元数据快照，不需要挂载磁盘或读取 /dev 设备。
列出挂载文件系统可直接输出 {"status":"ready","argv":["findmnt","--output","SOURCE,TARGET,FSTYPE"],"explanation":"列出当前 Linux 的挂载信息"}。findmnt 的类型列叫 FSTYPE，不是 TYPE，也没有 MOUNTPOINT 列。用户要求用 mount 列出信息时输出 {"status":"ready","argv":["mount"],"explanation":"只列出挂载信息，不修改挂载状态"}。
可以使用 /proc、/sys 中的只读系统信息；支持 awk 单行文本过滤及 sed --sandbox。不支持执行脚本、system()、重定向写入或修改系统状态。
argv 是直接传给程序的参数数组，不能加入 Shell 的包裹引号。例如 awk 过滤用 ["awk","{print $1}","sample.txt"]；sed 前两行用 ["sed","--sandbox","-n","1,2p","sample.txt"]，不要把单引号字符放进过滤表达式。
例如计算 sample.txt 的 SHA-256 校验值：{"status":"ready","argv":["sha256sum","--","./sample.txt"],"explanation":"计算文件校验值"}。路径必须使用实际用户文件。
复制、移动、重命名、创建文件或目录、整理、回收和恢复必须输出 {"status":"protected"}，后续由受保护流程解析条件及预览，不生成这些操作的命令。
真正缺少信息输出 {"status":"need_clarification","clarification":"具体问题"}。
危险或不支持的写入、删除、提权、安装及网络输出 {"status":"unsupported","reason":"具体原因"}。
业务文件使用工作区内相对路径。命令仍在只读、无网络的隔离 Linux 环境中运行；lsblk/findmnt 的输入由程序绑定到当前 Linux 元数据，不推断主机物理硬盘数量。'''
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


def sandbox_argv(argv, cwd, *, writable=False, excluded=(), system_root=None, internal_entry=None):
    if os.name != 'posix' or any(not Path(v).is_file() for v in ('/usr/bin/bwrap','/usr/bin/prlimit')):
        raise RuntimeError('黑名单模式需要 Linux 的 bwrap 和 prlimit；不会降级到主机执行')
    root = Path(cwd).resolve(strict=True)
    if not root.is_dir() or _sensitive(root, root):
        raise ValueError('黑名单工作目录无效或属于敏感路径')
    masks, pending, count = [], [root], 0
    deadline = time.monotonic() + 5
    excluded = [Path(value).resolve() for value in excluded]
    entry = None
    if internal_entry is not None:
        if not writable or not re.fullmatch(r'[0-9a-f]{32}', internal_entry):
            raise ValueError('回收区绑定无效')
        entry = workspace_path(root, f'{TRASH}/{internal_entry}', internal=True)
        if any(value == entry or value in entry.parents or entry in value.parents for value in excluded):
            raise ValueError('回收区与审计路径重叠，未执行')
    while pending:
        for path in pending.pop().iterdir():
            count += 1
            if count > 10000 or time.monotonic() > deadline:
                raise ValueError('黑名单工作区检查超出预算，请缩小工作目录')
            if path.is_symlink():
                raise ValueError('黑名单工作目录包含符号链接，请选择不含链接的目录')
            if entry is not None and path == root/TRASH:
                if not path.is_dir():
                    raise ValueError('回收区路径不是目录')
                pending.append(path)
                continue
            within_entry = entry is not None and (path == entry or entry in path.parents)
            internal_sensitive = within_entry and any(
                fnmatch.fnmatchcase(part.lower(),pattern)
                for part in path.relative_to(entry).parts for pattern in SENSITIVE)
            if ((not within_entry and _sensitive(path, root)) or internal_sensitive or path in excluded):
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
    binary = binary.resolve(strict=True)
    if not binary.is_relative_to(Path('/usr')):
        raise ValueError('系统程序解析到运行库目录外，未执行')
    if not writable and (binary.name in DENIED or binary.name.startswith(('mkfs','python','pip'))):
        raise ValueError('系统程序别名指向黑名单命令，未执行')
    if not writable and binary.name != argv[0]:
        review_query([binary.name,*argv[1:]],cwd)
    command = ['/usr/bin/prlimit', '--as=536870912', '--cpu=20', '--nproc=256', '--nofile=64',
               '--fsize=' + str(1024**3 if writable else 1024**2), '--', '/usr/bin/bwrap',
               '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL', '--clearenv',
               '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib']
    if Path('/lib64').exists():
        command += ['--ro-bind', '/lib64', '/lib64']
    proc = ['--proc','/proc'] if system_root is None else ['--ro-bind',str(system_root/'proc'),'/proc']
    command += ['--symlink','usr/bin','/bin',*proc,'--dev','/dev','--tmpfs','/tmp',
                '--bind' if writable else '--ro-bind',str(root),'/workspace']
    if not writable:
        command += ['--ro-bind','/sys','/sys','--remount-ro','/proc']
    if system_root is not None:
        command += ['--ro-bind',str(system_root),SYSTEM_VIEW,'--ro-bind','/sys',SYSTEM_VIEW+'/sys',
                    '--ro-bind',str(system_root/'proc/mounts'),'/etc/mtab']
    for path, directory in masks:
        target = '/workspace/' + path.relative_to(root).as_posix()
        command += ['--tmpfs',target] if directory else ['--ro-bind','/dev/null',target]
    return command + ['--chdir','/workspace','--setenv','PATH','/usr/bin:/bin','--setenv','HOME','/tmp',
                      '--setenv','LANG','C.UTF-8','--setenv','LC_ALL','C.UTF-8','--',str(binary),*argv[1:]]


def native_argv(operation, item):
    destination = './' + item['destination']
    if operation == 'create_file':
        return ['dd','if=/dev/null','of='+destination,'conv=excl','status=none']
    if operation == 'create_directory':
        return ['mkdir','--',destination]
    if operation == 'copy_files':
        return ['cp','--update=none','--no-target-directory','--recursive','--no-dereference',
                '--','./' + item['source'],destination]
    if operation in {'move_files','rename','organize_files','trash','restore'}:
        return ['mv','--update=none','--no-copy','--no-target-directory','--','./' + item['source'],destination]
    return None


def native_parent_argv(item):
    parent = Path(item['destination']).parent.as_posix()
    return None if parent == '.' else ['mkdir','--parents','--','./'+parent]


def native_internal_entry(operation, item):
    if operation not in {'trash','restore'}:
        return None
    identifier = item.get('trash_id','')
    field = 'destination' if operation == 'trash' else 'source'
    if (not isinstance(identifier,str) or not re.fullmatch(r'[0-9a-f]{32}',identifier)
            or item.get(field) != f'{TRASH}/{identifier}/data'):
        raise ValueError('回收文件与授权编号不匹配')
    return identifier


def prepare_native(prepared):
    for action in prepared['actions']:
        for item in action['items']:
            argv = native_argv(action['operation'], item)
            if argv is not None:
                entry = native_internal_entry(action['operation'],item)
                for key in ('source','destination'):
                    if key in item:
                        internal = (action['operation'],key) in {('trash','destination'),('restore','source')}
                        path = workspace_path(Path(prepared['cwd']), item[key],internal=internal)
                        if not internal and _sensitive(path, Path(prepared['cwd'])):
                            raise ValueError('黑名单写命令不访问敏感路径')
                sandbox_argv(argv, prepared['cwd'], writable=True, excluded=prepared['excluded'],internal_entry=entry)
                item['native_argv'] = argv
                if action['operation'] != 'trash' and not workspace_path(
                        Path(prepared['cwd']),item['destination']).parent.exists():
                    item['parent_argv'] = native_parent_argv(item)
                action['execution_kind'] = 'blacklist_native'
    return prepared
