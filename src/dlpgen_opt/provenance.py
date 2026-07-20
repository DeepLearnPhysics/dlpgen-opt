from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(data, stream, sort_keys=False)
    temporary.replace(path)


def read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    return value if isinstance(value, dict) else {}


def git_commit(path: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def dependency_commits(repository: Path) -> dict[str, str | None]:
    root = repository / "dependencies"
    commits = {
        name: git_commit(root / name)
        for name in (
            "DLPGenerator",
            "GENIE",
            "dk2nu",
            "edep-sim",
            "SuperaAtomic",
            "edep2supera",
        )
    }
    versions_file = root / "versions.yaml"
    if versions_file.exists():
        with versions_file.open(encoding="utf-8") as stream:
            pinned = yaml.safe_load(stream) or {}
        commits = {name: commits[name] or pinned.get(name) for name in commits}
    return commits


def host_info() -> dict[str, str]:
    return {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python": platform.python_version(),
    }


class JsonFormatter:
    @staticmethod
    def event(level: str, message: str, **fields: Any) -> str:
        return json.dumps(
            {"timestamp": now(), "level": level, "message": message, **fields},
            sort_keys=True,
        )
