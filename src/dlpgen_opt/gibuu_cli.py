from __future__ import annotations

import argparse
import gzip
import hashlib
import heapq
import json
import math
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .nuhepmc_cli import (
    GIBUU_NUCLEAR_REMNANT_PDG,
    NUCLEAR_REMNANT_PDG,
    _event_metadata,
    _momentum_scale,
    _selected_hepmc_input,
)
from .provenance import checksum, read_yaml, write_yaml
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


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="python -m dlpgen_opt.gibuu_cli",
        description="Run GiBUU from canonical dk2nu throws and project to HEPEVT.",
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


def _flux_histograms(
    flux_table: Path,
    directory: Path,
    *,
    energy_min: float,
    energy_max: float,
    bins: int,
) -> dict[int, dict[str, object]]:
    try:
        import ROOT  # type: ignore
    except ImportError as error:
        raise RuntimeError(
            "ROOT is required to project canonical flux throws"
        ) from error
    root_file = ROOT.TFile.Open(str(flux_table), "READ")
    tree = root_file.Get("fluxThrows") if root_file else None
    if not root_file or root_file.IsZombie() or not tree:
        raise RuntimeError(f"invalid canonical flux table: {flux_table}")
    width = (energy_max - energy_min) / bins
    contents: dict[int, list[float]] = {}
    outside: dict[int, float] = {}
    for event in tree:
        pdg = int(event.pdg)
        weight = float(event.flux_weight_per_cm2)
        energy = float(event.energy_gev)
        if pdg not in FLAVOR_ID or weight < 0 or not math.isfinite(weight):
            raise RuntimeError("canonical flux table contains an invalid throw")
        contents.setdefault(pdg, [0.0] * bins)
        outside.setdefault(pdg, 0.0)
        index = int(math.floor((energy - energy_min) / width))
        if index == bins and energy == energy_max:
            index -= 1
        if not 0 <= index < bins:
            outside[pdg] += weight
            continue
        contents[pdg][index] += weight
    root_file.Close()
    if any(weight > 0 for weight in outside.values()):
        raise RuntimeError(
            f"canonical flux lies outside configured GiBUU energy range: {outside}"
        )
    result: dict[int, dict[str, object]] = {}
    for pdg, values in sorted(contents.items()):
        integral = sum(values)
        if integral <= 0:
            continue
        path = directory / f"flux-{pdg}.dat"
        with path.open("w", encoding="utf-8") as stream:
            stream.write("# energy_GeV flux_per_GeV_cm2\n")
            for index, content in enumerate(values):
                center = energy_min + (index + 0.5) * width
                stream.write(f"{center:.12g} {content / width:.16g}\n")
        result[pdg] = {"path": path, "integral_per_cm2": integral}
    return result


def materialize_flux_spectra(
    *,
    flux_table: Path,
    flux_manifest: Path,
    output_manifest: Path,
    energy_min: float,
    energy_max: float,
    bins: int,
) -> dict[str, object]:
    """Project canonical throws once into compact, reusable GiBUU spectra."""
    if output_manifest.exists():
        raise RuntimeError(f"refusing to overwrite flux spectra: {output_manifest}")
    canonical = read_yaml(flux_manifest)
    if not canonical.get("normalization", {}).get("valid_per_selected_pot"):
        raise RuntimeError("canonical flux is not normalized over complete selected files")
    table_sha256 = checksum(flux_table)
    if canonical.get("output", {}).get("sha256") != table_sha256:
        raise RuntimeError("canonical flux table does not match its manifest")
    output_manifest.parent.mkdir(parents=True, exist_ok=True)
    histograms = _flux_histograms(
        flux_table,
        output_manifest.parent,
        energy_min=energy_min,
        energy_max=energy_max,
        bins=bins,
    )
    flavors: dict[str, object] = {}
    for pdg, histogram in histograms.items():
        path = Path(histogram["path"])
        flavors[str(pdg)] = {
            "path": path.name,
            "integral_per_cm2": histogram["integral_per_cm2"],
            "sha256": checksum(path),
        }
    manifest: dict[str, object] = {
        "schema_version": 1,
        "format": "dlpgen-opt-gibuu-flux-spectra",
        "canonical_flux_sha256": table_sha256,
        "selected_pot": canonical.get("normalization", {}).get("selected_pot"),
        "binning": {
            "energy_min_gev": energy_min,
            "energy_max_gev": energy_max,
            "bins": bins,
        },
        "flavors": flavors,
    }
    write_yaml(output_manifest, manifest)
    return manifest


def _cached_flux_histograms(
    spectra_manifest: Path,
    canonical_manifest: Path,
    *,
    energy_min: float,
    energy_max: float,
    bins: int,
) -> dict[int, dict[str, object]]:
    manifest = read_yaml(spectra_manifest)
    expected_binning = {
        "energy_min_gev": energy_min,
        "energy_max_gev": energy_max,
        "bins": bins,
    }
    if manifest.get("format") != "dlpgen-opt-gibuu-flux-spectra":
        raise RuntimeError(f"invalid GiBUU flux spectra manifest: {spectra_manifest}")
    if manifest.get("binning") != expected_binning:
        raise RuntimeError("cached GiBUU flux spectra use different energy binning")
    canonical = read_yaml(canonical_manifest)
    if manifest.get("canonical_flux_sha256") != canonical.get("output", {}).get(
        "sha256"
    ):
        raise RuntimeError("cached GiBUU spectra do not match the canonical flux table")
    result: dict[int, dict[str, object]] = {}
    for pdg_text, record in manifest.get("flavors", {}).items():
        pdg = int(pdg_text)
        path = spectra_manifest.parent / record["path"]
        if checksum(path) != record["sha256"]:
            raise RuntimeError(f"cached GiBUU flux spectrum checksum mismatch: {path}")
        result[pdg] = {
            "path": path,
            "integral_per_cm2": float(record["integral_per_cm2"]),
        }
    if not result:
        raise RuntimeError("cached GiBUU flux spectra contain no flavors")
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
                    )
                )
    return candidates


def _write_hepevt(
    selected: list[Candidate], output: Path, vertex_cm: tuple[float, float, float]
) -> None:
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for index, candidate in enumerate(selected):
            x, y, z = vertex_cm
            stream.write(
                f"{index} 0 {len(candidate.particles)} "
                f"{x:.12g} {y:.12g} {z:.12g} 0\n"
            )
            for pdg, px, py, pz, energy, mass in candidate.particles:
                stream.write(
                    f"1 {pdg} 0 0 0 0 {px:.12g} {py:.12g} {pz:.12g} "
                    f"{energy:.12g} {mass:.12g}\n"
                )
    temporary.replace(output)


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
    args.work_dir.mkdir(parents=True, exist_ok=True)
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
    validate_nonempty(args.jobcard)
    if not args.input_tables.is_dir():
        raise RuntimeError(
            f"GiBUU input tables are not a directory: {args.input_tables}"
        )
    template = args.jobcard.read_text(encoding="utf-8")
    jobcard_records: dict[str, object] = {}
    all_candidates: list[Candidate] = []
    with tempfile.TemporaryDirectory(prefix=".gibuu-", dir=args.work_dir) as temporary:
        workspace = Path(temporary)
        histograms = _cached_flux_histograms(
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
        _write_hepevt(selected, args.output, tuple(args.vertex_cm))
        _write_reproducible_archive(workspace, args.native_archive)

    write_yaml(args.resolved_jobcards, {"components": jobcard_records})
    metadata: dict[str, object] = {
        "format": "GiBUU-dk2nu-weighted-resample-to-edep-sim-pbomb",
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
