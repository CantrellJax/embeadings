import pytest


@pytest.fixture(autouse=True)
def _no_user_config(monkeypatch, tmp_path):
    """Tests never read the developer's own emBEADings config file."""

    monkeypatch.setenv("EMBEAD_CONFIG", str(tmp_path / "no-embead-config.toml"))
