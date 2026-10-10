"""Run the contract against the demo cell (examples/demo), generated into a temporary directory."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from cellkit.new import generate
from cellkit.testing import client, harness, owner, platform  # noqa: F401

_DEMO = Path(tempfile.mkdtemp(prefix="ck-demo-"))
shutil.copy(Path(__file__).resolve().parents[1] / "examples" / "demo" / "cell.yaml", _DEMO / "cell.yaml")
generate(_DEMO)
os.environ["CELLKIT_ROOT"] = str(_DEMO)


@pytest.fixture(scope="session")
def cell_root() -> Path:
    return _DEMO
