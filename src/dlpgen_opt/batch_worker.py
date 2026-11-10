"""Run independent production jobs within one scheduler allocation."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def run_jobs(jobs, command, *, cpus, log_dir):
    """Bound concurrency, preserve each job's logs, and propagate any failure."""
    jobs = list(jobs)
    if cpus < 1 or not jobs:
        raise ValueError('positive CPUs and a nonempty job range are required')
    try:
        available = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        available = os.cpu_count() or 1
    budget = min(cpus, available)
    concurrency = min(len(jobs), budget)
    env = dict(os.environ, DLPGEN_OPT_CPUS_PER_TASK=str(budget // concurrency),
               OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
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
                    process = subprocess.Popen(command(job), stdout=out, stderr=err,
                                               env=env, start_new_session=True)
                active[process] = job
                print(f'[batch] Started production job {job}; logs: {log_dir}/{job:05d}.*', flush=True)
            for process, job in list(active.items()):
                status = process.poll()
                if status is not None:
                    failed |= status != 0
                    print(f'[batch] Finished production job {job}; exit={status}', flush=True)
                    del active[process]
            if active:
                time.sleep(0.1)
        return 1 if failed else 0
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
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('--first', type=int, required=True)
    parser.add_argument('--last', type=int, required=True)
    parser.add_argument('--count', type=int, required=True)
    parser.add_argument('--cpus', type=int, required=True)
    parser.add_argument('--log-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    from .config import load_config
    config = load_config(args.config)
    if not 0 <= args.first <= args.last < config.production.jobs or args.count < 1 or args.cpus < 1:
        parser.error('invalid production job range, count, or CPU allocation')
    jobs = range(args.first, min(args.first + args.count, args.last + 1))
    return run_jobs(jobs, lambda job: [sys.executable, '-m', 'dlpgen_opt.cli', 'run',
                    str(config.config_path), '--job', str(job)],
                    cpus=args.cpus, log_dir=args.log_dir)


if __name__ == '__main__':
    raise SystemExit(main())
