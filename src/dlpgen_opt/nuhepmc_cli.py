from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import islice
from pathlib import Path


NUCLEAR_REMNANT_PDG = 2_009_900_000
# GiBUU 2025 writes 200990000 in its native NuHepMC implementation while the
# NuHepMC convention reserves 2009900000. Treat both as non-transportable
# bookkeeping particles and retain the exact native record for provenance.
GIBUU_NUCLEAR_REMNANT_PDG = 200_990_000
NUHEPMC_VERSION_KEYS = (
    "NuHepMC.Version.Major",
    "NuHepMC.Version.Minor",
    "NuHepMC.Version.Patch",
)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="python -m dlpgen_opt.nuhepmc_cli",
        description="Convert NuHepMC final states to edep-sim's pbomb HEPEVT input.",
    )
    command.add_argument("input", type=Path, help="NuHepMC/HepMC3 event vector")
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--metadata-output", type=Path, required=True)
    command.add_argument("--events", type=int, required=True)
    command.add_argument("--skip", type=int, default=0)
    command.add_argument("--vertex-cm", type=float, nargs=3, default=(0.0, 0.0, 0.0))
    return command


def _attribute_text(attribute: object) -> str:
    """Return the payload of both parsed and HepMC ``UnparsedAttribute`` values."""
    return str(attribute).strip()


def _nuhepmc_version(event: object) -> tuple[int, int, int]:
    run_info = event.run_info
    if run_info is None:
        raise RuntimeError("NuHepMC event has no GenRunInfo")
    missing_run = [
        key for key in NUHEPMC_VERSION_KEYS if key not in run_info.attributes
    ]
    if missing_run:
        raise RuntimeError(f"NuHepMC GenRunInfo is missing {missing_run}")
    try:
        return tuple(
            int(_attribute_text(run_info.attributes[key]))
            for key in NUHEPMC_VERSION_KEYS
        )
    except ValueError as error:
        raise RuntimeError("NuHepMC version attributes must be integers") from error


def _event_metadata(event: object) -> tuple[int, list[float]]:
    version = _nuhepmc_version(event)
    # GiBUU 2025 implements NuHepMC 0.9, before LabPos was renamed to
    # lab_pos. Its process identifier is likewise emitted as ProcID.
    process_key = "signal_process_id" if version[0] >= 1 else "ProcID"
    position_key = "lab_pos" if version[0] >= 1 else "LabPos"
    missing_event = [
        key for key in (process_key, position_key) if key not in event.attributes
    ]
    if missing_event:
        raise RuntimeError(
            f"NuHepMC event {event.event_number} is missing {missing_event}"
        )
    if event.event_number < 0:
        raise RuntimeError("NuHepMC event numbers must be non-negative")
    try:
        process_id = int(_attribute_text(event.attributes[process_key]))
        lab_position = [
            float(value)
            for value in _attribute_text(event.attributes[position_key]).split()
        ]
    except ValueError as error:
        raise RuntimeError(
            f"NuHepMC event {event.event_number} has invalid event metadata"
        ) from error
    if len(lab_position) not in (3, 4) or not all(
        math.isfinite(value) for value in lab_position
    ):
        raise RuntimeError(
            f"NuHepMC event {event.event_number} has an invalid lab position"
        )
    return process_id, lab_position


@contextmanager
def _selected_hepmc_input(
    input_path: Path,
    directory: Path,
    *,
    skip: int,
    events: int,
) -> Iterator[tuple[Path, int]]:
    """Select and normalize Asciiv3 records for HepMC3's reader.

    GiBUU 2025 writes valid whitespace-separated Asciiv3 particle records with
    leading blanks in positive Fortran fields. The HepMC3 3.02 reader bundled
    by pyhepmc treats those repeated blanks as empty columns. Restricting the
    normalization to ``P`` records preserves string-valued run metadata. Copy
    only the requested event range so large shared vectors are not duplicated
    in full by every production job.
    """
    with input_path.open("rb") as source:
        is_ascii_v3 = source.read(128).startswith(b"HepMC::Version")
    if not is_ascii_v3:
        yield input_path, skip
        return

    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=directory,
        prefix=".nuhepmc-normalized-",
        suffix=".hepmc3",
        delete=False,
    )
    normalized = Path(handle.name)
    try:
        with input_path.open("r", encoding="utf-8") as source, handle:
            event_index = -1
            copy_event = False
            for line in source:
                if line.startswith("HepMC::Asciiv3-END_EVENT_LISTING"):
                    break
                if line.startswith("E "):
                    event_index += 1
                    if event_index >= skip + events:
                        break
                    copy_event = event_index >= skip
                if event_index >= 0 and not copy_event:
                    continue
                if line.startswith("P "):
                    line = " ".join(line.split()) + "\n"
                handle.write(line)
            handle.write("HepMC::Asciiv3-END_EVENT_LISTING\n")
        yield normalized, 0
    finally:
        normalized.unlink(missing_ok=True)


def _momentum_scale(event: object, pyhepmc: object) -> float:
    if event.momentum_unit == pyhepmc.Units.GEV:
        return 1.0
    if event.momentum_unit == pyhepmc.Units.MEV:
        return 1.0e-3
    raise RuntimeError(f"unsupported HepMC momentum unit: {event.momentum_unit}")


def convert(
    input_path: Path,
    output_path: Path,
    metadata_path: Path,
    *,
    events: int,
    skip: int = 0,
    vertex_cm: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> dict[str, object]:
    if events <= 0:
        raise ValueError("events must be positive")
    if skip < 0:
        raise ValueError("skip must be non-negative")
    if not all(math.isfinite(value) for value in vertex_cm):
        raise ValueError("vertex coordinates must be finite")
    try:
        import pyhepmc
    except ImportError as error:
        raise RuntimeError(
            "pyhepmc is required to read NuHepMC event vectors"
        ) from error

    output_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    source_event_numbers: list[int] = []
    process_ids: list[int] = []
    lab_positions: list[list[float]] = []
    event_weights: list[list[float]] = []
    weight_names: list[str] | None = None
    generator_tools: list[dict[str, str]] | None = None
    particles_written = 0
    remnants_skipped = 0
    seen_event_numbers: set[int] = set()
    try:
        with _selected_hepmc_input(
            input_path,
            output_path.parent,
            skip=skip,
            events=events,
        ) as (readable, reader_skip):
            with pyhepmc.open(str(readable)) as source, temporary.open(
                "w", encoding="utf-8"
            ) as output:
                selected = islice(source, reader_skip, reader_skip + events)
                for output_event, event in enumerate(selected):
                    process_id, lab_position = _event_metadata(event)
                    if event.event_number in seen_event_numbers:
                        raise RuntimeError(
                            f"duplicate NuHepMC event number: {event.event_number}"
                        )
                    seen_event_numbers.add(event.event_number)
                    source_event_numbers.append(event.event_number)
                    process_ids.append(process_id)
                    lab_positions.append(lab_position)
                    current_weight_names = list(event.run_info.weight_names)
                    if weight_names is None:
                        weight_names = current_weight_names
                    elif weight_names != current_weight_names:
                        raise RuntimeError(
                            "NuHepMC weight names changed within the event vector"
                        )
                    current_tools = [
                        {
                            "name": tool.name,
                            "version": tool.version,
                            "description": tool.description,
                        }
                        for tool in event.run_info.tools
                    ]
                    if generator_tools is None:
                        generator_tools = current_tools
                    elif generator_tools != current_tools:
                        raise RuntimeError(
                            "NuHepMC generator tools changed within the event vector"
                        )
                    event_weights.append(list(event.weights))
                    scale = _momentum_scale(event, pyhepmc)
                    final_state = []
                    for particle in event.particles:
                        if particle.status != 1:
                            continue
                        if particle.pid in (
                            NUCLEAR_REMNANT_PDG,
                            GIBUU_NUCLEAR_REMNANT_PDG,
                        ):
                            remnants_skipped += 1
                            continue
                        final_state.append(particle)
                    if not final_state:
                        raise RuntimeError(
                            f"NuHepMC event {event.event_number} has no transportable "
                            "status-1 final-state particles"
                        )
                    x, y, z = vertex_cm
                    output.write(
                        f"{output_event} 0 {len(final_state)} "
                        f"{x:.12g} {y:.12g} {z:.12g} 0\n"
                    )
                    for particle in final_state:
                        momentum = particle.momentum
                        mass = (
                            particle.generated_mass
                            if particle.is_generated_mass_set()
                            else momentum.m()
                        )
                        values = (
                            momentum.px * scale,
                            momentum.py * scale,
                            momentum.pz * scale,
                            momentum.e * scale,
                            abs(mass) * scale,
                        )
                        output.write(
                            "1 {} 0 0 0 0 {}\n".format(
                                particle.pid,
                                " ".join(f"{value:.12g}" for value in values),
                            )
                        )
                        particles_written += 1
        if len(source_event_numbers) != events:
            raise RuntimeError(
                f"requested {events} NuHepMC events after offset {skip}, "
                f"but found {len(source_event_numbers)}"
            )
        temporary.replace(output_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    metadata: dict[str, object] = {
        "format": "NuHepMC-to-edep-sim-pbomb",
        "input": str(input_path),
        "output": str(output_path),
        "event_offset": skip,
        "events": len(source_event_numbers),
        "source_event_numbers": source_event_numbers,
        "process_ids": process_ids,
        "lab_positions": lab_positions,
        "weight_names": weight_names or [],
        "event_weights": event_weights,
        "generator_tools": generator_tools or [],
        "particles_written": particles_written,
        "nuclear_remnants_skipped": remnants_skipped,
        "vertex_cm": list(vertex_cm),
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        metadata = convert(
            args.input,
            args.output,
            args.metadata_output,
            events=args.events,
            skip=args.skip,
            vertex_cm=tuple(args.vertex_cm),
        )
        print(json.dumps(metadata, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
