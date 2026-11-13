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
    if site != 'nersc_cpu':
        profile.submission_mode = 'arrays'
        profile.max_concurrent = 2
    plan = stages(config, profile.max_array_size, jobs_per_task=profile.jobs_per_task,
                  prepare_jobs_per_task=getattr(profile, 'prepare_jobs_per_task', 1))
    expected = ([None, 100, 25, None, 4] if site == 'nersc_cpu' else
                [None, 11, None, 2] if site == 'aurora' else [None, 32, None, 4])
    assert [stage.array_count for stage in plan] == expected
    image = 'docker:example/image:tag' if site == 'nersc_cpu' else tmp_path / 'image.sif'
    submit_arrays(config, profile, container=image, dry_run=True, max_concurrent=2)
    script = capsys.readouterr().out
    subprocess.run(['bash', '-n'], input=script, text=True, check=True)
    assert not config.production.output_dir.exists()
    assert '--phase finalize' in script
    assert '--last 124 --count' in script
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
        selection = 'select=1:system=polaris:mpiprocs=1' if site == 'polaris' else 'select=1:mpiprocs=1'
        assert '#PBS -l ' + selection + '\n' in script
        assert (':system=' in script) == (site == 'polaris')
        assert ('#PBS -l singularity_fakeroot=true' in script) == (site == 'polaris')
        assert ('--fakeroot' in script) == (site == 'polaris')
        assert '#PBS -r y' in script
        assert f'#PBS -J 0-{expected[1]-1}%2' in script
        assert f'--first "$((PBS_ARRAY_INDEX * {profile.prepare_jobs_per_task} + 0))"' in script
        assert f'--depth {profile.cpus_per_task} --cpu-bind {profile.cpu_bind}' in script
        assert f'--env "DLPGEN_OPT_CPUS_PER_TASK={profile.cpus_per_task}"' in script
        assert ':ncpus=' not in script
        assert '#PBS -l place=scatter:excl' in script
        assert '--pin-cpus' in script


def test_pbs_full_array_ids_in_dependencies(production_config, tmp_path):
    profile = load_profile('configs/batch/sites.yaml', 'polaris')
    profile.account = 'project'
    profile.submission_mode = 'arrays'
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
    profile.submission_mode = 'arrays'
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
    profile = load_profile('configs/batch/sites.yaml', 'nersc_cpu')
    assert profile.account == 'm5252'
    profile.account = 'REPLACE_WITH_PROJECT'
    with pytest.raises(ValueError, match='allocation'):
        submit_arrays(production_config, profile,
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


def test_packed_preparation_covers_each_task_once(tmp_path):
    from dlpgen_opt.batch_worker import main
    config = load_config('configs/production.gibuu-bnb.yaml')
    config.production.jobs = 125
    plan = stages(config, 3, jobs_per_task=32, prepare_jobs_per_task=12)
    seen = []
    for stage in plan:
        if stage.kind != 'worker':
            continue
        assert stage.array_count <= 3
        for array_index in range(stage.array_count):
            first = stage.first + array_index * stage.jobs_per_task
            seen.extend(range(first, min(stage.last + 1, first + stage.jobs_per_task)))
    assert seen == list(range(125))
    # Exercise worker command dispatch with the actual production task count.
    with patch('dlpgen_opt.config.load_config', return_value=config), \
         patch('dlpgen_opt.batch_worker.run_jobs', return_value=0) as run:
        assert main([str(config.config_path), '--phase', 'worker', '--round', '2',
                     '--first', '120', '--last', '124', '--count', '12',
                     '--cpus', '102', '--log-dir', str(tmp_path)]) == 0
    assert list(run.call_args.args[0]) == [120,121,122,123,124]
    command = run.call_args.args[1](123)
    assert command[-6:] == ['--phase', 'worker', '--round', '2', '--task', '123']


def test_node_budget_validation():
    from pydantic import ValidationError
    from dlpgen_opt.pbs import PBSProfile
    settings = load_profile('configs/batch/sites.yaml', 'aurora').model_dump()
    settings['cpus_per_task'] = 103
    with pytest.raises(ValidationError, match='node_cpus'):
        PBSProfile(**settings)
    settings['cpus_per_task'] = 32
    settings['cpu_bind'] = 'list:0-15'
    with pytest.raises(ValidationError, match='enough CPUs'):
        PBSProfile(**settings)


def test_cpu_child_pinning(monkeypatch):
    import os
    from dlpgen_opt.batch_worker import main
    with patch.object(os, 'sched_setaffinity', create=True) as bind, \
         patch.object(os, 'execvpe', side_effect=SystemExit(0)) as execute:
        with pytest.raises(SystemExit):
            main(['--exec-cpus', '1-3,53', '--', 'python3', '-V'])
    bind.assert_called_once_with(0, [1,2,3,53])
    assert execute.call_args.args[:2] == ('python3', ['python3', '-V'])


def test_utilization_summary(tmp_path):
    import json
    assert run_jobs([0], lambda job: [sys.executable, '-c',
        'sum(i*i for i in range(1000000))'], cpus=1, log_dir=tmp_path) == 0
    report = json.loads(next(tmp_path.glob('utilization-*.json')).read_text())
    assert report['cpu_seconds'] > 0
    assert report['wall_seconds'] > 0
    assert report['effective_cpus'] == report['concurrency'] == 1
    assert report['completed'] == 1
    assert report['budget_utilization'] > 0


def test_linux_packed_children_inherit_disjoint_affinity(tmp_path):
    import json
    import os
    if not hasattr(os, 'sched_getaffinity') or len(os.sched_getaffinity(0)) < 4:
        pytest.skip('requires Linux with four allowed CPUs')
    child = ('import os,time,json; start=time.process_time();\n'
             'while time.process_time()-start<0.1: pass\n'
             'print(json.dumps(sorted(os.sched_getaffinity(0))))')
    code = ('import os,sys,subprocess,json; '
            'print(json.dumps(dict(parent=sorted(os.sched_getaffinity(0)), '
            "child=json.loads(subprocess.check_output([sys.executable,'-c',sys.argv[1]])))))")
    assert run_jobs([0,1], lambda job: [sys.executable, '-c', code, child],
                    cpus=4, log_dir=tmp_path, pin_cpus=True) == 0
    records = [json.loads((tmp_path / f'{job:05d}.out').read_text()) for job in range(2)]
    assert all(r['parent'] == r['child'] and len(r['parent']) == 2 for r in records)
    assert not set(records[0]['parent']) & set(records[1]['parent'])
    summary = json.loads(next(tmp_path.glob('utilization-*.json')).read_text())
    assert summary['cpu_seconds'] >= 0.2


@pytest.mark.parametrize('fail_worker', [False, True])
def test_capacity_workflow_barriers_and_failure(tmp_path, fail_worker):
    import os
    config = load_config('configs/production.gibuu-bnb.yaml')
    config.production.jobs = 9
    config.production.output_dir = tmp_path / 'output'
    profile = load_profile('configs/batch/sites.yaml', 'polaris')
    profile.modules = []
    profile.module_paths = []
    image = tmp_path / 'image.sif'
    image.touch()
    with patch('dlpgen_opt.scheduling.subprocess.run', return_value=
               subprocess.CompletedProcess([], 0, '123.server\n', '')) as submit:
        results = submit_arrays(config, profile, container=image)
    assert submit.call_count == 1
    script = results[0][0]
    assert '#PBS -J' not in script.read_text()
    fake = tmp_path / 'mpiexec'
    fake.write_text('#!/usr/bin/env python3\nimport os,sys,json\n'
                    "args=sys.argv; phase=args[args.index('--phase')+1]\n"
                    "with open(os.environ['TRACE'],'a') as f: f.write(json.dumps(args)+'\\n')\n"
                    "sys.exit(7 if phase=='worker' and os.environ.get('FAIL_WORKER')=='1' else 0)\n")
    fake.chmod(0o755)
    trace = tmp_path / 'trace'
    env = dict(os.environ, PATH=str(tmp_path)+':'+os.environ['PATH'],
               PBS_JOBID='123.server', TRACE=str(trace), FAIL_WORKER=str(int(fail_worker)))
    result = subprocess.run(['bash', str(script)], env=env)
    import json
    commands = [json.loads(line) for line in trace.read_text().splitlines()]
    phases = [args[args.index('--phase')+1] for args in commands]
    if fail_worker:
        assert result.returncode == 7
        assert phases == ['plan', 'worker']
    else:
        assert result.returncode == 0
        assert phases == ['plan', 'worker', 'worker', 'worker', 'finalize', 'run']
        ids = []
        for args in commands[1:4]:
            first, last, count = [int(args[args.index(flag)+1]) for flag in ('--first','--last','--count')]
            ids.extend(range(first, min(last+1,first+count)))
        assert ids == list(range(9))


def test_automatic_mounts_external_yaml_glob_and_overlay(tmp_path):
    from types import SimpleNamespace
    from dlpgen_opt.batch_mounts import production_mounts
    root = tmp_path / 'project'
    root.mkdir()
    external = tmp_path / 'flux'
    external.mkdir()
    nested = tmp_path / 'settings'
    nested.mkdir()
    (nested / 'source.yaml').write_text('flux:\n  file_pattern: '+str(external / 'NuBeam*.root')+'\n')
    config_path = root / 'production.yaml'
    config_path.write_text('source:\n  config: ../settings/source.yaml\n')
    storage = tmp_path / 'storage'
    (storage / 'campaign').mkdir(parents=True)
    config = SimpleNamespace(config_path=config_path,
        production=SimpleNamespace(output_dir=storage / 'campaign'))
    overlay = str(root / 'src')+':/usr/local/lib/python3.10/dist-packages/dlpgen_opt:ro'
    mounts = production_mounts(config, [], [overlay])
    assert str(root) in mounts and str(external) in mounts and str(nested) in mounts
    assert str(storage) in mounts  # Includes sibling shared caches.
    assert mounts[-1] == overlay
    assert not (root / 'new').exists()
    assert '/' not in mounts
