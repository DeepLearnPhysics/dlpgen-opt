from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
import tarfile

import pytest

from dlpgen_opt.gibuu_cli import (
    Candidate, _execute_native_tasks, _generate_cache_shard, _native_tasks,
    _run_native_task, _read_cached_candidates,
)
from dlpgen_opt.provenance import write_yaml
from test_gibuu_generation import TEMPLATE


def settings(tmp_path, workers=1):
    flux = tmp_path / 'flux.dat'
    flux.write_text('0.5 1\n')
    card = tmp_path / 'template.job'
    card.write_text(TEMPLATE)
    manifest = tmp_path / 'flux.yaml'
    write_yaml(manifest, {'normalization': {'valid_per_selected_pot': True}})
    return SimpleNamespace(
        runs=1, ensembles=100, time_steps=150, target_a=40, target_z=18,
        input_tables=tmp_path, processes=['cc', 'nc'], executable='GiBUU.x',
        workers=workers, flux_manifest=manifest, flux_spectra_manifest=tmp_path / 'spectra.yaml',
        energy_min_gev=0., energy_max_gev=20., energy_bins=400, jobcard=card,
    ), {14: {'path': flux, 'integral_per_cm2': 1.0}}


def test_task_seeds_and_work_do_not_depend_on_worker_count(tmp_path):
    args, histograms = settings(tmp_path)
    first = _native_tasks(args, histograms, TEMPLATE, 'a'*64, 0, tmp_path, {'pdg14-cc': 64})
    args.workers = 12
    second = _native_tasks(args, histograms, TEMPLATE, 'a'*64, 0, tmp_path, {'pdg14-cc': 64})
    assert first == second
    assert len(first) == 32
    assert len({t['seed'] for t in first}) == 32
    assert len({t['directory'] for t in first}) == 32
    assert sum(t['batches'] for t in first) == 64
    assert all(t['batches'] == 2 for t in first)
    third = _native_tasks(args, histograms, TEMPLATE, 'a'*64, 1, tmp_path, {'pdg14-cc': 64})
    assert {t['seed'] for t in first}.isdisjoint({t['seed'] for t in third})


def test_out_of_order_completion_returns_canonical_order(monkeypatch):
    class Pool:
        def __init__(self, **kwargs):
            assert kwargs['max_workers'] == 2
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def submit(self, function, args, task):
            result = Future()
            result.set_result(task['result'])
            return result
    monkeypatch.setattr('dlpgen_opt.gibuu_cli.ProcessPoolExecutor', Pool)
    monkeypatch.setattr('dlpgen_opt.gibuu_cli.as_completed', lambda futures: reversed(list(futures)))
    records = _execute_native_tasks(SimpleNamespace(workers=2), [{'result': i} for i in range(4)])
    assert records == [0, 1, 2, 3]


def test_interrupted_shard_reuses_completed_native_tasks(tmp_path, monkeypatch):
    args, histograms = settings(tmp_path)
    monkeypatch.setattr('dlpgen_opt.gibuu_cli.cached_flux_histograms', lambda *a, **kw: histograms)
    calls = []
    fail = True

    def run(command, *, cwd, **kwargs):
        calls.append(cwd)
        assert kwargs['env']['OMP_NUM_THREADS'] == '1'
        if fail and 'pdg14-nc' in str(cwd):
            return SimpleNamespace(returncode=9)
        (cwd / 'EventOutput.Pert.hepmc3').write_text('native event')
        return SimpleNamespace(returncode=0)

    def read(native, *, component, cache_prefix, **kwargs):
        return [Candidate(component=component, source_event=0, process_id=2,
                          native_weight=1, flux_integral=1, score=0, particles=(),
                          cache_id=f'{cache_prefix}/{component}/0')]

    monkeypatch.setattr('dlpgen_opt.gibuu_cli.subprocess.run', run)
    monkeypatch.setattr('dlpgen_opt.gibuu_cli._read_candidates', read)
    destination = tmp_path / 'shards' / '00000'
    with pytest.raises(RuntimeError, match='exit code 9'):
        _generate_cache_shard(args, cache_key='a'*64, shard_index=0, destination=destination)
    checkpoint = destination.parent / '.shard-00000.work/pdg14-cc/chunk-000/completed.yaml'
    assert checkpoint.exists()
    assert not destination.exists()
    fail = False
    # A new campaign may want more batches or list processes in another order.
    # Complete the assigned wave first, preserving its successful checkpoint.
    args.processes.reverse()
    record = _generate_cache_shard(args, cache_key='a'*64, shard_index=0,
                                  destination=destination, selected_components={'pdg14-cc': 8})
    assert len(calls) == 3  # CC succeeded once; only NC reran.
    assert record['candidate_events'] == 2
    assert record['components']['pdg14-cc']['sampling_batches'] == 1
    rows = _read_cached_candidates(destination / 'candidates.jsonl.gz')
    assert len({c.cache_id for c in rows}) == 2
    assert not checkpoint.parent.exists()
    with tarfile.open(destination / 'native.tar.gz') as archive:
        assert {'plan.yaml', 'flux-14.dat'} <= set(archive.getnames())


def test_checkpoint_corruption_is_rejected(tmp_path, monkeypatch):
    args, histograms = settings(tmp_path)
    task = _native_tasks(args, histograms, TEMPLATE, 'a'*64, 0, tmp_path / 'work', {'pdg14-cc': 1})[0]
    directory = Path(task['directory'])
    directory.mkdir(parents=True)
    write_yaml(directory / 'completed.yaml', {'contract': {'wrong': True}})
    monkeypatch.setattr('dlpgen_opt.gibuu_cli.subprocess.run', lambda *a, **kw: pytest.fail('must not run'))
    with pytest.raises(RuntimeError, match='different inputs'):
        _run_native_task(args, task)


def test_worker_pool_respects_slurm_cpu_allocation(monkeypatch, capsys):
    monkeypatch.setenv('SLURM_CPUS_PER_TASK', '1')
    monkeypatch.setattr('dlpgen_opt.gibuu_cli._run_native_task', lambda args, task: task)
    tasks = [{'index': i} for i in range(3)]
    assert _execute_native_tasks(SimpleNamespace(workers=8), tasks) == tasks
    assert 'Limiting requested 8 workers to 1 available CPUs' in capsys.readouterr().err
