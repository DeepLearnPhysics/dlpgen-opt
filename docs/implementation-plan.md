# Staged implementation plan

## Milestone 1: deterministic local job (implemented)

- Python package and CLI
- strict production schema
- pinned source dependencies
- DLPGenerator source adapter
- shared edep-sim and edep2supera stages
- local job layout, seed derivation, logs, provenance, restart markers
- non-empty, CSV-structure, and ROOT integrity validation
- full common-stack Dockerfile

Acceptance was demonstrated by building the image and completing one event
through the provided liquid-argon-vat smoke configuration.

## Milestone 2: harden the physics handoff

- Resolve true multi-vertex DLPGenerator calls by extending the upstream
  edep-sim HEPEVT reader or maintaining the direct kinematics plugin.
- Validate the simple GDML detector name and Supera voxel bounds against one
  known-good reference sample.
- Add product-level LArCV checks: expected producers, event count, non-empty
  sparse tensors, and SPINE reader smoke loading.
- Publish a digest-pinned image after CI builds and smoke tests it.

## Milestone 3: production scale

- Add bounded local parallelism.
- Add per-production summary aggregation and failed-job selection.
- Add S3DF submission as a thin site adapter after local restart behavior is
  proven; keep job commands identical to local execution.
- Add image/SIF digest capture and storage-system-aware atomic finalization.

## Milestone 4: GENIE reference source (implemented)

- Build pinned GENIE and dk2nu submodules into the common production image.
- Implement `GenieBackend` producing RooTracker with argon-40 interactions and
  explicit vertex placement.
- Feed RooTracker into the same edep-sim and Supera stages.
- Record flux, spline, tune, target, and GENIE/dk2nu release checksums.

Before using the full beam-production catalog, add deterministic per-job file
selection and a catalog-level provenance artifact so initialization does not
hash 10,000 ROOT files for every job.

## Milestone 5: optimization study layer

Only after the production contract is stable, add a study layer that creates
versioned DLPGenerator tunes and consumes standalone SPINE evaluation metrics.
It should reference immutable production manifests rather than embed training
inside this repository.

## Unresolved environment-specific details

- Final S3DF container runtime, bind roots, queues, accounting, and storage
  conventions.
- The detector geometry and voxelization to use for the actual BNB comparison;
  the included LAr vat is only a software smoke target.
- The authoritative SPINE-readable producer list and minimum content checks.
- Whether true multi-interaction calls represent pileup that must stay in one
  Geant event.
- The authoritative transverse SBND/ICARUS beam-frame flux-window centers;
  the baseline profiles currently use nominal longitudinal distances of 110 m
  for SBND and 600 m for ICARUS.
- Registry/release location and immutable digest for the production image.
