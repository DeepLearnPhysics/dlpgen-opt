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
  expected_commit: 7b13a2a88d4f7a214ac84b53a66640392a50aec7
software:
  container_image: registry.example/dlpgen-opt@sha256:<digest>
  edep_sim:
    executable: edep-sim
    expected_commit: 4548701be5bd82daae65c9f1e51f63b1886b71d9
  edep2supera:
    executable: dlpgen-opt-supera
    expected_commit: 55484e2577df96f80f8f719818145645787c71e1
  supera_atomic:
    expected_commit: 799b2bb84d2e27aa3a2e5d90869fa453f86c68b8
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

SLURM resource profiles are intentionally separate from this physics schema in
`configs/slurm/s3df.yaml`. This lets the same immutable production YAML run
locally, on Milano, or on Roma without changing its recorded configuration.

## GENIE source

Select the alternative source with `source.type: genie`:

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
the study's generic LAr vat.

The maximum energy is a lower bound used while dk2nu scans for its maximum
energy and ray weight; it should safely cover the selected beam. The example
allows electron and muon neutrinos and antineutrinos. The generated stage
records the matched flux-file checksums, cross-section spline checksum, GENIE
tune, and target isotope in the production manifest.
