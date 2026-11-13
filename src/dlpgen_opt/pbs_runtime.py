"""Python 3.6-compatible PBS rendering and submission; no physics imports."""
from copy import copy
from pathlib import Path
import re
import shlex
from .batch_runtime import container_command, container_reference
from .scheduling import Stage, stages, submit_chain
from .batch_mounts import production_mounts

def render_script(config, profile, stage, *, container, job_name, log_dir, bind_paths, max_concurrent):
    cpus=(profile.prepare_cpus_per_task or profile.cpus_per_task) if stage.kind!='run' else profile.cpus_per_task
    walltime=(profile.prepare_time or profile.time) if stage.kind!='run' else profile.time
    # Omit ncpus for the whole-node profiles; CPU budgets only constrain execution.
    # Polaris defines a system resource; Aurora's PBS server does not.
    resources='select=1'
    if profile.system == 'polaris':
        resources+=':system=polaris'
    resources+=':mpiprocs=1'
    if profile.node_cpus is None:
        resources+=f':ncpus={cpus}'
    if profile.mem:
        resources+=f':mem={profile.mem}'
    lines=['#!/bin/bash -l',f'#PBS -A {profile.account}',f'#PBS -q {profile.queue}',
           f'#PBS -l {resources}',f'#PBS -l place={profile.place}',f'#PBS -l walltime={walltime}',
           f'#PBS -l filesystems={profile.filesystems}',f'#PBS -N {job_name}', '#PBS -r y']
    if profile.fakeroot:
        lines.append('#PBS -l singularity_fakeroot=true')
    if stage.array_count is not None and stage.array_count > 1:
        lines.append(f'#PBS -J 0-{stage.array_count-1}%{min(max_concurrent,stage.array_count)}')
    # PBS supplies unique scheduler log names. Redirect within the batch shell
    # to live per-subjob files; directive values do not expand shell variables.
    lines+=['set -euo pipefail',f'cd {shlex.quote(str(config.config_path.parent))}',
            f'test -r {shlex.quote(str(container))}',
            f'exec >{shlex.quote(str(log_dir))}/"{stage.name}_${{PBS_JOBID}}_${{PBS_ARRAY_INDEX:-0}}.out" '
            f'2>{shlex.quote(str(log_dir))}/"{stage.name}_${{PBS_JOBID}}_${{PBS_ARRAY_INDEX:-0}}.err"']
    lines += ['module use '+shlex.quote(str(path)) for path in profile.module_paths]
    lines += ['module load '+shlex.quote(module) for module in profile.modules]
    command=container_command(profile.container_runtime,container,bind_paths,cpus=str(cpus),fakeroot=profile.fakeroot)
    cfg=shlex.quote(str(config.config_path))
    command+=(f' python3 -m dlpgen_opt.batch_worker {cfg}'
              f' --phase {stage.kind} --round {stage.round_index}'
              f' --cpus {cpus} --pin-cpus'
              f' --log-dir {shlex.quote(str(log_dir / "packed" / stage.name))}')
    if stage.array_count is not None:
        if stage.array_count == 1:
            command += f' --first {stage.first}'
        else:
            command += f' --first "$((PBS_ARRAY_INDEX * {stage.jobs_per_task} + {stage.first}))"'
        command += f' --last {stage.last} --count {stage.jobs_per_task}'
    # Launch on the allocated compute node with enough CPU affinity for Python's
    # native worker pool. This is one launcher rank, not an MPI physics program.
    lines.append(f'exec mpiexec -n 1 --ppn 1 --depth {cpus} --cpu-bind {profile.cpu_bind} '+command)
    return '\n'.join(lines)+'\n'


def submit_arrays(config,profile,*,container,max_concurrent=None,job_name=None,
                  extra_bind_paths=None,dry_run=False,prepare_round=0):
    container=container_reference(container,profile.container_runtime,dry_run)
    cap=profile.max_concurrent if max_concurrent is None else max_concurrent
    if cap<=0:
        raise ValueError('max_concurrent must be positive')
    if cap>profile.max_concurrent:
        raise ValueError('max_concurrent exceeds the PBS profile limit; adjust the profile after checking queue policy')
    name=job_name or config.production.name
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',name):
        raise ValueError('invalid PBS job name')
    directory=config.production.output_dir/'pbs'
    binds=production_mounts(config, profile.bind_paths, extra_bind_paths or [])
    if profile.submission_mode == 'allocation':
        # One scheduler entry, including preparation barriers. Native worker
        # packs retain their CPU budget; production children run in waves.
        plan=stages(config,1,prepare_round,config.production.jobs,profile.prepare_jobs_per_task)
        whole=Stage('run',0,config.production.jobs-1,jobs_per_task=config.production.jobs)
        def render_all(_):
            allocation_profile=copy(profile)
            allocation_profile.cpus_per_task=max(profile.cpus_per_task,
                profile.prepare_cpus_per_task or profile.cpus_per_task)
            script=render_script(config,allocation_profile,whole,container=container,job_name=name,
                log_dir=directory/'logs',bind_paths=binds,max_concurrent=1)
            header=script.rsplit('exec mpiexec ',1)[0]
            commands=[]
            for stage in plan:
                rendered=render_script(config,profile,stage,container=container,job_name=name,
                    log_dir=directory/'logs',bind_paths=binds,max_concurrent=1)
                commands.append("echo " + shlex.quote('[batch] Starting ' + stage.name))
                commands.append('mpiexec ' + rendered.rsplit('exec mpiexec ',1)[1].rstrip())
                commands.append("echo " + shlex.quote('[batch] Completed ' + stage.name))
            return header + '\n'.join(commands) + '\n'
        print(('# ' if dry_run else '') +
              '[submit] One PBS allocation; preparation and production run in CPU-bounded waves; '
              'walltime=' + profile.time)
        return submit_chain([whole],directory,render_all,'pbs',dry_run)
    plan=stages(config,profile.max_array_size,prepare_round,profile.jobs_per_task,profile.prepare_jobs_per_task)
    return submit_chain(plan,directory,lambda stage: render_script(config,profile,stage,
        container=container,job_name=name,log_dir=directory/'logs',bind_paths=binds,max_concurrent=cap),'pbs',dry_run)
