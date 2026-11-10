"""Manual 125-slot / 10-process distributed round test.

Use --synthetic locally for scheduler/accounting tests. Omit it inside the GiBUU
container for native generation and RooTracker validation with full transport.
All outputs are isolated under a new --output-dir; production caches are untouched.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from types import SimpleNamespace
import json
import os
import time

from dlpgen_opt import gibuu_cli as g
from dlpgen_opt import gibuu_distributed as d
from dlpgen_opt.provenance import checksum, read_yaml, write_yaml


def fake_shard(args, *, cache_key, shard_index, destination, selected_components):
    components = selected_components or {'pdg14-cc': 1, 'pdg14-nc': 1}
    time.sleep(.1 + (shard_index % 5) * .01)
    destination.mkdir(parents=True)
    rows = [g.Candidate(component=c, native_weight=9. if c.endswith('cc') else 1.,
                flux_integral=1., source_event=i, process_id=2, score=0., particles=(),
                cache_id=f'shard-{shard_index:05d}/{c}/{i}')
            for c, batches in components.items() for i in range(batches)]
    g._write_candidates(destination/'candidates.jsonl.gz', rows)
    (destination/'native.tar.gz').write_bytes(b'synthetic archive')
    record = dict(index=shard_index, cache_key=cache_key, candidate_events=len(rows),
                  candidates_sha256=checksum(destination/'candidates.jsonl.gz'),
                  native_archive_sha256=checksum(destination/'native.tar.gz'),
                  components={c: {'sampling_batches': n} for c,n in components.items()})
    write_yaml(destination/'shard.yaml',record)
    return record


def configure(synthetic):
    if synthetic:
        g._cache_contract = lambda args: {'physics': 'synthetic-distributed-v1'}
        g._generate_cache_shard = fake_shard


def worker(payload):
    args, synthetic, round_index, task = payload
    configure(synthetic)
    started = time.monotonic()
    args.workers = 1
    d.run_worker(args,125,round_index,task)
    return dict(task=task, pid=os.getpid(), start=started, end=time.monotonic())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--synthetic',action='store_true')
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--flux-dir',type=Path)
    parser.add_argument('--total-events',type=int)
    cli = parser.parse_args()
    root = cli.output_dir.resolve()
    root.mkdir(parents=True,exist_ok=False)
    configure(cli.synthetic)
    flux = cli.flux_dir or root
    args = SimpleNamespace(candidate_cache_dir=root/'cache', campaign_dir=root/'campaign',
        distributed_cache=True, reserve_fraction=.1, total_events=cli.total_events or (1250 if cli.synthetic else 200),
        seed=104741, job_index=0, events=10, cache_sizing='auto',cache_shards=None,
        max_cache_shards=1000,cache_allocation='adaptive',prepare_only=True,
        flux_manifest=flux/'canonical.yaml',flux_spectra_manifest=flux/'spectra.yaml',
        energy_min_gev=0.,energy_max_gev=20.,energy_bins=400,
        jobcard=Path('/opt/gibuu/jobcards/sbnd-argon40-nuhepmc.job'),
        input_tables=Path('/opt/gibuu/buuinput'), target_a=40,target_z=18,
        ensembles=100,runs=1,time_steps=150,processes=['cc','nc'],
        executable='GiBUU.x',workers=8,software_identity='dlpgen-opt:0.4.4',
        vertex_cm=[0.,0.,0.])
    rounds=[]
    begun=time.monotonic()
    for round_index in range(4):
        plan=d.plan_round(args,125,round_index)
        entry,key=d._location(args)
        before=(entry/'cache.yaml').read_bytes()
        started=time.monotonic()
        with ProcessPoolExecutor(max_workers=10,mp_context=get_context('spawn')) as pool:
            intervals=list(pool.map(worker,[(args,cli.synthetic,round_index,i) for i in reversed(range(125))]))
        elapsed=time.monotonic()-started
        assert (entry/'cache.yaml').read_bytes()==before
        changes=sorted([(r['start'],1) for r in intervals]+[(r['end'],-1) for r in intervals])
        active=peak=0
        for _,change in changes:
            active+=change
            peak=max(peak,active)
        assert peak <= 10
        assigned_ids={a['task'] for a in plan['assignments']}
        work_intervals=[r for r in intervals if r['task'] in assigned_ids]
        active=work_peak=0
        for _,change in sorted([(r['start'],1) for r in work_intervals]+[(r['end'],-1) for r in work_intervals]):
            active+=change
            work_peak=max(work_peak,active)
        rounds.append(dict(round=round_index,assigned_shards=len(plan['assignments']),
                           worker_seconds=elapsed,peak_processes=peak,peak_assigned_workers=work_peak,intervals=intervals))
        try:
            d.finalize_round(args,125,round_index)
            break
        except RuntimeError as error:
            if 'pool is insufficient' not in str(error): raise
            print(f'Additional preparation round needed: {error}',flush=True)
    else:
        raise RuntimeError('test did not reach target within four rounds')
    manifest=read_yaml(entry/'cache.yaml')
    stats,ess=g._pool_statistics(g._load_cache_candidates(entry,manifest),manifest)
    rows=list(g._load_cache_candidates(entry,manifest))
    assert len(rows)==len({c.cache_id for c in rows})
    selected_path=args.campaign_dir/'selected.jsonl.gz'
    original=selected_path.read_bytes()
    selected=g._read_cached_candidates(selected_path)
    assert len(selected)==args.total_events
    assert len({c.cache_id for c in selected})==args.total_events
    expected=g._select_campaign_candidates(g._normalized_cache_candidates(entry,manifest,stats,True),
                                           args.total_events,args.seed,key)
    g._write_candidates(root/'serial-selection.jsonl.gz',expected)
    assert (root/'serial-selection.jsonl.gz').read_bytes()==original
    assignment=plan['assignments'][0] if plan['assignments'] else None
    if assignment:
        with ProcessPoolExecutor(max_workers=10,mp_context=get_context('spawn')) as pool:
            list(pool.map(worker,[(args,cli.synthetic,round_index,assignment['task'])]*10))
        d.finalize_round(args,125,round_index)
        assert (selected_path).read_bytes()==original
        assert read_yaml(entry/'cache.yaml')==manifest
    replay=None
    if not cli.synthetic and assignment:
        # Same native seed/work, different workspace and no parallelism.
        args.workers=1
        replay=g._generate_cache_shard(args,cache_key=key,shard_index=assignment['index'],
            destination=root/'serial-replay'/f"{assignment['index']:05d}",selected_components=assignment['components'])
        assert replay['candidates_sha256']==manifest['shards'][assignment['index']]['candidates_sha256']
    jobs=[]
    if not cli.synthetic:
        import ROOT
        args.prepare_only=False
        for job in (0,args.total_events//args.events-1):
            args.job_index=job
            args.output=root/f'job-{job}.root'
            args.metadata_output=root/f'job-{job}.json'
            meta=g._run_cached(args)
            handle=ROOT.TFile.Open(str(args.output))
            assert handle and not handle.IsZombie()
            tree=handle.Get('gRooTracker')
            assert tree and tree.GetEntries()==args.events
            handle.Close()
            expected_ids=[c.cache_id for c in selected[job*args.events:(job+1)*args.events]]
            assert [row['cache_id'] for row in meta['selected']]==expected_ids
            jobs.append(dict(job=job,events=meta['events'],cache_ids=expected_ids))
        assert set(jobs[0]['cache_ids']).isdisjoint(jobs[1]['cache_ids'])
    result=dict(synthetic=cli.synthetic,tasks=125,max_concurrent=10,total_seconds=time.monotonic()-begun,
        shards=len(manifest['shards']),raw_candidates=len(rows),effective_events=ess,
        selected_events=len(selected),serial_selection_matches=True,duplicate_retries_idempotent=True,
        native_serial_replay_matches=replay is not None,rootracker_jobs=jobs,rounds=rounds)
    (root/'results.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('rounds','rootracker_jobs')}),flush=True)


if __name__=='__main__': main()
