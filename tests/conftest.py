"""测试诊断写入临时应用目录；真实网络模型调用一律禁止。"""
import httpx
import pytest


@pytest.fixture(autouse=True)
def diagnostic_storage_and_offline(monkeypatch, tmp_path_factory):
    root = tmp_path_factory.mktemp("application-data") / "diagnostics"
    monkeypatch.setattr("core.runtime_diagnostics.default_diagnostics_root", lambda: root)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
                        lambda *args, **kwargs: pytest.fail("自动化测试禁止真实网络请求"))
    return root
