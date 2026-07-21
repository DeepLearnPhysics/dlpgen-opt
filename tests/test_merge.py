from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


LAUNCHER = Path(__file__).resolve().parents[1] / "merge.py"


def load_launcher():
    spec = importlib.util.spec_from_file_location("standalone_merge", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_production(tmp_path, jobs=10, missing=()):
    root = tmp_path / "sample_v001"
    root.mkdir()
    (root / "manifest.yaml").write_text(
        "production: sample_v001\n", encoding="utf-8"
    )
    (root / "resolved_config.yaml").write_text(
        "production:\n  jobs: {}\n".format(jobs), encoding="utf-8"
    )
    for job in range(jobs):
        if job in missing:
            continue
        job_root = root / "jobs" / "{:05d}".format(job)
        (job_root / "supera").mkdir(parents=True)
        (job_root / "supera.yaml").write_text(
            "status: completed\n", encoding="utf-8"
        )
        (job_root / "supera" / "supera.root").write_bytes(b"x")
    return root


def test_parse_size():
    launcher = load_launcher()
    assert launcher.parse_size("80GB") == 80_000_000_000
    assert launcher.parse_size("1.5 GiB") == int(1.5 * (1 << 30))
    with pytest.raises(ValueError, match="invalid size"):
        launcher.parse_size("many")


def test_split_and_chunk_names_are_deterministic(tmp_path):
    launcher = load_launcher()
    root = make_production(tmp_path)
    production = launcher.read_production(root)
    first = launcher.split_inputs(production["inputs"], 0.8, 104739)
    second = launcher.split_inputs(production["inputs"], 0.8, 104739)
    assert first == second
    assert len(first["train"]) == 8
    assert len(first["test"]) == 2

    tasks = launcher.build_tasks(production, first, 5, root / "merged")
    assert [task["output"].name for task in tasks] == [
        "sample_v001_train_0.root",
        "sample_v001_train_1.root",
        "sample_v001_test.root",
    ]
    assert all(task["input_bytes"] <= 5 for task in tasks)


def test_missing_jobs_are_explicit(tmp_path):
    launcher = load_launcher()
    root = make_production(tmp_path, jobs=4, missing=(2,))
    with pytest.raises(ValueError, match="--allow-missing"):
        launcher.read_production(root)
    production = launcher.read_production(root, allow_missing=True)
    assert production["missing"] == [2]
    assert len(production["inputs"]) == 3


def test_plan_supports_legacy_pyyaml_safe_dump(tmp_path, monkeypatch):
    launcher = load_launcher()
    root = make_production(tmp_path)
    production = launcher.read_production(root)
    split_files = launcher.split_inputs(production["inputs"], 0.8, 12345)
    output_dir = root / "merged"
    tasks = launcher.build_tasks(production, split_files, 5, output_dir)
    safe_dump = launcher.yaml.safe_dump

    # Old PyYAML accepts only data and stream here; extra modern keywords fail.
    def legacy_safe_dump(data, stream):
        return safe_dump(data, stream)

    monkeypatch.setattr(launcher.yaml, "safe_dump", legacy_safe_dump)
    launcher.prepare_plan(production, tasks, output_dir, 0.8, 12345, 5)
    assert (output_dir / "merge_plan.yaml").is_file()


def test_prepare_only_needs_no_project_install(tmp_path):
    root = make_production(tmp_path)
    container = tmp_path / "not-needed-during-preparation.sif"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(LAUNCHER),
            str(root),
            "--container",
            str(container),
            "--profile",
            "s3df_roma",
            "--runtime",
            "apptainer",
            "--max-file-size",
            "5B",
            "--max-array-size",
            "2",
            "--prepare-only",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "prepared 3 merge task(s)" in result.stdout

    merged = root / "merged"
    assert (merged / "merge_plan.yaml").is_file()
    assert (merged / "merge_tasks.tsv").is_file()
    scripts = sorted((merged / "slurm").glob("*.sbatch"))
    assert [path.name for path in scripts] == [
        "merge_00000_00001.sbatch",
        "merge_00002_00002.sbatch",
    ]
    first_script = scripts[0].read_text(encoding="utf-8")
    second_script = scripts[1].read_text(encoding="utf-8")
    assert "#SBATCH --partition=roma" in first_script
    assert "#SBATCH --array=0-1" in first_script
    assert "hadd " in first_script
    assert "@$2" in first_script
    assert "LD_PRELOAD=" in first_script
    assert "liblarcv.so" in first_script
    assert "--cleanenv" in first_script
    assert "SLURM_ARRAY_TASK_ID + 2" in second_script
