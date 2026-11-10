# Distributed GiBUU preparation

GiBUU preparation can run as a bounded Slurm or PBS array across multiple nodes.
See [site scheduler profiles](schedulers.md) for Aurora, Polaris and NERSC. This
uses the existing weighted native generation, exposure normalization, and global
selection algorithm. It does not enable the experimental rejection sampler.

## Configuration and submission

Generated GiBUU productions use automatic distributed preparation by default.
The effective defaults are:

```yaml
candidate_cache:
  enabled: true
  sizing: auto
  allocation: adaptive
  distributed_tasks: auto
  workers: auto
  reserve_fraction: 0.1
  max_shards: 1000
```

Then use the usual launcher with the updated code installed in the container:

```sh
dlpgen-opt submit configs/production.gibuu-bnb.yaml \
  --container /path/to/updated-image.sif --max-concurrent 10
```

`distributed_tasks: auto` follows `production.jobs`. An integer overrides the
preparation slot count; `null` selects single-machine preparation. Disabled caches,
fixed-size caches, uniform allocation, and import mode retain their non-distributed
behavior when the automatic default is used.

`workers: auto` uses the forwarded allocation (`DLPGEN_OPT_CPUS_PER_TASK` or
`SLURM_CPUS_PER_TASK`), capped by available CPU affinity.
Locally it uses CPU affinity, falling back to the detected CPU count. An integer
overrides the worker count but is still capped by the allocation and available
work. Each native process remains single-threaded.

The launcher requests `prepare_cpus_per_task`, falling back to `cpus_per_task`,
and explicitly passes the allocation through `--cleanenv`. Explicit worker counts
do not increase the CPU request. For example, ten concurrent tasks with four CPUs
each can run up to 40 native processes. `prepare_time` applies to preparation;
normal `cpus_per_task` and `time` apply to detector jobs.

Existing productions with explicitly saved integer settings keep those settings.
To adopt automatic sizing, remove the old entries or change them to `auto`, and
use a fresh production directory because the resolved configuration changes.

The submission chain is:

1. Initialize shared inputs, reuse compatible completed cache data, and generate
   a pilot if needed. Persist a deterministic work plan.
2. Run preparation slots, at most ten concurrently. Each assigned slot generates
   one shard, publishes it atomically, and exits. Unneeded slots exit immediately.
3. Validate all assigned shards, merge in shard-index order, check pool ESS, and
   select the complete campaign globally.
4. Run the detector-production arrays only after finalization succeeds.

Profiles with `max_array_size: 99` split 125 preparation slots into arrays of 99
and 26. These arrays run sequentially, each capped at ten, so they cannot jointly
run twenty workers. This can leave fewer than ten workers active at the end of a
chunk. Profiles allowing 125 slots can use one `0-124%10` array instead.

Dependencies use `afterok` on each entire preceding array, as specified in
[Slurm's array documentation](https://slurm.schedmd.com/job_array.html).
Slurm distributed submissions use `--kill-on-invalid-dep=yes`, so jobs whose prerequisites
fail are cancelled instead of remaining pending indefinitely; see
[the sbatch reference](https://slurm.schedmd.com/sbatch.html).

## Retries and additional rounds

Each round has an immutable plan with a checksum, fixed component exposure,
shard IDs, and deterministic native seeds. Worker completion order cannot change
these assignments. Workers lock only their assigned shard. They never wait for
queued workers and never modify the shared cache manifest. Duplicate retries
reuse completed output; interrupted native tasks reuse validated checkpoints.

If a task times out or fails, rerun the same submission/round after addressing the
cause. Already completed work is reused. Native task logs remain in the shard's
`.shard-NNNNN.work` directory until publication, then in `native.tar.gz`. The
worker's Slurm stderr names these paths and reports task starts/completions.

A pilot estimates the needed work; it cannot guarantee the eventual ESS. Each
slot is bounded to at most 64 base batches per component. If the completed round
is insufficient, finalization fails **without starting serial generation or
releasing detector jobs**. Submit the indicated next round explicitly:

```sh
dlpgen-opt submit configs/production.gibuu-bnb.yaml \
  --container /path/to/updated-image.sif --max-concurrent 10 --prepare-round 1
```

Round 1 incorporates all completed round-0 exposure and assigns only further
estimated deficits. It cannot start while previous assignments are incomplete.
Automatic submission of additional rounds is not implemented. `max_shards` is
still a hard limit, including the pilot and imported shards; reaching it requires
an explicit configuration decision rather than silently reducing the target.

Distributed cache entries are scoped to the campaign path, isolating their shard
reservations from serial writers and other campaigns. Compatible completed serial
cache shards can be reused as immutable snapshots. Plans and cached records do
not permit changing their configuration mid-round. Use a fresh production output
directory for a changed production configuration, as with ordinary productions.
All workers need the same mounted paths, executable, input tables, and shared
filesystem with POSIX advisory locks and atomic directory rename. Local tests do
not establish these filesystem guarantees on S3DF.

## Local commands and tests

The same phases can be driven without Slurm:

```sh
dlpgen-opt prepare-gibuu-distributed production.yaml --phase plan --round 0
dlpgen-opt prepare-gibuu-distributed production.yaml --phase worker --round 0 --task 0
# Execute the remaining task IDs with a bounded process pool.
dlpgen-opt prepare-gibuu-distributed production.yaml --phase finalize --round 0
```

Only planning initializes shared inputs. A normal `run`, `generate`, or `prepare`
command cannot fill a missing distributed pool; it fails with instructions to
finish or extend preparation. This prevents production array slots from becoming
accidental serial preparation jobs.

`tests/integration/gibuu_distributed.py` exercises 125 slots using ten separate
spawned processes. Its `--synthetic` mode checks accounting and coordination; its
native mode runs the real GiBUU container with 100 ensembles and 150 transport
steps, then validates RooTracker output. It compares global selection to serial
selection, retries the same shard concurrently ten times, checks candidate IDs,
and regenerates one native shard serially for an exact candidate-byte comparison.

Unit tests also cover missing workers, publication-before-worker-exit failure,
corrupted plans and shards, additional preparation rounds, no-work slots, CLI
routing, Slurm dependencies and limits, and shell syntax of generated scripts.
Native subprocess checkpoint recovery is covered by the existing parallel
preparation tests. These checks address orchestration and preservation of the
existing sampling algorithm; they do not provide new physics-model validation.

## Results, November 9, 2026

The clean-checkout suite passed **169 tests**. Manual runs used separate spawned
processes on the local host/container, without a Slurm daemon. Native runs used
the `dlpgen-opt:0.4.4` linux/amd64 image on an ARM host with the modified source
mounted in, the existing local BNB/SBND flux, all four flavors, and CC/NC channels.

| Test | Array slots | Peak assigned workers | Raw candidates | Final ESS | Selected events |
|---|---:|---:|---:|---:|---:|
| Synthetic accounting | 125 | 10 | 1,376 | 1,376.0 | 1,250 |
| Native small campaign | 125 | 3 | 15,382 | 366.9 | 200 |
| Native larger campaign | 125 per round | 10 | 53,271 | 1,108.2 | 1,000 |

The larger native run required two rounds. The pilot had ESS 101.7. The first
round assigned 14 shards (16 base batches) and finished at ESS 580.5, below the
1,100 target; finalization refused to publish a selection. The local harness then
explicitly called the planner for round 1, assigning seven more shards (12 base
batches), after which finalization succeeded. The two worker phases took 183.8 s
and 101.0 s. The full test took 451.9 s, including the pilot, verification,
concurrent retries, a separate serial native replay, and RooTracker output.
These are test timings on a shared emulated host, not a forecast for 125,000 events.

Both native runs passed byte-for-byte serial selection and native-shard replay
comparisons. Ten concurrent retries of a completed shard left the exposure and
selected file unchanged. The first and last RooTracker job slices had the expected
entries and disjoint candidate IDs. The public worker/finalizer CLI also reused
the larger native pool without changing its selection. The synthetic test
preserved the analytically expected 90/10 component rates after unequal exposure.

The [machine-readable results](benchmarks/gibuu-distributed-2026-11-09.json) retain
settings, per-round workload/concurrency, timings, and validation outcomes.
Generated Slurm scripts passed dependency assertions and `bash -n`; actual
multi-node scheduling and S3DF filesystem behavior remain to be checked on site.

## Using the 0.4.6 release image

After the release image build succeeds, stage it once on S3DF:

```sh
apptainer pull /sdf/data/neutrino/images/dlpgen-opt_v0-4-6.sif \
  docker://ghcr.io/deeplearnphysics/dlpgen-opt:0.4.6
```

Pull the updated checkout and install the host-side launcher (`pip install -e .`
in your virtual environment). Use the `dlpgen-opt submit` command, not the minimal
top-level `submit.py` launcher, for distributed GiBUU preparation.

Use a fresh production name/output directory, set `software.container_image` to
`dlpgen-opt:0.4.6`, and submit with `--container` pointing to the new SIF and
`--max-concurrent 10`. The source-package overlay bind shown below is unnecessary
with this image; all GiBUU improvements are included. The profile, log locations,
and additional-round procedure below are unchanged.

## Rerunning the S3DF production with an existing SIF

Distributed preparation and worker sizing now default to **auto**. The rejected
native rejection-sampling experiment remains an isolated diagnostic, not a
production option.

The following recipe uses your checkout at
`/sdf/data/neutrino/generic/sim/debug/dlpgen-opt` and the existing 0.4.4 image.
It mounts the new Python package read-only over the installed package, preserving
the container's ROOT environment. This package location was verified in the local
0.4.4 image. Keep the checkout at the same commit while the production is active.
Updating the checkout during a running campaign would also update queued workers.

```sh
cd /sdf/data/neutrino/generic/sim/debug/dlpgen-opt
git pull --ff-only origin main
# Use Python 3.10 or newer for the host-side submission command.
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp configs/production.gibuu-bnb.yaml configs/production.gibuu-bnb-v002.yaml
```

In the new production YAML, set:

```yaml
production:
  name: gibuu_bnb_v002
  output_dir: ../runs/gibuu_bnb_v002
  jobs: 125
  generator_calls_per_job: 1000
  base_seed: 104741
  seed_stride: 10

source:
  type: gibuu
  config: gibuu/bnb_sbnd.yaml
  candidate_cache:
    enabled: true
    sizing: auto
    allocation: adaptive
    distributed_tasks: auto
    workers: auto
    reserve_fraction: 0.1
    max_shards: 1000
```

Keep the remaining detector/software settings from the copied YAML, but set
`software.container_image: dlpgen-opt:0.4.4` to identify the actual base SIF in this
recipe. Use a new output directory rather than changing the resolved configuration
of an old production. The default cache will live under this checkout's `runs/`,
so you need not write Angela's cache or edit her configuration. Existing cache data
is reused only if it is in the configured cache root and its input identities match.

Save `configs/slurm/gibuu-local.yaml` with your allocation and mounts:

```yaml
profiles:
  gibuu:
    account: neutrino:dune-ml
    partition: milano
    container_runtime: apptainer
    cpus_per_task: 1
    prepare_cpus_per_task: 1
    mem_per_cpu: 4G
    time: "02:00:00"
    prepare_time: "02:00:00"
    max_array_size: 99
    bind_paths: [/sdf, /cvmfs]
```

Submit from the host, where `sbatch` is available:

```sh
repo=/sdf/data/neutrino/generic/sim/debug/dlpgen-opt
image=/sdf/data/neutrino/images/dlpgen-opt_v0-4-4.sif
"$repo/.venv/bin/dlpgen-opt" submit "$repo/configs/production.gibuu-bnb-v002.yaml" \
  --profiles "$repo/configs/slurm/gibuu-local.yaml" --profile gibuu \
  --container "$image" --max-concurrent 10 \
  --bind "$repo/src/dlpgen_opt:/usr/local/lib/python3.10/dist-packages/dlpgen_opt:ro"
```

Add `--dry-run` first to inspect the scripts without submitting. The mapped bind
is included in the planning job, every worker, finalization, and detector jobs;
pulling host code alone would not update the package inside an old SIF. Logs are
under `runs/gibuu_bnb_v002/slurm/logs/`.

If finalization requests round 1, repeat the **same submission command**, including
the package bind, with `--prepare-round 1`. If a worker timed out instead, retry
the same round (and adjust the profile's preparation time if necessary). Previously
completed shards and native-task checkpoints are retained. Do not increase the
round number until the previous round's assigned work is complete.
