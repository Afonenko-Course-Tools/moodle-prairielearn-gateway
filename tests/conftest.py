import sys
import pytest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

@pytest.fixture
def anyio_backend(): return "asyncio"
