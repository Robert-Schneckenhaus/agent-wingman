import pytest
from click.testing import CliRunner

from wingman.config import ENV_DB, ENV_HOME


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.delenv(ENV_HOME, raising=False)
    monkeypatch.delenv(ENV_DB, raising=False)


@pytest.fixture
def runner():
    return CliRunner()
