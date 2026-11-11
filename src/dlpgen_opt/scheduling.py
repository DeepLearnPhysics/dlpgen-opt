"""Scheduler-independent production/preparation dependency chain."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import math
import re
import subprocess




@dataclass(frozen=True)
class Stage:
    kind: str
    first: int = 0
    last: int | None = None
    round_index: int = 0
    jobs_per_task: int = 1

    @property
    def array_count(self):
        return None if self.last is None else math.ceil((self.last - self.first + 1) / self.jobs_per_task)

    @property
    def name(self):
        if self.kind == 'run':
            return f'submit_{self.first:05d}_{self.last:05d}'
        if self.kind == 'prepare':
            return 'prepare_gibuu'
        return f'prepare_r{self.round_index:03d}_{self.kind}_{self.first:05d}'


def preparation_settings(source, jobs):
    """Resolve scheduling fields without importing the physics configuration model."""
    if getattr(source, 'type', None) != 'gibuu':
        return False, None
    mode = getattr(source, 'mode', 'import')
    cache = getattr(source, 'candidate_cache', None)
    enabled = getattr(cache, 'enabled', True)
    sizing = getattr(cache, 'sizing', 'auto')
    allocation = getattr(cache, 'allocation', 'adaptive')
    tasks = getattr(cache, 'distributed_tasks', 'auto')
    if mode not in ('generate', 'import') or type(enabled) is not bool:
        raise ValueError('invalid GiBUU mode or candidate_cache.enabled')
    if sizing not in ('auto', 'fixed') or allocation not in ('adaptive', 'uniform'):
        raise ValueError('invalid GiBUU cache sizing or allocation')
    if tasks is not None and tasks != 'auto' and (type(tasks) is not int or tasks <= 0):
        raise ValueError('distributed_tasks must be auto, null, or a positive integer')
    compatible = mode == 'generate' and enabled and sizing == 'auto' and allocation == 'adaptive'
    if not compatible:
        if tasks not in (None, 'auto'):
            raise ValueError('distributed GiBUU preparation requires generate mode and enabled, auto, adaptive caching')
        tasks = None
    elif tasks == 'auto':
        tasks = jobs
    return mode == 'generate' and enabled, tasks


def stages(config, max_array_size, prepare_round=0, jobs_per_task=1, prepare_jobs_per_task=1):
    if prepare_round < 0:
        raise ValueError('prepare-round must be nonnegative')
    prepare, tasks = preparation_settings(config.source, config.production.jobs)
    if prepare_round and not tasks:
        raise ValueError('prepare-round requires distributed GiBUU preparation')
    result=[]
    if tasks:
        result.append(Stage('plan',round_index=prepare_round))
        width = max_array_size * prepare_jobs_per_task
        result.extend(Stage('worker', first, min(first+width,tasks)-1, prepare_round, prepare_jobs_per_task)
                      for first in range(0,tasks,width))
        result.append(Stage('finalize',round_index=prepare_round))
    elif prepare:
        result.append(Stage('prepare'))
    width=max_array_size*jobs_per_task
    result.extend(Stage('run',first,min(first+width,config.production.jobs)-1,
                        jobs_per_task=jobs_per_task)
                  for first in range(0,config.production.jobs,width))
    return result


def submit_chain(plan, directory, render, scheduler, dry_run=False):
    """Chain entire arrays so chunking never multiplies the concurrency cap."""
    suffix='pbs' if scheduler=='pbs' else 'sbatch'
    results=[]
    dependency=None
    for stage in plan:
        path=directory/f'{stage.name}.{suffix}'
        script=render(stage)
        if dry_run:
            print(f'# {path}\n# Dependency: afterok of preceding stage (if any)\n{script}',end='')
            results.append((path,None))
            continue
        (directory/'logs').mkdir(parents=True,exist_ok=True)
        path.write_text(script,encoding='utf-8')
        command=['qsub'] if scheduler=='pbs' else ['sbatch','--parsable']
        if dependency:
            command.extend(['-W',f'depend=afterok:{dependency}'] if scheduler=='pbs'
                           else ['--dependency',f'afterok:{dependency}'])
        command.append(str(path))
        try:
            completed=subprocess.run(command,check=True,capture_output=True,text=True)
        except subprocess.CalledProcessError as error:
            detail=(error.stderr or error.stdout or str(error)).strip()
            raise RuntimeError(f'{command[0]} failed for {path}: {detail}') from error
        job_id=completed.stdout.strip()
        if scheduler=='slurm':
            job_id=job_id.split(';',1)[0]
        pattern=r'\d+(?:\[\])?(?:\.[A-Za-z0-9_.-]+)?' if scheduler=='pbs' else r'\d+'
        if not re.fullmatch(pattern,job_id):
            raise RuntimeError(f'{command[0]} returned invalid job ID for {path}: {job_id!r}')
        dependency=job_id
        print(f'submitted {path}: {job_id}')
        results.append((path,job_id))
    return results
