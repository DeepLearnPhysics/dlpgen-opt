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
    assert (
        "apptainer exec --cleanenv --env PYTHONNOUSERSITE=1 --bind /sdf"
        in result.stdout
    )
    assert '--job "$((SLURM_ARRAY_TASK_ID + 0))"' in result.stdout
    assert (
        'dlpgen-opt job ${SLURM_JOB_ID}/${SLURM_ARRAY_TASK_ID} '
        'completed successfully'
    ) in result.stdout
    assert 'echo "Completed: $(date --iso-8601=seconds)"' in result.stdout
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
    first_script = results[0][0].read_text(encoding="utf-8")
    second_script = results[1][0].read_text(encoding="utf-8")
    assert "#SBATCH --array=0-1" in first_script
    assert '--job "$((SLURM_ARRAY_TASK_ID + 0))"' in first_script
    assert "#SBATCH --array=0-0" in second_script
    assert '--job "$((SLURM_ARRAY_TASK_ID + 2))"' in second_script


def test_gibuu_standalone_matches_installed_submission(tmp_path):
    import yaml
    from dlpgen_opt.config import load_config
    from dlpgen_opt.slurm import SlurmProfile, submit_arrays

    launcher = load_launcher()
    raw = yaml.safe_load((LAUNCHER.parent / 'configs/production.gibuu-bnb.yaml').read_text())
    raw['production']['jobs'] = 125
    raw['production']['output_dir'] = str(tmp_path / 'production')
    raw['source']['config'] = str(LAUNCHER.parent / 'configs/gibuu/bnb_sbnd.yaml')
    config_path = tmp_path / 'production.yaml'
    config_path.write_text(yaml.safe_dump(raw))
    production = launcher.read_production(config_path)
    config = load_config(config_path)
    profile = launcher.read_profile(LAUNCHER.parent / 'configs/batch/sites.yaml', 's3df_milano')
    profile['prepare_cpus_per_task'] = 4
    profile['prepare_time'] = '12:00:00'
    container = tmp_path / 'image.sif'
    container.touch()
    options = SimpleNamespace(bind=[], container=container, dry_run=False,
                              max_concurrent=10, runtime='apptainer', prepare_round=0)
    def submit_ids():
        return [subprocess.CompletedProcess([], 0, f'{i};cluster\n', '') for i in range(100,106)]
    with patch.object(launcher.subprocess, 'run', side_effect=submit_ids()) as run:
        standalone = launcher.submit(production, profile, options)
    scripts = [p.read_text() for p, _ in standalone]
    assert len(scripts) == 6
    assert '--phase plan --round 0' in scripts[0]
    assert '--task "$((SLURM_ARRAY_TASK_ID + 99))"' in scripts[2]
    assert '--phase finalize --round 0' in scripts[3]
    for index, call in enumerate(run.call_args_list[1:], start=1):
        assert call.args[0][2:4] == ['--dependency', f'afterok:{99+index}']
    for script in scripts:
        assert '#SBATCH --kill-on-invalid-dep=yes' in script
        subprocess.run(['bash', '-n'], input=script, text=True, check=True)
    runtime = SlurmProfile(**dict(profile, container_runtime='apptainer'))
    with patch.object(launcher.subprocess, 'run', side_effect=submit_ids()):
        installed = submit_arrays(config, runtime, container=container, max_concurrent=10)
    assert scripts == [p.read_text() for p, _ in installed]


def test_standalone_gibuu_policy_source_merge(tmp_path):
    import pytest
    import yaml
    launcher = load_launcher()
    source = tmp_path / 'source.yaml'
    source.write_text('mode: generate\ncandidate_cache:\n  distributed_tasks: 7\n')
    raw = dict(production=dict(name='test', jobs=125, output_dir='output'),
               source=dict(type='gibuu', config='source.yaml'))
    path = tmp_path / 'production.yaml'
    for cache, expected in [(None, [None, 7, None, 99, 26]),
                            ({}, [None, 99, 26, None, 99, 26]),
                            ({'distributed_tasks': None}, [None, 99, 26]),
                            ({'enabled': False}, [99, 26]),
                            ({'allocation': 'uniform'}, [None, 99, 26])]:
        raw['source'].pop('candidate_cache', None)
        if cache is not None:
            raw['source']['candidate_cache'] = cache
        path.write_text(yaml.safe_dump(raw))
        config = launcher.scheduler_config(launcher.read_production(path))
        plan = launcher.stages(config, 99)
        assert [stage.array_count for stage in plan] == expected
    raw['source']['candidate_cache'] = {'distributed_tasks': 0}
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match='positive integer'):
        launcher.scheduler_config(launcher.read_production(path))


def test_standalone_gibuu_imports_no_heavy_dependencies(tmp_path):
    # Fail if a future refactor imports Pydantic or any physics stack on the host.
    code = '''
import importlib.abc, runpy, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('pydantic', 'ROOT', 'numpy', 'pyhepmc'):
            raise RuntimeError('Heavy host import: ' + fullname)
sys.meta_path.insert(0, Block())
sys.argv = %r
runpy.run_path(%r, run_name='__main__')
''' % ([str(LAUNCHER), str(LAUNCHER.parent / 'configs/production.gibuu-bnb.yaml'),
         '--container', str(tmp_path / 'absent.sif'), '--dry-run', '--runtime', 'apptainer'], str(LAUNCHER))
    result = subprocess.run([sys.executable, '-I', '-c', code], check=True, text=True, capture_output=True)
    assert '--phase plan' in result.stdout
    assert '--phase worker' in result.stdout
    assert '--phase finalize' in result.stdout


def test_standalone_pbs_matches_installed(production_config, tmp_path):
    from dlpgen_opt.batch import load_profile, submit_arrays
    launcher = load_launcher()
    for site in ('polaris', 'aurora'):
        options = SimpleNamespace(bind=[], container=tmp_path / 'image.sif',
            dry_run=False, max_concurrent=1, runtime='apptainer', prepare_round=0)
        options.container.touch()
        profile = launcher.read_profile(LAUNCHER.parent / 'configs/batch/sites.yaml', site)
        production = launcher.read_production(production_config.config_path)
        response = subprocess.CompletedProcess([], 0, '123[].server\n', '')
        with patch.object(launcher.subprocess, 'run', return_value=response):
            result = launcher.submit(production, profile, options)
        expected = [p.read_text() for p, _ in result]
        with patch.object(launcher.subprocess, 'run', return_value=response):
            result = submit_arrays(production_config,
                load_profile(LAUNCHER.parent / 'configs/batch/sites.yaml', site),
                container=options.container, max_concurrent=1)
        assert expected == [p.read_text() for p, _ in result]


def test_pbs_defaults_match_both_loaders():
    from dlpgen_opt.pbs import PBSProfile
    from dlpgen_opt.pbs_profile import normalize_pbs_profile
    minimal = dict(account='test::allocation', queue='capacity', system='polaris', filesystems='home:eagle')
    assert normalize_pbs_profile(minimal) == PBSProfile(**minimal).model_dump()
