# Production on Slurm and PBS

`dlpgen-opt submit` supports S3DF Slurm, ALCF Aurora/Polaris PBS Professional,
and NERSC Perlmutter CPU Slurm with Shifter. `run CONFIG --job INDEX` remains
scheduler independent. The standalone top-level `submit.py` is the lightweight S3DF launcher (Python
3.8+ and PyYAML). It shares the Slurm preparation/production workflow with the
installed CLI, including distributed GiBUU. Use the installed CLI for PBS/Shifter.

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

The Aurora, Polaris and NERSC example profiles request one node and 32 CPUs per array element, with
`jobs_per_task: 32` for production and `prepare_cpus_per_task: 32` for GiBUU
preparation. These are editable starting points, not measured optimal settings.
Memory-intensive jobs may need fewer packed jobs. PBS profiles have a
`max_concurrent` limit (2 in the examples); the CLI may lower it. To raise it,
change the profile after checking queue policy. For Slurm use `--max-concurrent`.

All config, flux, geometry, cache and output paths must be accessible on compute
nodes and inside the container. Replace S3DF `/sdf` paths in production configs;
do not assume CVMFS is available at other sites. Stage flux files to a site
filesystem and update `flux.file_pattern` if needed. Set `bind_paths` in the
profile or repeat `--bind /absolute/path`. The output/cache storage must support
shared file locks and atomic rename. Keep each production's output directory
unique; do not submit the same production simultaneously through two backends.

Both the submission installation and compute-node container must contain this
scheduler implementation, including `dlpgen_opt.batch_worker`. The v0.4.6 image
predates these additions: rebuild the image or use a matching source overlay.

## Aurora / Polaris

Use an Apptainer/Singularity SIF accessible on the target cluster. Example from
the checkout, after editing the copied site profile:

```bash
dlpgen-opt submit /path/to/production.yaml \
  --profiles /path/to/sites.yaml --profile polaris \
  --container /path/to/dlpgen-opt.sif \
  --bind /path/to/project --max-concurrent 2 --dry-run
```

Choose `--profile aurora` for Aurora. Remove `--dry-run` to submit with `qsub`.
Scripts go under `OUTPUT/pbs`; live logs go under `OUTPUT/pbs/logs`. PBS itself
may also create default output files for bootstrap errors before redirection.
Modules and container launch errors after redirection appear in the live logs.

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

`jobs_per_task` controls independent production jobs per scheduler array element.
For 125 jobs and packing of 32, production uses four array elements, with the
last running jobs 96–124. Seeds and job directories still use the original
production indices. Per-job logs go in `logs/packed/00000.out` and `.err` (append
on retry). An array element fails if any packed job fails. On termination the
runner stops its active child process groups. Concurrency is bounded by the
requested CPUs and visible CPU affinity; each child's GiBUU worker budget is
capped to its share. Packing does not change event selection or physics.

GiBUU still uses the same distributed plan → worker arrays → finalize →
production chain. Preparation tasks are **not** packed: each receives the
preparation CPU allocation for its native worker pool. Automatic task counts
still follow `production.jobs`, independently of production packing. For 125
jobs, this means 125 preparation tasks even when production is four packed
array elements. A concurrency cap counts allocations, not individual production
jobs or native subprocesses.

Array chunks depend on the entire preceding chunk to keep the concurrency cap
global. Failed preparation prevents production from running. PBS may leave
dependent jobs held; inspect/cancel those jobs before resubmitting. Partial
cache work remains resumable. If finalization reports insufficient statistics,
submit the next explicit `--prepare-round N`, as described in
[gibuu-distributed.md](gibuu-distributed.md). No worker waits inside an allocation
for another queued worker.
