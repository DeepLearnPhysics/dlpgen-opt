"""Native fixed-work scaling check, run inside the GiBUU container.

python3 tests/integration/gibuu_scaling.py --flux-dir PATH --output-dir NEW_PATH
Runs the same eight flavor/process pilot tasks at 1, 2, 4 and 8 workers, with
100 ensembles and full 150-step transport. Requires all four beam flavors.
"""
import argparse
import json
import os
import resource
import time
from pathlib import Path
from types import SimpleNamespace

from dlpgen_opt.gibuu_cli import _generate_cache_shard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--flux-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--workers', type=int, nargs='+', default=[1, 2, 4, 8])
    cli = parser.parse_args()
    if cli.output_dir.exists():
        parser.error('output directory must not already exist')
    cli.output_dir.mkdir(parents=True)
    args = SimpleNamespace(
        flux_manifest=cli.flux_dir / 'canonical.yaml',
        flux_spectra_manifest=cli.flux_dir / 'spectra.yaml',
        energy_min_gev=0., energy_max_gev=20., energy_bins=400,
        jobcard=Path('/opt/gibuu/jobcards/sbnd-argon40-nuhepmc.job'),
        input_tables=Path('/opt/gibuu/buuinput'),
        target_a=40, target_z=18, ensembles=100, runs=1, time_steps=150,
        processes=['cc', 'nc'], executable='GiBUU.x',
    )
    results = []
    expected = None
    for workers in cli.workers:
        args.workers = workers
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.monotonic()
        record = _generate_cache_shard(
            args, cache_key='b' * 64, shard_index=0,
            destination=cli.output_dir / f'workers-{workers}' / 'shard',
        )
        elapsed = time.monotonic() - started
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        if expected is None:
            expected = record['candidates_sha256']
        assert record['candidates_sha256'] == expected, 'worker count changed candidates'
        result = dict(
            workers=workers, seconds=elapsed, events=record['candidate_events'],
            speedup=results[0]['seconds'] / elapsed if results else 1.,
            child_cpu_seconds=(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime),
            candidates_sha256=record['candidates_sha256'],
        )
        results.append(result)
        print(json.dumps(result), flush=True)
        (cli.output_dir / 'results.json').write_text(json.dumps({
            'available_cpus': len(os.sched_getaffinity(0)), 'results': results,
        }, indent=2) + '\n')
    print('PASS: identical serialized candidates at every worker count', flush=True)


if __name__ == '__main__':
    main()
