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
  provenance.py          checksums, timestamps, commits, host data
  validation.py          CSV, non-empty file, and ROOT checks
  sources/
    base.py              minimal source-stage interface
    dlpgen.py            DLPGenerator CSV and HEPEVT adapter
    genie.py             dk2nu/GENIE GHEP and RooTracker adapter
  genie_cli.py           flux-window config, GENIE run, and conversion
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
- GENIE tune `AR23_20i_00_000` with the reduced SBN argon spline table
