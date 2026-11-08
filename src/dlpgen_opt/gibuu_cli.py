from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import heapq
import json
import math
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path
from typing import Iterable, Iterator

from .flux_spectra import cached_flux_histograms
from .nuhepmc_cli import (
    GIBUU_NUCLEAR_REMNANT_PDG,
    NUCLEAR_REMNANT_PDG,
    _event_metadata,
    _momentum_scale,
    _selected_hepmc_input,
)
from .nuhepmc_rootracker import (
    ParticleRecord,
    RooTrackerEvent,
    project_event,
    write_rootracker,
)
from .provenance import checksum, read_yaml, write_yaml
from .progress import progress
from .validation import validate_nonempty


FLAVOR_ID = {12: 1, -12: 1, 14: 2, -14: 2, 16: 3, -16: 3}
PROCESS_ID = {"cc": 2, "nc": 3}


@dataclass(frozen=True)
class Candidate:
    component: str
    source_event: int
    process_id: int
    native_weight: float
    flux_integral: float
    score: float
    particles: tuple[tuple[int, float, float, float, float, float], ...]
    cache_id: str = ""
    incoming_neutrino: tuple[int, float, float, float, float] | None = None
    target_pdg: int = -1
    nucleon_pdg: int = -1
    reaction: str = ""
    cross_section_1e38_cm2: float = 0.0
    sampling_batches: int = 1

    @property
    def global_weight(self) -> float:
        return self.native_weight * self.flux_integral / self.sampling_batches


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="python -m dlpgen_opt.gibuu_cli",
        description="Run GiBUU from canonical dk2nu throws and write RooTracker.",
    )
    command.add_argument("--flux-manifest", type=Path, required=True)
    command.add_argument("--flux-spectra-manifest", type=Path, required=True)
    command.add_argument("--jobcard", type=Path, required=True)
    command.add_argument("--work-dir", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--metadata-output", type=Path, required=True)
    command.add_argument("--native-archive", type=Path, required=True)
    command.add_argument("--resolved-jobcards", type=Path, required=True)
    command.add_argument("--events", type=int, required=True)
    command.add_argument("--seed", type=int, required=True)
    command.add_argument("--executable", default="GiBUU.x")
    command.add_argument("--input-tables", type=Path, required=True)
    command.add_argument("--target-a", type=int, required=True)
    command.add_argument("--target-z", type=int, required=True)
    command.add_argument("--energy-min-gev", type=float, required=True)
    command.add_argument("--energy-max-gev", type=float, required=True)
    command.add_argument("--energy-bins", type=int, required=True)
    command.add_argument("--ensembles", type=int, required=True)
    command.add_argument("--runs", type=int, required=True)
    command.add_argument("--time-steps", type=int, required=True)
    command.add_argument("--processes", nargs="+", choices=("cc", "nc"), required=True)
    command.add_argument("--vertex-cm", type=float, nargs=3, required=True)
    command.add_argument("--candidate-cache-dir", type=Path)
    command.add_argument("--campaign-dir", type=Path)
    command.add_argument("--total-events", type=int)
    command.add_argument("--job-index", type=int)
    command.add_argument("--cache-sizing", choices=("auto", "fixed"), default="auto")
    command.add_argument("--cache-allocation", choices=("adaptive", "uniform"), default="adaptive")
    command.add_argument("--workers", type=int, default=1)
    command.add_argument("--cache-shards", type=int)
    command.add_argument("--reserve-fraction", type=float, default=0.10)
    command.add_argument("--max-cache-shards", type=int, default=1000)
    command.add_argument("--software-identity", default="unknown")
    command.add_argument("--prepare-only", action="store_true")
    return command


def _format_namelist(value: object) -> str:
    if isinstance(value, bool):
        return ".true." if value else ".false."
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def _set_namelist(text: str, section: str, key: str, value: object) -> str:
    lines = text.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(rf"^\s*&{re.escape(section)}\b", line, re.IGNORECASE)
        ),
        None,
    )
    if start is None:
        raise RuntimeError(f"GiBUU jobcard has no &{section} namelist")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.match(r"^\s*/\s*(?:!.*)?$", lines[index])
        ),
        None,
    )
    if end is None:
        raise RuntimeError(f"GiBUU jobcard has no end for &{section}")
    replacement = f"      {key} = {_format_namelist(value)}"
    assignment = re.compile(rf"^\s*{re.escape(key)}\s*=", re.IGNORECASE)
    for index in range(start + 1, end):
        if assignment.match(lines[index]):
            lines[index] = replacement
            return "\n".join(lines) + "\n"
    lines.insert(end, replacement)
    return "\n".join(lines) + "\n"


def resolved_jobcard(
    template: str,
    *,
    pdg: int,
    process: str,
    flux_file: Path,
    input_tables: Path,
    target_a: int,
    target_z: int,
    ensembles: int,
    runs: int,
    time_steps: int,
    seed: int,
) -> str:
    values = (
        (
            "neutrino_induced",
            "process_ID",
            PROCESS_ID[process] if pdg > 0 else -PROCESS_ID[process],
        ),
        ("neutrino_induced", "flavor_ID", FLAVOR_ID[pdg]),
        ("neutrino_induced", "nuXsectionMode", 16),
        ("neutrino_induced", "nuExp", 99),
        ("neutrino_induced", "FileNameFlux", flux_file),
        ("target", "A", target_a),
        ("target", "Z", target_z),
        ("input", "numEnsembles", ensembles),
        ("input", "num_runs_SameEnergy", runs),
        ("input", "num_Energies", 1),
        ("input", "numTimeSteps", time_steps),
        ("input", "path_to_input", input_tables),
        ("input", "version", 2025),
        ("initRandom", "SEED", seed),
        ("EventOutput", "WritePerturbativeParticles", True),
        ("EventOutput", "WriteRealParticles", False),
        ("EventOutput", "EventFormat", 7),
    )
    result = template
    for section, key, value in values:
        result = _set_namelist(result, section, key, value)
    return result


def _selection_uniform(seed: int, component: str, event: int) -> float:
    digest = hashlib.sha256(f"{seed}:{component}:{event}".encode("ascii")).digest()
    return (int.from_bytes(digest[:8], "big") + 1) / (2**64 + 1)


def _read_candidates(
    path: Path,
    *,
    component: str,
    flux_integral: float,
    seed: int,
    cache_prefix: str = "",
) -> list[Candidate]:
    try:
        import pyhepmc
    except ImportError as error:
        raise RuntimeError("pyhepmc is required to read GiBUU output") from error
    candidates: list[Candidate] = []
    with _selected_hepmc_input(
        path, path.parent, skip=0, events=2_147_483_647
    ) as (readable, _):
        with pyhepmc.open(str(readable)) as source:
            for event_index, event in enumerate(source):
                process_id, _ = _event_metadata(event)
                if not event.weights:
                    raise RuntimeError("GiBUU NuHepMC event has no CV weight")
                native_weight = float(event.weights[0])
                global_weight = native_weight * flux_integral
                if not math.isfinite(global_weight) or global_weight <= 0:
                    continue
                scale = _momentum_scale(event, pyhepmc)
                projection = project_event(
                    event,
                    process_id=process_id,
                    momentum_scale=scale,
                    vertex_cm=(0.0, 0.0, 0.0),
                    output_event_number=event_index,
                )
                particles = []
                for particle in event.particles:
                    if particle.status != 1 or particle.pid in (
                        NUCLEAR_REMNANT_PDG,
                        GIBUU_NUCLEAR_REMNANT_PDG,
                    ):
                        continue
                    momentum = particle.momentum
                    mass = (
                        particle.generated_mass
                        if particle.is_generated_mass_set()
                        else momentum.m()
                    )
                    particles.append(
                        (
                            int(particle.pid),
                            momentum.px * scale,
                            momentum.py * scale,
                            momentum.pz * scale,
                            momentum.e * scale,
                            abs(mass) * scale,
                        )
                    )
                if not particles:
                    continue
                uniform = _selection_uniform(seed, component, event_index)
                candidates.append(
                    Candidate(
                        component=component,
                        source_event=int(event.event_number),
                        process_id=process_id,
                        native_weight=native_weight,
                        flux_integral=flux_integral,
                        score=-math.log(uniform) / global_weight,
                        particles=tuple(particles),
                        cache_id=(
                            f"{cache_prefix}/{component}/{event_index}"
                            if cache_prefix
                            else f"{component}/{event_index}"
                        ),
                        incoming_neutrino=next(
                            (
                                (particle.pdg, *particle.momentum_gev)
                                for particle in projection.particles
                                if particle.status == 0
                                and abs(particle.pdg) in FLAVOR_ID
                            ),
                            None,
                        ),
                        target_pdg=next(
                            (
                                particle.pdg
                                for particle in projection.particles
                                if particle.status == 0
                                and abs(particle.pdg) >= 1_000_000_000
                            ),
                            -1,
                        ),
                        nucleon_pdg=projection.nucleon_pdg,
                        reaction=projection.reaction,
                        cross_section_1e38_cm2=projection.cross_section_1e38_cm2,
                    )
                )
    return candidates


def _write_rootracker_candidates(
    selected: list[Candidate], output: Path, vertex_cm: tuple[float, float, float]
) -> None:
    x, y, z = vertex_cm
    events = []
    for index, candidate in enumerate(selected):
        particles = []
        if candidate.incoming_neutrino is not None:
            pdg, px, py, pz, energy = candidate.incoming_neutrino
            particles.append(ParticleRecord(pdg, 0, (px, py, pz, energy)))
        if candidate.target_pdg >= 1_000_000_000:
            particles.append(
                ParticleRecord(candidate.target_pdg, 0, (0.0, 0.0, 0.0, 0.0))
            )
        particles.extend(
            ParticleRecord(pdg, 1, (px, py, pz, energy))
            for pdg, px, py, pz, energy, _ in candidate.particles
        )
        events.append(
            RooTrackerEvent(
                event_number=index,
                process_id=candidate.process_id,
                reaction=candidate.reaction,
                vertex_m=(x / 100.0, y / 100.0, z / 100.0, 0.0),
                particles=tuple(particles),
                nucleon_pdg=candidate.nucleon_pdg,
                cross_section_1e38_cm2=candidate.cross_section_1e38_cm2,
                # Candidate selection already accounts for its native weight.
                weight=1.0,
                probability=1.0,
            )
        )
    write_rootracker(events, output)


def _write_reproducible_archive(source: Path, output: Path) -> None:
    """Archive native GiBUU products without host- or run-time timestamps."""
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("wb") as raw, gzip.GzipFile(
        filename="", mode="wb", fileobj=raw, mtime=0
    ) as compressed, tarfile.open(fileobj=compressed, mode="w") as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            info = archive.gettarinfo(str(path), arcname=str(path.relative_to(source)))
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            with path.open("rb") as stream:
                archive.addfile(info, stream)
    temporary.replace(output)


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            progress("gibuu", f"Waiting for lock: {path}")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            progress("gibuu", f"Acquired lock: {path}")
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _candidate_record(candidate: Candidate) -> dict[str, object]:
    return {
        "cache_id": candidate.cache_id,
        "component": candidate.component,
        "source_event": candidate.source_event,
        "process_id": candidate.process_id,
        "native_weight": candidate.native_weight,
        "sampling_batches": candidate.sampling_batches,
        "flux_integral": candidate.flux_integral,
        "particles": [list(particle) for particle in candidate.particles],
        "incoming_neutrino": (
            list(candidate.incoming_neutrino)
            if candidate.incoming_neutrino is not None
            else None
        ),
        "target_pdg": candidate.target_pdg,
        "nucleon_pdg": candidate.nucleon_pdg,
        "reaction": candidate.reaction,
        "cross_section_1e38_cm2": candidate.cross_section_1e38_cm2,
    }


def _candidate_from_record(record: dict[str, object]) -> Candidate:
    if int(record.get("sampling_batches", 1)) <= 0:
        raise RuntimeError("cached GiBUU candidate has invalid sampling exposure")
    truth_fields = {
        "incoming_neutrino",
        "target_pdg",
        "nucleon_pdg",
        "reaction",
        "cross_section_1e38_cm2",
    }
    missing = sorted(truth_fields - record.keys())
    if missing:
        raise RuntimeError(
            "cached GiBUU candidate predates the RooTracker truth schema: "
            + ", ".join(missing)
        )
    return Candidate(
        cache_id=str(record["cache_id"]),
        component=str(record["component"]),
        source_event=int(record["source_event"]),
        process_id=int(record["process_id"]),
        native_weight=float(record["native_weight"]),
        sampling_batches=int(record.get("sampling_batches", 1)),
        flux_integral=float(record["flux_integral"]),
        score=0.0,
        particles=tuple(
            tuple([int(values[0]), *(float(value) for value in values[1:])])
            for values in record["particles"]
        ),
        incoming_neutrino=(
            tuple(
                [
                    int(record["incoming_neutrino"][0]),
                    *(float(value) for value in record["incoming_neutrino"][1:]),
                ]
            )
            if record.get("incoming_neutrino") is not None
            else None
        ),
        target_pdg=int(record.get("target_pdg", -1)),
        nucleon_pdg=int(record.get("nucleon_pdg", -1)),
        reaction=str(record.get("reaction", "")),
        cross_section_1e38_cm2=float(record.get("cross_section_1e38_cm2", 0.0)),
    )


def _write_candidates(path: Path, candidates: list[Candidate]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as raw, gzip.GzipFile(
        filename="", mode="wb", fileobj=raw, mtime=0
    ) as compressed:
        for candidate in candidates:
            line = json.dumps(
                _candidate_record(candidate), separators=(",", ":")
            ) + "\n"
            compressed.write(line.encode("utf-8"))
    temporary.replace(path)


def _read_cached_candidates(path: Path) -> list[Candidate]:
    return list(_iter_cached_candidates(path))


def _iter_cached_candidates(path: Path) -> Iterator[Candidate]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise RuntimeError(f"invalid cached GiBUU candidate in {path}")
            yield _candidate_from_record(value)


def _counter_uniform(*parts: object) -> float:
    payload = ":".join(str(part) for part in parts).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return (int.from_bytes(digest[:8], "big") + 1) / (2**64 + 1)


def _executable_identity(executable: str) -> dict[str, object]:
    resolved = shutil.which(executable)
    if resolved and Path(resolved).is_file():
        path = Path(resolved).resolve()
        return {"path": str(path), "sha256": checksum(path)}
    return {"path": executable, "sha256": None}


def _cache_contract(args: argparse.Namespace) -> dict[str, object]:
    spectra = read_yaml(args.flux_spectra_manifest)
    flavor_spectra = {
        pdg: {
            "sha256": record.get("sha256"),
            "integral_per_cm2": record.get("integral_per_cm2"),
        }
        for pdg, record in sorted(spectra.get("flavors", {}).items())
    }
    return {
        "schema_version": 2,
        "generator": {"name": "GiBUU", "version": "2025"},
        "executable": _executable_identity(args.executable),
        "canonical_flux_sha256": spectra.get("canonical_flux_sha256"),
        "flavor_spectra": flavor_spectra,
        "jobcard_sha256": checksum(args.jobcard),
        "input_tables": str(args.input_tables.resolve()),
        "software_identity": args.software_identity,
        "target": {"a": args.target_a, "z": args.target_z},
        "processes": sorted(args.processes),
        "energy_binning": {
            "minimum_gev": args.energy_min_gev,
            "maximum_gev": args.energy_max_gev,
            "bins": args.energy_bins,
        },
        "ensembles_per_shard": args.ensembles,
        "runs_per_shard": args.runs,
        "time_steps": args.time_steps,
        "adapter": "dlpgen-opt-gibuu-candidate-cache-v2-rootracker-truth",
    }


def _cache_key(contract: dict[str, object]) -> str:
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _shard_seed(cache_key: str, shard_index: int, component_index: int) -> int:
    # 100003 is coprime to the allowed range, so this sequence cannot collide
    # for the supported 1000 shards and fewer than 1000 components per shard.
    modulus = 2_147_000_000
    base = int(hashlib.sha256(cache_key.encode("ascii")).hexdigest()[:16], 16)
    sequence = shard_index * 1000 + component_index
    return 1 + (base + sequence * 100_003) % modulus


@contextmanager
def _shard_workspace(parent: Path, index: int):
    # One coordinator owns this cache. Stable workspace names allow completed
    # native tasks to survive a timeout before the entire shard is published.
    workspace = parent / f".shard-{index:05d}.work"
    workspace.mkdir(mode=0o700, exist_ok=True)
    try:
        yield workspace
    except BaseException:
        progress("gibuu", f"Shard failed; native logs retained at {workspace}")
        raise
    else:
        shutil.rmtree(workspace)


def _run_native_task(args, task):
    """Run/checkpoint one isolated native task; never mutate the cache manifest."""
    directory = Path(task["directory"])
    directory.mkdir(parents=True, exist_ok=True)
    jobcard = directory / "jobcard.nml"
    contract = {
        "seed": task["seed"], "sampling_batches": task["batches"],
        "jobcard_sha256": hashlib.sha256(task["jobcard"].encode()).hexdigest(),
        "flux_sha256": checksum(Path(task["flux_path"])),
        "flux_integral": task["flux_integral"], "cache_prefix": task["cache_prefix"],
    }
    completed_path = directory / "completed.yaml"
    native = directory / "EventOutput.Pert.hepmc3"
    candidates_path = directory / "candidates.jsonl.gz"
    if completed_path.exists():
        record = read_yaml(completed_path)
        if record.get("contract") != contract:
            raise RuntimeError(f"GiBUU task checkpoint has different inputs: {directory}")
        for name, field in ((native, "native_sha256"), (candidates_path, "candidates_sha256"),
                            (jobcard, "jobcard_sha256")):
            if checksum(name) != record[field]:
                raise RuntimeError(f"GiBUU task checkpoint checksum mismatch: {name}")
        progress("gibuu", f"Reusing completed task {task['label']}: {directory}")
        return record
    started = time.monotonic()
    jobcard.write_text(task["jobcard"], encoding="utf-8")
    progress("gibuu", f"Starting {task['label']}: {args.runs * task['batches']} runs; logs: {directory}")
    with jobcard.open() as stdin, (directory / "stdout.log").open("w") as stdout, (directory / "stderr.log").open("w") as stderr:
        completed = subprocess.run(
            [args.executable], cwd=directory, stdin=stdin, stdout=stdout, stderr=stderr,
            text=True, check=False,
            env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"},
        )
    if completed.returncode:
        tail = []
        for name in ("stdout.log", "stderr.log"):
            tail.extend((directory / name).read_text(errors="replace").splitlines()[-20:])
        raise RuntimeError(f"GiBUU task {task['label']} failed with exit code {completed.returncode}; logs: {directory}\n" + "\n".join(tail))
    validate_nonempty(native)
    candidates = _read_candidates(
        native, component=task["component"], flux_integral=task["flux_integral"],
        seed=task["seed"], cache_prefix=task["cache_prefix"],
    )
    _write_candidates(candidates_path, candidates)
    record = {
        "contract": contract, "seed": task["seed"], "sampling_batches": task["batches"],
        "candidate_events": len(candidates), "native_sha256": checksum(native),
        "candidates_sha256": checksum(candidates_path), "jobcard_sha256": checksum(jobcard),
        "elapsed_seconds": time.monotonic() - started,
    }
    write_yaml(completed_path, record)
    progress("gibuu", f"Finished {task['label']}: {len(candidates):,} candidates; elapsed {record['elapsed_seconds']:.1f}s")
    return record


def _native_tasks(args, histograms, template, cache_key, shard_index, workspace, selected_components):
    """Fixed two-base-batch chunks; seeds and task order do not depend on workers."""
    tasks = []
    component_index = 0
    for pdg, histogram in sorted(histograms.items()):
        for process in sorted(args.processes):
            component = f"pdg{pdg}-{process}"
            slot = component_index
            component_index += 1
            batches = selected_components.get(component, 0) if selected_components is not None else 1
            if not 0 <= batches <= 64:
                raise ValueError("GiBUU component batches must be between 0 and 64")
            for chunk_index, offset in enumerate(range(0, batches, 2)):
                chunk_batches = min(2, batches - offset)
                seed = _shard_seed(cache_key, shard_index, slot * 32 + chunk_index)
                name = f"{component}/chunk-{chunk_index:03d}"
                flux_path = Path(histogram["path"])
                tasks.append({
                    "name": name, "component": component, "seed": seed,
                    "batches": chunk_batches, "directory": str(workspace / name),
                    "flux_path": str(flux_path), "flux_integral": float(histogram["integral_per_cm2"]),
                    "label": f"shard {shard_index} {name}",
                    "cache_prefix": f"shard-{shard_index:05d}/chunk-{chunk_index:03d}",
                    "jobcard": resolved_jobcard(
                        template, pdg=pdg, process=process, flux_file=flux_path,
                        input_tables=args.input_tables, target_a=args.target_a, target_z=args.target_z,
                        ensembles=args.ensembles, runs=args.runs * chunk_batches,
                        time_steps=args.time_steps, seed=seed,
                    ),
                })
    return tasks


def _execute_native_tasks(args, tasks):
    requested = getattr(args, "workers", 1)
    try:
        available = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        available = os.cpu_count() or 1
    allocation = os.environ.get("SLURM_CPUS_PER_TASK", "")
    if allocation.isdigit() and int(allocation) > 0:
        available = min(available, int(allocation))
    workers = min(requested, len(tasks), available)
    if workers < 1:
        raise ValueError("GiBUU preparation requires positive workers and nonempty tasks")
    if requested > available:
        progress("gibuu", f"Limiting requested {requested} workers to {available} available CPUs")
    progress("gibuu", f"Executing {len(tasks)} native tasks with {workers} workers")
    if workers == 1:
        return [_run_native_task(args, task) for task in tasks]
    records = {}
    # Spawn isolates native readers and avoids forking imported ROOT state.
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = {pool.submit(_run_native_task, args, task): index for index, task in enumerate(tasks)}
        for future in as_completed(futures):
            records[futures[future]] = future.result()
    # Completion order cannot change the serialized candidates or campaign selection.
    return [records[index] for index in range(len(tasks))]


def _generate_cache_shard(
    args: argparse.Namespace,
    *,
    cache_key: str,
    shard_index: int,
    destination: Path,
    selected_components: dict[str, int] | None = None,
) -> dict[str, object]:
    flux_manifest = read_yaml(args.flux_manifest)
    if not flux_manifest.get("normalization", {}).get("valid_per_selected_pot"):
        raise RuntimeError(
            "GiBUU generation requires complete scans of the selected flux files"
        )
    histograms = cached_flux_histograms(
        args.flux_spectra_manifest,
        args.flux_manifest,
        energy_min=args.energy_min_gev,
        energy_max=args.energy_max_gev,
        bins=args.energy_bins,
    )
    histograms = {pdg: dict(histogram) for pdg, histogram in histograms.items()}
    template = args.jobcard.read_text(encoding="utf-8")
    components: dict[str, object] = {}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _shard_workspace(destination.parent, shard_index) as workspace:
        # Production directories can differ while sharing a physics cache key.
        # Stable local flux paths let another campaign resume task checkpoints.
        for pdg, histogram in histograms.items():
            flux_path = workspace / f"flux-{pdg}.dat"
            temporary_flux = flux_path.with_suffix(".tmp")
            shutil.copyfile(Path(histogram["path"]), temporary_flux)
            temporary_flux.replace(flux_path)
            histogram["path"] = flux_path
        plan_path = workspace / "plan.yaml"
        previous = read_yaml(plan_path) if plan_path.exists() else None
        if previous is not None:
            if previous.get("cache_key") != cache_key or previous.get("shard_index") != shard_index:
                raise RuntimeError(f"Interrupted GiBUU shard has a different identity: {workspace}")
            # Finish an already assigned wave even if a later campaign needs
            # a different pool size. Its completed native work remains valid.
            selected_components = {}
            for task in previous["tasks"]:
                selected_components[task["component"]] = selected_components.get(task["component"], 0) + task["batches"]
        tasks = _native_tasks(args, histograms, template, cache_key, shard_index, workspace, selected_components)
        plan = {"cache_key": cache_key, "shard_index": shard_index, "tasks": tasks}
        if previous is not None and previous != plan:
            raise RuntimeError(f"Interrupted GiBUU shard has a different task plan: {workspace}")
        write_yaml(plan_path, plan)
        records = _execute_native_tasks(args, tasks)
        for task, record in zip(tasks, records):
            row = components.setdefault(task["component"], {
                "sampling_batches": 0, "candidate_events": 0, "elapsed_seconds": 0.0, "tasks": [],
            })
            row["sampling_batches"] += record["sampling_batches"]
            row["candidate_events"] += record["candidate_events"]
            row["elapsed_seconds"] += record["elapsed_seconds"]
            row["tasks"].append({"name": task["name"], **record})
        staging = workspace / "cache-products"
        # An interrupted aggregation can be rebuilt from completed task checkpoints.
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        candidate_path = staging / "candidates.jsonl.gz"
        archive_path = staging / "native.tar.gz"
        def candidates():
            for task in tasks:
                yield from _iter_cached_candidates(Path(task["directory"]) / "candidates.jsonl.gz")
        _write_candidates(candidate_path, candidates())
        archive_source = workspace / "archive"
        if archive_source.exists():
            shutil.rmtree(archive_source)
        archive_source.mkdir()
        shutil.copy2(plan_path, archive_source / "plan.yaml")
        for histogram in histograms.values():
            flux_path = Path(histogram["path"])
            shutil.copy2(flux_path, archive_source / flux_path.name)
        for task in tasks:
            source_dir = Path(task["directory"])
            target_dir = archive_source / task["name"]
            target_dir.mkdir(parents=True)
            for name in ("EventOutput.Pert.hepmc3", "jobcard.nml", "stdout.log", "stderr.log"):
                shutil.copy2(source_dir / name, target_dir / name)
        _write_reproducible_archive(archive_source, archive_path)
        count = sum(record["candidate_events"] for record in records)
        record = {
            "schema_version": 2, "cache_key": cache_key, "index": shard_index,
            "candidate_events": count, "candidates_sha256": checksum(candidate_path),
            "native_archive_sha256": checksum(archive_path), "components": components,
        }
        write_yaml(staging / "shard.yaml", record)
        staging.rename(destination)
        progress("gibuu", f"Published shard {shard_index}: {count:,} candidates; archived native logs: {destination / 'native.tar.gz'}")
    return record


def _load_cache_candidates(
    entry: Path, manifest: dict[str, object], *, verify: bool = True
) -> Iterator[Candidate]:
    for shard in manifest.get("shards", []):
        directory = entry / "shards" / f"{int(shard['index']):05d}"
        progress("gibuu", f"{'Validating and loading' if verify else 'Reading'} shard {shard['index']}: {directory}")
        if verify and read_yaml(directory / "shard.yaml") != shard:
            raise RuntimeError(f"cached GiBUU shard manifest mismatch: {directory}")
        path = directory / "candidates.jsonl.gz"
        if verify and checksum(path) != shard.get("candidates_sha256"):
            raise RuntimeError(f"cached GiBUU candidate checksum mismatch: {path}")
        archive = directory / "native.tar.gz"
        if verify and checksum(archive) != shard.get("native_archive_sha256"):
            raise RuntimeError(f"cached GiBUU native archive checksum mismatch: {archive}")
        yield from _iter_cached_candidates(path)


def _recover_published_shards(
    entry: Path, manifest_path: Path, manifest: dict[str, object]
) -> None:
    """Adopt a complete shard published just before an interrupted manifest write."""
    while True:
        index = len(manifest["shards"])
        directory = entry / "shards" / f"{index:05d}"
        if not directory.exists():
            return
        record_path = directory / "shard.yaml"
        record = read_yaml(record_path)
        candidates = directory / "candidates.jsonl.gz"
        archive = directory / "native.tar.gz"
        if (
            record.get("cache_key") != manifest.get("key")
            or record.get("index") != index
            or checksum(candidates) != record.get("candidates_sha256")
            or checksum(archive) != record.get("native_archive_sha256")
        ):
            raise RuntimeError(f"incomplete GiBUU cache shard: {directory}")
        manifest["shards"].append(record)
        write_yaml(manifest_path, manifest)


def _effective_sample_size(candidates: list[Candidate]) -> float:
    if not candidates:
        return 0.0
    weights = [candidate.global_weight for candidate in candidates]
    return sum(weights) ** 2 / sum(weight * weight for weight in weights)


def _pool_statistics(candidates, manifest):
    """Estimate component rates per equal-exposure batch, not per stored event.

    GiBUU's perweight is cross section/(A*numEnsembles); it is not divided
    by num_runs_SameEnergy. A component generated B base batches therefore
    contributes sum(native_weight*flux_integral)/B to the rate estimate.
    """
    stats = {}
    for shard in manifest["shards"]:
        for component, record in shard["components"].items():
            row = stats.setdefault(component, dict(batches=0, count=0, weight=0.0, squared=0.0))
            batches = int(record.get("sampling_batches", 1))
            if batches <= 0:
                raise RuntimeError(f"invalid sampling exposure for {component}")
            row["batches"] += batches
    for candidate in candidates:
        row = stats[candidate.component]
        weight = candidate.native_weight * candidate.flux_integral
        row["count"] += 1
        row["weight"] += weight
        row["squared"] += weight * weight
    return stats, _finish_statistics(stats)


def _finish_statistics(stats):
    rate = sum(row["weight"] / row["batches"] for row in stats.values())
    variance_sum = sum(row["squared"] / row["batches"]**2 for row in stats.values())
    for row in stats.values():
        row["share"] = row["weight"] / row["batches"] / rate if rate else 0.0
        row["ess"] = row["weight"]**2 / row["squared"] if row["squared"] else 0.0
    ess = rate**2 / variance_sum if variance_sum else 0.0
    return ess


def _normalized_cache_candidates(entry, manifest, stats, adaptive):
    # Published shards are immutable and were validated during the first pass.
    for item in _load_cache_candidates(entry, manifest, verify=False):
        yield replace(item, sampling_batches=stats[item.component]["batches"]) if adaptive else item


def _select_campaign_candidates(candidates: Iterable[Candidate], count: int, seed: int, key: str) -> list[Candidate]:
    # Same exponential-race scores as the previous full sort, but keep only
    # count payloads in memory even when the pool contains millions of records.
    return heapq.nsmallest(
        count, candidates,
        key=lambda item: -math.log(_counter_uniform("select", seed, key, item.cache_id)) / item.global_weight,
    )


def _allocation_plan(stats, target):
    """Fill ESS deficits per component; schedule up to 64 base batches per wave.

    ESS_c >= target*share_c implies global ESS >= target. The estimates are
    recomputed after every shard; the global stopping check is authoritative.
    Zero-yield components are retried rather than declared physically absent.
    """
    plan = {}
    for component, row in sorted(stats.items()):
        missing = target * row["share"] - row["ess"]
        if row["ess"] == 0:
            plan[component] = 1
        elif missing > 0:
            per_batch = row["ess"] / row["batches"]
            plan[component] = max(1, min(64, math.ceil(missing / per_batch)))
    return plan


def _report_pool(stats, ess, target, shards):
    progress("gibuu", f"Cache progress: {shards} shards, ESS {ess:,.1f}/{target:,} ({100 * ess / target:.2f}%)")
    for component, row in sorted(stats.items()):
        progress("gibuu", f"  {component}: {row['count']:,} candidates, {row['batches']} batches, weight share {row['share']:.3%}, ESS {row['ess']:,.1f}")


def _reuse_candidate_cache(root, legacy_contract, manifest, entry):
    """Snapshot completed legacy shards without modifying the original cache."""
    key = _cache_key(legacy_contract)
    legacy = root / key
    if not (legacy / "cache.yaml").exists():
        return
    # cache.yaml is published by atomic rename and completed shards are
    # immutable. Snapshot it without opening the legacy writer's lock file,
    # which may be read-only to this user or held for a long generation run.
    previous = read_yaml(legacy / "cache.yaml")
    if previous.get("contract") != legacy_contract or previous.get("key") != key:
        raise RuntimeError(f"invalid legacy GiBUU cache: {legacy}")
    for shard in previous["shards"]:
        source = legacy / "shards" / f"{shard['index']:05d}"
        if read_yaml(source / "shard.yaml") != shard:
            raise RuntimeError(f"invalid legacy GiBUU shard: {source}")
        destination = entry / "shards" / source.name
        destination.mkdir()
        for name in ("shard.yaml", "candidates.jsonl.gz", "native.tar.gz"):
            # Copy small manifests; immutable payloads can share storage.
            if name == "shard.yaml":
                shutil.copy2(source / name, destination / name)
            else:
                try:
                    (destination / name).hardlink_to(source / name)
                except OSError:
                    shutil.copy2(source / name, destination / name)
        manifest["shards"].append(shard)
    manifest["reused_candidate_cache"] = str(legacy)
    progress("gibuu", f"Reused {len(manifest['shards'])} completed shards from {legacy}")


def _ensure_candidate_cache(
    args: argparse.Namespace, required_events: int
) -> tuple[Path, dict[str, object], Iterator[Candidate], float]:
    legacy_contract = _cache_contract(args)
    adaptive = getattr(args, "cache_allocation", "uniform") == "adaptive"
    contract = {**legacy_contract, "allocation": "component-ess-exposure-v1"} if adaptive else legacy_contract
    previous_contract = contract
    contract = {**contract, "native_task_layout": "component-chunks-v1"}
    key = _cache_key(contract)
    root = args.candidate_cache_dir
    entry = root / key
    target = max(required_events, math.ceil(required_events * (1 + args.reserve_fraction)))
    with _exclusive_lock(root / f".{key}.lock"):
        manifest_path = entry / "cache.yaml"
        if manifest_path.exists():
            manifest = read_yaml(manifest_path)
            if manifest.get("key") != key or manifest.get("contract") != contract:
                raise RuntimeError(f"invalid GiBUU candidate cache entry: {entry}")
        else:
            if entry.exists():
                raise RuntimeError(f"incomplete GiBUU candidate cache entry: {entry}")
            root.mkdir(parents=True, exist_ok=True)
            manifest = {
                "schema_version": 1,
                "format": "dlpgen-opt-gibuu-candidate-cache",
                "key": key,
                "contract": contract,
                "shards": [],
            }
            with tempfile.TemporaryDirectory(prefix=f".{key}.init.", dir=root) as temporary:
                staging = Path(temporary) / "entry"
                (staging / "shards").mkdir(parents=True)
                compatible = [previous_contract]
                if adaptive:
                    compatible += [{**legacy_contract, "native_task_layout": "component-chunks-v1"}, legacy_contract]
                for old_contract in compatible:
                    if (root / _cache_key(old_contract) / "cache.yaml").exists():
                        _reuse_candidate_cache(root, old_contract, manifest, staging)
                        break
                write_yaml(staging / "cache.yaml", manifest)
                staging.rename(entry)
        progress("gibuu", f"Validating and loading candidate cache: {entry}")
        _recover_published_shards(entry, manifest_path, manifest)
        candidates = _load_cache_candidates(entry, manifest)
        stats, effective_events = _pool_statistics(candidates, manifest)
        _report_pool(stats, effective_events, target, len(manifest["shards"]))
        fixed_target = args.cache_shards if args.cache_sizing == "fixed" else None
        while (
            (fixed_target is not None and len(manifest["shards"]) < fixed_target)
            or (fixed_target is None and effective_events < target)
        ):
            index = len(manifest["shards"])
            if index >= args.max_cache_shards:
                raise RuntimeError(
                    f"GiBUU cache reached max_cache_shards={args.max_cache_shards} "
                    f"with effective sample size {effective_events:.3g}; "
                    f"{target} are required"
                )
            shard = _generate_cache_shard(
                args,
                cache_key=key,
                shard_index=index,
                destination=entry / "shards" / f"{index:05d}",
                selected_components=(
                    _allocation_plan(stats, target)
                    if adaptive and stats and fixed_target is None else None
                ),
            )
            manifest["shards"].append(shard)
            write_yaml(manifest_path, manifest)
            new_stats, _ = _pool_statistics(
                _iter_cached_candidates(entry / "shards" / f"{index:05d}" / "candidates.jsonl.gz"),
                {"shards": [shard]},
            )
            for component, row in new_stats.items():
                if component not in stats:
                    stats[component] = row
                else:
                    for field in ("batches", "count", "weight", "squared"):
                        stats[component][field] += row[field]
            effective_events = _finish_statistics(stats)
            _report_pool(stats, effective_events, target, len(manifest["shards"]))
            manifest["statistics"] = {
                "effective_sample_size": effective_events,
                "components": stats,
            }
            write_yaml(manifest_path, manifest)
        if fixed_target is not None and effective_events < required_events:
            raise RuntimeError(
                f"fixed GiBUU cache has effective sample size "
                f"{effective_events:.3g}; {required_events} are required"
            )
        candidates = _normalized_cache_candidates(entry, manifest, stats, adaptive)
        progress("gibuu", f"Candidate cache ready: {entry}")
        return entry, manifest, candidates, effective_events


def _run_cached(args: argparse.Namespace) -> dict[str, object]:
    if args.total_events is None or args.job_index is None or args.campaign_dir is None:
        raise ValueError(
            "cached GiBUU generation requires total-events, job-index, and campaign-dir"
        )
    if args.job_index < 0 or args.total_events <= 0:
        raise ValueError("invalid cached GiBUU campaign range")
    campaign = args.campaign_dir
    campaign.mkdir(parents=True, exist_ok=True)
    campaign_manifest_path = campaign / "campaign.yaml"
    selected_path = campaign / "selected.jsonl.gz"
    expected = {
        "schema_version": 3 if getattr(args, "cache_allocation", "uniform") == "adaptive" else 2,
        "format": "dlpgen-opt-gibuu-campaign",
        "boundary": "NuHepMC-to-RooTracker-truth-v1",
        "total_events": args.total_events,
        "selection_seed": args.seed,
        "selection": "deterministic weighted sampling without replacement",
    }
    campaign_manifest: dict[str, object] | None = None
    with _exclusive_lock(campaign / ".campaign.lock"):
        if campaign_manifest_path.exists():
            campaign_manifest = read_yaml(campaign_manifest_path)
            for key, value in expected.items():
                if campaign_manifest.get(key) != value:
                    raise RuntimeError(
                        f"GiBUU campaign allocation has a different {key}: {campaign}"
                    )
            if checksum(selected_path) != campaign_manifest.get("selected_sha256"):
                raise RuntimeError(f"GiBUU campaign selection checksum mismatch: {campaign}")
    if campaign_manifest is None:
        cache_entry, cache_manifest, candidates, effective_events = _ensure_candidate_cache(
            args, args.total_events
        )
        with _exclusive_lock(campaign / ".campaign.lock"):
            if campaign_manifest_path.exists():
                campaign_manifest = read_yaml(campaign_manifest_path)
                for key, value in {**expected, "cache_key": cache_manifest["key"]}.items():
                    if campaign_manifest.get(key) != value:
                        raise RuntimeError(
                            f"GiBUU campaign allocation has a different {key}: {campaign}"
                        )
                if checksum(selected_path) != campaign_manifest.get("selected_sha256"):
                    raise RuntimeError(
                        f"GiBUU campaign selection checksum mismatch: {campaign}"
                    )
            else:
                progress("gibuu", f"Selecting {args.total_events:,} distinct campaign events")
                expected["cache_key"] = cache_manifest["key"]
                selected = _select_campaign_candidates(
                    candidates, args.total_events, args.seed, cache_manifest["key"]
                )
                _write_candidates(selected_path, selected)
                campaign_manifest = {
                    **expected,
                    "candidate_cache": str(cache_entry),
                    "cache_shards_used": len(cache_manifest["shards"]),
                    "cached_candidates": sum(
                        int(shard["candidate_events"])
                        for shard in cache_manifest["shards"]
                    ),
                    "effective_sample_size": effective_events,
                    "reserve_fraction": args.reserve_fraction,
                    "selected_sha256": checksum(selected_path),
                }
                write_yaml(campaign_manifest_path, campaign_manifest)
    if getattr(args, "prepare_only", False):
        progress("gibuu", f"Campaign preparation complete: {campaign_manifest_path}")
        return {
            "format": "GiBUU-cached-campaign-preparation",
            "campaign_manifest": str(campaign_manifest_path),
            "campaign_manifest_sha256": checksum(campaign_manifest_path),
            "events": args.total_events,
        }
    offset = args.job_index * args.events
    job_candidates = list(islice(_iter_cached_candidates(selected_path), offset, offset + args.events))
    if len(job_candidates) != args.events:
        raise RuntimeError(
            f"GiBUU campaign contains no complete allocation for job {args.job_index}"
        )
    _write_rootracker_candidates(job_candidates, args.output, tuple(args.vertex_cm))
    metadata = {
        "format": "GiBUU-cached-unweighted-campaign-to-edep-sim-RooTracker",
        "events": len(job_candidates),
        "generator_tools": [
            {"name": "GiBUU", "version": "2025", "description": "native"}
        ],
        "candidate_cache": campaign_manifest["candidate_cache"],
        "candidate_cache_key": campaign_manifest["cache_key"],
        "campaign_manifest": str(campaign_manifest_path),
        "campaign_manifest_sha256": checksum(campaign_manifest_path),
        "job_index": args.job_index,
        "global_event_offset": offset,
        "selected": [
            {
                "cache_id": item.cache_id,
                "component": item.component,
                "source_event": item.source_event,
                "process_id": item.process_id,
                "native_weight": item.native_weight,
                "sampling_batches": item.sampling_batches,
                "flux_integral_per_cm2": item.flux_integral,
            }
            for item in job_candidates
        ],
        "vertex_cm": list(args.vertex_cm),
    }
    metadata_temporary = args.metadata_output.with_suffix(
        args.metadata_output.suffix + ".tmp"
    )
    metadata_temporary.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    metadata_temporary.replace(args.metadata_output)
    return metadata


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.events <= 0 or args.seed < 0:
        raise ValueError("events must be positive and seed non-negative")
    if args.target_z < 0 or args.target_z > args.target_a:
        raise ValueError("invalid GiBUU target")
    if args.ensembles < 100 or args.runs <= 0 or args.time_steps < 0:
        raise ValueError(
            "GiBUU requires at least 100 ensembles; runs must be positive and "
            "time steps non-negative"
        )
    if args.energy_bins <= 0 or not 0 <= args.energy_min_gev < args.energy_max_gev:
        raise ValueError("invalid GiBUU energy binning")
    if len(set(args.processes)) != len(args.processes):
        raise ValueError("GiBUU processes must be unique")
    if not all(math.isfinite(value) for value in args.vertex_cm):
        raise ValueError("vertex coordinates must be finite")
    if getattr(args, "workers", 1) <= 0:
        raise ValueError("workers must be positive")
    if args.reserve_fraction < 0 or args.max_cache_shards <= 0:
        raise ValueError("invalid GiBUU candidate-cache sizing")
    if args.cache_sizing == "fixed" and not args.cache_shards:
        raise ValueError("fixed GiBUU candidate-cache sizing requires cache-shards")
    validate_nonempty(args.jobcard)
    if not args.input_tables.is_dir():
        raise RuntimeError(
            f"GiBUU input tables are not a directory: {args.input_tables}"
        )
    args.work_dir.mkdir(parents=True, exist_ok=True)
    if args.candidate_cache_dir is not None:
        if not args.prepare_only:
            for path in (args.output, args.metadata_output):
                if path.exists():
                    raise RuntimeError(f"refusing to overwrite output: {path}")
                path.parent.mkdir(parents=True, exist_ok=True)
        return _run_cached(args)
    for path in (
        args.output,
        args.metadata_output,
        args.native_archive,
        args.resolved_jobcards,
    ):
        if path.exists():
            raise RuntimeError(f"refusing to overwrite output: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
    flux_manifest = read_yaml(args.flux_manifest)
    if not flux_manifest.get("normalization", {}).get("valid_per_selected_pot"):
        raise RuntimeError(
            "GiBUU generation requires complete scans of the selected flux files"
        )
    template = args.jobcard.read_text(encoding="utf-8")
    jobcard_records: dict[str, object] = {}
    all_candidates: list[Candidate] = []
    with tempfile.TemporaryDirectory(prefix=".gibuu-", dir=args.work_dir) as temporary:
        workspace = Path(temporary)
        histograms = cached_flux_histograms(
            args.flux_spectra_manifest,
            args.flux_manifest,
            energy_min=args.energy_min_gev,
            energy_max=args.energy_max_gev,
            bins=args.energy_bins,
        )
        component_index = 0
        for pdg, histogram in histograms.items():
            for process in args.processes:
                component = f"pdg{pdg}-{process}"
                directory = workspace / component
                directory.mkdir()
                flux_file = Path(histogram["path"])
                jobcard = directory / "jobcard.nml"
                # GiBUU stores SEED in a signed four-byte Fortran integer.
                job_seed = 1 + (
                    (args.seed - 1 + component_index * 100_003) % 2_147_000_000
                )
                jobcard.write_text(
                    resolved_jobcard(
                        template,
                        pdg=pdg,
                        process=process,
                        flux_file=flux_file,
                        input_tables=args.input_tables,
                        target_a=args.target_a,
                        target_z=args.target_z,
                        ensembles=args.ensembles,
                        runs=args.runs,
                        time_steps=args.time_steps,
                        seed=job_seed,
                    ),
                    encoding="utf-8",
                )
                with jobcard.open(encoding="utf-8") as stdin, (
                    directory / "stdout.log"
                ).open("w", encoding="utf-8") as stdout, (
                    directory / "stderr.log"
                ).open("w", encoding="utf-8") as stderr:
                    completed = subprocess.run(
                        [args.executable],
                        cwd=directory,
                        stdin=stdin,
                        stdout=stdout,
                        stderr=stderr,
                        text=True,
                        check=False,
                    )
                if completed.returncode:
                    stderr_tail = (directory / "stderr.log").read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()[-20:]
                    stdout_tail = (directory / "stdout.log").read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()[-20:]
                    raise RuntimeError(
                        f"GiBUU component {component} failed with "
                        f"exit code {completed.returncode}; output tail: "
                        + "\n".join([*stdout_tail, *stderr_tail])
                    )
                native = directory / "EventOutput.Pert.hepmc3"
                validate_nonempty(native)
                candidates = _read_candidates(
                    native,
                    component=component,
                    flux_integral=float(histogram["integral_per_cm2"]),
                    seed=args.seed,
                )
                all_candidates.extend(candidates)
                jobcard_records[component] = {
                    "pdg": pdg,
                    "process": process,
                    "seed": job_seed,
                    "flux_integral_per_cm2": histogram["integral_per_cm2"],
                    "candidate_events": len(candidates),
                    "jobcard_sha256": checksum(jobcard),
                    "native_sha256": checksum(native),
                }
                component_index += 1
        if len(all_candidates) < args.events:
            raise RuntimeError(
                f"GiBUU produced {len(all_candidates)} weighted candidates; "
                f"{args.events} are required"
            )
        selected = heapq.nsmallest(
            args.events, all_candidates, key=lambda item: item.score
        )
        _write_rootracker_candidates(selected, args.output, tuple(args.vertex_cm))
        _write_reproducible_archive(workspace, args.native_archive)

    write_yaml(args.resolved_jobcards, {"components": jobcard_records})
    metadata: dict[str, object] = {
        "format": "GiBUU-dk2nu-weighted-resample-to-edep-sim-RooTracker",
        "events": len(selected),
        "generator_tools": [
            {"name": "GiBUU", "version": "2025", "description": "native"}
        ],
        "flux_table": flux_manifest.get("output"),
        "flux_manifest": validate_nonempty(args.flux_manifest),
        "flux_spectra_manifest": validate_nonempty(args.flux_spectra_manifest),
        "jobcard_template": validate_nonempty(args.jobcard),
        "candidate_events": len(all_candidates),
        "selection_policy": "deterministic weighted sampling without replacement",
        "selection_weight": "GiBUU CV event weight times flavor flux integral",
        "mixture_contract": (
            "all flavor/process components use identical target, ensembles, and "
            "trials; "
            "GiBUU samples energy from each normalized flavor spectrum, while the "
            "canonical flavor integral restores relative beam composition"
        ),
        "selected": [
            {
                "component": item.component,
                "source_event": item.source_event,
                "process_id": item.process_id,
                "native_weight": item.native_weight,
                "flux_integral_per_cm2": item.flux_integral,
                "selection_score": item.score,
            }
            for item in selected
        ],
        "vertex_cm": list(args.vertex_cm),
    }
    args.metadata_output.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        print(json.dumps(run(args), sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
