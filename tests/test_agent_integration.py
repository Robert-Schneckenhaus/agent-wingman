import json
import shlex

import pytest

from wingman import claude_setup, hooks
from wingman.cli import main
from wingman.workspace import Workspace


@pytest.fixture
def workspace(ws, tmp_path):
    return Workspace(tmp_path / ".wingman")


def test_post_edit_reindexes_only_the_edited_file(workspace, tmp_path):
    tax = tmp_path / "ts_app/src/billing/tax.ts"
    tax.write_text(tax.read_text().replace("breakdown(", "computeLines("))
    other = tmp_path / "ts_app/src/shared/money.ts"
    other.write_text(other.read_text() + "\n// also changed, but not part of this edit\n")
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(tax)}}

    message = hooks.post_edit(workspace, payload)
    assert message.startswith("wingman: re-indexed ts_app:src/billing/tax.ts (")
    assert "money.ts" not in message
    assert hooks.post_edit(workspace, payload) == ""  # unchanged since


def test_post_edit_message_and_relative_paths(workspace, tmp_path):
    money = tmp_path / "ts_app/src/shared/money.ts"
    money.write_text(money.read_text() + "\n// changed\n")
    payload = {"tool_input": {"file_path": "src/shared/money.ts"}, "cwd": str(tmp_path / "ts_app")}
    assert hooks.post_edit(workspace, payload).startswith(
        "wingman: re-indexed ts_app:src/shared/money.ts ("
    )
    assert hooks.post_edit(workspace, payload) == ""  # unchanged now
    outside = {"tool_input": {"file_path": "/etc/hosts"}}
    assert hooks.post_edit(workspace, outside) == ""


def test_post_edit_removes_deleted_files(workspace, tmp_path):
    view = tmp_path / "ts_app/src/ui/InvoiceView.tsx"
    view.unlink()
    message = hooks.post_edit(workspace, {"tool_input": {"file_path": str(view)}})
    assert "re-indexed ts_app:src/ui/InvoiceView.tsx" in message


def test_hook_commands_never_fail(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no workspace here
    for event in ("post-edit", "session-start"):
        result = runner.invoke(main, ["hook", event], input="not json")
        assert result.exit_code == 0


def test_session_start_reports_index(ws, runner):
    result = ws("hook", "session-start")
    assert result.output.startswith("agent-wingman: code index ready (3 repos, 21 files")
    assert "find_relevant_for_task" in result.output


def test_read_payload():
    assert hooks.read_payload('{"a": 1}') == {"a": 1}
    assert hooks.read_payload("[1]") == {}
    assert hooks.read_payload("garbage") == {}


def test_install_local_merges_hooks_and_keeps_existing(tmp_path):
    project = tmp_path / "proj"
    settings = project / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo other"}]}
    settings.write_text(
        json.dumps({"permissions": {"allow": []}, "hooks": {"PostToolUse": [other]}})
    )

    home = tmp_path / ".wingman"
    for _ in range(2):  # idempotent
        claude_setup.install(project, home, claude_md=True, register_mcp=False)
    data = json.loads(settings.read_text())
    assert data["permissions"] == {"allow": []}
    post = data["hooks"]["PostToolUse"]
    assert post[0] == other
    assert len(post) == 2
    command = shlex.split(post[1]["hooks"][0]["command"])
    assert command[-4:] == ["--home", str(home), "hook", "post-edit"]
    assert len(data["hooks"]["SessionStart"]) == 1
    assert (project / "CLAUDE.md").read_text().count("<!-- wingman:start -->") == 1

    claude_setup.uninstall(project)
    data = json.loads(settings.read_text())
    assert data == {"permissions": {"allow": []}, "hooks": {"PostToolUse": [other]}}
    assert not (project / "CLAUDE.md").exists()


def test_install_shared_writes_portable_config(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "CLAUDE.md").write_text("# Project\n\nExisting text.\n")
    claude_setup.install(project, tmp_path / ".wingman", shared=True, claude_md=True)

    mcp = json.loads((project / ".mcp.json").read_text())
    assert mcp == {"mcpServers": {"wingman": {"command": "wingman", "args": ["mcp"]}}}
    settings = json.loads((project / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] == (
        "wingman hook session-start"
    )
    assert "Existing text." in (project / "CLAUDE.md").read_text()

    claude_setup.uninstall(project, shared=True)
    assert not (project / ".mcp.json").exists()
    assert not (project / ".claude" / "settings.json").exists()
    assert (project / "CLAUDE.md").read_text() == "# Project\n\nExisting text.\n"


def test_install_rejects_invalid_settings(tmp_path):
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text("{ broken")
    with pytest.raises(ValueError, match="not valid JSON"):
        claude_setup.install(tmp_path, None, register_mcp=False)


def test_reindex_command(ws, tmp_path):
    money = tmp_path / "ts_app/src/shared/money.ts"
    money.write_text(money.read_text() + "\n// changed\n")
    result = ws("reindex", "ts_app/src/shared/money.ts")
    assert "ts_app: 1 indexed" in result.output


def test_notes_cli(ws):
    assert (
        "added to Billing.Tax.TaxCalculator.Breakdown"
        in ws(
            "note", "add", "Billing.Tax.TaxCalculator.Breakdown", "Discount", "before", "tax"
        ).output
    )
    assert "added to cs_app:Api" in ws("note", "add", "cs_app:Api", "Controllers audit").output
    assert "Unknown target" in ws("note", "add", "nope", "x", ok=False).output
    assert "ambiguous" in ws("note", "add", "Breakdown", "x", ok=False).output

    listing = ws("note", "list").output
    assert "Discount before tax" in listing and "Controllers audit" in listing
    assert "note: Discount before tax" in ws("symbol", "Billing.Tax.TaxCalculator.Breakdown").output

    context_text = ws("context", "tax", "endpoint", "Billing.Api.InvoicesController.GetTax").output
    assert "cs_app:Api: Controllers audit" in context_text

    ws("note", "remove", "1")
    assert "Discount before tax" not in ws("note", "list").output
