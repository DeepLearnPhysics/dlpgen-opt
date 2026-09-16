# dlpgen-opt

Reproducible production orchestration for the first stage of the DLPGenerator
phase-space optimization study, with DLPGenerator, GENIE, GiBUU, NuWro, and NEUT source
backends:

```text
DLPGenerator -> HEPEVT ------------------------+
dk2nu -> GENIE -> RooTracker ------------------+-> edep-sim -> edep2supera/SuperaAtomic -> LArCV ROOT
dk2nu -> canonical flux -> GiBUU -> RooTracker +
dk2nu -> canonical flux -> NuWro -> RooTracker +
dk2nu -> canonical flux -> NEUT -> NuHepMC -> RooTracker +
```

SPINE training and evaluation intentionally remain outside this repository.
The current milestone is a deterministic production chain for local execution
or S3DF SLURM arrays. SPINE remains a standalone consumer of its LArCV output.

## What is implemented

- Pinned Git submodules for DLPGenerator, GENIE, NuWro, ROOTEGPythia6, dk2nu,
  edep-sim, SuperaAtomic, and edep2supera.
- A strict, versioned top-level production YAML schema.
- `prepare`, `run`, `generate`, `edep-sim`, `supera`, `validate`, and S3DF
  `submit` CLI commands.
- Deterministic, non-overlapping source, detector-simulation, and Supera seeds.
- Stable per-job paths, stage manifests, exact command capture, stdout/stderr
  logs, input checksums, dependency commits, and output validation.
- Restart of completed valid stages, with explicit `--force` handling for
  incomplete outputs.
- Deterministic whole-file/POT-bounded dk2nu sampling with shared,
  lock-protected caches of canonical throws, compact generator flavor spectra, and
  immutable GiBUU candidate shards with non-overlapping campaign allocation.
- A common production Dockerfile that builds Geant4, Pythia8, GENIE, GiBUU, NuWro,
  dk2nu, edep-sim, DLPGenerator, SuperaAtomic, and edep2supera on a pinned LArCV2/ROOT
  base. The standalone ROOTEGPythia6 adapter restores the interface removed in
  ROOT 6.30, allowing the same GENIE build to contain both Pythia6 and Pythia8;
  Pythia8 remains the explicit default. NuWro uses the same pinned Pythia6
  runtime for DIS hadronization.
- A NEUT 5.8.0 backend extracted from a digest-pinned upstream image. Its ROOT
  6.34 libraries are isolated to NEUT subprocesses while the main stack remains
  on ROOT 6.32. The backend converts cached spectra to native ROOT histograms,
  caches per-flavor interaction normalizations once per campaign, and retains
  the native event vectors, resolved cards, and NuHepMC records.
- A guarded Supera frontend that exits after `IOManager.finalize()` to avoid
  unstable PyROOT static teardown; the pipeline then independently reopens and
  validates the populated `sparse3d_pcluster_tree`.
- A minimal liquid-argon-vat geometry and smoke-test configuration.

## Checkout and build

```bash
git clone --recurse-submodules <repository-url> dlpgen-opt
cd dlpgen-opt
git submodule update --init --recursive
docker build --platform linux/amd64 -t dlpgen-opt:0.4.2 .
```

The default `runtime` target includes GENIE, GiBUU, and NuWro plus the NEUT
adapter, but not the NEUT binaries. To use NEUT, assemble the optional runtime
locally from its upstream public quickstart image:

```bash
docker build --platform linux/amd64 --target runtime-neut \
  -t dlpgen-opt:0.4.2-neut .
```

After that one-time build, NEUT uses the same `dlpgen-opt run` interface as the
other generators. No NEUT source checkout or manual library setup is required.

```bash
docker run --rm -v "$PWD:/work" dlpgen-opt:0.4.2-neut \
  run configs/production.neut-bnb.yaml --job 0
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
an older release cannot move `latest` backwards. Buildx exports both a scoped
GitHub Actions cache and the durable, disposable registry cache
`ghcr.io/deeplearnphysics/dlpgen-opt-buildcache:buildcache`; subsequent releases
read both. Each release recursively verifies its published image, platform, and
attestation manifests before completing.

The production image package contains untagged platform images and attestations
referenced by its tagged OCI indexes. Do not remove those untagged objects as
"cleanup." Build-cache cleanup belongs only in the separate
`dlpgen-opt-buildcache` package.

For a finalized production, record the digest returned by:

```bash
docker image inspect dlpgen-opt:0.4.2 --format '{{index .RepoDigests 0}}'
```

and replace `software.container_image` in the production YAML with that
immutable image reference.

## Inspect and run one job

Dry-run is read-only and prints every resolved command and output path:

```bash
docker run --rm \
  -v "$PWD:/work" \
  dlpgen-opt:0.4.2 \
  run configs/production.example.yaml --job 0 --dry-run
```

Execute the complete job:

```bash
docker run --rm \
  -v "$PWD:/work" \
  dlpgen-opt:0.4.2 \
  run configs/production.example.yaml --job 0
```

## Iterate on DLPGenerator code

The released image remains the default and contains its pinned DLPGenerator
build. To test code from another checkout without rebuilding the image, add an
optional path to the DLPGenerator source configuration:

```yaml
source:
  type: dlpgen
  config: dlpgen/baseline.yaml
  checkout: /sdf/data/neutrino/users/drielsma/DLPGenerator
```

The checkout must be visible inside the container. `/sdf` is already bound by
the S3DF profiles; for a checkout elsewhere, add the corresponding `--bind` to
`submit.py`. With Docker, mount either the checkout itself or a parent
directory containing it.

At production initialization, the pipeline fingerprints tracked, modified,
and untracked non-ignored source files and records the Git commit, dirty state,
and content fingerprint. The first array task copies and compiles that exact
snapshot under `PRODUCTION_DIR/.dlpgen-cache/FINGERPRINT`; a filesystem lock
makes the other tasks wait and then reuse it. Build output is recorded in
`build.log`. The custom executable, Python package, shared library, and ROOT
headers are selected only for the generation stage. Omitting `checkout`
retains the standard embedded build with no extra work.

A production directory cannot mix DLPGenerator fingerprints. After changing
the checkout, use a new production name/output directory. This keeps fast
code iteration reproducible while allowing dirty development checkouts.

## Submit an S3DF SLURM production

Following the `s3df_milano`/`s3df_roma` pattern in
[`DeepLearnPhysics/spine-prod`](https://github.com/DeepLearnPhysics/spine-prod),
the submitter creates zero-based SLURM arrays whose task IDs map directly to
the existing `--job` argument. Stage behavior, seeds, resume handling, and
output layout are therefore identical to local execution.

First stage the released image once on S3DF (do not make every array task pull
the multi-GB image):

```bash
apptainer pull /sdf/data/neutrino/images/dlpgen-opt_0-4-0.sif \
  docker://ghcr.io/deeplearnphysics/dlpgen-opt:0.4.2
```

The top-level `submit.py` launcher uses the PyYAML already provided at S3DF. It
does not install or import this project, Pydantic, Jinja2, or any physics
software on the login node. From the checkout, submit directly:

```bash
export DLPGEN_OPT_CONTAINER_PATH=/sdf/data/neutrino/images/dlpgen-opt_0-2-0.sif
python3 submit.py configs/production.example.yaml \
  --profile s3df_milano --max-concurrent 20
```

Use `--profile s3df_roma` to select Roma. The launcher reads
`configs/slurm/s3df.yaml`, whose defaults mirror spine-prod: account
`neutrino:ml-dev`, one CPU, 4 GB per CPU, two hours, `/sdf` bound into the
container, and at most 99 tasks per array. Override the account, partition, or
resources directly on the command line for a production allocation.
Productions larger than 99 jobs are split into dependency-chained arrays. Each
SLURM array uses local indices starting at zero, which are offset back to the
production's global job indices inside the task. Add another filesystem with
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
same runtime environment as Docker. The container starts with a clean
environment and disables Python's per-user site-packages so host packages in
`~/.local` cannot override ROOT, Supera, or edep2supera from the image.
Initialization metadata is protected by a filesystem lock because all tasks
share one production directory. A successful array task prints an explicit
completion message and timestamp to its SLURM output log.

For a one-event integration check, use `configs/production.smoke.yaml`.

## Merge a production into train/test files

The standalone `merge.py` launcher assigns complete Supera job files to a
reproducible train/test split, writes auditable file lists, and submits `hadd`
tasks with the same S3DF profiles and production image:

```bash
export DLPGEN_OPT_CONTAINER_PATH=/sdf/data/neutrino/images/dlpgen-opt_0-2-0.sif
python3 merge.py runs/baseline_sbn_v002 \
  --profile s3df_milano \
  --train-fraction 0.8 \
  --max-file-size 80GB \
  --max-concurrent 10
```

With one chunk per split, this produces
`baseline_sbn_v002_train.root` and `baseline_sbn_v002_test.root`. If a split
needs multiple chunks, its outputs are numbered from zero, for example
`baseline_sbn_v002_train_0.root` and
`baseline_sbn_v002_train_1.root`. Outputs, file lists, the full
`merge_plan.yaml`, SLURM scripts, and logs live under the production's
`merged/` directory by default.

The size limit is conservatively planned from the sum of input file sizes;
ROOT compression and merge metadata mean the exact output size is only known
after `hadd` completes. Splitting is deterministic for a given `--seed`
(default `12345`) and never divides an individual production job. By default,
every configured job must have a completed, nonempty Supera output. Use
`--allow-missing` to explicitly merge only the completed subset,
`--prepare-only` to create file lists and SLURM scripts without submitting, or
`--force` to allow `hadd` to replace existing merged outputs. Run
`python3 merge.py --help` for resource and output-directory overrides.

## Generate from a dk2nu beam flux

The GENIE backend reads native dk2nu files, generates argon-40 interactions,
converts GHEP to RooTracker, and then uses the same edep-sim and Supera stages.
Set `source.hadronization` to `pythia8` (the default) or `pythia6`. Each GENIE
process receives a matching, isolated XML configuration through `GXMLPATH`; a
requested backend that is absent from the image is an error rather than a
silent fallback. `configs/production.genie-pythia6-smoke.yaml` provides an
explicit Pythia6 comparison profile.
The supplied local flux artifact is intentionally ignored by Git and by the
Docker build context; mount the repository (or a flux-data directory) at run
time:

```bash
docker run --rm \
  -v "$PWD:/work" \
  dlpgen-opt:0.4.2 \
  run configs/production.genie-smoke.yaml --job 0
```

`source.flux.distance_m` is the longitudinal beam-coordinate location of the
sampling window. `center_m` and `window_size_m` define its transverse center
and dimensions. They affect flux ray reweighting only: they are not detector
geometry. The neutrino interaction is independently placed at
`source.vertex_cm`, which defaults to the center of the 4 m LAr vat.

Two BNB source configurations are provided. They use the same native dk2nu
decay-record input but project it to the nominal mean detector baselines:

- `configs/genie/bnb_sbnd.yaml`: SBND at 110 m.
- `configs/genie/bnb_icarus.yaml`: ICARUS at 600 m.

LBNF FHC and RHC profiles are also provided for all four generator backends:
`configs/{genie,gibuu,nuwro,neut}/lbnf_{fhc,rhc}_{nd,fd}.yaml`. They use the
v3r5p10 `OfficialEngDesignSept2021_OnAxis` CVMFS catalogs and the generic
locations embedded in their dk2nu metadata. The ND profiles use a 7 x 5 m
beam-normal window at 574 m; the FD profiles use a 12 x 14 m one-module
beam-normal window at 1,297 km. These are flux-sampling windows, not the
edep-sim detector geometry. Canonical adapters scan one complete, roughly
one-million-decay file into a shared 0--120 GeV, 1200-bin cache by default;
GENIE instead selects one catalog file directly for each job. Payloads are
read from CVMFS without checksumming or staging their approximately 730 MB
files.

GiBUU, NuWro, and NEUT use the same canonical dk2nu projection and compact per-flavor
histograms. Unlike GiBUU, NuWro samples that mixed beam internally and writes
the requested number of unweighted events directly, so it needs no candidate
pool. The supplied NuWro profiles are `configs/nuwro/bnb_sbnd.yaml` and
`configs/nuwro/bnb_icarus.yaml`; their production entry points are
`configs/production.nuwro-bnb.yaml` and
`configs/production.nuwro-bnb_icarus.yaml`.

NEUT accepts one neutrino species per native run. `dlpgen-opt prepare` therefore
runs one single-event probe per present flavor and records NEUT's
flux-averaged total cross section. Each job draws an exact, deterministic
multinomial flavor allocation using the canonical flux integral times that
cross section, then runs only flavors with a nonzero allocation. This is a
small normalization cache, not a GiBUU-style candidate pool. The supplied
profiles are `configs/neut/bnb_sbnd.yaml` and `configs/neut/bnb_icarus.yaml`,
with `configs/production.neut-bnb.yaml` and
`configs/production.neut-bnb_icarus.yaml` as production entry points.

The upstream quickstart image is public and pinned by digest, but its embedded
NEUT source checkout is not publicly readable and the image does not expose a
clear redistribution license. The public dlpgen-opt image therefore ships the
NEUT adapter without its binaries. Build the local `runtime-neut` target shown
above before using a `production.neut-*` profile.

For example:

```bash
docker run --rm -v "$PWD:/work" dlpgen-opt:0.4.2 \
  run configs/production.bnb_sbnd.yaml --job 0
```

`file_pattern` accepts a shell-style filename pattern, including a catalog on
CVMFS. The matched paths are sorted and each job selects one file using
`(base_seed + job) % file_count`, distributing an array deterministically
through the catalog without opening every file. The production manifest stores
the catalog path-list digest and the job record stores the selected path.

Set `checksum_files: false` for immutable CVMFS inputs to avoid downloading a
complete ROOT file merely to hash it. With `stage_to_local: true`, the logged
GENIE process copies only its selected file to node-local temporary storage
before ROOT opens it. This is preferable to copying the full beam catalog to
`/sdf/data`.

Run or debug individual stages:

```bash
docker run --rm -v "$PWD:/work" dlpgen-opt:0.4.2 \
  generate configs/production.example.yaml --job 0
docker run --rm -v "$PWD:/work" dlpgen-opt:0.4.2 \
  dlpgen-opt edep-sim configs/production.example.yaml --job 0
docker run --rm -v "$PWD:/work" dlpgen-opt:0.4.2 \
  supera configs/production.example.yaml --job 0
docker run --rm -v "$PWD:/work" dlpgen-opt:0.4.2 \
  validate configs/production.example.yaml --job 0
```

The explicit `dlpgen-opt` prefix is needed for this Docker invocation because
the image also exposes bare `edep-sim` as a shortcut to the native simulator.

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
        │   └── events.pbomb.hepevt  # DLPGenerator; GENIE/GiBUU use events.gtrac.root
        ├── edep-sim/
        │   ├── run.mac
        │   └── edep.root
        ├── supera/
        │   ├── config.yaml
        │   └── supera.root
        └── logs/
```

### Inspecting edep-sim output

[`examples/read_edep.py`](examples/read_edep.py) is a minimal PyROOT example
that reads the energy-deposit segments in `edep.root` and resolves their
contributor track IDs to the corresponding particle trajectories:

```bash
docker run --rm -v "$PWD:/work" dlpgen-opt:0.4.2 \
  python3 examples/read_edep.py \
  runs/baseline_v001/jobs/00000/edep-sim/edep.root
```

Use `--event N` to select another tree entry and `--max-deposits N` to control
how many deposits are printed per sensitive detector. The example uses PyROOT
because the nested, memberwise-serialized `TG4HitSegment` and trajectory-point
vectors in edep-sim output are not currently readable by uproot alone.

## DLPGenerator interaction-to-image contract

dlpgen-opt intentionally maps exactly one DLPGenerator interaction to one
edep-sim event and therefore one output image. DLPGenerator owns interaction
mixtures through its `InteractionSelection` configuration. The
`weighted_random` mode makes an independent, seed-reproducible block selection
for every call. Each named block carries its own `SelectionWeight`, keeping the
mixture probability next to the particle distribution it controls.

`configs/dlpgen/baseline_dune.yaml` records the current, pre-optimization DUNE
interaction-context reference. It selects one CC-like or NC-like block per
image with equal probability. Consecutive events may have the same type, while
the sample approaches a 50/50 mixture at large size. A lepton is mandatory in
the CC-like block and absent from the NC-like block, so its presence is not
correlated with random particle multiplicity. MPR singles are intentionally
excluded. The profile retains the supplied uniform directions, kinetic-energy
and multiplicity ranges. Like the SBN baseline, all interactions use the single
space-time point `X/Y/Z/T: [0, 0]`.

Profiles that instead request multiple interactions from a selected block are
still rejected by the source-stage guard. This pipeline is sampling interaction
context, not pileup or the ability of clustering to separate vertices. A
detector-specific production entry point also needs a matching DUNE GDML and
Supera image definition.

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
