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

Full beam-production catalogs use deterministic one-file-per-job selection and
catalog-level path provenance, avoiding payload reads during initialization.
Selected immutable CVMFS files can be staged individually into node-local
scratch before GENIE starts.

## Milestone 5: optimization study layer

Only after the production contract is stable, add a study layer that creates
versioned DLPGenerator tunes and consumes standalone SPINE evaluation metrics.
It should reference immutable production manifests rather than embed training
inside this repository.

## Generator-robustness roadmap

The generator-comparison work is staged independently of the optimization
study layer:

1. Package multiple GENIE tunes by default and establish a controlled matched
   hA/hN FSI comparison. AR23, G18 hA/hN, and N24 are packaged. Add the G24
   hA/hN/INCL/Bertini stress-test family when an authoritative matching spline
   is available; add AR25 after pinning its SBN configuration overlay.
2. Add a generator-neutral NuHepMC handoff to edep-sim. The GiBUU 2025 import
   path now projects NuHepMC to RooTracker, preserving the incoming neutrino,
   target, struck nucleon, reaction mode, cross section, weight, and physical
   final state. It has been validated with native GiBUU output through
   edep-sim's TG4 initial-state record and edep2supera's LArCV
   `neutrino_mc_truth` product.
3. Add a reproducible dk2nu-to-energy/flavor flux interface shared by GiBUU,
   NuWro, and NEUT. The deterministic, weighted detector-window throw table and
   normalization manifest are implemented and validated on a complete BNB
   dk2nu file. Its GiBUU projection is implemented as per-flavor equidistant
   energy histograms and has been exercised through a native 150-time-step
   GiBUU run and edep-sim transport, including the standard `dlpgen-opt`
   generate and transport commands. The NuWro projection is also implemented
   and validated end to end. The NEUT projection now converts the compact
   spectra to ROOT histograms, caches flux-averaged cross sections by flavor,
   and deterministically allocates exact unweighted job samples. Broader
   cross-generator sampling-equivalence checks remain.
4. Integrate independent generators in physics-value order: GiBUU, NuWro, then
   NEUT subject to obtaining a reproducible supported build. GiBUU has a
   checksum-pinned installation and supports both dk2nu-driven native execution
   and pre-generated NuHepMC import passes behind `dlpgen-opt`. NuWro 25.11.1
   is checksum-pinned and supports dk2nu-driven native execution through the
   same source abstraction. The NEUT 5.8.0 backend is implemented through its
   native NuHepMC converter and has been exercised from the BNB spectrum through
   edep-sim and LArCV neutrino truth. The public image ships the adapter without
   NEUT binaries; users assemble the optional `runtime-neut` target locally
   from the digest-pinned upstream quickstart image.

Every comparison must retain the native generator output and record generator
version, configuration, input-flux provenance, event weights, conversion
metadata, and the common detector-simulation configuration. Weighted generator
outputs must be propagated or deterministically unweighted before they are
treated as an equal-probability training sample.

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
