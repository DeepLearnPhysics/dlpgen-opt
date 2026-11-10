"""Opt-in native sampling experiment; never writes production cache entries.

Run in the GiBUU container. Weighted mode enables maximum-weight diagnostics.
A measured pilot maximum is NOT a proven rejection bound.
"""
import argparse
import json
import math
import os
import subprocess
import time
from pathlib import Path

from dlpgen_opt.gibuu_cli import _read_candidates, _set_namelist, resolved_jobcard


def check_native_result(returncode, stdout, native, bound):
    if "totalWeight > equalWeights_Max" in stdout or "You have to increase" in stdout:
        raise RuntimeError(f"rejection bound {bound} exceeded; discard this run")
    if returncode or not native.is_file():
        raise RuntimeError(f"native run failed (exit {returncode}); inspect logs")
    if "HepMC::Asciiv3-END_EVENT_LISTING" not in native.read_text():
        raise RuntimeError("incomplete native event file; discard this run")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['weighted', 'rejection'], required=True)
    parser.add_argument('--bound', type=float)
    parser.add_argument('--flux-file', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--runs', type=int, default=1)
    cli = parser.parse_args()
    if cli.mode == 'rejection' and (cli.bound is None or not math.isfinite(cli.bound) or cli.bound <= 0):
        parser.error('rejection requires an explicit finite positive --bound')
    if cli.runs <= 0:
        parser.error('--runs must be positive')
    directory = cli.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    card = resolved_jobcard(
        Path('/opt/gibuu/jobcards/sbnd-argon40-nuhepmc.job').read_text(),
        pdg=14, process='cc', flux_file=cli.flux_file.resolve(),
        input_tables=Path('/opt/gibuu/buuinput'), target_a=40, target_z=18,
        ensembles=100, runs=cli.runs, time_steps=150, seed=cli.seed,
    )
    card = _set_namelist(card, 'neutrino_induced', 'equalWeights_Mode', 1 if cli.mode == 'weighted' else 2)
    # Native input validation requires a positive value even in diagnostic mode,
    # where it is unused by the rejection decision.
    card = _set_namelist(card, 'neutrino_induced', 'equalWeights_Max', cli.bound or 1.)
    (directory / 'jobcard.nml').write_text(card)
    print(f'Starting {cli.mode}: seed={cli.seed}, runs={cli.runs}, bound={cli.bound}', flush=True)
    start = time.monotonic()
    with (directory / 'stdout.log').open('w') as stdout, (directory / 'stderr.log').open('w') as stderr, (directory / 'jobcard.nml').open() as stdin:
        result = subprocess.run(['GiBUU.x'], stdin=stdin, text=True, cwd=directory,
                                stdout=stdout, stderr=stderr,
                                env={**os.environ, 'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'})
    elapsed = time.monotonic() - start
    (directory / 'run.json').write_text(json.dumps(dict(
        mode=cli.mode, seed=cli.seed, runs=cli.runs, bound=cli.bound,
        seconds=elapsed, returncode=result.returncode,
    ), indent=2) + '\n')
    native = directory / 'EventOutput.Pert.hepmc3'
    check_native_result(result.returncode, (directory / 'stdout.log').read_text(), native, cli.bound)
    summarize(directory, cli, elapsed)


def summarize(directory, cli, elapsed):
    native = directory / 'EventOutput.Pert.hepmc3'
    # The run header has a W CV weight-name record; only event W records
    # following the first E record contain numeric weights.
    weights = []
    in_events = False
    for line in native.read_text().splitlines():
        if line.startswith('E '):
            in_events = True
        if in_events and line.startswith('W '):
            weights.append(float(line.split()[1]))
    rows = _read_candidates(native, component='pdg14-cc', flux_integral=1., seed=cli.seed)
    positive = sum(c.native_weight for c in rows)
    squares = sum(c.native_weight**2 for c in rows)
    ess = positive**2 / squares if squares else 0.
    maxima = [float(x.replace('D', 'E')) for x in (directory / 'fort.87').read_text().split()]
    stats = dict(mode=cli.mode, seed=cli.seed, runs=cli.runs, bound=cli.bound,
                 seconds=elapsed, native_events=len(weights), positive_candidates=len(rows),
                 negative_events=sum(w < 0 for w in weights),
                 negative_weight=sum(w for w in weights if w < 0),
                 positive_weight=positive, signed_weight=sum(weights),
                 positive_rate_per_run=positive / cli.runs,
                 ess=ess, ess_per_second=ess / elapsed, observed_max=max(maxima),
                 neutrino_energy_mean=(sum(c.native_weight*c.incoming_neutrino[-1] for c in rows)/positive if positive else None),
                 proton_count_mean=(sum(c.native_weight*sum(p[0] == 2212 for p in c.particles) for c in rows)/positive if positive else None))
    (directory / 'result.json').write_text(json.dumps(stats, indent=2) + '\n')
    print(json.dumps(stats), flush=True)


if __name__ == '__main__':
    main()
