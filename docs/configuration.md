# Production configuration

`schema_version: 1` is strict: unknown keys are rejected so misspellings cannot
silently change a production. Relative paths are resolved against the YAML file,
not the caller's working directory.

```yaml
schema_version: 1
production:
  name: baseline_v001
  output_dir: ../runs/baseline_v001
  jobs: 100
  generator_calls_per_job: 100
  base_seed: 104729
  seed_stride: 10
source:
  type: dlpgen
  config: dlpgen/baseline.yaml
  executable: dlpgen
  # Optional development checkout; omit to use the image's pinned build.
  checkout: /sdf/data/neutrino/users/example/DLPGenerator
  expected_commit: dcf6f6aeac706ab27631781900e9af998777c368
software:
  container_image: registry.example/dlpgen-opt@sha256:<digest>
  edep_sim:
    executable: edep-sim
    expected_commit: 4548701be5bd82daae65c9f1e51f63b1886b71d9
  edep2supera:
    executable: dlpgen-opt-supera
    expected_commit: da89c94cfcce10c39b994df5e7ca5dffa067ea6c
  supera_atomic:
    expected_commit: 426408371d0a4bb20495cffabc0a8539df6fdae4
detector:
  geometry: geometry/lar_vat.gdml
  supera_config: supera/lar_vat.yaml
  physics_list: QGSP_BERT
execution:
  resume: true
```

For job `j`, seeds are derived as:

```text
source = base_seed + j * seed_stride
edep   = source + 1
supera = source + 2
```

The stride must be at least three and the schema verifies that all derived
values remain within the signed integer range. The per-job Supera config is
copied into the run directory and its `BBoxConfig.Seed` is replaced with the
derived seed.

## Default spatial contract

The example geometry defines a centered `400 cm` cube of sensitive liquid
argon. Supera uses the identical fixed extent, `[-200, 200] cm` on each axis,
with `0.5 cm` voxels, producing an `800 x 800 x 800` image grid. DLPGenerator's
spatial ranges use millimetres and default to `[0, 0]` on every axis, fixing the
primary vertex at the vat center. A centered generation cube can be enabled by
giving each range explicit symmetric limits, such as `[-50, 50]` for a `10 cm`
cube; the detector and rasterization extents should remain unchanged.

Completed production directories are immutable with respect to their resolved
configuration. Re-running the same configuration resumes completed, valid
stages. A pre-existing output without a completed stage record is never
overwritten unless `--force` is supplied.

When `source.checkout` is set, its path is resolved like the other input paths
and must be mounted inside the container. The checkout is fingerprinted and
built once in a locked, content-addressed cache below the production
directory. Both clean commits and dirty development trees are supported. The
fingerprint is stored in the production manifest, and changing source content
requires a new production directory. Without this field, `source.executable`
and the DLPGenerator build embedded in the image are used exactly as before.

SLURM resource profiles are intentionally separate from this physics schema in
`configs/slurm/s3df.yaml`. This lets the same immutable production YAML run
locally, on Milano, or on Roma without changing its recorded configuration.

## GENIE source

Select the alternative source with `source.type: genie`:

```yaml
source:
  type: genie
  config: genie/bnb_sbnd.yaml
  executable: dlpgen-opt-genie
  expected_commit: 4a6d9e5e50ed9ae72636dd363a2f3fbf672330a6
  dk2nu_expected_commit: 5b1d8c2cb72b5752a82592ea66af61d8e64a8343
```

The referenced GENIE source configuration contains the beam and interaction
settings:

```yaml
flux:
  file_pattern: ../../NuBeam_production_BooNE_50m_I174000A_0.dk2nu.root
  distance_m: 110.0
  center_m: [0.0, 0.0]
  window_size_m: [1.0, 1.0]
  flavors: [12, -12, 14, -14]
  max_energy_gev: 20.0
  max_weight_scan_entries: 250000
  checksum_files: false
  stage_to_local: true
tune: AR23_20i_00_000
spline: /opt/genie/xsec/gxspl-AR23_20i_00_000.xml
target_pdg: 1000180400
vertex_cm: [0.0, 0.0, 0.0]
```

For compatibility, GENIE settings may still be written inline:

```yaml
source:
  type: genie
  flux:
    file_pattern: ../NuBeam_production_BooNE_50m_I174000A_0.dk2nu.root
    distance_m: 50.0
    center_m: [0.0, 0.0]
    window_size_m: [1.0, 1.0]
    flavors: [12, -12, 14, -14]
    max_energy_gev: 20.0
    max_weight_scan_entries: 250000
  tune: AR23_20i_00_000
  spline: /opt/genie/xsec/gxspl-AR23_20i_00_000.xml
  target_pdg: 1000180400
  vertex_cm: [0.0, 0.0, 0.0]
```

The flux window is expressed in dk2nu beam coordinates. `distance_m` is its
longitudinal position and `center_m` is its transverse center; no detector GDML
or active-volume model is used by GENIE. The target is a point-like argon-40
mixture, and edep-sim places every resulting interaction at `vertex_cm` in the
simulation geometry. This cleanly separates beam spectrum/flavor sampling from
the study's generic LAr vat. The supplied `configs/genie/bnb_sbnd.yaml` and
`configs/genie/bnb_icarus.yaml` profiles use nominal mean BNB baselines of
110 m and 600 m, respectively.

The packaged LBNF source profiles follow the same convention and are named
`lbnf_{fhc,rhc}_{nd,fd}.yaml` under each of `configs/genie`, `configs/gibuu`,
`configs/nuwro`, and `configs/neut`. They reference the DUNE v3r5p10
`OfficialEngDesignSept2021_OnAxis` neutrino and antineutrino CVMFS catalogs.
The generic dk2nu locations and agreed sampling faces are:

| Profile suffix | Longitudinal position | Beam-normal window |
| --- | ---: | ---: |
| `_nd` | 574 m | 7 x 5 m |
| `_fd` | 1,297 km | 12 x 14 m |

The ND setting intentionally represents the generic file-native location, not
the DUNE ND-LAr production window at 562.1179 m. The current rectangular-window
contract is axis-aligned in beam coordinates and does not encode the ND-LAr
detector-frame rotation. The FD face represents one nominal module. All LBNF
profiles cover 0--120 GeV. GiBUU, NuWro, and NEUT use 1200 cached bins and one
fully scanned input file by default; GENIE directly selects one file per job
from the complete catalog. `checksum_files: false` and `stage_to_local: false`
avoid preliminary or duplicate reads of these immutable, approximately 730 MB
CVMFS payloads.

The maximum energy is a lower bound used while dk2nu scans for its maximum
energy and ray weight; it should safely cover the selected beam. The example
allows electron and muon neutrinos and antineutrinos. The generated stage
records the selected flux path, cross-section spline checksum, GENIE tune, and
target isotope. The production manifest records a digest of the sorted catalog
paths without reading every payload. Each job deterministically selects one
catalog member using its job index and `base_seed`.

The production image includes these GENIE 3.6.2 spline selections by default:

| Tune | FSI model | Spline |
| --- | --- | --- |
| `AR23_20i_00_000` | hA2018 | `/opt/genie/xsec/gxspl-AR23_20i_00_000.xml` |
| `G18_10a_02_11b` | hA2018 | `/opt/genie/xsec/gxspl-G18_10a_02_11b.xml` |
| `G18_10b_02_11b` | hN2018 | `/opt/genie/xsec/gxspl-G18_10b_02_11b.xml` |
| `N24_20i_02_11b` | hA2018 | `/opt/genie/xsec/gxspl-N24_20i_02_11b.xml` |

hA and hN are alternative final-state-interaction transport models, not tunes
by themselves. The matched G18 `10a`/`10b` pair has the same primary-interaction
model and tune parameters; `10b` replaces hA2018 with hN2018. Consequently both
use the same published primary-interaction cross-section table, and the hN path
in the image is a tune-ID-adjusted copy of the hA spline. Override both fields
from a referenced source profile to select the comparison without duplicating
its flux settings:

```yaml
source:
  type: genie
  config: genie/bnb_sbnd.yaml
  tune: G18_10b_02_11b
  spline: /opt/genie/xsec/gxspl-G18_10b_02_11b.xml
```

`N24_20i_02_11b` is based on AR23 and restores the correlated high-momentum
tail in its spectral-function-like local Fermi-gas nuclear ground state. It is
therefore an initial-state/nuclear-model comparison, rather than another FSI
choice.

The pinned GENIE source also contains the G24 hA, hN, INCL, and Geant4/Bertini
configurations. They are not exposed as production selections yet: G24 changes
the primary QE and MEC models, and no authoritative G24 spline is published in
the GENIE/Fermilab catalog used by this image. Reusing or relabeling a G18
spline for G24 would give inconsistent cross sections. AR25 is gated similarly
until its SBN configuration overlay can be pinned alongside its published
splines.

Use a new `production.name` and `production.output_dir` for every tune so that
the immutable resolved-configuration check keeps samples from being mixed.

For immutable CVMFS inputs, `checksum_files: false` prevents a full remote read
before GENIE starts. `stage_to_local: true` copies only the selected file into
node-local temporary storage inside the logged generation process. Set
`checksum_files: true` when payload hashes are required for mutable local input.

## Canonical dk2nu flux throws

The internal flux materializer samples dk2nu beam decays once at a configured
rectangular detector window. It does not generate neutrino interactions. It
writes a generator-neutral ROOT table plus a YAML provenance and normalization
manifest for subsequent GiBUU, NuWro, and NEUT projections. It is library and
subprocess implementation code, not a separate public production command;
generator backends own its invocation when their native generation paths are
enabled.

The `fluxThrows` tree records the neutrino PDG code, energy, unit direction,
sampled position, parent and decay identifiers, dk2nu source file/entry/job
identity, and three weights. `ray_weight_per_cm2` is the dk2nu ray probability
density including the beam simulation's decay importance weight.
`flux_weight_per_cm2` additionally projects the ray onto the z-normal detector
plane and divides by `throws_per_decay`; this is the canonical analysis and
generator-sampling weight. The table retains weights rather than silently
unweighting, so generator adapters must either propagate them or perform a
recorded deterministic unweighting step.

Sampling is counter based: the window point is a function of the seed, sorted
source-file index, dk2nu entry, replica, and coordinate axis. Reruns therefore
reproduce each throw without depending on traversal state. Input and output
checksums, simulated POT, flavor totals, geometry, seed, algorithm identifier,
and summed weights are recorded in the YAML manifest.

Large catalogs are bounded with `flux.max_files`, `flux.target_pot`, or both.
Files are ranked deterministically by the catalog path and seed, only selected
files are opened, and every decay in each selected file is scanned. Per-POT
flux is normalized by the summed POT of those completely scanned files. If
`target_pot` cannot be reached before `max_files`, initialization fails rather
than silently using an undersized sample. `--max-decays` remains development
only because truncating inside a file does not provide this normalization.

Materialization is stored in a contract-addressed cache keyed by catalog path
digest, sample controls, detector window, flavor set, seed, algorithm, checksum
policy, and GiBUU energy binning. `flux.cache_dir` can select a shared cache;
otherwise sibling production directories share
`.dlpgen-opt-flux-cache` beside `production.output_dir`. Cache creation is
lock-safe. Productions hard-link cached artifacts when possible and copy them
only across filesystems.

For versioned, immutable CVMFS catalogs, `checksum_files: false` avoids payload
reads during cache lookup. With `checksum_files: true`, reuse rechecks the
payload hashes of the bounded selected subset before accepting the cache.

The cache also contains compact, checksum-recorded per-flavor GiBUU spectra.
Individual GiBUU jobs read those small templates directly; they do not reopen
the dk2nu files or rescan the canonical ROOT table. A larger
`throws-per-decay` improves Monte Carlo integration over a broad window at the
cost of a proportionally larger cached table.

## DUNE interaction-context reference

`configs/dlpgen/baseline_dune.yaml` records the current DUNE MiniProdN5p2
CC-like and NC-like particle distributions before optimization. The spatial
ranges are in millimetres and retain approximately 20 cm of padding around the
quoted DUNE detector boundaries; kinetic energies are in GeV and directions
are sampled uniformly by DLPGenerator. The earlier MPR singles block is not
part of this interaction-context profile.

DLPGenerator's root-level `InteractionSelection` setting uses
`Mode: weighted_random` with finite positive weights. Each call independently
selects exactly one named interaction block through a counter-based draw that
is reproducible from the seed. The DUNE profile uses weights `{CC: 1, NC: 1}`:
large samples approach a 50/50 mixture, while consecutive images may have the
same type. CC has a mandatory lepton and NC has none. Both blocks use
`NumEvent: [1, 1]`; DLPGenerator rejects selected blocks with any other range.
Selection has its own random stream, removing the correlation that would arise
from a lepton `NumRange: [0, 1]` inside the particle-multiplicity sampler.
The weights are stored as `SelectionWeight` inside the `CC` and `NC` blocks,
rather than in a parallel root-level mapping, so future block-specific hadron
content and its mixture probability remain one configuration unit.

dlpgen-opt retains its final guard against more than one interaction per call.
The generated images therefore study particle reconstruction under different
single-interaction contexts, not multi-vertex pileup or clustering.

## NEUT generation

The NEUT production entry point is the same standard call:

```bash
dlpgen-opt run configs/production.neut-bnb.yaml --job 0
```

`configs/neut/bnb_sbnd.yaml` and `configs/neut/bnb_icarus.yaml` select the
110 m and 600 m BNB projections. The backend translates each cached text
spectrum into a ROOT histogram and specializes the shipped argon card with the
target, process mask, flavor, event count, deterministic random seeds, and
flux-histogram name. The default `mdlqe: 2002` and `mdl2p2h: 1` reproduce the
argon card's Nieves QE model and its available tabulated 2p2h calculation.

NEUT generates one flavor at a time. Before array tasks begin, `prepare` runs
one probe event for every nonempty flux flavor and reads
`NuHepMC.FluxAveragedTotalCrossSection` from the native converter output. The
campaign normalization records

```text
rate(flavor) = canonical flux integral(flavor) * NEUT flux-averaged cross section(flavor)
```

Each job uses those rates for a seed-stable multinomial allocation whose counts
sum exactly to `generator_calls_per_job`. This preparation is automatically
invoked by local `run`/`generate` and by the singleton dependency in submitted
productions. Native ROOT event vectors, NuHepMC files, seed files, and resolved
cards are retained in compressed archives. The common NuHepMC adapter then
writes RooTracker for edep-sim; edep2supera consequently receives the same
initial-state neutrino contract as the GiBUU path.

The NEUT binaries use a private ROOT 6.34 runtime under `/opt/neut-runtime`.
Do not add it to a shell-wide `LD_LIBRARY_PATH`: the adapter does so only for
the two NEUT subprocesses, leaving the ROOT 6.32 detector stack isolated.
Finally, the currently pinned upstream image does not publish a clear NEUT
redistribution license. Technical development and validation can proceed, but
a public image release containing that runtime must wait for permission.

## GiBUU generation and NuHepMC import

The normal native-generation entry point is the standard production call:

```bash
dlpgen-opt run configs/production.gibuu-bnb.yaml --job 0
```

The `configs/gibuu/bnb_sbnd.yaml` and `configs/gibuu/bnb_icarus.yaml` source
profiles project the same BNB catalog to the nominal 110 m SBND and 600 m
ICARUS baselines respectively. The selected profile makes the call perform the
whole source path:

```text
dk2nu catalog -> canonical weighted throws -> per-flavor GiBUU flux files
              -> GiBUU flavor x CC/NC runs -> weighted event selection
              -> truth-preserving RooTracker -> edep-sim
```

GiBUU's custom external-flux interface accepts an equidistant two-column energy
histogram, not individual dk2nu rays. Cache initialization bins the canonical
`flux_weight_per_cm2` by neutrino flavor and energy once. The backend then reads
those templates and runs GiBUU once for every
present flavor and configured process (`cc`, `nc`), and combines all candidates
using deterministic weighted sampling without replacement. The selection
weight is GiBUU's native CV cross-section weight multiplied by the canonical
absolute flux integral for that flavor. All components use identical target,
ensemble, and trial settings.

This preserves the beam's energy/flavor composition but cannot preserve each
dk2nu throw's direction or detector-window position through GiBUU's
one-dimensional interface. The detector interaction vertex is imposed by the
profile's `vertex_cm`, as it is for the other source adapters. The projection
and selection policy are recorded in the source metadata. The shared candidate
cache retains reproducible native NuHepMC vectors, exact resolved jobcards, and
GiBUU logs once per immutable shard.

The profile specifies the dk2nu catalog/window, target A/Z, CC/NC processes,
energy range and binning, ensembles, runs, FSI time steps, GiBUU executable and
input tables. Production defaults use 150 time steps; setting zero bypasses the
transport evolution and is not an FSI-enabled physics configuration. GiBUU
requires at least 100 ensembles for this mode, which the configuration schema
enforces before launching a job.

Native generation uses a shared candidate cache by default. One balanced cache
shard uses the configured `ensembles` and `runs` for every flavor/process
component; independently seeded shards are appended as necessary. Automatic
sizing is based on the whole campaign,
`production.jobs * generator_calls_per_job`, rather than on one array task:

```yaml
candidate_cache:
  enabled: true
  sizing: auto
  reserve_fraction: 0.10
  max_shards: 1000
```

Candidates use their GiBUU CV weight times the absolute canonical flavor-flux
integral. Shards are added until the measured effective sample size exceeds the
campaign requirement plus the configured reserve. A frozen campaign manifest
then performs deterministic weighted sampling without replacement once and
assigns non-overlapping contiguous ranges to jobs. Job retries receive the same
interactions, independent of array execution order.

The default cache is `.dlpgen-opt-gibuu-cache` beside the production output
directory. Set `candidate_cache.directory` to place it on shared storage. Its
physics key includes the flux spectra, jobcard, target, process list, energy
binning, transport/statistics settings, executable identity, and configured
container image. For a deliberately bounded expert configuration, set
`sizing: fixed` and provide `shards`.

Run `dlpgen-opt prepare PRODUCTION.yaml` to build and freeze the allocation.
Local `generate` and `run` calls invoke preparation automatically.
`dlpgen-opt submit` creates a singleton preparation job before its dependent
production arrays.

An existing native GiBUU event vector can instead be imported:

```yaml
source:
  type: gibuu
  config: gibuu/nuhepmc-import.yaml
  input: ../inputs/gibuu/events.hepmc3
  jobcard: ../inputs/gibuu/jobcard.nml
```

`configs/gibuu/nuhepmc-import.yaml` holds the reusable GiBUU 2025 import
settings. `input` and `jobcard` remain production inputs because a native event
vector must be paired with the exact jobcard that generated it. This mode does
not claim that the imported vector came from the configured beam.

Each import job reads a non-overlapping contiguous range from the input: job
`j` skips `j * generator_calls_per_job` events. The adapter accepts the 0.9
names written by GiBUU 2025 (`ProcID` and `LabPos`) as well as their NuHepMC 1.0
counterparts, checks the mandatory metadata, and projects each interaction to
RooTracker. The incoming neutrino and target nucleus are status zero, physical
final-state particles are status one, and the canonical reaction string
preserves the struck nucleon, CC/NC current, mapped interaction mode, and native
GiBUU process ID. It normalizes GiBUU's fixed-width particle records before
using the HepMC3 reader; the native file is never modified. Nuclear-remnant
pseudoparticles (`2009900000`, or the `200990000` value emitted by GiBUU 2025)
are not sent to Geant4.

In both modes the GiBUU backend invokes the flux and NuHepMC adapters internally;
there are no additional public conversion commands in the supported workflow.
NuHepMC remains authoritative for richer native metadata. RooTracker carries
the cross section and event weight into edep-sim, while cached-candidate output
uses unit weight because weighted selection has already unweighted the sample.
The shared candidate-cache schema includes the incoming neutrino, target,
struck nucleon, reaction, and cross section; the schema-key change fences out
older final-state-only cache shards.

The RooTracker projection preserves GiBUU's native four-momenta. GiBUU may emit
outgoing hadrons with off-vacuum-shell energies; Geant4 constructs particles
from their PDG identities and three-momenta. NuHepMC remains the record to use
when the precise native off-shell state is required.

edep-sim labels these primaries `GiBUU` and copies the status-zero neutrino and
target into an `initial-state` TG4 informational vertex. edep2supera 2.1 and
newer recognize that generator-neutral contract and write one LArCV
`neutrino_mc_truth` object. An event without a recognizable initial-state
neutrino still transports normally and simply produces no neutrino-truth
object.

`checksum_input: true` is the reproducible import default. For a large immutable
file on CVMFS or another content-addressed store it may be disabled, in which
case the manifest records that the payload checksum was skipped. Ensure input
directories are visible inside Singularity, using `submit --bind` when they are
outside the profile's default `/sdf` bind.
