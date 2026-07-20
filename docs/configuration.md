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
