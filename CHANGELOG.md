# Changelog

All notable changes to this project are documented in this file.

## [0.4.4] - 2026-09-16

### Fixed

- Accept zero visible SPINE truth interactions for a generated interaction
  whose particles leave no retained detector voxels, while continuing to reject
  multiple interactions per image and reporting the empty-event count.

## [0.4.3] - 2026-09-16

### Added

- Added an optional SPINE 1.2.4 conversion stage that writes validated HDF5
  directly from Supera LArCV truth products without installing PyTorch.
- Added shared SPINE dataset configuration plus generator-specific interaction
  schemes for DLPGenerator, GENIE, GiBUU, NuWro, and NEUT.
- Added a deterministic production-wide SPINE event cap for compact diagnostic
  samples, defaulting to the first 100 events in every packaged production.

### Changed

- Record both the generator-specific and shared SPINE configurations in
  production provenance and validate one truth interaction per converted event.

## [0.4.2] - 2026-09-16

### Changed

- Preserve GiBUU, NuWro, and NEUT native process IDs as LArCV neutrino
  interaction types while retaining normalized coarse interaction modes for
  cross-generator comparisons.
- Updated edep2supera to v2.1.2 for the native-process metadata contract.

### Fixed

- Avoid NEUT 5.8.0's zero-divisor failure for requests below 20 native events
  while returning exactly the requested number of converted events.
- Read NEUT NuHepMC cross-section metadata through implementations that expose
  attributes as a mapping view without a `get` method.

## [0.4.1] - 2026-09-15

### Fixed

- Updated edep2supera to v2.1.1 so neutrino truth uses Supera's event-local
  interaction IDs and attaches to the corresponding SPINE truth interaction in
  every event, rather than only the first event in a generator source file.

## [0.4.0] - 2026-09-14

### Added

- Added an experimental, checksum-pinned NEUT 5.8.0 backend with reusable SBND
  and ICARUS BNB profiles, shared dk2nu spectrum projection, cached
  flux-averaged flavor normalization, deterministic flavor allocation, native
  NuHepMC conversion, and complete production provenance.
- Added FHC and RHC LBNF dk2nu source profiles for near- and far-detector
  projections across GENIE, GiBUU, NuWro, and NEUT.
- Added the DUNE single-interaction context baseline with independent weighted
  CC/NC selection and no MPR singles.

### Changed

- Isolated NEUT and its private ROOT 6.34 runtime from the ROOT 6.32 production
  stack, allowing NEUT generation and the existing edep-sim/Supera chain to
  coexist in one image without crossing the ROOT ABI boundary.
- Updated DLPGenerator to v1.2.0, with block-local selection weights and
  seed-reproducible interaction-type draws independent of particle multiplicity.
- Split the container into a public `runtime` target without NEUT binaries and
  an opt-in `runtime-neut` target assembled locally from the pinned upstream
  quickstart image.

### Release note

- The public image includes the complete NEUT adapter but not the NEUT runtime.
  Build `runtime-neut` locally to run NEUT-backed profiles.

## [0.3.0] - 2026-09-12

### Added

- Added a checksum-pinned NuWro 25.11.1 backend with reusable SBND and ICARUS
  BNB profiles, direct sampling from the shared canonical dk2nu spectra, native
  event conversion to RooTracker, and complete production provenance.
- Added standalone ROOTEGPythia6 6.28.0 as a pinned dependency shared by NuWro
  and GENIE without downgrading the ROOT 6.32 runtime.
- Built GENIE 3.6.2 with both Pythia 6 and Pythia 8 and exposed an explicit
  `source.hadronization` selection. Pythia 8 remains the default, while the
  supplied Pythia 6 smoke profile supports controlled hadronization studies.
- Added isolated GENIE XML overlays and image self-checks for both hadronization
  backends, plus a versioned dual-backend build cache.

### Changed

- Factored the shared flavor-spectrum representation out of the GiBUU adapter
  so independent generators use the same dk2nu projection and validation.
- Updated example production configurations and documentation for the 0.3.0
  image.

### Fixed

- Added compatibility patches needed to build NuWro against ROOT 6.32.
- Ensured GENIE Pythia 6 links its standalone adapter and receives its include
  path throughout the build instead of silently reusing Pythia 8-only objects.

## [0.2.2] - 2026-09-11

### Added

- Preserved GiBUU neutrino interaction truth through the NuHepMC-to-RooTracker
  boundary and into edep-sim/Supera output.

### Changed

- Made absent neutrino metadata non-fatal so legacy and particle-bomb sources
  continue to run unchanged.

## [0.2.1] - 2026-09-11

### Changed

- Added Buildx registry and GitHub Actions caches to warm subsequent production
  image builds and verify published image manifests recursively.
- Updated the pinned edep-sim, edep2supera, and SuperaAtomic dependency metadata.

## [0.2.0] - 2026-09-11

### Added

- Added GitHub Actions CI on pull requests and `main`, covering the minimum and
  current development Python versions, package installation, dependency
  consistency, source compilation, packaged configuration loading, and the
  complete unit-test suite.
- Added an automatically sized, shared GiBUU candidate cache. Balanced native
  shards grow according to measured effective sample size and are sampled once
  per campaign without replacement or cross-job overlap; SLURM submissions
  prepare and freeze this allocation before launching production tasks.
- Installed the reduced `G18_10a_02_11b` spline table in the production image
  and exposed it for the matched hA/hN `G18_10a`/`G18_10b` FSI comparison.
- Installed the published `N24_20i_02_11b` spline table for an AR23-derived
  correlated high-momentum-tail nuclear-model comparison.
- Recorded the staged GENIE, NuHepMC, shared-flux, and independent-generator
  robustness roadmap.
- Added native GiBUU 2025 generation and native-NuHepMC import backends, plus a
  validated, provenance-preserving NuHepMC-to-edep-sim HEPEVT adapter.
- Packaged the checksum-pinned GiBUU 2025 patch-5 executable, matching input
  tables, GPL license, and exact source archive in the production image.
- Added compatibility for GiBUU's NuHepMC 0.9 metadata and fixed-width Asciiv3
  records, preserving process IDs, native positions, weights, and four-vectors.
- Added a deterministic canonical dk2nu detector-window throw materializer with
  per-throw flux weights, source identities, and a checksum-pinned YAML
  normalization manifest for GiBUU and future NuWro and NEUT projections.
- Connected canonical dk2nu throws to GiBUU's native one-dimensional flux
  interface through per-flavor energy histograms and separate CC/NC runs, then
  combined the weighted native events reproducibly while retaining every
  native vector, resolved jobcard, flux histogram, and log.
- Kept the flux and NuHepMC adapters behind the standard `dlpgen-opt` source
  abstraction instead of installing additional public executables.
- Added reusable `configs/gibuu` profiles for dk2nu-driven BNB generation and
  pre-generated native-NuHepMC import.
- Added a bounded-cache BNB/ICARUS GiBUU profile at the nominal 600 m baseline
  and a ready-to-run production configuration.
- Added deterministic whole-file/POT-bounded dk2nu sampling and a lock-safe,
  contract-addressed cache containing canonical throws and compact per-flavor
  GiBUU spectra. The default SBND profile opens at most 32 CVMFS files.

### Fixed

- Prevented empty or uninitialized dependency submodule directories from being
  misidentified as the parent repository when recording provenance.

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

[Unreleased]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.4.4...HEAD
[0.4.4]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.1.5...v0.2.0
[0.1.5]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/DeepLearnPhysics/dlpgen-opt/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/DeepLearnPhysics/dlpgen-opt/releases/tag/v0.1.0
