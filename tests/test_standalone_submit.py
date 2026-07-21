from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


LAUNCHER = Path(__file__).resolve().parents[1] / "submit.py"


def load_launcher():
    spec = importlib.util.spec_from_file_location("standalone_submit", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_standalone_launcher_needs_no_project_install(production_config, tmp_path):
    container = tmp_path / "missing-but-valid-for-dry-run.sif"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(LAUNCHER),
            str(production_config.config_path),
            "--container",
            str(container),
            "--profile",
            "s3df_roma",
            "--runtime",
            "apptainer",
            "--max-concurrent",
            "1",
            "--dry-run",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "#SBATCH --partition=roma" in result.stdout
    assert "#SBATCH --array=0-1%1" in result.stdout
    assert "apptainer exec --cleanenv --bind /sdf" in result.stdout
    assert '--job "${SLURM_ARRAY_TASK_ID}"' in result.stdout
    assert not production_config.production.output_dir.exists()


def test_standalone_launcher_resolves_relative_output(tmp_path):
    config = tmp_path / "production.yaml"
    config.write_text(
        """schema_version: 1
production:
  name: quoted_test
  output_dir: 'relative_output'
  jobs: 1 # one task
source: {}
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(LAUNCHER),
            str(config),
            "--container",
            str(tmp_path / "image.sif"),
            "--runtime",
            "apptainer",
            "--dry-run",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(tmp_path / "relative_output" / "slurm") in result.stdout


def test_standalone_launcher_chains_array_chunks(tmp_path):
    launcher = load_launcher()
    container = tmp_path / "image.sif"
    container.touch()
    production = {
        "config": tmp_path / "production.yaml",
        "name": "chunk_test",
        "output_dir": tmp_path / "runs",
        "jobs": 3,
    }
    production["config"].write_text("production: {}\n", encoding="utf-8")
    profile = launcher.read_profile(
        LAUNCHER.parent / "configs" / "slurm" / "s3df.yaml", "s3df_milano"
    )
    profile["max_array_size"] = 2
    options = SimpleNamespace(
        bind=[],
        container=container,
        dry_run=False,
        max_concurrent=None,
        runtime="apptainer",
    )
    completed = subprocess.CompletedProcess(
        args=["sbatch"], returncode=0, stdout="12345;cluster\n", stderr=""
    )
    with patch.object(launcher.subprocess, "run", return_value=completed) as run:
        results = launcher.submit(production, profile, options)

    assert len(results) == 2
    assert run.call_args_list[1].args[0][2:4] == ["--dependency", "afterok:12345"]
    assert "#SBATCH --array=0-1" in results[0][0].read_text(encoding="utf-8")
    assert "#SBATCH --array=2-2" in results[1][0].read_text(encoding="utf-8")
