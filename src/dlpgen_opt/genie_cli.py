from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree


CONFIG_NAME = "dlpgen_flux"


def write_flux_config(
    path: Path,
    *,
    distance_m: float,
    center_m: tuple[float, float],
    window_size_m: tuple[float, float],
    max_energy_gev: float,
    max_weight_scan_entries: int,
) -> None:
    root = ElementTree.Element("gdk2nu_config")
    params = ElementTree.SubElement(root, "param_set", name=CONFIG_NAME)
    ElementTree.SubElement(params, "verbose").text = "0"
    ElementTree.SubElement(params, "lunits").text = "m"
    direction = ElementTree.SubElement(params, "beamdir", type="newxyz")
    direction.text = "1 0 0  0 1 0  0 0 1"
    ElementTree.SubElement(params, "beampos").text = "0 0 0"

    cx, cy = center_m
    width, height = window_size_m
    x0, y0 = cx - width / 2.0, cy - height / 2.0
    window = ElementTree.SubElement(params, "window")
    for point in (
        (x0, y0, distance_m),
        (x0 + width, y0, distance_m),
        (x0, y0 + height, distance_m),
    ):
        ElementTree.SubElement(window, "point", coord="det").text = " ".join(
            f"{value:.12g}" for value in point
        )
    ElementTree.SubElement(params, "enumax").text = (
        f"{max_energy_gev:.12g} 1.05 1.05 {max_weight_scan_entries}"
    )
    ElementTree.SubElement(params, "reuse").text = "1"
    ElementTree.SubElement(params, "upstreamz").text = "-3.4e38"
    ElementTree.indent(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    ElementTree.ElementTree(root).write(
        path, encoding="ISO-8859-1", xml_declaration=True
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Generate GENIE RooTracker events")
    result.add_argument("--gevgen", default="gevgen_fnal")
    result.add_argument("--converter", default="gntpc")
    result.add_argument("--flux-pattern", required=True)
    result.add_argument("--flux-config", type=Path, required=True)
    result.add_argument("--ghep-prefix", type=Path, required=True)
    result.add_argument("--rootracker-output", type=Path, required=True)
    result.add_argument("--events", type=int, required=True)
    result.add_argument("--run", type=int, required=True)
    result.add_argument("--seed", type=int, required=True)
    result.add_argument("--distance-m", type=float, required=True)
    result.add_argument("--center-m", type=float, nargs=2, required=True)
    result.add_argument("--window-size-m", type=float, nargs=2, required=True)
    result.add_argument("--flavors", type=int, nargs="+", required=True)
    result.add_argument("--max-energy-gev", type=float, required=True)
    result.add_argument("--max-weight-scan-entries", type=int, required=True)
    result.add_argument("--target-pdg", type=int, required=True)
    result.add_argument("--tune", required=True)
    result.add_argument("--spline", type=Path, required=True)
    result.add_argument("--stage-flux", action="store_true")
    return result


@contextmanager
def flux_input(path: str, stage: bool) -> Iterator[str]:
    if not stage:
        yield path
        return

    source = Path(path)
    if not source.is_file():
        raise RuntimeError(f"selected flux input is not a file: {source}")
    with tempfile.TemporaryDirectory(prefix="dlpgen-opt-flux-") as temporary:
        destination = Path(temporary) / source.name
        started = time.monotonic()
        print(
            json.dumps(
                {
                    "event": "stage_flux_started",
                    "source": str(source),
                    "destination": str(destination),
                    "bytes": source.stat().st_size,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        shutil.copy2(source, destination)
        print(
            json.dumps(
                {
                    "event": "stage_flux_completed",
                    "source": str(source),
                    "destination": str(destination),
                    "seconds": time.monotonic() - started,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        yield str(destination)


def run_genie(args: argparse.Namespace, environment: dict[str, str], flux_path: str) -> Path:
    flux = ",".join(
        [flux_path, CONFIG_NAME, *(str(pdg) for pdg in args.flavors)]
    )
    generate = [
        args.gevgen,
        "-r",
        str(args.run),
        "-n",
        str(args.events),
        "-g",
        str(args.target_pdg),
        "-f",
        flux,
        "-o",
        str(args.ghep_prefix),
        "--seed",
        str(args.seed),
        "--cross-sections",
        str(args.spline),
        "--tune",
        args.tune,
    ]
    subprocess.run(
        generate,
        check=True,
        env=environment,
        cwd=args.ghep_prefix.parent,
    )

    ghep = Path(f"{args.ghep_prefix}.{args.run}.ghep.root")
    convert = [
        args.converter,
        "-i",
        str(ghep),
        "-o",
        str(args.rootracker_output),
        "-f",
        "rootracker",
        "-c",
        "--seed",
        str(args.seed),
    ]
    subprocess.run(
        convert,
        check=True,
        env=environment,
        cwd=args.ghep_prefix.parent,
    )
    return ghep


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    write_flux_config(
        args.flux_config,
        distance_m=args.distance_m,
        center_m=tuple(args.center_m),
        window_size_m=tuple(args.window_size_m),
        max_energy_gev=args.max_energy_gev,
        max_weight_scan_entries=args.max_weight_scan_entries,
    )
    args.ghep_prefix.parent.mkdir(parents=True, exist_ok=True)
    args.rootracker_output.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["GDK2NUFLUXXML"] = str(args.flux_config)

    with flux_input(args.flux_pattern, args.stage_flux) as flux_path:
        ghep = run_genie(args, environment, flux_path)
    print(
        json.dumps(
            {
                "flux_config": str(args.flux_config),
                "ghep": str(ghep),
                "rootracker": str(args.rootracker_output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
