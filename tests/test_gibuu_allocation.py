from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import math

from dlpgen_opt.gibuu_cli import (
    Candidate, _allocation_plan, _pool_statistics, _effective_sample_size,
    _candidate_record, _candidate_from_record, _ensure_candidate_cache,
    _shard_workspace, _write_candidates,
    _select_campaign_candidates, _counter_uniform,
)
from dlpgen_opt.provenance import checksum, write_yaml


def candidate(component, weight, index=0):
    return Candidate(component=component, native_weight=weight, flux_integral=1.0,
                     source_event=index, process_id=2, score=0.0, particles=(),
                     cache_id=f'{component}/{index}')


def manifest(batches):
    return {'shards': [{'components': {
        c: {'sampling_batches': n} for c, n in batches.items()
    }}]}


def test_component_exposure_preserves_rates_when_one_component_is_oversampled():
    base = [candidate('a', 9), candidate('b', 1)]
    stats, _ = _pool_statistics(base, manifest({'a': 1, 'b': 1}))
    expanded = [candidate('a', 9, i) for i in range(8)] + [candidate('b', 1)]
    newer, ess = _pool_statistics(expanded, manifest({'a': 8, 'b': 1}))
    assert newer['a']['share'] == pytest.approx(stats['a']['share'])
    assert newer['b']['share'] == pytest.approx(stats['b']['share'])
    corrected = [replace(c, sampling_batches=newer[c.component]['batches']) for c in expanded]
    assert _effective_sample_size(corrected) == pytest.approx(ess)
    assert sum(c.global_weight for c in corrected) == pytest.approx(10)
    # Cache / campaign serialization must retain the correction.
    restored = [_candidate_from_record(_candidate_record(c)) for c in corrected]
    assert [c.global_weight for c in restored] == [c.global_weight for c in corrected]


def test_empty_batches_count_as_exposure_and_legacy_batches_default_to_one():
    cache = {'shards': [{'components': {'a': {}, 'b': {}}},
                        {'components': {'a': {'sampling_batches': 2}}}]}
    stats, _ = _pool_statistics([candidate('a', 3), candidate('b', 1)], cache)
    assert stats['a']['batches'] == 3
    assert stats['a']['share'] == pytest.approx(0.5)


def test_allocation_targets_deficits_and_retries_zero_yield():
    stats = {'dominant': {'share': .9, 'ess': 10, 'batches': 1},
             'minor': {'share': .1, 'ess': 20, 'batches': 1},
             'zero': {'share': 0, 'ess': 0, 'batches': 1}}
    assert _allocation_plan(stats, 100) == {'dominant': 8, 'zero': 1}


def test_allocation_substantially_reduces_work_for_imbalanced_mixture():
    def simulate(adaptive):
        candidates = []
        cache = {'shards': []}
        calls = 0
        stats = {}
        ess = 0
        while ess < 300:
            plan = _allocation_plan(stats, 300) if adaptive and stats else {'a': 1, 'b': 1, 'c': 1, 'd': 1}
            cache['shards'].append({'components': {c: {'sampling_batches': n} for c, n in plan.items()}})
            for c, n in plan.items():
                calls += n
                # Same underlying component distributions in both strategies.
                for _ in range(n):
                    candidates.extend(candidate(c, w) for w in ([90, 10] if c == 'a' else [0.5, 0.5]))
            stats, ess = _pool_statistics(candidates, cache)
        return calls, stats
    uniform, baseline = simulate(False)
    adaptive, result = simulate(True)
    assert adaptive < uniform / 2
    for c in result:
        assert result[c]['share'] == pytest.approx(baseline[c]['share'])


def test_failed_native_workspace_is_retained(tmp_path):
    with pytest.raises(RuntimeError):
        with _shard_workspace(tmp_path, 4) as path:
            (path / 'stdout.log').write_text('native diagnostic')
            raise RuntimeError('failed')
    assert (path / 'stdout.log').read_text() == 'native diagnostic'
    with _shard_workspace(tmp_path, 5) as successful:
        pass
    assert not successful.exists()


def test_adaptive_cache_reuses_legacy_and_resumes_with_correct_exposure(tmp_path, monkeypatch):
    monkeypatch.setattr('dlpgen_opt.gibuu_cli._cache_contract', lambda args: {'test': 'same-physics'})
    calls = []

    def generate(args, *, cache_key, shard_index, destination, selected_components):
        plan = selected_components or {'a': 1, 'b': 1}
        calls.append(plan)
        destination.mkdir()
        rows = [candidate(c, 9 if c == 'a' else 1, i)
                for c, n in plan.items() for i in range(n)]
        _write_candidates(destination / 'candidates.jsonl.gz', rows)
        (destination / 'native.tar.gz').write_bytes(b'archive')
        record = dict(index=shard_index, cache_key=cache_key, candidate_events=len(rows),
                      candidates_sha256=checksum(destination / 'candidates.jsonl.gz'),
                      native_archive_sha256=checksum(destination / 'native.tar.gz'),
                      components={c: {'sampling_batches': n} for c, n in plan.items()})
        write_yaml(destination / 'shard.yaml', record)
        return record

    monkeypatch.setattr('dlpgen_opt.gibuu_cli._generate_cache_shard', generate)
    args = SimpleNamespace(candidate_cache_dir=tmp_path, reserve_fraction=0,
                           cache_sizing='auto', cache_shards=None, max_cache_shards=100,
                           cache_allocation='uniform')
    original, old_manifest, _, _ = _ensure_candidate_cache(args, 2)
    original_bytes = (original / 'cache.yaml').read_bytes()
    count = len(calls)
    args.cache_allocation = 'adaptive'
    entry, record, rows, ess = _ensure_candidate_cache(args, 5)
    rows = list(rows)
    assert entry != original
    assert record['reused_candidate_cache'] == str(original)
    assert (original / 'cache.yaml').read_bytes() == original_bytes
    assert len(record['shards']) == len(old_manifest['shards']) + len(calls) - count
    assert any(set(plan) == {'a'} for plan in calls[count:])
    assert _effective_sample_size(rows) == pytest.approx(ess)
    assert ess >= 5
    count = len(calls)
    _, _, resumed, resumed_ess = _ensure_candidate_cache(args, 5)
    resumed = list(resumed)
    assert len(calls) == count
    assert resumed == rows
    assert resumed_ess == ess


def test_streamed_selection_matches_full_sort_exactly():
    rows = [candidate('a', (i % 17 + 1) / 10, i) for i in range(1000)]
    expected = sorted(rows, key=lambda c: -math.log(
        _counter_uniform('select', 17, 'key', c.cache_id)
    ) / c.global_weight)[:35]
    selected = _select_campaign_candidates(iter(rows), 35, 17, 'key')
    assert selected == expected
    assert len({c.cache_id for c in selected}) == 35
