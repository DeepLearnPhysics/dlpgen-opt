from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from dlpgen_opt.config import load_config
from dlpgen_opt.slurm import SlurmProfile, submit_arrays


def profile(max_array_size: int = 99) -> SlurmProfile:
    return SlurmProfile(
        account="neutrino:ml-dev",
        partition="milano",
        cpus_per_task=1,
        mem_per_cpu="4G",
        time="02:00:00",
        max_array_size=max_array_size,
        bind_paths=[Path("/sdf")],
    )


def test_dry_run_renders_zero_based_array(production_config, tmp_path, capsys):
    container = tmp_path / "image.sif"
    submit_arrays(
        production_config,
        profile(),
        container=container,
        max_concurrent=1,
        dry_run=True,
    )
    script = capsys.readouterr().out
    assert "#SBATCH --partition=milano" in script
    assert "#SBATCH --array=0-1%1" in script
    assert (
        "singularity exec --cleanenv --env PYTHONNOUSERSITE=1 --bind /sdf"
        in script
    )
    assert '--job "$((SLURM_ARRAY_TASK_ID + 0))"' in script
    assert (
        "dlpgen-opt job ${SLURM_JOB_ID}/${SLURM_ARRAY_TASK_ID} "
        "completed successfully"
    ) in script
    assert 'echo "Completed: $(date --iso-8601=seconds)"' in script
    assert not production_config.production.output_dir.exists()


def test_submit_chunks_at_profile_limit(production_config, tmp_path):
    production_config.production.jobs = 3
    container = tmp_path / "image.sif"
    container.touch()
    completed = subprocess.CompletedProcess(
        args=["sbatch"], returncode=0, stdout="12345;cluster\n", stderr=""
    )
    with patch("dlpgen_opt.slurm.subprocess.run", return_value=completed) as run:
        results = submit_arrays(
            production_config,
            profile(max_array_size=2),
            container=container,
        )

    assert [job_id for _, job_id in results] == ["12345", "12345"]
    assert run.call_count == 2
    assert run.call_args_list[1].args[0][2:4] == ["--dependency", "afterok:12345"]
    scripts = [path.read_text(encoding="utf-8") for path, _ in results]
    assert "#SBATCH --array=0-1" in scripts[0]
    assert '--job "$((SLURM_ARRAY_TASK_ID + 0))"' in scripts[0]
    assert "#SBATCH --array=0-0" in scripts[1]
    assert '--job "$((SLURM_ARRAY_TASK_ID + 2))"' in scripts[1]


def test_large_production_uses_local_array_indices(production_config, tmp_path, capsys):
    production_config.production.jobs = 120
    submit_arrays(
        production_config,
        profile(max_array_size=99),
        container=tmp_path / "image.sif",
        max_concurrent=40,
        dry_run=True,
    )

    scripts = capsys.readouterr().out
    assert "#SBATCH --array=0-98%40" in scripts
    assert "#SBATCH --array=0-20" in scripts
    assert "#SBATCH --array=99-119" not in scripts
    assert '--job "$((SLURM_ARRAY_TASK_ID + 99))"' in scripts


def test_cached_gibuu_submission_prepares_before_array(tmp_path, capsys):
    config = load_config("configs/production.gibuu-bnb.yaml")
    submit_arrays(
        config,
        profile(),
        container=tmp_path / "image.sif",
        dry_run=True,
    )

    scripts = capsys.readouterr().out
    assert "prepare_r000_plan_00000.sbatch" in scripts
    assert "dlpgen-opt-entrypoint prepare" in scripts
    assert scripts.index("prepare_r000_plan_00000.sbatch") < scripts.index("submit_00000")


def test_parallel_prepare_respects_profile_allocation(tmp_path):
    from dlpgen_opt.config import load_config
    from dlpgen_opt.slurm import SlurmProfile, render_prepare_script, render_script
    config = load_config('configs/production.gibuu-bnb.yaml')
    config.source.candidate_cache.workers = 8
    profile = SlurmProfile(account='test', partition='cpu', cpus_per_task=1,
                           prepare_cpus_per_task=4, prepare_time='12:00:00')
    script = render_prepare_script(config, profile, container=tmp_path / 'image.sif',
                                   job_name='test', log_dir=tmp_path, bind_paths=[tmp_path])
    assert '#SBATCH --cpus-per-task=4' in script
    assert '--env "SLURM_CPUS_PER_TASK=${SLURM_CPUS_PER_TASK:-4}"' in script
    assert '#SBATCH --time=12:00:00' in script
    assert profile.cpus_per_task == 1
    assert profile.time == '02:00:00'
    array = render_script(config, profile, container=tmp_path / 'image.sif',
                          first_job=0, last_job=2, max_concurrent=None,
                          job_name='test', log_dir=tmp_path, bind_paths=[tmp_path])
    assert '#SBATCH --cpus-per-task=1' in array
    assert '#SBATCH --time=02:00:00' in array


def test_distributed_chain_caps_each_array_and_gates_production(tmp_path):
    config = load_config('configs/production.gibuu-bnb.yaml')
    config.production.output_dir = tmp_path / 'production'
    config.production.jobs = 125
    config.source.candidate_cache.distributed_tasks = 125
    container = tmp_path / 'image.sif'
    container.touch()
    responses = [subprocess.CompletedProcess(args=[], returncode=0, stdout=f'{i}\n', stderr='')
                 for i in range(100,106)]
    with patch('dlpgen_opt.slurm.subprocess.run', side_effect=responses) as submit:
        result = submit_arrays(config,profile(),container=container,max_concurrent=10)
    scripts = [path.read_text() for path, _ in result]
    assert len(scripts) == 6  # plan, two worker arrays, finalize, two production arrays
    assert '--phase plan --round 0' in scripts[0]
    assert '#SBATCH --array=0-98%10' in scripts[1]
    assert '--task "$((SLURM_ARRAY_TASK_ID + 0))"' in scripts[1]
    assert '#SBATCH --array=0-25%10' in scripts[2]
    assert '--task "$((SLURM_ARRAY_TASK_ID + 99))"' in scripts[2]
    assert '--phase finalize --round 0' in scripts[3]
    for i, call in enumerate(submit.call_args_list):
        if i:
            assert call.args[0][2:4] == ['--dependency',f'afterok:{99+i}']
        else:
            assert '--dependency' not in call.args[0]
    for script in scripts:
        assert '#SBATCH --kill-on-invalid-dep=yes' in script
        checked = subprocess.run(['bash','-n'],input=script,text=True,capture_output=True)
        assert checked.returncode == 0, checked.stderr


def test_apptainer_and_package_overlay_are_retained_in_all_stages(tmp_path,capsys):
    config=load_config('configs/production.gibuu-bnb.yaml')
    config.source.candidate_cache.distributed_tasks=125
    runtime=profile()
    runtime.container_runtime='apptainer'
    overlay='/sdf/debug/dlpgen-opt/src/dlpgen_opt:/usr/local/lib/python3.10/dist-packages/dlpgen_opt:ro'
    submit_arrays(config,runtime,container=tmp_path/'image.sif',max_concurrent=10,
                  extra_bind_paths=[overlay],dry_run=True)
    scripts=capsys.readouterr().out
    assert scripts.count('apptainer exec --cleanenv')==5
    assert scripts.count(overlay)==5


def test_auto_preparation_tracks_jobs_and_cpu_profile(tmp_path,capsys):
    config=load_config('configs/production.gibuu-bnb.yaml')
    config.production.jobs=125
    runtime=profile()
    runtime.cpus_per_task=4
    submit_arrays(config,runtime,container=tmp_path/'image.sif',max_concurrent=10,dry_run=True)
    scripts=capsys.readouterr().out
    assert '#SBATCH --array=0-98%10' in scripts
    assert '#SBATCH --array=0-25%10' in scripts
    assert '--env "SLURM_CPUS_PER_TASK=${SLURM_CPUS_PER_TASK:-4}"' in scripts
    config.source.candidate_cache.distributed_tasks=None
    submit_arrays(config,runtime,container=tmp_path/'image.sif',dry_run=True)
    serial=capsys.readouterr().out
    assert 'prepare_gibuu.sbatch' in serial
    assert '--phase worker' not in serial


def test_cpu_allocation_is_expanded_before_clean_container_environment(tmp_path):
    import os
    from dlpgen_opt.slurm import render_prepare_script
    config=load_config('configs/production.gibuu-bnb.yaml')
    runtime=profile()
    runtime.prepare_cpus_per_task=4
    fake=tmp_path/'singularity'
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    script=render_prepare_script(config,runtime,container=tmp_path/'image.sif',
        job_name='test',log_dir=tmp_path,bind_paths=[tmp_path],phase='plan')
    for allocation in (None,'2'):
        env={**os.environ,'PATH':f'{tmp_path}:'+os.environ['PATH']}
        env.pop('SLURM_CPUS_PER_TASK',None)
        if allocation:
            env['SLURM_CPUS_PER_TASK']=allocation
        result=subprocess.run(['bash'],input=script,text=True,capture_output=True,env=env,check=True)
        assert f'SLURM_CPUS_PER_TASK={allocation or "4"}' in result.stdout.splitlines()
