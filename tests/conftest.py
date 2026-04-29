import pytest


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "app.db")
