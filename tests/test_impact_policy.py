from core.impact import analyze
from core.policy import MANUAL, READ_ONLY, WORKSPACE, evaluate


def test_read_command_is_recognized():
    impact = analyze("ls -la")
    assert impact.known is True
    assert impact.tags == ("read",)
    assert evaluate(impact, READ_ONLY, "/work").allowed is True


def test_pipeline_is_conservatively_unknown():
    impact = analyze("ls | wc -l")
    assert impact.known is False
    assert evaluate(impact, WORKSPACE, "/work").allowed is False


def test_workspace_blocks_absolute_and_parent_paths():
    assert evaluate(analyze("touch /tmp/file"), WORKSPACE, "/work").allowed is False
    assert evaluate(analyze("touch ../file"), WORKSPACE, "/work").allowed is False


def test_delete_requires_confirmation_in_workspace_but_manual_allows_it():
    impact = analyze("rm file.txt")
    workspace = evaluate(impact, WORKSPACE, "/work")
    assert workspace.allowed is True
    assert workspace.requires_confirmation is True
    assert evaluate(impact, MANUAL, "/work").allowed is True
