"""PBS Professional submission for ALCF Aurora and Polaris CPU workloads."""
from pathlib import Path
from typing import Literal
import re
import shlex

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .batch_runtime import container_command, container_reference
from .scheduling import stages, submit_chain


class PBSProfile(BaseModel):
    model_config=ConfigDict(extra='forbid')
    scheduler: Literal['pbs']='pbs'
    account: str=Field(pattern=r'^[A-Za-z0-9_.:-]+$')
    queue: str=Field(pattern=r'^[A-Za-z0-9_.-]+$')
    system: Literal['aurora','polaris']
    container_runtime: Literal['apptainer','singularity']='apptainer'
    cpus_per_task: int=Field(default=1,gt=0)
    prepare_cpus_per_task: int | None=Field(default=None,gt=0)
    jobs_per_task: int=Field(default=1,gt=0)
    time: str=Field(default='02:00:00',pattern=r'^\d+:\d{2}:\d{2}$')
    prepare_time: str | None=Field(default=None,pattern=r'^\d+:\d{2}:\d{2}$')
    filesystems: str=Field(pattern=r'^[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)*$')
    place: Literal['scatter','scatter:excl']='scatter:excl'
    mem: str | None=Field(default=None,pattern=r'^[0-9]+[KMGTPkmgpt]?[bB]?$')
    max_array_size: int=Field(default=100,gt=0)
    max_concurrent: int=Field(default=2,gt=0)
    bind_paths: list[Path]=Field(default_factory=list)
    modules: list[str]=Field(default_factory=lambda:['apptainer'])
    module_paths: list[Path]=Field(default_factory=list)

    @model_validator(mode='after')
    def valid_resources(self):
        if self.jobs_per_task > self.cpus_per_task:
            raise ValueError('jobs_per_task cannot exceed cpus_per_task')
        if any(not re.fullmatch(r'[A-Za-z0-9_./+-]+',name) for name in self.modules):
            raise ValueError('invalid module name')
        return self


def render_script(config, profile, stage, *, container, job_name, log_dir, bind_paths, max_concurrent):
    cpus=(profile.prepare_cpus_per_task or profile.cpus_per_task) if stage.kind!='run' else profile.cpus_per_task
    walltime=(profile.prepare_time or profile.time) if stage.kind!='run' else profile.time
    resources=f'select=1:system={profile.system}:ncpus={cpus}:mpiprocs=1'
    if profile.mem:
        resources+=f':mem={profile.mem}'
    lines=['#!/bin/bash -l',f'#PBS -A {profile.account}',f'#PBS -q {profile.queue}',
           f'#PBS -l {resources}',f'#PBS -l place={profile.place}',f'#PBS -l walltime={walltime}',
           f'#PBS -l filesystems={profile.filesystems}',f'#PBS -N {job_name}', '#PBS -r y']
    if stage.array_count is not None:
        lines.append(f'#PBS -J 0-{stage.array_count-1}%{min(max_concurrent,stage.array_count)}')
    # PBS supplies unique scheduler log names. Redirect within the batch shell
    # to live per-subjob files; directive values do not expand shell variables.
    lines+=['set -euo pipefail',f'cd {shlex.quote(str(config.config_path.parent))}',
            f'test -r {shlex.quote(str(container))}',
            f'exec >{shlex.quote(str(log_dir))}/"{stage.name}_${{PBS_JOBID}}_${{PBS_ARRAY_INDEX:-0}}.out" '
            f'2>{shlex.quote(str(log_dir))}/"{stage.name}_${{PBS_JOBID}}_${{PBS_ARRAY_INDEX:-0}}.err"']
    lines += ['module use '+shlex.quote(str(path)) for path in profile.module_paths]
    lines += ['module load '+shlex.quote(module) for module in profile.modules]
    command=container_command(profile.container_runtime,container,bind_paths,cpus=str(cpus))
    cfg=shlex.quote(str(config.config_path))
    if stage.kind=='run':
        if stage.jobs_per_task==1:
            command+=f' run {cfg} --job "$((PBS_ARRAY_INDEX + {stage.first}))"'
        else:
            command+=(f' python3 -m dlpgen_opt.batch_worker {cfg}'
                f' --first "$((PBS_ARRAY_INDEX * {stage.jobs_per_task} + {stage.first}))"'
                f' --last {stage.last} --count {stage.jobs_per_task} --cpus {cpus}'
                f' --log-dir {shlex.quote(str(log_dir/"packed"))}')
    elif stage.kind=='prepare':
        command+=f' prepare {cfg}'
    else:
        command+=f' prepare-gibuu-distributed {cfg} --phase {stage.kind} --round {stage.round_index}'
        if stage.kind=='worker':
            command+=f' --task "$((PBS_ARRAY_INDEX + {stage.first}))"'
    # Launch on the allocated compute node with enough CPU affinity for Python's
    # native worker pool. This is one launcher rank, not an MPI physics program.
    lines.append(f'exec mpiexec -n 1 --ppn 1 --depth {cpus} --cpu-bind depth '+command)
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
    binds=[path.expanduser().resolve() for path in profile.bind_paths]
    binds.extend(Path(path).expanduser().resolve() for path in extra_bind_paths or [])
    binds=list(dict.fromkeys(binds))
    plan=stages(config,profile.max_array_size,prepare_round,profile.jobs_per_task)
    return submit_chain(plan,directory,lambda stage: render_script(config,profile,stage,
        container=container,job_name=name,log_dir=directory/'logs',bind_paths=binds,max_concurrent=cap),'pbs',dry_run)
