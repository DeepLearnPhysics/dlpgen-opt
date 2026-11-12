# Production on Slurm and PBS

`dlpgen-opt submit` supports S3DF Slurm, ALCF Aurora/Polaris PBS Professional,
and NERSC Perlmutter CPU Slurm with Shifter. `run CONFIG --job INDEX` remains
scheduler independent. The top-level `python3 submit.py` launcher supports
S3DF Slurm and Aurora/Polaris PBS with Python **3.6+ and PyYAML only**. It loads
shared scheduling helpers from the checkout without installing dlpgen-opt,
Pydantic or physics dependencies on the login node. PBS runtime modules are
loaded on the compute node; Apptainer need not be on the login node's PATH.
Use the installed CLI (Python 3.10+) for NERSC/Shifter.

The new backends have local rendering, dependency, and subprocess tests. They
have not yet been smoke-tested on the clusters. Use a small production first.
These are CPU workflows; they do not use Aurora GPUs or enable GPU acceleration.

## Site configuration

`configs/batch/sites.yaml` is the default profile file for all sites, including
`s3df_milano` (the default profile) and `s3df_roma`. The old
`configs/slurm/s3df.yaml` path is a compatibility symlink to this same file.

Aurora defaults to `neutrinoGPU::debug_aurora`; Polaris defaults to
`neutrinoGPU::SpineDev`. These full project/suballocation strings are passed to
PBS `-A`, following [ALCF's suballocation syntax](https://docs.alcf.anl.gov/account-project-management/allocation-management/allocation-management/).
They are separate from the PBS queue: both profiles still select `capacity`.
The `debug_aurora` suballocation name does not itself select the `debug` queue.
For NERSC, copy the file and replace `REPLACE_WITH_PROJECT` with your allocation. Adjust queues, requested filesystems, module setup, walltime, CPU
counts and concurrency to your allocation's current policies. Submission refuses
the placeholder account (dry runs allow it). S3DF resource settings are unchanged.

The PBS profiles reserve one exclusive whole node (`select=1:system=...`,
`place=scatter:excl`) without a partial `ncpus` request. `node_cpus` describes the
usable CPU capacity; `cpus_per_task` and `prepare_cpus_per_task` are execution
budgets and may be smaller. `cpu_bind` gives the single launcher rank a physical
CPU list. Children receive disjoint subsets of that list, inherited by their
native subprocesses. A smaller affinity mask is logged and caps concurrency.

| Profile | Usable cores | Concurrent production jobs | GiBUU tasks per preparation pack |
| --- | ---: | ---: | ---: |
| Aurora | 102 | 102 | 12 (up to 8 native workers each with a full pack) |
| Polaris | 32 | 32 | 4 (up to 8 native workers each with a full pack) |

Aurora binds `1-51,53-103`, excluding the reserved physical cores and SMT siblings;
Polaris binds `0-31`. These follow the current
[Aurora CPU layout](https://docs.alcf.anl.gov/aurora/running-jobs-aurora/) and
[Polaris hardware](https://docs.alcf.anl.gov/polaris/). Validate the logged affinity
on site before scaling. The PBS `node_cpus` ceiling prevents larger CPU budgets;
it is not a scheduler `ncpus` value or a memory limit.

`jobs_per_task` and `prepare_jobs_per_task` are packing counts, not node requests.
The runner bounds simultaneous children by the CPU budget, divides that budget
between them, and pins each child. Packing counts may exceed the budget (remaining
tasks wait locally). Native GiBUU workers inherit each child's CPU share. Pilot
planning and finalization still use one child; they may not fill the node.

No fixed per-worker memory requirement is assumed. Lower packing/concurrency for
memory-heavy jobs; reducing the CPU budget while keeping a large production pack
runs that pack in smaller waves. Use measured peak job memory with headroom rather
than assuming one CPU implies a fixed amount of RAM. NERSC retains its previous
32-CPU example settings. The ALCF capacity profiles use `submission_mode: allocation`
and `max_concurrent: 1`: each submission occupies one node and one queue entry.
Custom PBS profiles can select `submission_mode: arrays` for queues permitting
multiple allocations; `max_concurrent` then caps simultaneous array subjobs.

All config, flux, geometry, cache and output paths must be accessible on compute
nodes and inside the container. Replace S3DF `/sdf` paths in production configs;
do not assume CVMFS is available at other sites. Stage flux files to a site
filesystem and update `flux.file_pattern` if needed. The launcher automatically
mounts config directories, output storage, and existing host paths referenced by
production/source YAML (including flux glob directories). Container-only paths
remain supplied by the image. For paths not visible on the login node or paths
constructed dynamically, set `bind_paths` in the profile or use `--bind`.
Explicit source:destination:mode mounts are preserved. The output/cache storage must support
shared file locks and atomic rename. Keep each production's output directory
unique; do not submit the same production simultaneously through two backends.

Both the submission checkout and compute-node container must contain this
scheduler implementation, including `dlpgen_opt.batch_worker`. The v0.4.6 image
predates these additions: rebuild the image or use a matching source overlay.

## Aurora / Polaris

Use an Apptainer/Singularity SIF accessible on the target cluster. Example from
the checkout, with the updated checkout and source overlay:

```bash
python3 submit.py /path/to/production.yaml \
  --profile polaris \
  --container /path/to/dlpgen-opt.sif \
  --bind "$PWD/src/dlpgen_opt:/usr/local/lib/python3.10/dist-packages/dlpgen_opt:ro" \
  --dry-run
```

Choose `--profile aurora` for Aurora. Remove `--dry-run` to submit with `qsub`.
Scripts go under `OUTPUT/pbs`; live logs go under `OUTPUT/pbs/logs`. PBS itself
may also create default output files for bootstrap errors before redirection.
Modules and container launch errors after redirection appear in the live logs.
The Polaris profile enables `fakeroot: true`, adding both the PBS
`singularity_fakeroot=true` resource and Apptainer's `--fakeroot`, as recommended
by [Polaris container documentation](https://docs.alcf.anl.gov/polaris/containers/containers/).
Aurora retains its separate defaults.

PBS scripts use rerunnable arrays (`-r y`, `-J ...%N`) and retain complete PBS job
IDs, including `[].server`, in `afterok` dependencies. Each allocation launches
one rank with `mpiexec`, with CPU depth set to the profile's CPU allocation. The
programs inside that rank use ordinary subprocesses, not MPI physics.

Polaris allocates whole nodes; packing avoids assigning an entire node to each
small detector job. The example requests `home:eagle` on Polaris and `home:flare`
on Aurora; change these to the filesystems your paths actually use. Queue and
array limits vary, including eligible-subjob limits; a concurrency setting alone
does not guarantee admission under every site's queued-job limit.
See [ALCF job examples](https://docs.alcf.anl.gov/running-jobs/example-job-scripts/),
[Aurora job guidance](https://docs.alcf.anl.gov/aurora/running-jobs-aurora/), and
[Aurora containers](https://docs.alcf.anl.gov/aurora/containers/containers/).

## NERSC Perlmutter CPU

Import the matching image into Shifter first and wait until it is ready:

```bash
shifterimg pull docker:YOUR_REGISTRY/dlpgen-opt:YOUR_TAG

dlpgen-opt submit /path/to/production.yaml \
  --profiles /path/to/sites.yaml --profile nersc_cpu \
  --container docker:YOUR_REGISTRY/dlpgen-opt:YOUR_TAG \
  --max-concurrent 10 --dry-run
```

Remove `--dry-run` to submit. Scripts/logs go under `OUTPUT/slurm`. The profile
uses `qos: regular`, `constraint: cpu`, `srun`, and Shifter with `--clearenv` and
`--module=none`; these non-MPI workloads do not need host MPI/GPU library
injection. Shifter accepts a `docker:...` reference or `id:HASH`, not a SIF path.
Additional `--bind` paths become Shifter volume mounts. Use `/global/cfs/...`
rather than `/cfs/...` when explicitly binding Community File System paths.
See [Perlmutter jobs](https://docs.nersc.gov/systems/perlmutter/running-jobs/)
and [Shifter usage](https://docs.nersc.gov/development/containers/shifter/how-to-use/).

## Packing, preparation, and failures

The default ALCF capacity profiles submit one ordinary PBS job. It runs GiBUU
planning, bounded preparation packs, finalization, and production in that order.
Production tasks run in waves of up to the CPU budget (32 on Polaris, 102 on
Aurora); preparation packs retain the profile's `prepare_jobs_per_task` setting
so each GiBUU task has CPUs for its native workers. No array-index, packing or
concurrency flags are needed. A failure stops later phases.

The profile's `time` is the walltime for the **entire allocation**, including
preparation; `prepare_time` applies only in array mode. Large campaigns may need
a longer walltime within queue policy, or resubmission with `execution.resume: true`
to reuse completed work. There is no automatic walltime extension or resubmission.
Existing project jobs may still exhaust the queue allowance: check `qstat -Qf capacity`
and `qstat -f`. The launcher does not bypass project limits.

For custom PBS profiles using `submission_mode: arrays`, `jobs_per_task` controls
production jobs per array element. For 125 jobs and packing of 32, that means four
array elements. GiBUU uses plan → worker arrays → finalize → production dependencies.
These profiles require enough queue slots for the whole submitted chain, including
held jobs. Array chunks depend on the preceding chunk to keep concurrency bounded.
S3DF and NERSC retain their Slurm submission structure.

Seeds, task IDs, cache shards, normalization and selection are unchanged by either
submission mode. PBS task logs go in `logs/packed/<stage>/00000.out` and `.err`
(append on retry). On termination the runner stops active child process groups.
Concurrency is bounded by the CPU budget and visible affinity, with each child's
GiBUU worker budget capped to its share. Finalization still checks completeness
and ESS. For insufficient statistics, submit the next explicit `--prepare-round N`,
as described in [gibuu-distributed.md](gibuu-distributed.md).

## Testing node utilization

Pull the updated checkout and use the source-overlay bind below with the 0.4.6
SIF (a plain 0.4.6 container lacks this runner). First run the one-event
`configs/production.smoke.yaml` to check the environment. Then from a checkout
on the requested shared filesystem, submit the CPU benchmark:

```bash
python3 submit.py configs/production.node-benchmark.yaml --profile aurora \
  --container /path/to/dlpgen-opt_v0-4-6.sif \
  --bind "$PWD/src/dlpgen_opt:/usr/local/lib/python3.10/dist-packages/dlpgen_opt:ro"
```

Use `--profile polaris` on Polaris. The benchmark runs 102 jobs with 20 generator
calls each and requires no external flux. Site defaults run it in one normal PBS
job: up to 102 children concurrently on Aurora or 32 on Polaris, with remaining
tasks waiting inside the allocation. After a release image includes this runner,
remove the source-overlay bind; no other flags are needed.
Use a fresh production name/output directory for comparisons; otherwise resume
will reuse completed events and invalidate timing. Increase generator calls per
job if startup dominates. Keep the source overlay unchanged while jobs run.

Read `runs/node_benchmark_v001/pbs/logs/*.out`. Each runner reports its allowed
CPU IDs, requested/effective budget, concurrency and CPUs per child. At completion
it writes `utilization-<timestamp>-<PID>.json` below `pbs/logs/packed/<stage>/` with wall time,
child CPU seconds, average busy cores and worker-budget utilization. CPU time
includes reaped descendants (native generators and detector processes). The
report covers the whole runner, including startup and I/O; it is not an
instantaneous CPU measurement. Reports may be absent after a hard scheduler kill.

While running and after completion, inspect the actual array subjob:

```bash
qstat -f 'JOBID[0].SERVER'
qstat -xf 'JOBID[0].SERVER'
```

Check `resources_used.cput`, `resources_used.walltime`, `resources_used.mem`,
`resources_used.cpupercent`, exit status and scheduler comments. Availability and
update frequency depend on PBS accounting. Divide CPU seconds by wall seconds
for average busy cores. Divide again by **102 on Aurora or 32 on Polaris** for
node CPU utilization; the JSON's budget utilization instead divides by the
configured effective budget. For example, 28 busy cores on Polaris is 87.5%.

For a long CPU-bound phase, roughly 70–90% or higher is a useful initial target,
not a correctness requirement. Inspect per-task logs to distinguish startup,
I/O waits, sparse/no-work preparation tasks and a nearly empty final pack. A high
budget percentage with a small budget can still waste most of a node. Compare
events per wall second and events per node-hour, not CPU percentage alone.
Check memory peaks against the scheduler's usable node memory and leave headroom;
OOM, swapping or falling throughput means reduce concurrency. GPUs are unused
by this CPU workflow, so low GPU utilization is expected.

For GiBUU, repeat with your flux configured on an ALCF filesystem. Inspect
`prepare_rNNN_worker_*` logs and each task's `.err`: they report the effective
native worker count. The 102/32-CPU defaults are starting points, not cluster
benchmark results; compare lower CPU budgets and packing counts on a fresh
campaign, and confirm finalization succeeds before comparing throughput.
