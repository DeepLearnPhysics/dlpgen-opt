from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import sys
from array import array
from pathlib import Path

from .provenance import checksum, write_yaml
from .sources.genie import flux_files
from .validation import validate_nonempty


ALGORITHM = "dk2nu-calcEnuWgt-uniform-rectangular-window-v1"
RAY_DISK_RADIUS_CM = 100.0


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="python -m dlpgen_opt.flux_cli",
        description="Materialize deterministic dk2nu beam throws.",
    )
    command.add_argument("--flux-pattern", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--manifest-output", type=Path, required=True)
    command.add_argument("--distance-m", type=float, required=True)
    command.add_argument("--center-m", type=float, nargs=2, default=(0.0, 0.0))
    command.add_argument("--window-size-m", type=float, nargs=2, default=(1.0, 1.0))
    command.add_argument(
        "--flavors", type=int, nargs="+", default=(12, -12, 14, -14)
    )
    command.add_argument("--seed", type=int, required=True)
    command.add_argument("--throws-per-decay", type=int, default=1)
    command.add_argument("--max-decays", type=int)
    command.add_argument("--max-files", type=int)
    command.add_argument("--target-pot", type=float)
    command.add_argument(
        "--checksum-inputs",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return command


def _uniform(seed: int, file_index: int, entry: int, replica: int, axis: int) -> float:
    payload = f"{seed}:{file_index}:{entry}:{replica}:{axis}".encode("ascii")
    value = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    return value / 2**64


def _window_point_cm(
    *,
    seed: int,
    file_index: int,
    entry: int,
    replica: int,
    distance_m: float,
    center_m: tuple[float, float],
    window_size_m: tuple[float, float],
) -> tuple[float, float, float]:
    x = center_m[0] + window_size_m[0] * (
        _uniform(seed, file_index, entry, replica, 0) - 0.5
    )
    y = center_m[1] + window_size_m[1] * (
        _uniform(seed, file_index, entry, replica, 1) - 0.5
    )
    return 100.0 * x, 100.0 * y, 100.0 * distance_m


def _direction(
    point_cm: tuple[float, float, float],
    decay_cm: tuple[float, float, float],
) -> tuple[float, float, float]:
    delta = tuple(point - decay for point, decay in zip(point_cm, decay_cm))
    length = math.sqrt(sum(value * value for value in delta))
    if not math.isfinite(length) or length <= 0:
        raise RuntimeError("dk2nu decay and sampled detector point coincide")
    return tuple(value / length for value in delta)


def _flux_weights(
    raw_ray_weight: float,
    importance_weight: float,
    direction_z: float,
    throws_per_decay: int,
) -> tuple[float, float]:
    # calcEnuWgt returns the probability through a 100 cm-radius disk normal
    # to the ray. Convert it to a density, include the beam-simulation
    # importance weight, and project it onto the configured z-normal plane.
    ray_density = (
        raw_ray_weight
        * importance_weight
        / (math.pi * RAY_DISK_RADIUS_CM * RAY_DISK_RADIUS_CM)
    )
    plane_density = ray_density * abs(direction_z) / throws_per_decay
    return ray_density, plane_density


def _load_root() -> object:
    try:
        import ROOT  # type: ignore
    except ImportError as error:
        raise RuntimeError("ROOT and the dk2nu dictionaries are required") from error
    if ROOT.gSystem.Load("libdk2nuTree") < 0:
        raise RuntimeError("could not load libdk2nuTree")
    return ROOT


def catalog_digest(paths: list[Path]) -> str:
    """Identify an immutable/path-versioned catalog without reading payloads."""
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path).encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
    return digest.hexdigest()


def select_flux_files(
    paths: list[Path], seed: int, max_files: int | None, *, sample: bool = False
) -> list[Path]:
    """Choose a deterministic, uniform file sample independent of glob ordering."""
    if not sample and (max_files is None or max_files >= len(paths)):
        return list(paths)
    ranked = sorted(
        paths,
        key=lambda path: (
            hashlib.sha256(f"{seed}:{path}".encode("utf-8")).digest(),
            str(path),
        ),
    )
    return ranked if max_files is None else ranked[:max_files]


def _input_metadata(
    ROOT: object,
    paths: list[Path],
    checksum_inputs: bool,
    target_pot: float | None,
) -> tuple[list[Path], list[dict[str, object]], int, float]:
    selected: list[Path] = []
    records: list[dict[str, object]] = []
    total_entries = 0
    total_pot = 0.0
    for path in paths:
        root_file = ROOT.TFile.Open(str(path), "READ")
        if not root_file or root_file.IsZombie():
            raise RuntimeError(f"could not open dk2nu input: {path}")
        events = root_file.Get("dk2nuTree")
        metadata = root_file.Get("dkmetaTree")
        if not events or not metadata or metadata.GetEntries() < 1:
            root_file.Close()
            raise RuntimeError(f"missing dk2nuTree or dkmetaTree: {path}")
        entries = int(events.GetEntries())
        pots = 0.0
        jobs: list[int] = []
        for index in range(int(metadata.GetEntries())):
            metadata.GetEntry(index)
            pots += float(metadata.dkmeta.pots)
            jobs.append(int(metadata.dkmeta.job))
        record: dict[str, object] = {
            "path": str(path),
            "bytes": path.stat().st_size,
            "entries": entries,
            "dkmeta_entries": int(metadata.GetEntries()),
            "jobs": jobs,
            "pots": pots,
        }
        if checksum_inputs:
            record["sha256"] = checksum(path)
        else:
            record["checksum"] = "skipped"
        records.append(record)
        selected.append(path)
        total_entries += entries
        total_pot += pots
        root_file.Close()
        if target_pot is not None and total_pot >= target_pot:
            break
    return selected, records, total_entries, total_pot


def _branch_buffers(tree: object) -> dict[str, array]:
    definitions = {
        "throw_id": ("q", "L"),
        "source_file_index": ("i", "I"),
        "source_entry": ("q", "L"),
        "source_job": ("i", "I"),
        "source_potnum": ("i", "I"),
        "source_job_index": ("i", "I"),
        "pdg": ("i", "I"),
        "parent_pdg": ("i", "I"),
        "decay_mode": ("i", "I"),
        "energy_gev": ("d", "D"),
        "dir_x": ("d", "D"),
        "dir_y": ("d", "D"),
        "dir_z": ("d", "D"),
        "position_x_m": ("d", "D"),
        "position_y_m": ("d", "D"),
        "position_z_m": ("d", "D"),
        "decay_x_cm": ("d", "D"),
        "decay_y_cm": ("d", "D"),
        "decay_z_cm": ("d", "D"),
        "importance_weight": ("d", "D"),
        "ray_weight_per_cm2": ("d", "D"),
        "flux_weight_per_cm2": ("d", "D"),
    }
    buffers: dict[str, array] = {}
    for name, (kind, leaf) in definitions.items():
        value = array(kind, [0])
        tree.Branch(name, value, f"{name}/{leaf}")
        buffers[name] = value
    return buffers


def _fill(buffers: dict[str, array], values: dict[str, int | float]) -> None:
    for name, value in values.items():
        buffers[name][0] = value


def materialize(
    *,
    flux_pattern: Path,
    output: Path,
    manifest_output: Path,
    distance_m: float,
    center_m: tuple[float, float],
    window_size_m: tuple[float, float],
    flavors: list[int],
    seed: int,
    throws_per_decay: int = 1,
    max_decays: int | None = None,
    max_files: int | None = None,
    target_pot: float | None = None,
    checksum_inputs: bool = True,
    input_paths: list[Path] | None = None,
) -> dict[str, object]:
    if distance_m <= 0 or not math.isfinite(distance_m):
        raise ValueError("distance_m must be finite and positive")
    if any(not math.isfinite(value) for value in (*center_m, *window_size_m)):
        raise ValueError("window coordinates must be finite")
    if any(value <= 0 for value in window_size_m):
        raise ValueError("window_size_m values must be positive")
    if not flavors or len(flavors) != len(set(flavors)):
        raise ValueError("flavors must be non-empty and unique")
    if any(abs(pdg) not in (12, 14, 16) for pdg in flavors):
        raise ValueError("flavors must contain only neutrino PDG codes")
    if seed < 0:
        raise ValueError("seed must be non-negative")
    if throws_per_decay <= 0:
        raise ValueError("throws_per_decay must be positive")
    if max_decays is not None and max_decays <= 0:
        raise ValueError("max_decays must be positive")
    if max_files is not None and max_files <= 0:
        raise ValueError("max_files must be positive")
    if target_pot is not None and (
        not math.isfinite(target_pot) or target_pot <= 0
    ):
        raise ValueError("target_pot must be finite and positive")
    if output.exists() or manifest_output.exists():
        raise RuntimeError("refusing to overwrite an existing flux product")

    catalog_paths = list(input_paths) if input_paths is not None else flux_files(flux_pattern)
    paths = select_flux_files(
        catalog_paths,
        seed,
        max_files,
        sample=max_files is not None or target_pot is not None,
    )
    if not paths:
        raise RuntimeError(f"flux pattern matched no files: {flux_pattern}")
    ROOT = _load_root()
    paths, input_records, total_entries, total_pot = _input_metadata(
        ROOT, paths, checksum_inputs, target_pot
    )
    if not math.isfinite(total_pot) or total_pot < 0:
        raise RuntimeError("dk2nu metadata contains invalid simulated POT")
    if target_pot is not None and total_pot < target_pot:
        raise RuntimeError(
            f"selected flux files contain {total_pot} POT, below target {target_pot}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    root_file = ROOT.TFile.Open(str(temporary), "RECREATE")
    if not root_file or root_file.IsZombie():
        raise RuntimeError(f"could not create flux output: {temporary}")
    tree = ROOT.TTree("fluxThrows", "Canonical dk2nu detector-window beam throws")
    buffers = _branch_buffers(tree)
    flavors_set = set(flavors)
    flavor_summary = {
        pdg: {"decays": 0, "throws": 0, "sum_flux_weight_per_cm2": 0.0}
        for pdg in flavors
    }
    decays_scanned = 0
    throws_written = 0
    try:
        for file_index, path in enumerate(paths):
            source = ROOT.TFile.Open(str(path), "READ")
            try:
                events = source.Get("dk2nuTree")
                for source_entry in range(int(events.GetEntries())):
                    if max_decays is not None and decays_scanned >= max_decays:
                        break
                    events.GetEntry(source_entry)
                    record = events.dk2nu
                    decay = record.decay
                    decays_scanned += 1
                    pdg = int(decay.ntype)
                    if pdg not in flavors_set:
                        continue
                    flavor_summary[pdg]["decays"] += 1
                    decay_position = (
                        float(decay.vx),
                        float(decay.vy),
                        float(decay.vz),
                    )
                    for replica in range(throws_per_decay):
                        point = _window_point_cm(
                            seed=seed,
                            file_index=file_index,
                            entry=source_entry,
                            replica=replica,
                            distance_m=distance_m,
                            center_m=center_m,
                            window_size_m=window_size_m,
                        )
                        direction = _direction(point, decay_position)
                        energy = ctypes.c_double()
                        ray_weight = ctypes.c_double()
                        status = ROOT.bsim.calcEnuWgt(
                            record, ROOT.TVector3(*point), energy, ray_weight
                        )
                        if status:
                            raise RuntimeError(
                                f"calcEnuWgt failed with {status} for "
                                f"file {file_index}, entry {source_entry}"
                            )
                        importance_weight = float(decay.nimpwt)
                        ray_density, flux_weight = _flux_weights(
                            ray_weight.value,
                            importance_weight,
                            direction[2],
                            throws_per_decay,
                        )
                        if (
                            not all(
                                math.isfinite(value)
                                for value in (
                                    energy.value,
                                    importance_weight,
                                    ray_density,
                                    flux_weight,
                                )
                            )
                            or energy.value <= 0
                            or importance_weight < 0
                            or ray_density < 0
                            or flux_weight < 0
                        ):
                            raise RuntimeError("dk2nu produced an invalid throw")
                        _fill(
                            buffers,
                            {
                                "throw_id": throws_written,
                                "source_file_index": file_index,
                                "source_entry": source_entry,
                                "source_job": int(record.job),
                                "source_potnum": int(record.potnum),
                                "source_job_index": int(record.jobindx),
                                "pdg": pdg,
                                "parent_pdg": int(decay.ptype),
                                "decay_mode": int(decay.ndecay),
                                "energy_gev": energy.value,
                                "dir_x": direction[0],
                                "dir_y": direction[1],
                                "dir_z": direction[2],
                                "position_x_m": point[0] / 100.0,
                                "position_y_m": point[1] / 100.0,
                                "position_z_m": point[2] / 100.0,
                                "decay_x_cm": decay_position[0],
                                "decay_y_cm": decay_position[1],
                                "decay_z_cm": decay_position[2],
                                "importance_weight": importance_weight,
                                "ray_weight_per_cm2": ray_density,
                                "flux_weight_per_cm2": flux_weight,
                            },
                        )
                        tree.Fill()
                        throws_written += 1
                        flavor_summary[pdg]["throws"] += 1
                        flavor_summary[pdg][
                            "sum_flux_weight_per_cm2"
                        ] += flux_weight
            finally:
                source.Close()
            if max_decays is not None and decays_scanned >= max_decays:
                break
        if throws_written < 1:
            raise RuntimeError("no selected neutrino throws were produced")
        root_metadata = {
            "schema_version": 1,
            "algorithm": ALGORITHM,
            "units": {
                "energy": "GeV",
                "position": "m",
                "decay_position": "cm",
                "flux_weight": "cm^-2",
            },
            "window": {
                "distance_m": distance_m,
                "center_m": list(center_m),
                "size_m": list(window_size_m),
            },
            "seed": seed,
            "throws_per_decay": throws_per_decay,
        }
        root_file.cd()
        tree.Write()
        ROOT.TNamed(
            "fluxThrowMetadata", json.dumps(root_metadata, sort_keys=True)
        ).Write()
        root_file.Close()
        temporary.replace(output)
    except BaseException:
        root_file.Close()
        temporary.unlink(missing_ok=True)
        raise

    complete_selection = decays_scanned == total_entries
    complete_catalog = complete_selection and len(paths) == len(catalog_paths)
    for summary in flavor_summary.values():
        summary["flux_per_cm2_per_pot"] = (
            summary["sum_flux_weight_per_cm2"] / total_pot
            if complete_selection and total_pot > 0
            else None
        )
    manifest: dict[str, object] = {
        "schema_version": 1,
        "format": "dlpgen-opt-dk2nu-flux-throws",
        "algorithm": ALGORITHM,
        "inputs": input_records,
        "input_catalog": {
            "pattern": str(flux_pattern),
            "files": len(catalog_paths),
            "paths_sha256": catalog_digest(catalog_paths),
            "selected_files": len(paths),
            "entries": total_entries,
            "simulated_pot": total_pot,
        },
        "window": {
            "distance_m": distance_m,
            "center_m": list(center_m),
            "size_m": list(window_size_m),
            "coordinate_system": "dk2nu beam coordinates",
        },
        "sampling": {
            "seed": seed,
            "throws_per_decay": throws_per_decay,
            "max_decays": max_decays,
            "max_files": max_files,
            "target_pot": target_pot,
            "decays_scanned": decays_scanned,
            "throws_written": throws_written,
            "complete_selection": complete_selection,
            "complete_catalog": complete_catalog,
            "file_selection": (
                "sha256-ranked-uniform-sample"
                if max_files is not None or target_pot is not None
                else "complete-sorted-catalog"
            ),
        },
        "flavors": flavor_summary,
        "normalization": {
            "weight_branch": "flux_weight_per_cm2",
            "weight_includes": [
                "dk2nu decay importance weight",
                "detector-plane tilt projection",
                "inverse throws_per_decay",
            ],
            "selected_pot": total_pot,
            "flux_per_pot_valid_for_complete_selected_files": True,
            "valid_per_selected_pot": complete_selection and total_pot > 0,
        },
        "output": validate_nonempty(output),
    }
    try:
        write_yaml(manifest_output, manifest)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return manifest


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        manifest = materialize(
            flux_pattern=args.flux_pattern,
            output=args.output,
            manifest_output=args.manifest_output,
            distance_m=args.distance_m,
            center_m=tuple(args.center_m),
            window_size_m=tuple(args.window_size_m),
            flavors=list(args.flavors),
            seed=args.seed,
            throws_per_decay=args.throws_per_decay,
            max_decays=args.max_decays,
            max_files=args.max_files,
            target_pot=args.target_pot,
            checksum_inputs=args.checksum_inputs,
        )
        print(json.dumps(manifest, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
