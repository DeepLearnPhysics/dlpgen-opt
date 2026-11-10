from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import time

import pytest

from dlpgen_opt import gibuu_cli as g
from dlpgen_opt import gibuu_distributed as d
from dlpgen_opt.provenance import checksum, read_yaml, write_yaml


def fake_shard(args, *, cache_key, shard_index, destination, selected_components):
    components = selected_components or {'pdg14-cc': 1, 'pdg14-nc': 1}
    # Complete in a deliberately different order; keep locks/files real.
    time.sleep((shard_index % 5) * .002)
    destination.mkdir(parents=True)
    rows = [g.Candidate(component=c, native_weight=9. if c.endswith('cc') else 1.,
                flux_integral=1., source_event=i, process_id=2, score=0., particles=(),
                cache_id=f'shard-{shard_index:05d}/{c}/{i}')
            for c, batches in components.items() for i in range(batches)]
    g._write_candidates(destination / 'candidates.jsonl.gz', rows)
    (destination / 'native.tar.gz').write_bytes(b'native fixture')
    record = dict(index=shard_index, cache_key=cache_key, candidate_events=len(rows),
                  candidates_sha256=checksum(destination / 'candidates.jsonl.gz'),
                  native_archive_sha256=checksum(destination / 'native.tar.gz'),
                  components={c: {'sampling_batches': n} for c, n in components.items()})
    write_yaml(destination / 'shard.yaml', record)
    return record


@pytest.fixture
def distributed(tmp_path, monkeypatch):
    monkeypatch.setattr(g, '_cache_contract', lambda args: {'physics': 'fixture'})
    monkeypatch.setattr(g, '_generate_cache_shard', fake_shard)
    return SimpleNamespace(candidate_cache_dir=tmp_path/'cache', campaign_dir=tmp_path/'campaign',
        distributed_cache=True, reserve_fraction=.1, total_events=1250, seed=104741,
        job_index=0, events=10, cache_sizing='auto', cache_shards=None,
        max_cache_shards=1000, cache_allocation='adaptive', prepare_only=True)


def test_125_workers_limited_to_10_match_serial_selection(distributed):
    args = distributed
    plan = d.execute(args, phase='plan', tasks=125)
    assert len(plan['assignments']) == 125
    entry, _ = d._location(args)
    cache_before = (entry/'cache.yaml').read_bytes()
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda task: d.execute(args, phase='worker', tasks=125, task=task), reversed(range(125))))
    # Workers cannot increment global accounting or race on the manifest.
    assert (entry/'cache.yaml').read_bytes() == cache_before
    d.execute(args, phase='finalize', tasks=125)
    cache = read_yaml(entry/'cache.yaml')
    assert len(cache['shards']) == 126  # pilot plus 125 assigned shards
    assert [r['index'] for r in cache['shards']] == list(range(126))
    selected_path = args.campaign_dir/'selected.jsonl.gz'
    first = selected_path.read_bytes()
    selected = g._read_cached_candidates(selected_path)
    assert len(selected) == 1250
    assert len({c.cache_id for c in selected}) == 1250
    stats, ess = g._pool_statistics(g._load_cache_candidates(entry, cache), cache)
    assert ess >= 1375
    assert cache['statistics']['effective_sample_size'] == pytest.approx(ess)
    assert stats['pdg14-cc']['share'] == pytest.approx(.9)
    assert stats['pdg14-nc']['share'] == pytest.approx(.1)
    # Duplicate submissions/retries do not change exposure or selected events.
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda task: d.run_worker(args,125,0,task), [7]*10))
    d.finalize_round(args,125,0)
    assert selected_path.read_bytes() == first
    assert read_yaml(entry/'cache.yaml') == cache
    # Canonical serial selection of the same raw candidates is byte-identical.
    rows = g._normalized_cache_candidates(entry, cache, stats, True)
    expected = g._select_campaign_candidates(rows,1250,args.seed,cache['key'])
    g._write_candidates(entry/'expected.jsonl.gz',expected)
    assert (entry/'expected.jsonl.gz').read_bytes() == first


def test_missing_worker_blocks_finalize_and_next_round(distributed, monkeypatch):
    args = distributed
    d.plan_round(args, 2, 0)
    d.run_worker(args, 2, 0, 1)
    with pytest.raises(RuntimeError, match='not complete'):
        d.finalize_round(args,2,0)
    with pytest.raises(RuntimeError, match='not complete'):
        d.plan_round(args,2,1)
    assert not (args.campaign_dir/'campaign.yaml').exists()
    # An ordinary production job must not silently take over missing native work.
    monkeypatch.setattr(g, '_generate_cache_shard', lambda *a, **kw: pytest.fail('must not generate'))
    with pytest.raises(RuntimeError, match='not finalized'):
        g._run_cached(args)


def test_insufficient_round_can_be_extended(distributed):
    args = distributed
    args.total_events = 200
    # Two slots capped at 64 batches cannot meet the target in the first round.
    d.plan_round(args,2,0)
    for task in range(2):
        d.run_worker(args,2,0,task)
    with pytest.raises(RuntimeError, match='Next round: --prepare-round 1'):
        d.finalize_round(args,2,0)
    first = d._load_plan(args,2,0)[1]
    second = d.plan_round(args,2,1)
    assert second['assignments'][0]['index'] > first['assignments'][-1]['index']
    with pytest.raises(RuntimeError, match='newer distributed round'):
        d.finalize_round(args,2,0)
    with pytest.raises(RuntimeError, match='not complete'):
        d.finalize_round(args,2,1)
    for round_index in range(1, 5):
        d.plan_round(args,2,round_index)
        for task in range(2):
            d.run_worker(args,2,round_index,task)
        try:
            d.finalize_round(args,2,round_index)
            break
        except RuntimeError as error:
            assert 'pool is insufficient' in str(error)
    else:
        pytest.fail('top-up rounds did not reach target')
    assert len(g._read_cached_candidates(args.campaign_dir/'selected.jsonl.gz')) == 200


def test_plan_is_immutable_and_corruption_rejected(distributed):
    args = distributed
    first = d.plan_round(args,125,0)
    assert d.plan_round(args,125,0) == first
    args.total_events += 1
    with pytest.raises(RuntimeError, match='different total_events'):
        d.plan_round(args,125,0)
    args.total_events -= 1
    entry, _ = d._location(args)
    plan_path = d._plan_path(entry,0)
    changed = read_yaml(plan_path)
    changed['assignments'][0]['components']['pdg14-cc'] = 64
    write_yaml(plan_path,changed)
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        d.run_worker(args,125,0,0)


def test_published_corruption_is_rejected(distributed):
    args = distributed
    plan = d.plan_round(args,125,0)
    d.run_worker(args,125,0,0)
    entry, _ = d._location(args)
    (entry/'shards'/f"{plan['assignments'][0]['index']:05d}"/'candidates.jsonl.gz').write_bytes(b'bad')
    with pytest.raises(RuntimeError, match='validation failed'):
        d.run_worker(args,125,0,0)


def test_ready_pool_assigns_no_work(distributed):
    args = distributed
    args.total_events = 1
    args.reserve_fraction = 0
    plan = d.plan_round(args,125,0)
    assert plan['assignments'] == []
    for task in (0,124):
        d.run_worker(args,125,0,task)
    d.finalize_round(args,125,0)
    assert len(g._read_cached_candidates(args.campaign_dir/'selected.jsonl.gz')) == 1


def test_retry_after_publication_before_worker_exit(distributed, monkeypatch):
    args = distributed
    d.plan_round(args,125,0)
    calls = []
    def interrupted(*a, **kw):
        calls.append(kw['shard_index'])
        fake_shard(*a, **kw)
        raise RuntimeError('injected interruption after atomic publication')
    monkeypatch.setattr(g, '_generate_cache_shard', interrupted)
    with pytest.raises(RuntimeError, match='injected interruption'):
        d.run_worker(args,125,0,0)
    d.run_worker(args,125,0,0)
    assert calls == [1]


def test_cli_worker_skips_shared_initialization(monkeypatch):
    from dlpgen_opt import cli
    from dlpgen_opt.config import load_config
    config = load_config('configs/production.gibuu-bnb.yaml')
    config.source.candidate_cache.distributed_tasks = 125
    monkeypatch.setattr(cli,'load_config',lambda path: config)
    monkeypatch.setattr(cli.Pipeline,'initialize',lambda self: pytest.fail('workers must not prepare shared inputs'))
    calls=[]
    monkeypatch.setattr(d,'execute',lambda args,**kw: calls.append((args,kw)))
    assert cli.main(['prepare-gibuu-distributed','unused','--phase','worker','--task','124']) == 0
    args,kwargs=calls[0]
    assert args.distributed_cache
    assert args.seed == config.production.base_seed
    assert kwargs == dict(phase='worker',tasks=125,round_index=0,task=124)


def test_production_cannot_select_an_unfinalized_ready_pool(distributed):
    args = distributed
    args.total_events = 1
    args.reserve_fraction = 0
    d.plan_round(args,125,0)
    with pytest.raises(RuntimeError, match='not finalized'):
        g._run_cached(args)
    assert not (args.campaign_dir/'campaign.yaml').exists()
    d.finalize_round(args,125,0)


def test_auto_defaults_and_explicit_opt_out():
    from dlpgen_opt.config import load_config, GiBUUCandidateCacheSettings
    config = load_config('configs/production.gibuu-bnb.yaml')
    cache=config.source.candidate_cache
    assert cache.workers == 'auto'
    assert cache.distributed_tasks == 'auto'
    assert cache.preparation_tasks(125) == 125
    assert cache.preparation_tasks(7) == 7
    cache.distributed_tasks = None
    assert config.resolved_dict()['source']['candidate_cache']['distributed_tasks'] is None
    assert GiBUUCandidateCacheSettings.model_validate(cache.model_dump()).distributed_tasks is None
    config.source.candidate_cache.distributed_tasks = 125
    assert config.resolved_dict()['source']['candidate_cache']['distributed_tasks'] == 125
    for invalid in ({'enabled':False}, {'sizing':'fixed','shards':1}, {'allocation':'uniform'}):
        assert GiBUUCandidateCacheSettings(**invalid).distributed_tasks is None
        with pytest.raises(ValueError,match='requires enabled, auto, adaptive'):
            GiBUUCandidateCacheSettings(distributed_tasks=125,**invalid)


def test_distributed_imports_completed_serial_cache_without_modifying_it(distributed,monkeypatch):
    args=distributed
    args.distributed_cache=False
    args.total_events=1
    args.reserve_fraction=0
    serial,manifest,_,_=g._ensure_candidate_cache(args,1)
    before=(serial/'cache.yaml').read_bytes()
    args.distributed_cache=True
    monkeypatch.setattr(g,'_generate_cache_shard',lambda *a,**kw: pytest.fail('should reuse serial pool'))
    plan=d.plan_round(args,125,0)
    entry,_=d._location(args)
    assert entry != serial
    assert read_yaml(entry/'cache.yaml')['reused_candidate_cache']==str(serial)
    assert plan['assignments']==[]
    d.finalize_round(args,125,0)
    assert (serial/'cache.yaml').read_bytes()==before


def test_finalized_campaign_rejects_changed_physics(distributed,monkeypatch):
    args=distributed
    args.total_events=1
    args.reserve_fraction=0
    d.plan_round(args,125,0)
    d.finalize_round(args,125,0)
    monkeypatch.setattr(g,'_cache_contract',lambda args: {'physics':'changed'})
    with pytest.raises(RuntimeError,match='different cache_key'):
        g._run_cached(args)
