# GiBUU candidate allocation

## Evidence and scope

The reported BNB/SBND campaign produced 241,626 raw candidates in 21 shards but
only 1,470.9 effective events against a target near 137,500. Muon-neutrino CC/NC
carried 98.43% of total weight while accounting for 28.66% of candidates. Within
muon-neutrino CC, 30,181 candidates had ESS 801.6. These are two separate costs:
unbalanced component allocation and broad within-component weights.

The implementation improves the former and amortizes native startup. It leaves
GiBUU's transport and interaction settings intact. Native negative-weight handling
is unchanged: the existing adapter only selects positive-weight candidates. This
work does not establish validity for samples requiring signed native events.

## Primary sources inspected

- [GiBUU 2025 source archive](https://gibuu.hepforge.org/downloads?f=release2025.tar.gz),
  SHA-256 `bed77e069e657254a2e474d304722f568e57c3b4591559c5d132680c83fa3eed`,
  matching the Dockerfile pin.
  `code/init/neutrino/initNeutrino.f90` documents `equalWeights_Mode` and assigns
  `finalState%perweight=totalWeight/float(numtry)`. The normalization is by
  target size and ensembles, not by `num_runs_SameEnergy`.
  `code/inputOutput/EventOutput.f90`, NuHepMC `write_event_header`, writes that
  event weight as the `W` record. Its later cross-section estimator is separate.
- [Aliaga et al., arXiv:2311.14286v3](https://arxiv.org/html/2311.14286v3), section III,
  describes library-based conversion of weighted GiBUU events and separate
  handling of positive and negative native weights. It does not justify treating
  every stored weighted event as an independent unweighted event.

GiBUU's `equalWeights_Mode=2` performs rejection before transport, which may be
valuable for broad weights. It requires an explicit `equalWeights_Max`, and its
implementation stops when a positive weight exceeds that bound. We do not enable
it using an unvalidated pilot maximum. Establishing component-specific bounds and
handling signed native events requires a separate native-generator study.

## Exposure correction and scheduling

For component c, let B_c be the number of base batches, S_c the sum of raw weights
(native weight times flavor-flux integral), and Q_c their squared sum. A base
batch uses the configured ensembles and run count. Every attempted batch counts,
including batches producing no stored positive-weight candidates.

- Component rate estimate: R_c = S_c / B_c.
- Component ESS: E_c = S_c^2 / Q_c.
- Estimated share: p_c = R_c / sum(R).
- Global ESS: (sum(R))^2 / sum(Q_c / B_c^2).
- Candidate selection weight: native_weight * flux_integral / B_c.

The native weight is retained; the divisor is serialized as `sampling_batches`.
Without this correction, generating more batches of one component would change
its inferred cross section merely because more computing time was assigned to it.

After the initial full-component pilot (or compatible legacy-cache import), the
scheduler targets E_c >= T*p_c. These inequalities are sufficient for global
ESS >= T because sum(p_c^2/E_c) <= 1/T. Missing ESS divided by observed ESS per
batch estimates the additional batch count, bounded to 1–64 per wave, split into tasks of at most two base batches. Rates and
shares are estimates, not fixed physics inputs; they are updated after each
shard, and the global ESS check remains authoritative. Zero-yield components
are retried while preparation remains below target. This is an adaptive Monte
Carlo allocation heuristic, not a proof of unbiased finite-sample inference.

Fixed sizing retains full-component shards. Uniform allocation remains available
for controlled comparisons. Deterministic weighted selection without replacement
and non-overlapping job allocations are preserved. ESS itself does not prove that
without-replacement depletion is negligible; this change does not relax the old
criterion or claim to solve that separate statistical limitation.

## Validation

Unit tests cover exposure correction under deliberately unequal batch counts,
zero-yield exposure, serialization, selection deficits, compatible-cache reuse,
resume, and retained failure logs. A deterministic four-component synthetic
benchmark checks that adaptive allocation reaches the same ESS with fewer than
half the base batches while preserving estimated component shares. That is a
scheduler test, not a measured GiBUU or S3DF wall-time speedup.

Cache statistics are computed with a streaming pass and updated incrementally
for new shards. Final selection uses a bounded heap with the same deterministic
exponential-race scores as the old full sort. Memory for candidate payloads scales
with requested campaign size, not total pool size; per-job conversion materializes
only that job's slice. A regression test checks identical selection and order
against the original full-sort algorithm.

A native check on 2026-11-07 used the existing `dlpgen-opt:0.4.4` Linux/amd64
image locally (on an ARM host), the SBND jobcard, 100 ensembles, and all 150
transport steps. With the same initial seed, one nu_mu CC run produced 1,424
positive candidates in 40.7 s; two batched runs produced 2,793 in 68.1 s. Every
first-run stored weight matched exactly between the two invocations. Raw weight
sums were 0.92936 and 1.83307, respectively. This confirms that batch exposure
must be corrected separately and demonstrates startup amortization in this
small test; it is not an S3DF production timing claim. A reproducible manual
check is provided in `tests/integration/gibuu_batching.py`.

Using the user's measured component shares and ESS per base batch, and assuming
those remain constant, the component targets imply approximately 3,259 total
base batches versus 15,712 for uniform allocation (about 4.8 times less work).
That includes the 21 existing batches per component. Different component runtimes,
weight fluctuations, and selection requirements prevent translating that directly
into a guaranteed wall-time speedup. The native dominant CC weight distribution
still limits ultimate throughput.

A synthetic 100,000-candidate / 1,000-selection memory benchmark produced exactly
identical selected IDs and ordering. Python-tracked peak allocation fell from
48.43 MiB for a full sort to 0.57 MiB for the streamed heap (1.66 s versus 1.50 s
locally). These are synthetic Python allocation measurements, not whole-process
RSS or predictions for a full native production.

## Parallel preparation

The parallel task layout is versioned in the cache key. Each component wave is
split into fixed chunks of up to two base batches, independent of worker count.
The seed slot is `component_index * 32 + chunk_index` inside the existing shard
seed mapping; canonical PDG/process order makes task identities stable.
Up to 64 base batches per component allow 32 workers to contribute even when
only a single component needs more events. More workers cannot accelerate a
wave that has fewer tasks than workers.

One coordinator owns the cache and publishes the combined shard; spawned worker
processes do not acquire that cache lock. Workers run GiBUU in separate directories
and checkpoint their candidates after success. The coordinator streams results
in canonical task order, independent of completion order. The ownership lock
prevents competing coordinators from corrupting the same cache, while native
work inside the owning job executes concurrently. Distributed multi-node worker
claims are not implemented.

Each interrupted wave retains its plan and completed-task checksums. Resume first
finishes that plan, then recomputes ESS for the current campaign target. Stable
workspace flux paths allow reuse across production directories. A completed task
is validated before reuse; invalid checksums fail rather than silently regenerate.
An interrupted aggregation can be rebuilt entirely from task checkpoints.

`tests/integration/gibuu_scaling.py` runs the same eight full-transport pilot tasks
at 1, 2, 4, and 8 workers and requires identical serialized candidate checksums.
This measures parallelism at fixed work, separately from adaptive allocation's
reduction in total work. The benchmark does not multiply the two gains into a
claimed production speedup.

### Local native scaling results (2026-11-07)

The local Docker VM exposed 12 logical CPUs and about 16 GiB RAM, with no CPU
quota. The existing Linux/amd64 `dlpgen-opt:0.4.4` image ran on an ARM64 host
with the updated Python sources mounted in. Each run used the same eight
BNB/SBND flavor/process tasks, 100 ensembles, one native run per task, and all
150 transport steps. These are single fixed-work trials, including conversion
and archive publication; they are not S3DF measurements.

| Workers | Elapsed seconds | Speedup | Aggregate child CPU seconds |
| --- | ---: | ---: | ---: |
| 1 | 337.8 | 1.00x | 326.9 |
| 2 | 187.8 | 1.80x | 340.5 |
| 4 | 101.6 | 3.33x | 349.9 |
| 8 | 60.2 | 5.62x | 394.3 |

All runs produced 11,231 candidates with identical serialized-candidate SHA-256
`2674cfb8194e773d04ad83c1a06ed0e87293619f1e4d89290b9357c87caf8cb9`.
The gain is sublinear and aggregate CPU cost rises under concurrency. Task-length
imbalance, serial conversion/aggregation work, and the virtualized environment
limit scaling; these measurements do not establish their separate contributions.

[Machine-readable results](benchmarks/gibuu-parallel-2026-11-07.json) retain the
exact timings, CPU totals, checksums, and benchmark inputs. Larger preparation
allocations (for example 16 or 32 workers) are supported when sufficient tasks,
CPUs, and memory are available, but were not benchmarked on this 12-vCPU VM.

A separate native recovery check injected an interruption after the nu_mu CC task
completed, then resumed with two workers and a changed requested wave size. The
coordinator honored the saved plan, reused CC without rerunning it, generated only
the missing NC task, and produced 3,344 candidates byte-identical to the matching
subset of the serial baseline. This also exercised the stable workspace flux
copies and aggregation of checkpointed task outputs.
