# dlpgen-opt

Reproducible production orchestration for the first stage of the DLPGenerator
phase-space optimization study, with DLPGenerator and GENIE source backends:

```text
DLPGenerator -> HEPEVT -----+
                            +-> edep-sim -> edep2supera/SuperaAtomic -> LArCV ROOT
dk2nu -> GENIE -> RooTracker+
```

SPINE training and evaluation intentionally remain outside this repository.
The current milestone is a deterministic production chain for local execution
or S3DF SLURM arrays. SPINE remains a standalone consumer of its LArCV output.

## What is implemented

- Pinned Git submodules for DLPGenerator, GENIE, dk2nu, edep-sim,
  SuperaAtomic, and edep2supera.
- A strict, versioned top-level production YAML schema.
- `run`, `generate`, `edep-sim`, `supera`, `validate`, and S3DF `submit` CLI
  commands.
- Deterministic, non-overlapping source, detector-simulation, and Supera seeds.
- Stable per-job paths, stage manifests, exact command capture, stdout/stderr
  logs, input checksums, dependency commits, and output validation.
- Restart of completed valid stages, with explicit `--force` handling for
  incomplete outputs.
- A common production Dockerfile that builds Geant4, Pythia8, GENIE, dk2nu,
  edep-sim, DLPGenerator, SuperaAtomic, and edep2supera on a pinned LArCV2/ROOT
  base. GENIE is Pythia8-only because the base uses ROOT 6.32; the image also
  selects GENIE's Pythia8 decayer, DIS hadronizer, and charm hadronizer in
  place of the Pythia6 defaults still present in GENIE 3.6.2.
- A guarded Supera frontend that exits after `IOManager.finalize()` to avoid
  unstable PyROOT static teardown; the pipeline then independently reopens and
  validates the populated `sparse3d_pcluster_tree`.
- A minimal liquid-argon-vat geometry and smoke-test configuration.

## Checkout and build

```bash
git clone --recurse-submodules <repository-url> dlpgen-opt
cd dlpgen-opt
git submodule update --init --recursive
docker build --platform linux/amd64 -t dlpgen-opt:0.1.0 .
```

The explicit platform is useful on Apple Silicon because the pinned ROOT base
image is `linux/amd64`. Geant4 and its physics datasets make the first image
build substantial; subsequent builds use Docker layers and a persistent
BuildKit compiler cache.

GitHub publishes the production image only when a GitHub Release is explicitly
published. Source releases must use `vX.Y.Z`; the workflow removes that leading
`v` for the immutable image tag, yielding
`ghcr.io/deeplearnphysics/dlpgen-opt:X.Y.Z`. The newest non-prerelease also
updates `ghcr.io/deeplearnphysics/dlpgen-opt:latest`. The workflow checks
GitHub's current latest-release ID before applying the rolling tag, so rerunning
an older release cannot move `latest` backwards. Buildx retains a GitHub Actions
cache for subsequent releases.

For a finalized production, record the digest returned by:

```bash
docker image inspect dlpgen-opt:0.1.0 --format '{{index .RepoDigests 0}}'
```

and replace `software.container_image` in the production YAML with that
immutable image reference.

## Inspect and run one job

Dry-run is read-only and prints every resolved command and output path:

```bash
docker run --rm \
  -v "$PWD:/work" \
  dlpgen-opt:0.1.0 \
  run configs/production.example.yaml --job 0 --dry-run
```

Execute the complete job:

```bash
docker run --rm \
  -v "$PWD:/work" \
  dlpgen-opt:0.1.0 \
  run configs/production.example.yaml --job 0
```

## Submit an S3DF SLURM production

Following the `s3df_milano`/`s3df_roma` pattern in
[`DeepLearnPhysics/spine-prod`](https://github.com/DeepLearnPhysics/spine-prod),
the submitter creates zero-based SLURM arrays whose task IDs map directly to
the existing `--job` argument. Stage behavior, seeds, resume handling, and
output layout are therefore identical to local execution.

First stage the released image once on S3DF (do not make every array task pull
the multi-GB image):

```bash
apptainer pull /sdf/data/neutrino/images/dlpgen-opt_0-1-0.sif \
  docker://ghcr.io/deeplearnphysics/dlpgen-opt:0.1.0
```

The top-level `submit.py` launcher uses the PyYAML already provided at S3DF. It
does not install or import this project, Pydantic, Jinja2, or any physics
software on the login node. From the checkout, submit directly:

```bash
export DLPGEN_OPT_CONTAINER_PATH=/sdf/data/neutrino/images/dlpgen-opt_0-1-0.sif
python3 submit.py configs/production.example.yaml \
  --profile s3df_milano --max-concurrent 20
```

Use `--profile s3df_roma` to select Roma. The launcher reads
`configs/slurm/s3df.yaml`, whose defaults mirror spine-prod: account
`neutrino:ml-dev`, one CPU, 4 GB per CPU, two hours, `/sdf` bound into the
container, and at most 99 tasks per array. Override the account, partition, or
resources directly on the command line for a production allocation.
Productions larger than 99 jobs are split into dependency-chained arrays while
retaining their global job indices. Add another filesystem with
`--bind /path`; use `--dry-run` to print the exact scripts without writing or
calling `sbatch`.

The launcher uses `yaml.safe_load` but reads only `production.name`,
`production.output_dir`, and `production.jobs` because those values determine
the scheduler layout. The strict schema and all remaining configuration are
loaded and validated by `dlpgen-opt` inside each compute-job container. Use
`--runtime apptainer` if both compatibility command names exist and you want to
select Apptainer explicitly. Account, partition, CPU, memory, time, array-size,
and bind settings all have command-line overrides; run
`python3 submit.py --help` for the complete list.

Each array task invokes the image's entrypoint so Geant4 and GENIE receive the
same runtime environment as Docker. Initialization metadata is protected by a
filesystem lock because all tasks share one production directory.

For a one-event integration check, use `configs/production.smoke.yaml`.

## Generate from a dk2nu beam flux

The GENIE backend reads native dk2nu files, generates argon-40 interactions,
converts GHEP to RooTracker, and then uses the same edep-sim and Supera stages.
The supplied local flux artifact is intentionally ignored by Git and by the
Docker build context; mount the repository (or a flux-data directory) at run
time:

```bash
docker run --rm \
  -v "$PWD:/work" \
  dlpgen-opt:0.1.0 \
  run configs/production.genie-smoke.yaml --job 0
```

`source.flux.distance_m` is the longitudinal beam-coordinate location of the
sampling window. `center_m` and `window_size_m` define its transverse center
and dimensions. They affect flux ray reweighting only: they are not detector
geometry. The neutrino interaction is independently placed at
`source.vertex_cm`, which defaults to the center of the 4 m LAr vat. Replace
the example's provisional `50.0 m` once the desired SBND or ICARUS beam-frame
location is known.

`file_pattern` accepts a shell-style filename pattern, so a future flux
catalog can be mounted outside the image. For a 10,000-file production we
should add a deterministic per-job catalog selector before launching at scale;
the present implementation records checksums for every matched input, which is
deliberately conservative but unnecessarily expensive for that many files.

Run or debug individual stages:

```bash
docker run --rm -v "$PWD:/work" dlpgen-opt:0.1.0 \
  generate configs/production.example.yaml --job 0
docker run --rm -v "$PWD:/work" dlpgen-opt:0.1.0 \
  edep-sim configs/production.example.yaml --job 0
docker run --rm -v "$PWD:/work" dlpgen-opt:0.1.0 \
  supera configs/production.example.yaml --job 0
docker run --rm -v "$PWD:/work" dlpgen-opt:0.1.0 \
  validate configs/production.example.yaml --job 0
```

The run is written under `runs/baseline_v001/`:

```text
runs/baseline_v001/
├── manifest.yaml
├── resolved_config.yaml
└── jobs/
    └── 00000/
        ├── generate.yaml
        ├── edep-sim.yaml
        ├── supera.yaml
        ├── source/
        │   ├── events.csv
        │   └── events.pbomb.hepevt
        ├── edep-sim/
        │   ├── run.mac
        │   └── edep.root
        ├── supera/
        │   ├── config.yaml
        │   └── supera.root
        └── logs/
```

## Important current limitation

DLPGenerator can produce multiple interaction vertices in one `Generate()`
call, but the pinned upstream edep-sim text reader creates one Geant event per
extended HEPEVT vertex. The older direct `bomb` macro path referenced by
DLPGenerator is not present in current upstream edep-sim.

The initial handoff therefore requires `NumEvent: [1, 1]`. The source stage
checks this from the generated CSV and fails if a call contains multiple
interactions. This avoids silently splitting pileup into separate detector
events or collapsing distinct vertices. Supporting true multi-vertex calls
requires either a small upstream edep-sim reader extension or a maintained
DLPGenerator kinematics plugin.

## Development without the physics stack

The orchestration unit tests do not require ROOT or Geant4:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

See [Architecture](docs/architecture.md), [production schema](docs/configuration.md),
and [implementation plan](docs/implementation-plan.md) for the design boundary,
assumptions, and next milestones.
