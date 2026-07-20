from __future__ import annotations

import sys

import yaml

from dlpgen_opt.provenance import checksum
from dlpgen_opt.runner import execute_stage


def test_stage_record_contains_input_checksum(tmp_path):
    source = tmp_path / "input.txt"
    output = tmp_path / "output.txt"
    status = tmp_path / "status.yaml"
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    source.write_text("immutable input\n", encoding="utf-8")

    execute_stage(
        stage="test",
        command=[
            sys.executable,
            "-c",
            "from pathlib import Path; Path(r'%s').write_text('result')" % output,
        ],
        status_path=status,
        stdout_path=stdout,
        stderr_path=stderr,
        validator=lambda: {"ok": output.read_text(encoding="utf-8") == "result"},
        inputs=[source],
        outputs=[output],
        metadata={},
    )

    record = yaml.safe_load(status.read_text(encoding="utf-8"))
    assert record["status"] == "completed"
    assert record["inputs"] == [
        {"path": str(source), "bytes": source.stat().st_size, "sha256": checksum(source)}
    ]
