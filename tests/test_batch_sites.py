from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from dlpgen_opt.batch import load_profile, submit_arrays
from dlpgen_opt.batch_worker import run_jobs
from dlpgen_opt.config import load_config
from dlpgen_opt.scheduling import stages


@pytest.mark.parametrize('site', ['aurora', 'polaris', 'nersc_cpu'])
def test_site_chain_and_shell_syntax(site, tmp_path, capsys):
    config = load_config('configs/production.gibuu-bnb.yaml')
    config.production.jobs = 125
    config.production.output_dir = tmp_path / 'output'
    profile = load_profile('configs/batch/sites.yaml', site)
    plan = stages(config, profile.max_array_size, jobs_per_task=profile.jobs_per_task)
    assert [stage.kind for stage in plan] == ['plan', 'worker', 'worker', 'finalize', 'run']
    assert [stage.array_count for stage in plan] == [None, 100, 25, None, 4]
    image = 'docker:example/image:tag' if site == 'nersc_cpu' else tmp_path / 'image.sif'
    submit_arrays(config, profile, container=image, dry_run=True, max_concurrent=2)
    script = capsys.readouterr().out
    subprocess.run(['bash', '-n'], input=script, text=True, check=True)
    assert not config.production.output_dir.exists()
    assert '--phase finalize' in script
    assert '--last 124 --count 32 --cpus 32' in script
    if site == 'nersc_cpu':
        assert '#SBATCH --constraint=cpu' in script
        assert '#SBATCH --qos=regular' in script
        assert 'srun --nodes=1 --ntasks=1 --cpus-per-task=32 --cpu-bind=cores shifter' in script
        assert '--image=docker:example/image:tag --module=none --clearenv' in script
        assert 'test -r docker:' not in script
    else:
        account = 'neutrinoGPU::debug_aurora' if site == 'aurora' else 'neutrinoGPU::SpineDev'
        assert f'#PBS -A {account}\n' in script
        assert '#PBS -q capacity\n' in script
        assert '#PBS -r y' in script
        assert '#PBS -J 0-99%2' in script
        assert '--task "$((PBS_ARRAY_INDEX + 100))"' in script
        assert 'mpiexec -n 1 --ppn 1 --depth 32 --cpu-bind depth apptainer' in script
        assert '--env "DLPGEN_OPT_CPUS_PER_TASK=32"' in script


def test_pbs_full_array_ids_in_dependencies(production_config, tmp_path):
    profile = load_profile('configs/batch/sites.yaml', 'polaris')
    profile.account = 'project'
    profile.jobs_per_task = 1
    profile.max_array_size = 1
    image = tmp_path / 'image.sif'
    image.touch()
    with patch('dlpgen_opt.scheduling.subprocess.run', side_effect=[
        subprocess.CompletedProcess([], 0, '123[].server\n', ''),
        subprocess.CompletedProcess([], 0, '124[].server\n', ''),
    ]) as run:
        result = submit_arrays(production_config, profile, container=image)
    assert len(result) == 2
    assert run.call_args_list[1].args[0][1:3] == ['-W', 'depend=afterok:123[].server']


def test_submission_failure_stops_chain(production_config, tmp_path):
    profile = load_profile('configs/batch/sites.yaml', 'polaris')
    profile.account = 'project'
    profile.jobs_per_task = 1
    profile.max_array_size = 1
    image = tmp_path / 'image.sif'
    image.touch()
    with patch('dlpgen_opt.scheduling.subprocess.run', side_effect=
               subprocess.CalledProcessError(1, ['qsub'], stderr='queue rejected')) as run:
        with pytest.raises(RuntimeError, match='queue rejected'):
            submit_arrays(production_config, profile, container=image)
    assert run.call_count == 1


def test_placeholder_account_prevents_submission(production_config):
    with pytest.raises(ValueError, match='allocation'):
        submit_arrays(production_config, load_profile('configs/batch/sites.yaml', 'nersc_cpu'),
                      container='docker:example/image:tag')


def test_packed_jobs_execute_once_and_report_failure(tmp_path):
    # Real children exercise logging, CPU budget, and exit-status propagation.
    code = "import os,sys; print(sys.argv[1], os.environ['DLPGEN_OPT_CPUS_PER_TASK']); sys.exit(int(sys.argv[1]) == 3)"
    result = run_jobs(range(2, 5), lambda job: [sys.executable, '-c', code, str(job)],
                      cpus=1, log_dir=tmp_path)
    assert result == 1
    assert sorted(p.name for p in tmp_path.glob('*.out')) == ['00002.out', '00003.out', '00004.out']
    for job in range(2, 5):
        assert (tmp_path / f'{job:05d}.out').read_text().strip() == f'{job} 1'


def test_packed_final_range_clipped(production_config, tmp_path):
    from dlpgen_opt.batch_worker import main
    with patch('dlpgen_opt.batch_worker.run_jobs', return_value=0) as run:
        assert main([str(production_config.config_path), '--first', '1', '--last', '1',
                     '--count', '32', '--cpus', '32', '--log-dir', str(tmp_path)]) == 0
    assert list(run.call_args.args[0]) == [1]


def test_generic_cpu_cap(monkeypatch):
    from dlpgen_opt.gibuu_cli import _available_cpus
    monkeypatch.setenv('DLPGEN_OPT_CPUS_PER_TASK', '2')
    monkeypatch.delenv('SLURM_CPUS_PER_TASK', raising=False)
    with patch('os.cpu_count', return_value=64):
        assert 1 <= _available_cpus() <= 2


def test_packing_bounds_concurrency(tmp_path):
    import json
    code = ("import json,sys,time; "
            "start=time.monotonic(); time.sleep(.2); "
            "print(json.dumps([start,time.monotonic()]))")
    assert run_jobs(range(5), lambda job: [sys.executable, '-c', code],
                    cpus=2, log_dir=tmp_path) == 0
    intervals = [json.loads(path.read_text()) for path in tmp_path.glob('*.out')]
    assert len(intervals) == 5
    points = sorted([(start, 1) for start, end in intervals] +
                    [(end, -1) for start, end in intervals])
    concurrent = peak = 0
    for _, delta in points:
        concurrent += delta
        peak = max(peak, concurrent)
    assert peak <= 2


def test_packed_jobs_termination(tmp_path):
    import os
    import signal
    import time
    runner = tmp_path / 'runner.py'
    runner.write_text(
        'import sys\nfrom dlpgen_opt.batch_worker import run_jobs\n'
        "raise SystemExit(run_jobs([0], lambda job: [sys.executable, '-c', "
        "\"import time; print('ready', flush=True); time.sleep(60)\"], "
        f'cpus=1, log_dir={str(tmp_path)!r}))\n')
    process = subprocess.Popen([sys.executable, str(runner)], env={**os.environ,
                               'PYTHONPATH': str(Path('src').resolve())})
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            output = tmp_path / '00000.out'
            if output.exists() and 'ready' in output.read_text():
                break
            time.sleep(.05)
        else:
            pytest.fail('packed child did not start')
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 130
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize('site,launcher,index_name', [
    ('polaris', 'mpiexec', 'PBS_ARRAY_INDEX'),
    ('nersc_cpu', 'srun', 'SLURM_ARRAY_TASK_ID'),
])
def test_rendered_commands_expand_array_index(production_config, tmp_path, site, launcher, index_name):
    import os
    from dlpgen_opt.pbs import render_script as render_pbs
    from dlpgen_opt.slurm import render_script as render_slurm
    from dlpgen_opt.scheduling import Stage
    profile = load_profile('configs/batch/sites.yaml', site)
    profile.modules = []
    fake = tmp_path / launcher
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    image = tmp_path / 'image with spaces.sif'
    image.touch()
    options = dict(container=image, job_name='test', log_dir=tmp_path,
                   bind_paths=[tmp_path], max_concurrent=2)
    if site == 'polaris':
        profile.module_paths = []
        script = render_pbs(production_config, profile,
                            Stage('run', 100, 224, jobs_per_task=32), **options)
    else:
        options['container'] = 'docker:example/image:tag'
        script = render_slurm(production_config, profile, first_job=100, last_job=224, **options)
    env = {**os.environ, 'PATH': f'{tmp_path}:' + os.environ['PATH'], index_name: '3',
           'PBS_JOBID': '123[3].server', 'SLURM_JOB_ID': '123', 'SLURM_NODELIST': 'node'}
    result = subprocess.run(['bash'], input=script, text=True, capture_output=True, env=env, check=True)
    output = next(tmp_path.glob('submit*.out')).read_text() if site == 'polaris' else result.stdout
    assert '--first\n196\n--last\n224\n--count\n32' in output


def test_default_profiles_include_all_sites_and_keep_s3df_default():
    from dlpgen_opt.cli import parser
    from dlpgen_opt.slurm import default_profiles_path
    args = parser().parse_args(['submit', 'production.yaml'])
    assert args.profile == 's3df_milano'
    assert Path(args.profiles) == default_profiles_path()
    for name in ('s3df_milano', 's3df_roma', 'aurora', 'polaris', 'nersc_cpu'):
        load_profile(args.profiles, name)
    for name in ('s3df_milano', 's3df_roma'):
        assert load_profile(args.profiles, name) == load_profile('configs/slurm/s3df.yaml', name)
    assert load_profile(args.profiles, args.profile).cpus_per_task == 1
