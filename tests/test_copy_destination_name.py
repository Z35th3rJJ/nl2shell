import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from cli import execute_request
from core.engine import Engine
from core.history import HistoryStore
from core.operations import execute_action, prepare_plan, preview_details
from core.task_plan import TaskPlan, TaskStep, parse_operation_plan


def copy_plan(**parameters):
    return parse_operation_plan(json.dumps({"status": "ready", "steps": [
        {"operation": "copy_files", "parameters": parameters}]}))


@pytest.fixture(autouse=True)
def no_shell(monkeypatch):
    launch = Mock(side_effect=AssertionError("复制测试不得启动 Shell 或候选命令"))
    monkeypatch.setattr("subprocess.Popen", launch)
    yield
    launch.assert_not_called()


@pytest.mark.parametrize("user_input,parameters,answer,target", [
    ("将 copy_me.txt 复制为 copy_test.txt", {"destination_name": "copy_test.txt"}, None, "copy_test.txt"),
    ("将 copy_me.txt 复制到 backup 并命名为 copy_test.txt",
     {"destination": "backup", "destination_name": "copy_test.txt"}, None, "backup/copy_test.txt"),
    ("将 copy_me.txt 复制到 backup", {"destination": "backup"}, None, "backup/copy_me.txt"),
    ("复制 copy_me.txt", {"destination": ".", "destination_name": "copy_test.txt"},
     "复制到当前目录，目标文件名为 copy_test.txt", "copy_test.txt"),
])
def test_copy_destination_name_complete_flow(tmp_path, monkeypatch, capsys, user_input, parameters, answer, target):
    original = b"unchanged source\x00binary content\n"
    source = tmp_path / "copy_me.txt"
    source.write_bytes(original)
    raw = json.dumps({"status": "ready", "steps": [{"operation": "copy_files", "parameters": {
        "sources": ["copy_me.txt"], **parameters}}]})
    outputs = ([json.dumps({"status": "need_clarification", "clarification": "复制到哪里，目标文件名是什么？"})]
               if answer is not None else []) + [raw]
    model = Mock(side_effect=outputs)
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["copy_files"],
        "named_sources": ["copy_me.txt"], "selection_required": False})))
    history, executor = HistoryStore(tmp_path / "history.jsonl"), Mock()
    replies = iter(([answer] if answer is not None else []) + ["yes"])
    prompts = []

    def confirm(prompt):
        assert not (tmp_path / target).exists()
        prompts.append(prompt)
        return next(replies)

    status = execute_request(Engine(ssh_hosts=[]), executor, history, user_input, str(tmp_path), "confirm", input_fn=confirm)
    assert status == "verified"
    assert len(prompts) == (1 if answer is None else 2)
    assert model.call_count == (1 if answer is None else 2)
    record = history.query(limit=1)[0]
    assert Path(record["preview"][0]["items"][0]["destination"]).as_posix() == target
    assert Path(record["verification"][0]["items"][0]["destination"]).as_posix() == target
    assert record["verification"][0]["items"][0]["status"] == "verified"
    assert record["plan"]["steps"][0]["parameters"]["destination"] == str(Path(target).parent).replace("\\", "/")
    assert (tmp_path / target).read_bytes() == source.read_bytes() == original
    if answer is not None:
        assert answer in record["clarifications"][0]
    assert target in capsys.readouterr().out.replace("\\", "/")
    executor.execute.assert_not_called()
    executor.execute_query.assert_not_called()


def test_plain_copy_ignores_model_name_after_named_copies(tmp_path, monkeypatch):
    source = tmp_path / "copy_me.txt"
    source.write_bytes(b"unchanged source")
    (tmp_path / "backup").mkdir()
    requests = [
        ("将 copy_me.txt 复制为 copy_test.txt", ".", "copy_test.txt"),
        ("将 copy_me.txt 复制到 backup 并命名为 copy_test.txt", "backup", "backup/copy_test.txt"),
        ("将 copy_me.txt 复制到 backup", "backup", "backup/copy_me.txt"),
    ]
    model = Mock(side_effect=[json.dumps({"status": "ready", "steps": [{
        "operation": "copy_files", "parameters": {"sources": ["copy_me.txt"],
        "destination": directory, "destination_name": "copy_test.txt"}}]})
        for _, directory, _ in requests])
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["copy_files"],
        "named_sources": ["copy_me.txt"], "selection_required": False})))
    engine, executor = Engine(ssh_hosts=[]), Mock()
    history = HistoryStore(tmp_path / "history.jsonl")
    for request, _, target in requests:
        prompts = []

        def confirm(prompt):
            assert "请输入 yes 确认" in prompt
            assert not (tmp_path / target).exists()
            prompts.append(prompt)
            return "yes"

        assert execute_request(engine, executor, history, request, str(tmp_path), "confirm", input_fn=confirm) == "verified"
        assert len(prompts) == 1
        record = history.query(limit=1)[0]
        assert record["clarifications"] == []
        assert Path(record["preview"][0]["items"][0]["destination"]).as_posix() == target
        assert record["verification"][0]["items"][0]["status"] == "verified"
        assert (tmp_path / target).read_bytes() == source.read_bytes() == b"unchanged source"
    assert "destination_name" not in record["plan"]["steps"][0]["parameters"]
    assert len(engine._task_history) == 3 and model.call_count == 3
    assert "copy_test.txt" in model.call_args.args[0][1]["content"]
    executor.execute.assert_not_called()
    executor.execute_query.assert_not_called()


@pytest.mark.parametrize("user_input,answers,expected_name", [
    ("将 copy_me.txt 复制到 backup", [], None),
    ("将 copy_me.txt 复制为 copy_test.txt", [], "copy_test.txt"),
    ("将 copy_me.txt 复制到 backup 并命名为 copy_test.txt", [], "copy_test.txt"),
    ("复制 copy_me.txt", ["问题：复制到哪里？；用户回答：复制到当前目录，目标文件名为 copy_test.txt"], "copy_test.txt"),
])
def test_copy_name_normalization_shared_by_primary_and_reference(tmp_path, monkeypatch, user_input, answers, expected_name):
    directory = "." if "复制为" in user_input or answers else "backup"
    parameters = {"sources": ["copy_me.txt"], "destination": directory}
    primary_raw = json.dumps({"status": "ready", "steps": [{"operation": "copy_files", "parameters": {
        **parameters, "destination_name": "copy_test.txt"}}]})
    reference_raw = json.dumps({"status": "ready", "steps": [{"operation": "copy_files", "parameters": {
        **parameters, "destination_name": expected_name or "invented_by_reference.txt"}}]})
    model = Mock(side_effect=[primary_raw, reference_raw, '{"commands":[]}'])
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["copy_files"]})))
    # 仅隔离 Bash 候选构造；实际复制使用受保护的 Python 工具，不通过此接口执行。
    command_request = Mock(return_value=[{"role": "system", "content": ""}, {"role": "user", "content": ""}])
    monkeypatch.setattr("core.bash_queries.command_request", command_request)
    engine = Engine(ssh_hosts=[])
    primary = engine.generate_task_plan(user_input, str(tmp_path), answers)
    expected = dict(parameters)
    if expected_name is not None:
        expected["destination_name"] = expected_name
    assert primary.status == "ready" and primary.steps[0].parameters == expected
    assert engine.generate_bash_commands(user_input, str(tmp_path), primary, {}, answers) == {"commands": []}
    assert engine.command_attempts[0]["review"] == "accepted"
    assert engine.command_attempts[0]["raw_output"] == reference_raw
    assert json.loads(model.call_args_list[1].args[0][1]["content"]) == {
        "cwd": str(tmp_path), "request": user_input, "answers": answers}
    assert "steps" not in json.loads(model.call_args_list[1].args[0][1]["content"])
    assert model.call_count == 3


@pytest.mark.parametrize("case", ["ready", "implicit_directories", "swapped", "missing_source", "unbound_names", "ordinary_second"])
def test_multiple_copy_names_remain_bound_to_their_sources(tmp_path, monkeypatch, case):
    (tmp_path / "first.txt").write_bytes(b"first source")
    if case != "missing_source":
        (tmp_path / "missing.txt").write_bytes(b"second source")
    names = ["atomic_first_copy.txt", "atomic_missing_copy.txt"]
    user_input = "将 first.txt 复制为 atomic_first_copy.txt，然后将 missing.txt 复制为 atomic_missing_copy.txt"
    if case == "unbound_names":
        user_input = "复制 first.txt 和 missing.txt，目标文件名为 atomic_first_copy.txt，目标文件名为 atomic_missing_copy.txt"
    elif case == "ordinary_second":
        user_input = "将 first.txt 复制为 atomic_first_copy.txt，然后将 missing.txt 复制到 backup"
    steps = [{"operation": "copy_files", "parameters": {"sources": [source],
        "destination_name": names[1 - index] if case == "swapped" else names[index],
        **({} if case == "implicit_directories" else {"destination": "."})}}
        for index, source in enumerate(["first.txt", "missing.txt"])]
    if case == "ordinary_second":
        steps[1]["parameters"]["destination"] = "backup"
        names[1] = "backup/missing.txt"
    model = Mock(side_effect=[json.dumps({"status": "ready", "steps": steps})])
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["copy_files"]})))
    engine, executor = Engine(ssh_hosts=[]), Mock()
    # 准备阶段缺文件的解释建议是独立依赖，不能消耗计划模型的响应。
    monkeypatch.setattr(engine, "suggest_fix", Mock(return_value="请核对源文件是否存在。"))
    history, prompts = HistoryStore(tmp_path / "history.jsonl"), []

    def confirm(prompt):
        assert "请输入 yes 确认" in prompt
        prompts.append(prompt)
        return "yes"

    status = execute_request(engine, executor, history, user_input, str(tmp_path),
        "preview" if case in {"swapped", "unbound_names"} else "confirm", input_fn=confirm)
    model.assert_called_once()
    record = history.query(limit=1)[0]
    if case in {"ready", "implicit_directories", "ordinary_second"}:
        assert status == "verified" and len(prompts) == 1
        assert record["clarifications"] == []
        assert [(Path(item["items"][0]["source"]).as_posix(), Path(item["items"][0]["destination"]).as_posix())
                for item in record["preview"]] == [("first.txt", names[0]), ("missing.txt", names[1])]
        assert [item["status"] for item in record["verification"]] == ["verified", "verified"]
        assert (tmp_path / names[0]).read_bytes() == b"first source"
        assert (tmp_path / names[1]).read_bytes() == b"second source"
    else:
        assert status == ("blocked" if case == "missing_source" else "needs_clarification")
        assert not prompts and not record["executed"]
        assert not any((tmp_path / name).exists() for name in names)
    assert (tmp_path / "first.txt").read_bytes() == b"first source"
    executor.execute.assert_not_called()
    executor.execute_query.assert_not_called()


@pytest.mark.parametrize("before_preview", [True, False])
def test_copy_destination_name_never_overwrites(tmp_path, before_preview):
    (tmp_path / "source.txt").write_bytes(b"source")
    target = tmp_path / "copy_test.txt"
    if before_preview:
        target.write_bytes(b"keep")
    prepared = prepare_plan(copy_plan(sources=["source.txt"], destination=".", destination_name="copy_test.txt"), str(tmp_path))
    expected = "copy_test_1.txt" if before_preview else "copy_test.txt"
    assert preview_details(prepared)[0]["items"][0]["destination"] == expected
    if not before_preview:
        target.write_bytes(b"keep")
    outcome = execute_action(prepared, prepared["actions"][0])
    assert outcome["status"] == ("verified" if before_preview else "execution_failed")
    assert target.read_bytes() == b"keep"
    assert (tmp_path / "source.txt").read_bytes() == b"source"
    if before_preview:
        assert (tmp_path / expected).read_bytes() == b"source"


@pytest.mark.parametrize("name", [None, "changed.txt"])
def test_copy_destination_name_cannot_drop_or_change_requested_name(tmp_path, monkeypatch, name):
    parameters = {"sources": ["copy_me.txt"], "destination": "."}
    if name is not None:
        parameters["destination_name"] = name
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["copy_files"]})))
    model = Mock(return_value=json.dumps({"status": "ready", "steps": [{"operation": "copy_files", "parameters": parameters}]}))
    monkeypatch.setattr("core.engine.chat", model)
    result = Engine(ssh_hosts=[]).generate_task_plan("将 copy_me.txt 复制为 copy_test.txt", str(tmp_path))
    assert result.clarification and not result.steps
    model.assert_called_once()


@pytest.mark.parametrize("name", ["../escape", "/outside", "C:\\outside", "..\\escape", "nested/name", ".", "..",
                                   "", None, 42, "bad\x00name", "bad:name", "CON", "trailing."])
def test_copy_destination_name_rejects_invalid_names(name):
    with pytest.raises(ValueError):
        copy_plan(sources=["source.txt"], destination=".", destination_name=name)


@pytest.mark.parametrize("extra", [{"sources": ["a", "b"]}, {"preserve_structure": True}])
def test_copy_destination_name_rejects_multiple_sources_or_preserved_structure(extra):
    with pytest.raises(ValueError):
        copy_plan(**{"sources": ["a"], "destination": ".", "destination_name": "result.txt", **extra})


@pytest.mark.parametrize("count", [0, 1, 2])
def test_copy_destination_name_checks_resolved_source_count(tmp_path, count):
    for index in range(count):
        (tmp_path / f"source{index}.txt").write_bytes(b"source")
    value = TaskPlan((TaskStep(operation="find_files", parameters={"path": ".", "recursive": False, "pattern": "source*.txt"}),
                      TaskStep(operation="copy_files", parameters={"source_step": 1, "destination": ".", "destination_name": "result.txt"})))
    if count != 1:
        with pytest.raises(ValueError, match="单个源对象"):
            prepare_plan(value, str(tmp_path))
    else:
        prepared = prepare_plan(value, str(tmp_path))
        outcome = execute_action(prepared, prepared["actions"][1])
        assert outcome["status"] == "verified" and (tmp_path / "result.txt").read_bytes() == b"source"


def test_copy_destination_name_preserves_directory_source_and_path_boundary(tmp_path):
    (tmp_path / "folder").mkdir()
    (tmp_path / "folder/内容.txt").write_bytes(b"original")
    prepared = prepare_plan(copy_plan(sources=["folder"], destination="backup", destination_name="new folder"), str(tmp_path))
    assert execute_action(prepared, prepared["actions"][0])["status"] == "verified"
    assert (tmp_path / "backup/new folder/内容.txt").read_bytes() == (tmp_path / "folder/内容.txt").read_bytes()
    with pytest.raises(ValueError, match="工作目录之外"):
        prepare_plan(copy_plan(sources=["folder"], destination="..", destination_name="outside"), str(tmp_path))
    with pytest.raises(ValueError, match="不支持的操作参数"):
        parse_operation_plan(json.dumps({"status": "ready", "steps": [{"operation": "move_files", "parameters": {
            "sources": ["folder"], "destination": "backup", "destination_name": "other"}}]}))


@pytest.mark.parametrize("changed", ["source.txt", "copy_test.txt"])
def test_copy_destination_name_independently_verifies_source_and_copy(tmp_path, monkeypatch, changed):
    import core.operations as operations
    (tmp_path / "source.txt").write_bytes(b"source")
    prepared = prepare_plan(copy_plan(sources=["source.txt"], destination=".", destination_name="copy_test.txt"), str(tmp_path))
    actual_copy = operations._copy

    def corrupt(source, destination, deadline):
        actual_copy(source, destination, deadline)
        (tmp_path / changed).write_bytes(b"changed")

    monkeypatch.setattr(operations, "_copy", corrupt)
    outcome = execute_action(prepared, prepared["actions"][0])
    assert outcome["status"] == "execution_failed"
    assert "实际结果检查失败" in outcome["detail"]
