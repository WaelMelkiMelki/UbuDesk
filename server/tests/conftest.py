import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent
sys.path.insert(0, str(SERVER_DIR))

VECTOR_DIR = REPO_ROOT / "protocol" / "vectors"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every test gets its own ~/.config/ubudesk."""
    monkeypatch.setenv("UBUDESK_STATE_DIR", str(tmp_path / "state"))
    yield


@pytest.fixture
def vector_dir() -> Path:
    return VECTOR_DIR
