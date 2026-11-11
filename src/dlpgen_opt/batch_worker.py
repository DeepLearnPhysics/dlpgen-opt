"""Run independent production jobs within one scheduler allocation."""
from __future__ import annotations

import argparse
import json
import resource
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


from .cpu_affinity import parse_cpu_set


def run_jobs(jobs, command, *, cpus, log_dir, pin_cpus=False):
    """Bound concurrency, preserve each job's logs, and propagate any failure."""
    jobs = list(jobs)
    if cpus < 1 or not jobs:
        raise ValueError('positive CPUs and a nonempty job range are required')
    try:
        cpu_ids = sorted(os.sched_getaffinity(0))
        available = len(cpu_ids)
    except (AttributeError, OSError):
        available = os.cpu_count() or 1
        cpu_ids = list(range(available))
        if pin_cpus:
            raise RuntimeError("CPU pinning requires Linux sched_getaffinity")
    budget = min(cpus, available)
    concurrency = min(len(jobs), budget)
    env = dict(os.environ, DLPGEN_OPT_CPUS_PER_TASK=str(budget // concurrency),
               OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    usage_start = resource.getrusage(resource.RUSAGE_CHILDREN)
    per_job = budget // concurrency
    print(f'[batch] affinity={cpu_ids}; requested_cpus={cpus}; effective_cpus={budget}; '
          f'concurrency={concurrency}; cpus_per_child={per_job}', flush=True)
    if available < cpus:
        print('[batch] WARNING: CPU affinity is smaller than the requested worker budget', flush=True)
    slots = list(range(concurrency))
    active = {}
    pending = iter(jobs)
    failed = False
    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        exhausted = False
        while active or not exhausted:
            if stopped:
                return 130
            while not exhausted and len(active) < concurrency:
                job = next(pending, None)
                if job is None:
                    exhausted = True
                    break
                # Append on retries so previous failure diagnostics remain available.
                with (log_dir / f'{job:05d}.out').open('ab') as out, \
                     (log_dir / f'{job:05d}.err').open('ab') as err:
                    slot = slots.pop(0)
                    argv = command(job)
                    if pin_cpus:
                        assigned = cpu_ids[slot * per_job:(slot + 1) * per_job]
                        argv = [sys.executable, '-m', 'dlpgen_opt.batch_worker',
                                '--exec-cpus', ','.join(map(str, assigned)), '--', *argv]
                    process = subprocess.Popen(argv, stdout=out, stderr=err,
                                               env=env, start_new_session=True)
                active[process] = (job, slot)
                print(f'[batch] Started task {job}; logs: {log_dir}/{job:05d}.*', flush=True)
            for process, (job, slot) in list(active.items()):
                status = process.poll()
                if status is not None:
                    failed |= status != 0
                    print(f'[batch] Finished task {job}; exit={status}', flush=True)
                    del active[process]
                    slots.append(slot)
            if active:
                time.sleep(0.1)
        return 1 if failed else 0
    except BaseException:
        failed = True
        raise
    finally:
        for process in active:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 5
        for process in active:
            try:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        wall = time.monotonic() - started
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_seconds = usage.ru_utime + usage.ru_stime - usage_start.ru_utime - usage_start.ru_stime
        summary = dict(wall_seconds=wall, cpu_seconds=cpu_seconds,
                       average_cpu_cores=cpu_seconds / wall if wall else 0,
                       budget_utilization=cpu_seconds / (wall * budget) if wall else 0,
                       requested_cpus=cpus, effective_cpus=budget,
                       concurrency=concurrency, cpus_per_child=per_job,
                       affinity=cpu_ids, completed=len(jobs) if exhausted and not active else None,
                       failed=failed, interrupted=stopped)
        # Timestamp and PID retain summaries across containers and retries.
        path = log_dir / f'utilization-{time.time_ns()}-{os.getpid()}.json'
        path.write_text(json.dumps(summary, indent=2) + '\n')
        print(f'[batch] Average CPU cores={summary["average_cpu_cores"]:.2f}; '
              f'worker-budget utilization={summary["budget_utilization"]:.1%}; summary={path}', flush=True)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == '--exec-cpus':
        if len(argv) < 4 or argv[2] != '--':
            raise ValueError('invalid CPU-bound child command')
        os.sched_setaffinity(0, parse_cpu_set(argv[1]))
        os.execvpe(argv[3], argv[3:], os.environ)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('--first', type=int, default=0)
    parser.add_argument('--last', type=int)
    parser.add_argument('--count', type=int, default=1)
    parser.add_argument('--cpus', type=int, required=True)
    parser.add_argument('--log-dir', type=Path, required=True)
    parser.add_argument('--phase', choices=('run', 'worker', 'plan', 'finalize', 'prepare'), default='run')
    parser.add_argument('--round', type=int, default=0)
    parser.add_argument('--pin-cpus', action='store_true')
    args = parser.parse_args(argv)
    from .config import load_config
    config = load_config(args.config)
    limit = config.production.jobs
    if args.phase == 'worker':
        from .scheduling import preparation_settings
        _, limit = preparation_settings(config.source, config.production.jobs)
        if limit is None:
            parser.error('worker phase requires distributed GiBUU')
    elif args.phase != 'run':
        limit = 1
    last = args.first if args.last is None else args.last
    if not 0 <= args.first <= last < limit or args.count < 1 or args.cpus < 1 or args.round < 0:
        parser.error('invalid task range, count, round, or CPU allocation')
    jobs = range(args.first, min(args.first + args.count, last + 1))
    def command(job):
        prefix = [sys.executable, '-m', 'dlpgen_opt.cli']
        if args.phase == 'run':
            return [*prefix, 'run', str(config.config_path), '--job', str(job)]
        if args.phase == 'prepare':
            return [*prefix, 'prepare', str(config.config_path)]
        result = [*prefix, 'prepare-gibuu-distributed', str(config.config_path),
                  '--phase', args.phase, '--round', str(args.round)]
        return result + ['--task', str(job)] if args.phase == 'worker' else result
    return run_jobs(jobs, command, cpus=args.cpus, log_dir=args.log_dir, pin_cpus=args.pin_cpus)


if __name__ == '__main__':
    raise SystemExit(main())
