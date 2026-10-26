"""向沙箱提供当前 Linux 的只读内核元数据，不暴露磁盘设备或用户目录。"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile


SYSTEM_VIEW = '/run/nl2shell'
METADATA_LIMIT = 256 * 1024


@contextmanager
def system_view():
    with tempfile.TemporaryDirectory(prefix='nl2shell-system-view-') as directory:
        root = Path(directory)
        (root/'proc/self').mkdir(parents=True)
        (root/'sys').mkdir()
        content = {}
        for name in ('self/mountinfo', 'mounts', 'swaps', 'partitions'):
            with (Path('/proc')/name).open('rb') as source:
                data = source.read(METADATA_LIMIT + 1)
            if len(data) > METADATA_LIMIT:
                raise ValueError('Linux 系统元数据超过处理上限，未执行命令')
            (root/'proc'/name).write_bytes(data)
            content[name] = data
        mounted = {line.split()[2] for line in content['self/mountinfo'].decode('utf-8').splitlines()}
        devices = []
        for index, path in enumerate(Path('/sys/class/block').iterdir()):
            if index >= 1024:
                raise ValueError('块设备元数据超过处理上限')
            with (path/'dev').open() as source:
                identifier = source.read(64).strip()
            devices.append({'name':path.name, 'device_id':identifier,
                            'partition':(path/'partition').exists(), 'mounted':identifier in mounted})
        devices.sort(key=lambda item:item['name'])
        summary = {'scope':'当前 Linux/WSL 环境，不代表主机物理硬盘',
                   'captured_at':datetime.now(timezone.utc).isoformat(),
                   'block_device_count':len(devices),
                   'mounted_block_device_count':sum(item['mounted'] for item in devices),
                   'mount_record_count':len(content['self/mountinfo'].splitlines()), 'block_devices':devices,
                   'mountinfo_sha256':hashlib.sha256(content['self/mountinfo']).hexdigest()}
        yield root, summary
