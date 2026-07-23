# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

## [0.1.5] - 2026-07-22

### Changed

- Pointed the default SBND and ICARUS BNB profiles at the immutable CVMFS flux
  catalog used for production.
- Configured Supera driver verbosity through the supported nested
  `SuperaDriver` configuration block.

### Fixed

- Updated to SuperaAtomic v1.9.2, which initializes logger thresholds
  deterministically and applies the configured driver log level before any
  driver messages are emitted.

## [0.1.4] - 2026-07-22

### Fixed

- Preserved dk2nu metadata in GENIE GHEP output without relying on ROOT's
  ambient current directory.
- Translated per-event dk2nu beam-parent ancestry into the legacy RooTracker
  branches expected by edep-sim, including explicit length and time units.
- Updated to edep2supera v2.0.3, which removes duplicate SuperaAtomic ROOT
  dictionary declarations and correctly preserves Geant4 ancestor track IDs
  instead of serializing internal particle-array indices.

## [0.1.3] - 2026-07-22

### Added

- Added deterministic one-file-per-job selection from large GENIE flux
  catalogs using a seeded round-robin mapping.
- Added optional logged staging of each selected flux file from CVMFS into
  node-local temporary storage before GENIE opens it.

### Changed

- Flux-catalog provenance now records a digest of sorted immutable paths and
  the selected per-job input without reading every ROOT payload.
- Added an explicit input-checksum policy so immutable CVMFS flux files can
  skip redundant full-file hashing while ordinary inputs remain checksummed.
- Completed GENIE initialization manifests are reused by later array tasks,
  avoiding repeated catalog enumeration.

## [0.1.2] - 2026-07-21

### Added

- Added reusable referenced GENIE source configurations, including nominal BNB
  profiles for SBND at 110 m and ICARUS at 600 m.
- Added reproducible development builds from clean or dirty external
  DLPGenerator checkouts, with content fingerprints, locked build caching, and
  provenance capture.

### Changed

- GENIE production configurations can now select a source profile through
  `source.config`, while legacy inline settings remain supported.
- GENIE source configuration files are included in stage inputs and production
  manifests by checksum.

## [0.1.1] - 2026-07-21

### Added

- Added a standalone PyYAML-only S3DF launcher so login nodes can submit
  Apptainer/Singularity arrays without installing this project or its physics
  dependencies.
- Added SBN and DUNE production profiles with matching detector geometry and
  Supera rasterization configurations.
- Added a standalone S3DF merge launcher for combining completed production
  jobs into deterministic train/test files.

### Changed

- Standardized version naming: Git/GitHub releases use `vX.Y.Z`, while GHCR
  images use the corresponding `X.Y.Z` tag without the leading `v`.
- Made the standalone launchers compatible with the legacy PyYAML available on
  restricted S3DF login nodes.

### Fixed

- Corrected global job indices when productions are split across multiple
  scheduler arrays.
- Isolated Apptainer jobs from host Python environment variables.

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

[Unreleased]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/DeepLearnPhysics/dlpgen-opt/releases/tag/v0.1.0
