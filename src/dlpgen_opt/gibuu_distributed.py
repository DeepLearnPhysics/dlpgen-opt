"""Deterministic, bounded preparation rounds on a shared POSIX filesystem.

Only planning/finalization modify the cache manifest. Workers own one immutable
assignment, lock only that shard, publish atomically, and exit without barriers.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from . import gibuu_cli as g
from .progress import progress
from .provenance import checksum, read_yaml, write_yaml


def _location(args):
    contract, _ = g._cache_contracts(args)
    key = g._cache_key(contract)
    return args.candidate_cache_dir / key, key


def _plan_path(entry, round_index):
    return entry / 'rounds' / f'{round_index:05d}.yaml'


def _expected(args, tasks, round_index, key):
    return dict(schema_version=1, cache_key=key, tasks=tasks, round=round_index,
                total_events=args.total_events, reserve_fraction=args.reserve_fraction,
                selection_seed=args.seed, max_cache_shards=args.max_cache_shards)


def _load_plan(args, tasks, round_index):
    entry, key = _location(args)
    path = _plan_path(entry, round_index)
    if not path.is_file():
        raise RuntimeError(f'Distributed GiBUU round has not been planned: {path}')
    plan = read_yaml(path)
    digest = plan.pop('plan_sha256', None)
    if digest != g._cache_key(plan):
        raise RuntimeError(f'Distributed GiBUU plan checksum mismatch: {path}')
    for name, value in _expected(args, tasks, round_index, key).items():
        if plan.get(name) != value:
            raise RuntimeError(f'Distributed GiBUU plan has different {name}: {path}')
    return entry, plan


def _verify_shard(entry, key, assignment):
    directory = entry / 'shards' / f"{assignment['index']:05d}"
    if not directory.is_dir():
        raise RuntimeError(f'Distributed GiBUU shard is not complete: {directory}; retry its worker')
    record = read_yaml(directory / 'shard.yaml')
    exposure = {name: item['sampling_batches'] for name, item in record.get('components', {}).items()}
    if (record.get('cache_key') != key or record.get('index') != assignment['index']
            or exposure != assignment['components']
            or checksum(directory / 'candidates.jsonl.gz') != record.get('candidates_sha256')
            or checksum(directory / 'native.tar.gz') != record.get('native_archive_sha256')):
        raise RuntimeError(f'Distributed GiBUU shard validation failed: {directory}')
    return record


def _assignments(stats, target, tasks, first_index, max_shards):
    """Spread estimated deficits over slots, with at most 64 batches/component/slot.

    Work is fixed before launching the array. Completion order cannot change the
    allocation, normalization, seed assignments, or final selection.
    """
    slots = min(tasks, max_shards - first_index)
    if slots <= 0:
        raise RuntimeError(f'Distributed GiBUU cache reached max_cache_shards={max_shards}')
    plans = [{} for _ in range(slots)]
    for component, row in sorted(stats.items()):
        missing = target * row['share'] - row['ess']
        batches = (1 if row['ess'] == 0 else
                   max(0, math.ceil(missing / (row['ess'] / row['batches']))))
        batches = min(batches, 64 * slots)
        for slot in range(slots):
            count = batches // slots + (slot < batches % slots)
            if count:
                plans[slot][component] = count
    assignments = []
    for slot, components in enumerate(plans):
        if components:
            assignments.append(dict(task=slot, index=first_index + len(assignments), components=components))
    if not assignments:
        raise RuntimeError('Insufficient pool but no adaptive deficit was found')
    return assignments


def plan_round(args, tasks, round_index):
    entry, key = _location(args)
    # A separate planning lock covers initialization and pilot generation too.
    with g._exclusive_lock(args.campaign_dir / '.distributed-plan.lock'):
        path = _plan_path(entry, round_index)
        if path.exists():
            _, existing = _load_plan(args, tasks, round_index)
            progress('gibuu', f'Reusing distributed round {round_index}: {path}')
            return existing
        if round_index:
            _, previous = _load_plan(args, tasks, round_index - 1)
            for assignment in previous['assignments']:
                _verify_shard(entry, key, assignment)
        snapshot = argparse.Namespace(**vars(args))
        snapshot.cache_snapshot_only = True
        entry, manifest, _, ess = g._ensure_candidate_cache(snapshot, args.total_events)
        if not manifest['shards']:
            progress('gibuu', 'Generating the initial pilot before distributing work')
            pilot = argparse.Namespace(**vars(args))
            pilot.distributed_pilot = True
            pilot.cache_sizing = 'fixed'
            pilot.cache_shards = 1
            g._ensure_candidate_cache(pilot, 1)
        with g._exclusive_lock(args.candidate_cache_dir / f'.{key}.lock'):
            manifest_path = entry / 'cache.yaml'
            manifest = read_yaml(manifest_path)
            g._recover_published_shards(entry, manifest_path, manifest)
            stats, ess = g._pool_statistics(g._load_cache_candidates(entry, manifest), manifest)
            target = math.ceil(args.total_events * (1 + args.reserve_fraction))
            assignments = [] if ess >= target else _assignments(
                stats, target, tasks, len(manifest['shards']), args.max_cache_shards)
            plan = {**_expected(args, tasks, round_index, key), 'target_ess': target,
                    'initial_ess': ess, 'initial_shards': len(manifest['shards']),
                    'assignments': assignments}
            path.parent.mkdir(parents=True, exist_ok=True)
            write_yaml(path, {**plan, 'plan_sha256': g._cache_key(plan)})
            progress('gibuu', f'Planned round {round_index}: {len(assignments)} shards across {tasks} array slots; {path}')
            return plan


def run_worker(args, tasks, round_index, task):
    if not 0 <= task < tasks:
        raise ValueError(f'distributed task must be in [0, {tasks - 1}]')
    entry, plan = _load_plan(args, tasks, round_index)
    assignment = next((a for a in plan['assignments'] if a['task'] == task), None)
    if assignment is None:
        progress('gibuu', f'Round {round_index} task {task}: no work assigned; exiting')
        return
    index = assignment['index']
    destination = entry / 'shards' / f'{index:05d}'
    # Duplicate retries may wait for their own shard, never for other array slots.
    with g._exclusive_lock(entry / 'locks' / f'{index:05d}.lock'):
        if destination.exists():
            _verify_shard(entry, plan['cache_key'], assignment)
            progress('gibuu', f'Round {round_index} task {task}: reusing completed shard {index}')
            return
        progress('gibuu', f'Round {round_index} task {task}: starting shard {index}')
        g._generate_cache_shard(args, cache_key=plan['cache_key'], shard_index=index,
                               destination=destination, selected_components=assignment['components'])
        _verify_shard(entry, plan['cache_key'], assignment)
    progress('gibuu', f'Round {round_index} task {task}: complete; exiting')


def finalize_round(args, tasks, round_index):
    # Planning a new round and publishing a selection are mutually exclusive.
    # An old finalizer must not select a completion-order-dependent partial pool
    # while a more recent round is still running.
    with g._exclusive_lock(args.campaign_dir / '.distributed-plan.lock'):
        return _finalize_round(args, tasks, round_index)


def _finalize_round(args, tasks, round_index):
    entry, plan = _load_plan(args, tasks, round_index)
    if _plan_path(entry, round_index + 1).exists():
        raise RuntimeError('A newer distributed round has been planned; finalize the latest round')
    for assignment in plan['assignments']:
        _verify_shard(entry, plan['cache_key'], assignment)
    # This path is read-only with respect to native generation. It adopts all
    # published shards in canonical order, then uses the existing global selector.
    try:
        finalizer = argparse.Namespace(**vars(args))
        finalizer.distributed_finalize = True
        finalizer.prepare_only = True
        result = g._run_cached(finalizer)
    except RuntimeError as error:
        if 'pool is insufficient' in str(error):
            raise RuntimeError(f'{error} Next round: --prepare-round {round_index + 1} '
                               f'(or --round {round_index + 1} for local commands).') from error
        raise
    progress('gibuu', f'Distributed round {round_index} finalized; production jobs may start')
    return result


def execute(args, *, phase, tasks, round_index=0, task=None):
    if not getattr(args, 'distributed_cache', False) or tasks <= 0 or round_index < 0:
        raise ValueError('distributed preparation requires distributed caching, positive tasks, and a nonnegative round')
    if args.cache_sizing != 'auto' or args.cache_allocation != 'adaptive':
        raise ValueError('distributed preparation requires auto sizing and adaptive allocation')
    args.prepare_only = True
    if phase == 'plan':
        return plan_round(args, tasks, round_index)
    if phase == 'worker':
        if task is None:
            raise ValueError('worker phase requires --task')
        return run_worker(args, tasks, round_index, task)
    if phase == 'finalize':
        return finalize_round(args, tasks, round_index)
    raise ValueError(f'unknown distributed phase: {phase}')
