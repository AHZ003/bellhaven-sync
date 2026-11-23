import os
import sys
import tempfile
from pathlib import Path

# Tests always run offline against the fixture snapshot, in a throwaway data directory.
os.environ["BELLHAVEN_OFFLINE"] = "1"
os.environ["BELLHAVEN_DATA_DIR"] = tempfile.mkdtemp(prefix="bellhaven-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from bellhaven_sync.pipeline import run_pipeline  # noqa: E402
from bellhaven_sync.sandbox import FakeCRM, fixture_fetch  # noqa: E402
from bellhaven_sync.store import Store  # noqa: E402

BELLHAVEN = "0015QAPLGS3FVYEEEM"


@pytest.fixture
def crm():
    return FakeCRM()  # fresh in-memory copy of the CRM snapshot


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "sync.db")


@pytest.fixture
def run(crm, store):
    def _run(fetch=fixture_fetch):
        return run_pipeline(crm=crm, fetch=fetch, store=store, today="2026-11-21")
    return _run
