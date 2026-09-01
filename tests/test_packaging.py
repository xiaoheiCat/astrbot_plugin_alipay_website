from __future__ import annotations

import tomllib
from pathlib import Path


def test_astrbot_requirements_match_pyproject_dependencies() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_dependencies = set(project["project"]["dependencies"])
    astrbot_dependencies = {
        line.strip()
        for line in (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert astrbot_dependencies == project_dependencies
