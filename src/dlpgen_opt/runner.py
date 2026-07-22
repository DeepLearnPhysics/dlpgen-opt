from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from .artifacts import InputArtifact
from .provenance import checksum, now, write_yaml


Validator = Callable[[], dict[str, object]]


def _input_record(value: Path | InputArtifact) -> dict[str, object]:
    artifact = value if isinstance(value, InputArtifact) else InputArtifact(value)
    record: dict[str, object] = {
        "path": str(artifact.path),
        "bytes": artifact.path.stat().st_size,
    }
    if artifact.checksum:
        record["sha256"] = checksum(artifact.path)
    else:
        record["checksum"] = "skipped"
    return record


def execute_stage(
    *,
    stage: str,
    command: list[str],
    status_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    validator: Validator,
    inputs: list[Path | InputArtifact],
    outputs: list[Path],
    metadata: dict[str, object],
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "stage": stage,
        "status": "running",
        "started_at": now(),
        "command": command,
        "inputs": [_input_record(value) for value in inputs],
        "outputs": [str(path) for path in outputs],
        **metadata,
    }
    write_yaml(status_path, record)
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            completed = subprocess.run(
                command,
                stdout=stdout,
                stderr=stderr,
                text=True,
                check=False,
                env={**os.environ, **(environment or {})},
            )
        record["return_code"] = completed.returncode
        if completed.returncode:
            raise RuntimeError(
                f"{stage} failed with exit code {completed.returncode}; see {stderr_path}"
            )
        record["validation"] = validator()
        record["status"] = "completed"
        return record
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = str(error)
        raise
    finally:
        record["completed_at"] = now()
        write_yaml(status_path, record)
