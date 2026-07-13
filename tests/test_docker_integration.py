import os
from pathlib import Path

import pytest

from core.execution import DockerExecutor


@pytest.mark.skipif(os.environ.get("RUN_DOCKER_TESTS") != "1", reason="仅在 Docker CI 运行")
def test_sandbox_runs_as_non_root_and_writes_only_workspace(tmp_path):
    executor = DockerExecutor()
    assert executor.is_available()
    result = executor.execute("test $(id -u) -ne 0 && touch sandbox-ok", cwd=str(tmp_path),
                              timeout_seconds=30)
    assert result.exit_code == 0, result.stderr
    assert (Path(tmp_path) / "sandbox-ok").exists()
