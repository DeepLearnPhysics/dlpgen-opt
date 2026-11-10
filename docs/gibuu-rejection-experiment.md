# Experimental rejection before transport

This experiment is explicitly opt-in and isolated from production. It does not
change the GiBUU source configuration or write candidate-cache entries.

Run `tests/integration/gibuu_rejection.py` inside the pinned GiBUU container:

```sh
python3 tests/integration/gibuu_rejection.py \
  --mode weighted --seed 104741 --runs 2 \
  --flux-file /absolute/path/flux-14.dat --output-dir /new/path/pilot

python3 tests/integration/gibuu_rejection.py \
  --mode rejection --bound 1500 --seed 204741 --runs 4 \
  --flux-file /absolute/path/flux-14.dat --output-dir /new/path/rejection
```

The number 1500 is an experimental bound for this particular local pilot, **not
a recommended production setting**. An empirical maximum does not establish a
global bound. Output includes native logs, resolved jobcard, raw NuHepMC events,
and summary statistics. Bound overflow, a failed native process, or an incomplete
event file prevents publishing `result.json`. Do not silently increase the bound
and keep events from an earlier run.

## Correctness and statistical efficiency

For nonnegative proposal weights `w`, rejection with probability `w/M` and a
valid bound `M >= w` preserves weighted expectation values when accepted events
carry weight `M` with the original exposure normalization. It adds sampling
noise. It is not a deliberate approximation of interaction or transport physics.

For a fixed set of proposals, expected accepted count is `sum(w)/M`, which is no
greater than weighted ESS `sum(w)^2/sum(w^2)`. A speed benefit therefore requires
enough transport savings to compensate for this loss. ESS per second is the
screening metric here; ESS alone does not establish accuracy for every observable.

The exact release-2025 native implementation in
`code/init/neutrino/initNeutrino.f90` uses `equalWeights_Mode=1` to record increasing
absolute maxima to `fort.87` without changing sampling, and mode 2 for rejection
before transport. Mode 2 checks positive bound violations but rejects negative
`totalWeight`. It is not a general signed-weight sampler. The current production
adapter also discards nonpositive native candidates; this is an existing
limitation, not evidence that negative contributions are physically irrelevant.
The [GiBUU event-library paper, section III](https://arxiv.org/html/2311.14286v3)
describes preserving positive and negative samples separately and subtracting
their predictions. A production implementation would need an explicit policy
and validation for signed weights as well as valid rejection bounds.

No universal percentage error can be assigned from this pilot. Missing negative
contributions can affect different observables differently, and an underestimated
bound can bias the high-weight tail. Passing a small comparison is not proof of
distributional equivalence.

## Local screening results

The experiment uses the existing local BNB/SBND flux, muon-neutrino CC on argon-40,
100 ensembles per run, and all 150 transport steps. The container is
`dlpgen-opt:0.4.4`, linux/amd64 on an ARM host. Runs share the host with other work;
wall times are indicative, not controlled throughput measurements.

The pilot maximum was 701.958; the trial bound was 1500. The intentionally invalid
bound 0.001 triggered the expected native failure and was rejected by the runner.

| Mode | Seed | Runs | Native events | ESS | Seconds | ESS/second |
|---|---:|---:|---:|---:|---:|---:|
| Weighted pilot | 104741 | 2 | 2,845 | 52.5 | 103 | 0.510 |
| Weighted reference | 204741 | 4 | 5,638 | 128.2 | 191 | 0.671 |
| Rejection, M=1500 | 204741 | 4 | 14 | 14 | 102 | 0.137 |
| Rejection, M=1500 | 304741 | 2 | 5 | 5 | 57.2 | 0.087 |

Rejection achieved approximately **4.9 times lower ESS/second** than the reference
at the same proposal exposure and initial seed. Rejection consumes additional
random numbers, so the two streams diverge; these are not identical proposals.
No negative native events occurred in these samples. That observation does not
validate signed-weight handling elsewhere.

There are only 19 accepted rejection events across both trials, far too few to
establish distributional agreement. For example, the matched reference and
rejection means of neutrino energy were 1.067 and 1.771 GeV; neither equivalence
nor a quantitative bias claim is warranted from such a small rejection sample.

**Decision:** do not promote this global-bound rejection scheme to production.
It reduces elapsed time per fixed proposal exposure but loses more statistical
information than the time saved. Future work could study better proposal
distributions or stratified bounds, with independent normalization, signed-weight,
and final-state validation. This result does not establish their performance.

The [machine-readable results](benchmarks/gibuu-rejection-2026-11-08.json) record
the flux checksum, seeds, settings, maxima, signed-weight audit, and timing source.
The first three native runs completed successfully but initially hit a summary
parser error on the NuHepMC `W CV` header. Their preserved native files were
reprocessed after fixing the parser, using native START/STOP timestamps. The
fourth run completed end to end with the corrected runner. Four unit tests cover
bound overflow, failed native execution, truncated output, and signed-weight
auditing before the production candidate filter.
