"""Manual native check: run inside the GiBUU container, not ordinary pytest.

python3 tests/integration/gibuu_batching.py --flux-dir PATH --output-dir NEW_PATH
Uses the canonical 400-bin, 0–20 GeV flux products and full 150-step transport.
"""
import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace

from dlpgen_opt.gibuu_cli import _generate_cache_shard, _read_cached_candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--flux-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
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
    for batches in (1, 2):
        output = cli.output_dir / f'batches-{batches}'
        started = time.monotonic()
        _generate_cache_shard(
            args, cache_key='a' * 64, shard_index=0, destination=output,
            selected_components={'pdg14-cc': batches},
        )
        rows = _read_cached_candidates(output / 'candidates.jsonl.gz')
        results.append(rows)
        print(json.dumps(dict(
            batches=batches, events=len(rows), seconds=time.monotonic() - started,
            sum_native_weight=sum(c.native_weight for c in rows),
        )), flush=True)
    assert results[0] and len(results[1]) > len(results[0])
    assert [c.native_weight for c in results[0]] == [
        c.native_weight for c in results[1][:len(results[0])]
    ]
    print('PASS: batching preserves first-run native weights exactly', flush=True)


if __name__ == '__main__':
    main()
