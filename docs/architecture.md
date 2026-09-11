# Minimal architecture

The orchestration layer is deliberately small Python. It owns job identity,
paths, seeds, commands, provenance, restart behavior, and validation; it does
not own event generation, detector simulation, labeling, or SPINE training.

```text
ProductionConfig
      |
      v
SourceBackend ------------------------- GenieBackend
      |                                     |
      +-------- primary event file ------+
                       |
                       v
                 shared edep-sim
                       |
                       v
             shared edep2supera/Supera
                       |
                       v
                 SPINE-ready LArCV
```

## Repository tree

```text
configs/                 production, source-tune, geometry, and Supera examples
dependencies/            pinned upstream Git submodules
docker/                  runtime entrypoint
docs/                    contracts and staged plan
src/dlpgen_opt/
  cli.py                 command-line boundary
  config.py              strict schema and path resolution
  pipeline.py            shared stage orchestration
  runner.py              subprocess capture and atomic status records
  supera_cli.py          finalized-output frontend avoiding PyROOT teardown
  flux_cli.py            internal dk2nu detector-window throw materializer
  provenance.py          checksums, timestamps, commits, host data
  validation.py          CSV, non-empty file, and ROOT checks
  sources/
    base.py              minimal source-stage interface
    dlpgen.py            DLPGenerator CSV and HEPEVT adapter
    genie.py             dk2nu/GENIE GHEP and RooTracker adapter
    gibuu.py             native GiBUU generation and NuHepMC import backend
  genie_cli.py           flux-window config, GENIE run, and conversion
  gibuu_cli.py           dk2nu-flux projection, native runs, and event mixture
  nuhepmc_cli.py         internal NuHepMC to edep-sim HEPEVT adapter
tests/                   stack-independent orchestration tests
Dockerfile               complete common production runtime
```

## Source-stage interface

`SourceBackend` has only three responsibilities:

1. Construct the inspectable source command for one job.
2. Finalize and validate its output into a downstream-compatible event file.
3. Return that event-file path.

The DLPGenerator backend emits CSV first because it preserves call and
interaction identifiers. It then writes edep-sim's explicit `pbomb` HEPEVT
format, converting DLPGenerator millimetres to the centimetres required by the
edep-sim header. This adapter is orchestration-owned glue, not generator logic.

The GENIE backend uses `gevgen_fnal` with `GDk2NuFlux` and a point argon-40
target, converts the resulting GHEP record to RooTracker with `gntpc`, validates
both ROOT trees, and supplies the matching edep-sim macro. The flux window is
configured in beam coordinates, while edep-sim fixes the interaction at the
configured generic-vat vertex. GENIE and the downstream stack live in one
image so the RooTracker dictionaries and ABI are tested together.

The canonical flux boundary samples detector-window points directly from dk2nu
with `calcEnuWgt` and writes generator-neutral energy, flavor, direction,
position, source identity, and flux weights. Counter-based sampling makes every
throw stable under reruns and independent of processing order. The ROOT throw
table and its YAML provenance/normalization manifest can then be projected into
each generator's native flux interface without asking each generator to parse
dk2nu.

For large catalogs, a seed-keyed hash ranking selects a bounded number of whole
files or enough whole files to meet a requested POT. Listing the catalog is
O(number of paths), but ROOT opens and decay traversal are O(selected files).
The resulting throw table and compact generator projections live in a
lock-protected, contract-addressed cache shared by sibling productions.

The GiBUU cache projection converts the weighted canonical table once into
GiBUU's supported one-dimensional external-flux representation: an equidistant
energy histogram for each beam flavor. Jobs consume these compact templates
without reading the throw table. GiBUU runs every present flavor separately
for configured CC and NC processes, using the same target and trial settings. GiBUU samples the
normalized energy shape and supplies each event's cross-section weight; the
canonical integral for that flavor restores the relative beam composition.
Deterministic weighted sampling without replacement combines the components to
the requested event count. Direction and detector-window position cannot be
passed through GiBUU's external-flux interface and are explicitly recorded as a
projection loss.

Every native NuHepMC vector, energy histogram, resolved jobcard, and log is kept
in a reproducible archive. The selected physical final states are converted to
edep-sim's `pbomb` HEPEVT input. This is a lossy transport projection: NuHepMC
remains the authoritative physics record, while only status-1 physical particles
are handed to Geant4. A separate import mode consumes an existing GiBUU 2025
NuHepMC vector in non-overlapping job-indexed ranges. The image contains the
checksum-pinned GiBUU executable, matching input tables, official SBND template,
source archive, and license.

The adapter modules retain command-line `main` functions so source stages can
run them in isolated, logged subprocesses. They are not installed as public
executables. `dlpgen-opt` selects and invokes them through `SourceBackend`.

## Dependency policy

The six study-specific C++ projects are Git submodules so a repository commit
fixes their exact commits. SuperaAtomic's nested pybind11 submodule is included
recursively. LArCV2 is the pinned Docker base because it is the output I/O
runtime; Geant4 and Pythia8 are built at explicit versions in the image. The
production manifest records all study-specific commits and the configured image
reference.

Release pins currently selected:

- DLPGenerator `v1.1.2` (`7b13a2a...`)
- edep-sim `4548701...` (upstream commit tested with modern Geant4)
- SuperaAtomic `v1.9.2` (`4264083...`)
- edep2supera `v2.0.3` (`aabb3c7...`)
- LArCV2 image `2.4.1-ubuntu22.04`
- Geant4 `11.4.2`
- GENIE `R-3_06_02` (`4a6d9e5...`), Pythia8-only, with the shared decay/DIS/
  charm defaults mapped to GENIE's corresponding Pythia8 implementations
- dk2nu `v01_11_00` (`5b1d8c2...`)
- Pythia `8.317`
- GiBUU Release 2025 patch 5, with its matching `buuinput2025` tables
- GENIE tunes `AR23_20i_00_000`, the matched hA/hN pair
  `G18_10a_02_11b`/`G18_10b_02_11b`, and the AR23-derived correlated-tail
  variant `N24_20i_02_11b`, with published argon spline tables
