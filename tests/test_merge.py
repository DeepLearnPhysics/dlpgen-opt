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


@pytest.mark.parametrize('site', ['aurora', 'polaris'])
@pytest.mark.parametrize('fail', [False, True])
def test_pbs_merge_runs_chunks_and_propagates_failure(tmp_path, monkeypatch, site, fail):
    import json
    import os
    launcher = load_launcher()
    root = make_production(tmp_path)
    image = tmp_path / 'image with spaces.sif'
    image.touch()
    output = tmp_path / 'merged with spaces'
    # Login-node runtime lookup must not be required by PBS.
    monkeypatch.setattr(launcher.production_submit, 'choose_runtime',
                        lambda *a, **k: pytest.fail('host runtime lookup'))
    assert launcher.main([str(root), '--profile', site, '--container', str(image),
                          '--output-dir', str(output), '--max-file-size', '5B',
                          '--prepare-only']) == 0
    scripts = list((output / 'pbs').glob('*.pbs'))
    assert len(scripts) == 1
    script = scripts[0].read_text()
    assert '#PBS -J' not in script
    assert (':system=polaris' in script) == (site == 'polaris')
    assert ('--fakeroot' in script) == (site == 'polaris')
    assert 'LD_PRELOAD=' in script and 'hadd -j ' in script
    assert str(output) in script and str(root) in script
    subprocess.run(['bash', '-n', str(scripts[0])], check=True)
    # Execute the generated loop with a fake MPI/container boundary: verify
    # all task arguments, path quoting and that a failure stops subsequent tasks.
    fake = tmp_path / 'mpiexec'
    fake.write_text('#!/usr/bin/env python3\nimport sys,os,json\n'
                    'from pathlib import Path\n'
                    "with open(os.environ['TRACE'], 'a') as f: f.write(json.dumps(sys.argv[-2:])+'\\n')\n"
                    "if os.environ['FAIL']=='1': sys.exit(7)\n"
                    "Path(sys.argv[-2]).write_bytes(b'merged')\n")
    fake.chmod(0o755)
    shell_script = tmp_path / 'execute.sh'
    shell_script.write_text('module() { :; }\n' + script)
    trace = tmp_path / 'trace'
    result = subprocess.run(['bash', str(shell_script)], env=dict(os.environ,
        PATH=str(tmp_path)+':'+os.environ['PATH'], PBS_JOBID='123.server',
        TRACE=str(trace), FAIL=str(int(fail))))
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    assert len(calls) == (1 if fail else 3)
    assert result.returncode == (7 if fail else 0)
    for out, filelist in calls:
        assert Path(filelist).is_file()
        if not fail:
            assert Path(out).read_bytes() == b'merged'


def test_pbs_merge_submits_one_qsub_job(tmp_path, monkeypatch):
    launcher = load_launcher()
    root = make_production(tmp_path)
    image = tmp_path / 'image.sif'
    image.touch()
    from unittest.mock import patch
    with patch('dlpgen_opt.scheduling.subprocess.run', return_value=
               subprocess.CompletedProcess([], 0, '123.server\n', '')) as run:
        assert launcher.main([str(root), '--profile', 'aurora', '--container', str(image),
                              '--queue', 'debug', '--time', '01:00:00']) == 0
    assert run.call_count == 1
    args = run.call_args.args[0]
    assert args[0] == 'qsub'
    script = Path(args[1]).read_text()
    assert '#PBS -q debug' in script and '#PBS -l walltime=01:00:00' in script


def test_nersc_merge_submission_and_rendered_execution(tmp_path, monkeypatch):
    import os
    from unittest.mock import patch
    launcher = load_launcher()
    root = make_production(tmp_path)
    output = tmp_path / 'nersc output'
    monkeypatch.setattr(launcher.production_submit, 'choose_runtime',
                        lambda *a, **k: pytest.fail('host runtime lookup'))
    with patch.object(launcher.subprocess, 'run', side_effect=[
        subprocess.CompletedProcess([], 0, '123;cluster\n', ''),
        subprocess.CompletedProcess([], 0, '124;cluster\n', '')]) as submit:
        assert launcher.main([str(root), '--profile', 'nersc_cpu',
            '--container', 'docker:ghcr.io/deeplearnphysics/dlpgen-opt:0.5.0',
            '--output-dir', str(output), '--max-file-size', '5B',
            '--max-array-size', '2', '--max-concurrent', '1', '--cpus-per-task', '4']) == 0
    assert submit.call_count == 2
    assert submit.call_args_list[1].args[0][2:4] == ['--dependency', 'afterok:123']
    scripts = sorted((output / 'slurm').glob('*.sbatch'))
    text = scripts[0].read_text()
    assert '#SBATCH --account=m5252' in text
    assert '#SBATCH --qos=regular' in text and '#SBATCH --constraint=cpu' in text
    assert '#SBATCH --partition' not in text and '--mem-per-cpu' not in text
    assert '--image=docker:ghcr.io/deeplearnphysics/dlpgen-opt:0.5.0' in text
    assert 'srun --nodes=1 --ntasks=1 --cpus-per-task=4 --cpu-bind=cores shifter' in text
    assert '--volume' in text and 'hadd -j 4' in text
    assert '--module=none' in text and '--clearenv' in text
    fake = tmp_path / 'srun'
    fake.write_text('#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n'
                    "Path(sys.argv[-2]).write_bytes(Path(sys.argv[-1]).read_bytes())\n")
    fake.chmod(0o755)
    for script, indexes in zip(scripts, ([0, 1], [0])):
        subprocess.run(['bash', '-n', str(script)], check=True)
        for index in indexes:
            subprocess.run(['bash', str(script)], check=True, env=dict(os.environ,
                PATH=str(tmp_path)+':'+os.environ['PATH'], SLURM_JOB_ID='123',
                SLURM_ARRAY_TASK_ID=str(index)))
    assert len(list(output.glob('*.root'))) == 3


def test_shifter_rejects_sif(tmp_path, capsys):
    launcher = load_launcher()
    assert launcher.main([str(tmp_path), '--profile', 'nersc_cpu',
                          '--container', 'image.sif', '--prepare-only']) == 2
    assert 'Shifter requires' in capsys.readouterr().err
