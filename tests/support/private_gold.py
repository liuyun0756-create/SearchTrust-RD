"""Skip only unavailable private replay inputs; committed data must still validate."""
import json
from pathlib import Path

import pytest


def require_private_gold_inputs(directory: Path, private_root: Path) -> None:
    descriptor = json.loads((directory / "input.json").read_text())
    missing = [
        ref["path"] for ref in descriptor["snapshot_files"]
        if ref["base"] == "private" and not (private_root / ref["path"]).is_file()
    ]
    if missing:
        pytest.skip(
            f"{directory.name}: private frozen replay inputs unavailable "
            f"({', '.join(missing)}); replay correctness is not checked"
        )
