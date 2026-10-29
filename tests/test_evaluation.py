import shutil
import subprocess

import pytest
from conftest import FIXTURES, index_fixtures

from wingman import evaluation


def git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "PATH": "/usr/bin:/bin",
        },
    )


@pytest.fixture
def history(tmp_path):
    """The py_app fixture with a history: a fix in models.py, a later unrelated change
    to services.py after its fix, and a commit that only touches tests."""
    repo = tmp_path / "src" / "py_app"
    shutil.copytree(FIXTURES / "py_app", repo)
    models = repo / "src/shop/models.py"
    services = repo / "src/shop/services.py"
    final_models, final_services = models.read_text(), services.read_text()
    models.write_text(final_models.replace('self.status = "cancelled"', "pass"))
    services.write_text(final_services.replace("order.cancel()", "pass"))
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Initial import")
    models.write_text(final_models)
    git(repo, "commit", "-qam", "Fix: cancelled orders keep their old status")
    services.write_text(final_services)
    git(repo, "commit", "-qam", "Fix OrderService.cancel_order not cancelling the order")
    services.write_text(final_services + "\n# changed later\n")
    git(repo, "commit", "-qam", "Tidy up services")
    (repo / "tests/test_services.py").write_text("# bug fix in tests only\n")
    git(repo, "commit", "-qam", "Fix flaky test")
    return repo


def test_history_cases(history):
    cases = evaluation.history_cases(history)
    assert [(c.subject, c.files) for c in cases] == [
        ("Fix OrderService.cancel_order not cancelling the order", ["src/shop/services.py"]),
        ("Fix: cancelled orders keep their old status", ["src/shop/models.py"]),
    ]
    # services.py changed after its fix, so its line numbers are not usable
    assert cases[0].lines == {}
    assert list(cases[1].lines) == ["src/shop/models.py"]


def test_evaluate(history, tmp_path):
    ws = index_fixtures(tmp_path / "ws", [history])
    cases = evaluation.history_cases(history)
    with ws.engine.connect() as conn:
        report = evaluation.evaluate(conn, ws.workspace_id, "py_app", cases, k=5)
    first, second = report.results
    assert first.file_rank == 1  # names OrderService.cancel_order
    assert second.expected_symbols == ["Order.cancel"]
    assert second.file_rank is not None and second.symbol_rank is not None
    assert report.recall(5) == 1.0 and report.symbol_cases == 1
    assert report.to_dict()["files"]["recall@5"] == 1.0


def test_cli_eval(history, tmp_path, runner, monkeypatch):
    from wingman.cli import main

    monkeypatch.chdir(tmp_path)
    for args in (["init"], ["repo", "add", str(history)], ["index"]):
        assert runner.invoke(main, args).exit_code == 0
    result = runner.invoke(main, ["eval", "py_app", "-v"])
    assert result.exit_code == 0, result.output
    assert "2 commits, context packs of 10" in result.output
    assert "files    recall@1" in result.output
