import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config  # noqa: E402
from app.db import Database  # noqa: E402


@pytest.fixture
def db():
    database = Database(":memory:")
    database.migrate()
    return database


@pytest.fixture
def config(tmp_path):
    return load_config(tmp_path / "missing.yaml", env={})
