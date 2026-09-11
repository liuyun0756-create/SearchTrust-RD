from pathlib import Path
import json

import pytest


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "provider_contracts"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def provider_fixture():
    def load(relative_path: str):
        return json.loads((FIXTURE_ROOT / relative_path).read_text(encoding="utf-8"))

    return load
