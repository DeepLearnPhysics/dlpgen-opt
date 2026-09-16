from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .flux_spectra import cached_flux_histograms
from .nuhepmc_cli import convert
from .provenance import checksum, read_yaml, write_yaml
from .validation import validate_nonempty, validate_root


FORMAT = "dlpgen-opt-neut-normalization"
MIN_NATIVE_EVENTS = 20
CC_INDICES = {1, 2, 3, 4, 5, 14, 16, 19, 23, 25, 28, 29}
CCB_INDICES = {1, 2, 3, 4, 5, 11, 15, 17, 20, 23, 25, 28, 29}


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="python -m dlpgen_opt.neut_cli",
        description="Run NEUT from cached canonical dk2nu spectra.",
    )
    command.add_argument("--flux-manifest", type=Path, required=True)
    command.add_argument("--flux-spectra-manifest", type=Path, required=True)
    command.add_argument("--work-dir", type=Path, required=True)
    command.add_argument("--normalization", type=Path, required=True)
    command.add_argument("--native-archive", type=Path, required=True)
    command.add_argument("--resolved-cards", type=Path)
    command.add_argument("--output", type=Path)
    command.add_argument("--metadata-output", type=Path)
    command.add_argument("--events", type=int)
    command.add_argument("--seed", type=int, required=True)
    command.add_argument("--runtime", type=Path, default=Path("/opt/neut-runtime"))
    command.add_argument("--card", type=Path, required=True)
    command.add_argument("--executable", default="neutroot2")
    command.add_argument("--converter", default="neutvect-converter")
    command.add_argument("--target-a", type=int, required=True)
    command.add_argument("--target-z", type=int, required=True)
    command.add_argument("--energy-min-gev", type=float, required=True)
    command.add_argument("--energy-max-gev", type=float, required=True)
    command.add_argument("--energy-bins", type=int, required=True)
    command.add_argument("--mdlqe", type=int, required=True)
    command.add_argument("--mdl2p2h", type=int, required=True)
    command.add_argument("--processes", nargs="+", choices=("cc", "nc"), required=True)
    command.add_argument("--vertex-cm", type=float, nargs=3)
    command.add_argument("--prepare-only", action="store_true")
    return command


def _read_spectrum(path: Path) -> list[float]:
    values: list[float] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = stripped.split()
            if len(fields) != 2:
                raise RuntimeError(f"invalid spectrum row {path}:{line_number}")
            value = float(fields[1])
            if not math.isfinite(value) or value < 0:
                raise RuntimeError(f"invalid spectrum weight {path}:{line_number}")
            values.append(value)
    if not values or sum(values) <= 0:
        raise RuntimeError(f"empty flux spectrum: {path}")
    return values


def histogram_name(pdg: int) -> str:
    return f"flux_{'m' if pdg < 0 else 'p'}{abs(pdg)}"


def write_flux_file(
    histograms: dict[int, dict[str, object]],
    output: Path,
    *,
    energy_min: float,
    energy_max: float,
    bins: int,
) -> dict[int, str]:
    """Translate generator-neutral text spectra into NEUT ROOT histograms."""
    try:
        import ROOT  # type: ignore
    except ImportError as error:
        raise RuntimeError("ROOT is required to write the NEUT flux file") from error
    output.parent.mkdir(parents=True, exist_ok=True)
    root_file = ROOT.TFile.Open(str(output), "RECREATE")
    if not root_file or root_file.IsZombie():
        raise RuntimeError(f"could not create NEUT flux file: {output}")
    names: dict[int, str] = {}
    try:
        for pdg, record in sorted(histograms.items()):
            values = _read_spectrum(Path(record["path"]))
            if len(values) != bins:
                raise RuntimeError(
                    f"NEUT spectrum for PDG {pdg} has {len(values)} bins, "
                    f"expected {bins}"
                )
            name = histogram_name(pdg)
            histogram = ROOT.TH1D(name, name, bins, energy_min, energy_max)
            histogram.SetDirectory(root_file)
            for index, value in enumerate(values, start=1):
                histogram.SetBinContent(index, value)
            histogram.Write()
            names[pdg] = name
        root_file.Write()
    finally:
        root_file.Close()
    validate_nonempty(output)
    return names


def _factors(processes: list[str], antineutrino: bool) -> str:
    cc_indices = CCB_INDICES if antineutrino else CC_INDICES
    values = []
    for index in range(1, 31):
        current = "cc" if index in cc_indices else "nc"
        values.append("1." if current in processes else "0.")
    return " ".join(values)


def render_card(
    base_card: str,
    *,
    events: int,
    pdg: int,
    flux_filename: str,
    flux_histogram: str,
    target_a: int,
    target_z: int,
    processes: list[str],
    mdlqe: int,
    mdl2p2h: int,
) -> str:
    if events <= 0:
        raise ValueError("NEUT event count must be positive")
    if target_z < 0 or target_z > target_a:
        raise ValueError("invalid NEUT target nucleus")
    settings = {
        "EVCT-NEVT": str(events),
        "EVCT-IDPT": str(pdg),
        "EVCT-MPOS": "1",
        "EVCT-POS": "0. 0. 0.",
        "EVCT-MDIR": "1",
        "EVCT-DIR": "0. 0. 1.",
        "EVCT-MPV": "3",
        "EVCT-FILENM": f"'{flux_filename}'",
        "EVCT-HISTNM": f"'{flux_histogram}'",
        "EVCT-INMEV": "0",
        "NEUT-NUMBNDN": str(target_a - target_z),
        "NEUT-NUMBNDP": str(target_z),
        "NEUT-NUMFREP": "0",
        "NEUT-NUMATOM": str(target_a),
        "NEUT-MODE": "0",
        "NEUT-CRS": _factors(processes, False),
        "NEUT-CRSB": _factors(processes, True),
        "NEUT-MDLQE": str(mdlqe),
        "NEUT-MDL2P2H": str(mdl2p2h),
        "NEUT-RAND": "0",
    }
    found: set[str] = set()
    lines = []
    for line in base_card.splitlines():
        stripped = line.lstrip()
        key = stripped.split(maxsplit=1)[0] if stripped else ""
        if key == "NEUT-CRSPATH":
            lines.append("C" + line)
        elif key in settings:
            lines.append(f"{key} {settings[key]}")
            found.add(key)
        else:
            lines.append(line)
    lines.extend(f"{key} {settings[key]}" for key in settings if key not in found)
    return "\n".join(lines) + "\n"


def random_seeds(seed: int, pdg: int, purpose: str) -> tuple[int, ...]:
    payload = f"dlpgen-opt-neut-v1:{seed}:{pdg}:{purpose}".encode()
    digest = hashlib.sha256(payload).digest()
    limits = (900_000_000, 900_000_000, 30_000, 30_000, 30_000)
    return tuple(
        int.from_bytes(digest[4 * index : 4 * index + 4], "big") % limit + 1
        for index, limit in enumerate(limits)
    )


def neut_environment(runtime: Path, ranfile: Path) -> dict[str, str]:
    neut = runtime / "neut"
    libraries = [
        neut / "lib",
        runtime / "root",
        runtime / "nuhepmc" / "lib",
        runtime / "hepmc" / "lib64",
        runtime / "buildbox" / "lib64",
        runtime / "system",
    ]
    environment = dict(os.environ)
    environment.update(
        {
            "NEUT_ROOT": str(neut),
            "NEUT_CARDS": str(neut / "share" / "neut" / "Cards"),
            "NEUT_CRSPATH": str(neut / "share" / "neut" / "crsdat"),
            "RANFILE": str(ranfile),
            "LD_LIBRARY_PATH": os.pathsep.join(str(path) for path in libraries),
            "PATH": str(neut / "bin") + os.pathsep + os.environ.get("PATH", ""),
        }
    )
    return environment


def _executable(runtime: Path, configured: str) -> str:
    path = Path(configured)
    return str(path if path.is_absolute() else runtime / "neut" / "bin" / path)


def run_flavor(
    args: argparse.Namespace,
    *,
    directory: Path,
    flux_file: Path,
    histogram: str,
    pdg: int,
    events: int,
    purpose: str,
) -> tuple[Path, Path, Path]:
    directory.mkdir(parents=True, exist_ok=False)
    local_flux = directory / "flux.root"
    try:
        os.link(flux_file, local_flux)
    except OSError:
        shutil.copy2(flux_file, local_flux)
    card = directory / "NEUT.card"
    card.write_text(
        render_card(
            args.card.read_text(encoding="utf-8"),
            events=events,
            pdg=pdg,
            flux_filename=local_flux.name,
            flux_histogram=histogram,
            target_a=args.target_a,
            target_z=args.target_z,
            processes=args.processes,
            mdlqe=args.mdlqe,
            mdl2p2h=args.mdl2p2h,
        ),
        encoding="utf-8",
    )
    ranfile = directory / "random.tbl"
    ranfile.write_text(
        " ".join(str(value) for value in random_seeds(args.seed, pdg, purpose))
        + "\n",
        encoding="utf-8",
    )
    native = directory / "neutvect.root"
    hepmc = directory / "events.hepmc3"
    environment = neut_environment(args.runtime, ranfile)
    subprocess.run(
        [_executable(args.runtime, args.executable), str(card), str(native)],
        cwd=directory,
        env=environment,
        check=True,
    )
    validate_nonempty(native)
    subprocess.run(
        [
            _executable(args.runtime, args.converter),
            "-i",
            native.name,
            "-f",
            f"{local_flux.name},{histogram}",
            "-o",
            str(hepmc),
        ],
        cwd=directory,
        env=environment,
        check=True,
    )
    validate_nonempty(hepmc)
    return card, native, hepmc


def flux_averaged_cross_section(path: Path) -> tuple[float, str, str]:
    try:
        import pyhepmc
    except ImportError as error:
        raise RuntimeError("pyhepmc is required to inspect NEUT output") from error
    with pyhepmc.open(str(path)) as stream:
        event = next(iter(stream), None)
    if event is None or event.run_info is None:
        raise RuntimeError(f"NEUT probe contains no run information: {path}")
    attributes = event.run_info.attributes
    key = "NuHepMC.FluxAveragedTotalCrossSection"
    if key not in attributes:
        raise RuntimeError(f"NEUT probe is missing {key}: {path}")
    value = float(str(attributes[key]).strip())
    unit_key = "NuHepMC.Units.CrossSection.Unit"
    scale_key = "NuHepMC.Units.CrossSection.TargetScale"
    unit = str(attributes[unit_key]).strip() if unit_key in attributes else ""
    scale = str(attributes[scale_key]).strip() if scale_key in attributes else ""
    if not math.isfinite(value) or value <= 0 or not unit or not scale:
        raise RuntimeError(f"NEUT probe has invalid cross-section metadata: {path}")
    return value, unit, scale


def _archive(directory: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with tarfile.open(temporary, "w:gz") as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(directory))
    temporary.replace(output)


def prepare_normalization(
    args: argparse.Namespace,
    histograms: dict[int, dict[str, object]],
    flux_file: Path,
    names: dict[int, str],
    staging: Path,
) -> dict[str, object]:
    records: dict[str, object] = {}
    common_unit: str | None = None
    common_scale: str | None = None
    probes = staging / "probes"
    for pdg, record in sorted(histograms.items()):
        _, _, hepmc = run_flavor(
            args,
            directory=probes / str(pdg),
            flux_file=flux_file,
            histogram=names[pdg],
            pdg=pdg,
            events=native_event_count(1),
            purpose="normalization",
        )
        cross_section, unit, scale = flux_averaged_cross_section(hepmc)
        if common_unit is None:
            common_unit, common_scale = unit, scale
        elif (unit, scale) != (common_unit, common_scale):
            raise RuntimeError(
                "NEUT flavor probes use inconsistent cross-section units"
            )
        integral = float(record["integral_per_cm2"])
        records[str(pdg)] = {
            "flux_integral_per_cm2": integral,
            "flux_averaged_cross_section": cross_section,
            "rate_weight": integral * cross_section,
            "probe_sha256": checksum(hepmc),
        }
    result: dict[str, object] = {
        "schema_version": 1,
        "format": FORMAT,
        "canonical_manifest_sha256": checksum(args.flux_manifest),
        "spectra_manifest_sha256": checksum(args.flux_spectra_manifest),
        "base_card_sha256": checksum(args.card),
        "target": {"a": args.target_a, "z": args.target_z},
        "processes": args.processes,
        "models": {"mdlqe": args.mdlqe, "mdl2p2h": args.mdl2p2h},
        "cross_section_unit": common_unit,
        "cross_section_target_scale": common_scale,
        "flavors": records,
    }
    args.normalization.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(args.normalization, result)
    _archive(probes, args.native_archive)
    return result


def allocate_events(normalization: dict, events: int, seed: int) -> dict[int, int]:
    if events <= 0:
        raise ValueError("NEUT event count must be positive")
    pdgs = sorted(int(pdg) for pdg in normalization.get("flavors", {}))
    weights = [
        float(normalization["flavors"][str(pdg)]["rate_weight"]) for pdg in pdgs
    ]
    if not pdgs or any(not math.isfinite(value) or value < 0 for value in weights):
        raise RuntimeError("invalid NEUT flavor normalization")
    if sum(weights) <= 0:
        raise RuntimeError("NEUT flavor normalization has zero total rate")
    allocation = {pdg: 0 for pdg in pdgs}
    for pdg in random.Random(seed).choices(pdgs, weights=weights, k=events):
        allocation[pdg] += 1
    return allocation


def native_event_count(requested: int) -> int:
    """Avoid NEUT 5.8.0's MOD(I, INT(NEVT/20)) zero divisor on x86."""
    if requested <= 0:
        raise ValueError("requested NEUT event count must be positive")
    return max(requested, MIN_NATIVE_EVENTS)


def _merge_root_files(inputs: list[Path], output: Path) -> None:
    try:
        import ROOT  # type: ignore
    except ImportError as error:
        raise RuntimeError("ROOT is required to merge NEUT RooTracker files") from error
    output.parent.mkdir(parents=True, exist_ok=True)
    merger = ROOT.TFileMerger(False, False)
    if not merger.OutputFile(str(output), "RECREATE"):
        raise RuntimeError(f"could not create merged RooTracker output: {output}")
    for path in inputs:
        if not merger.AddFile(str(path)):
            raise RuntimeError(f"could not add RooTracker input: {path}")
    if not merger.Merge():
        raise RuntimeError("failed to merge NEUT RooTracker files")
    validate_root(output, "gRooTracker")


def run(args: argparse.Namespace) -> dict[str, object]:
    args.work_dir.mkdir(parents=True, exist_ok=True)
    histograms = cached_flux_histograms(
        args.flux_spectra_manifest,
        args.flux_manifest,
        energy_min=args.energy_min_gev,
        energy_max=args.energy_max_gev,
        bins=args.energy_bins,
    )
    # NEUT stores RANFILE in a fixed-width Fortran character buffer. Production
    # paths can easily exceed that historical limit, so all native work happens
    # under a short /tmp path and is archived into the durable job directory.
    staging = Path(tempfile.mkdtemp(prefix="dlpgen-neut-"))
    flux_file = staging / "flux.root"
    names = write_flux_file(
        histograms,
        flux_file,
        energy_min=args.energy_min_gev,
        energy_max=args.energy_max_gev,
        bins=args.energy_bins,
    )
    if args.prepare_only:
        result = prepare_normalization(args, histograms, flux_file, names, staging)
        shutil.rmtree(staging)
        return result

    if not all((args.output, args.metadata_output, args.resolved_cards, args.events)):
        raise ValueError("NEUT generation requires output, metadata, cards, and events")
    normalization = read_yaml(args.normalization)
    if normalization.get("format") != FORMAT:
        raise RuntimeError(f"invalid NEUT normalization: {args.normalization}")
    expected = {
        "canonical_manifest_sha256": checksum(args.flux_manifest),
        "spectra_manifest_sha256": checksum(args.flux_spectra_manifest),
        "base_card_sha256": checksum(args.card),
    }
    for key, value in expected.items():
        if normalization.get(key) != value:
            raise RuntimeError(f"NEUT normalization has mismatched {key}")
    expected_configuration = {
        "target": {"a": args.target_a, "z": args.target_z},
        "processes": args.processes,
        "models": {"mdlqe": args.mdlqe, "mdl2p2h": args.mdl2p2h},
    }
    for key, value in expected_configuration.items():
        if normalization.get(key) != value:
            raise RuntimeError(f"NEUT normalization has mismatched {key}")
    expected_flavors = {str(pdg) for pdg in histograms}
    normalized_flavors = set(normalization.get("flavors", {}))
    if normalized_flavors != expected_flavors:
        raise RuntimeError("NEUT normalization has mismatched flavors")
    allocation = allocate_events(normalization, args.events, args.seed)
    root_inputs: list[Path] = []
    conversions: list[dict[str, object]] = []
    cards: dict[str, object] = {}
    output_event_offset = 0
    generated = staging / "generated"
    for pdg, count in allocation.items():
        if count == 0:
            continue
        card, native, hepmc = run_flavor(
            args,
            directory=generated / str(pdg),
            flux_file=flux_file,
            histogram=names[pdg],
            pdg=pdg,
            events=native_event_count(count),
            purpose="events",
        )
        converted = generated / str(pdg) / "events.gtrac.root"
        conversion_path = generated / str(pdg) / "conversion.json"
        conversion = convert(
            hepmc,
            converted,
            conversion_path,
            events=count,
            output_event_offset=output_event_offset,
            vertex_cm=tuple(args.vertex_cm),
        )
        output_event_offset += count
        root_inputs.append(converted)
        conversions.append(conversion)
        cards[str(pdg)] = {
            "events": count,
            "native_events": native_event_count(count),
            "card_sha256": checksum(card),
            "native_sha256": checksum(native),
            "nuhepmc_sha256": checksum(hepmc),
        }
    _merge_root_files(root_inputs, args.output)
    _archive(generated, args.native_archive)
    write_yaml(
        args.resolved_cards,
        {
            "schema_version": 1,
            "format": "dlpgen-opt-neut-resolved-cards",
            "base_card": str(args.card),
            "base_card_sha256": checksum(args.card),
            "flavors": cards,
        },
    )
    metadata: dict[str, object] = {
        "format": "NEUT-NuHepMC-to-edep-sim-RooTracker",
        "events": args.events,
        "seed": args.seed,
        "flavor_allocation": {str(pdg): count for pdg, count in allocation.items()},
        "normalization_sha256": checksum(args.normalization),
        "generator_tools": (
            conversions[0].get("generator_tools", []) if conversions else []
        ),
        "process_ids": [
            process_id
            for conversion in conversions
            for process_id in conversion.get("process_ids", [])
        ],
        "particles_written": sum(
            int(conversion.get("particles_written", 0)) for conversion in conversions
        ),
        "nuclear_remnants_skipped": sum(
            int(conversion.get("nuclear_remnants_skipped", 0))
            for conversion in conversions
        ),
        "native_archive_sha256": checksum(args.native_archive),
        "output_sha256": checksum(args.output),
        "vertex_cm": args.vertex_cm,
    }
    args.metadata_output.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_output.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    shutil.rmtree(staging)
    return metadata


def main() -> None:
    args = parser().parse_args()
    print(json.dumps(run(args), sort_keys=True))


if __name__ == "__main__":
    main()
