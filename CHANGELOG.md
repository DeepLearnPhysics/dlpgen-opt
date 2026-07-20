# Changelog

All notable changes to this project are documented in this file.

## [0.1.0] - 2026-07-20

First production release of the DLPGenerator phase-space optimization workflow.

### Added

- Reproducible, resumable production orchestration from particle generation
  through edep-sim and edep2supera/SuperaAtomic rasterization.
- DLPGenerator particle-bomb and GENIE/dk2nu beam-flux source backends, with
  deterministic per-job seeds and provenance capture.
- A generic centered 4 m liquid-argon vat and matching 800 x 800 x 800 Supera
  bounding box, with fixed central interaction vertices by default.
- A common `linux/amd64` production image containing ROOT/LArCV2, Geant4,
  Pythia8, GENIE, dk2nu, edep-sim, DLPGenerator, SuperaAtomic, and edep2supera.
- S3DF SLURM submission through zero-based job arrays, including Milano and
  Roma CPU profiles, concurrency limits, array chunking, dependency chaining,
  Singularity execution, and serialized shared-production initialization.
- Release-triggered GitHub Actions image publishing to GHCR with Buildx cache,
  provenance, and SBOM generation.
- Unit tests for configuration, source backends, pipeline behavior, validation,
  and SLURM script generation/submission.

### Fixed

- Corrected edep2supera particle first/last-step propagation through the pinned
  edep2supera 2.0.1 patch release.
- Added the GENIE include path required to prevent ROOT/Cling autoload warnings
  for GENIE framework types.
- Avoided unstable PyROOT teardown after Supera output finalization while still
  independently validating the resulting LArCV file.

[0.1.0]: https://github.com/DeepLearnPhysics/dlpgen-opt/releases/tag/v0.1.0
