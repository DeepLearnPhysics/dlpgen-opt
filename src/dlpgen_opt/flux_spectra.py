from __future__ import annotations

import math
from pathlib import Path

from .provenance import checksum, read_yaml, write_yaml


SUPPORTED_FLAVORS = {12, -12, 14, -14, 16, -16}
FORMAT = "dlpgen-opt-flux-spectra"
LEGACY_FORMAT = "dlpgen-opt-gibuu-flux-spectra"


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
    try:
        for event in tree:
            pdg = int(event.pdg)
            weight = float(event.flux_weight_per_cm2)
            energy = float(event.energy_gev)
            if (
                pdg not in SUPPORTED_FLAVORS
                or not math.isfinite(weight)
                or not math.isfinite(energy)
                or energy <= 0
            ):
                raise RuntimeError("canonical flux table contains an invalid throw")
            contents.setdefault(pdg, [0.0] * bins)
            outside.setdefault(pdg, 0.0)
            index = int(math.floor((energy - energy_min) / width))
            if index == bins and energy == energy_max:
                index -= 1
            if not 0 <= index < bins:
                # Signed cancellation must not conceal excluded contributions.
                outside[pdg] += abs(weight)
                continue
            # G4BNB's beamHist.cc also retains signed nimpwt contributions.
            contents[pdg][index] += weight
    finally:
        root_file.Close()
    if any(weight > 0 for weight in outside.values()):
        raise RuntimeError(
            f"canonical flux lies outside configured energy range: {outside}"
        )
    result: dict[int, dict[str, object]] = {}
    # Validate every flavor before writing any generator input. Individual
    # signed contributions are valid, but a sampling density cannot be negative.
    for pdg, values in sorted(contents.items()):
        for index, content in enumerate(values):
            if not math.isfinite(content) or content < 0:
                raise RuntimeError(
                    "canonical flux spectrum has an invalid summed bin: "
                    f"pdg={pdg}, bin={index}, "
                    f"energy_low_gev={energy_min + index * width}, "
                    f"energy_high_gev={energy_min + (index + 1) * width}, "
                    f"flux_per_cm2={content!r}; signed contributions were preserved"
                )
        if not math.isfinite(sum(values)):
            raise RuntimeError(f"canonical flux spectrum integral is nonfinite: pdg={pdg}")
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
    """Project canonical throws into reusable, generator-neutral spectra."""
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
        "format": FORMAT,
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


def cached_flux_histograms(
    spectra_manifest: Path,
    canonical_manifest: Path,
    *,
    energy_min: float,
    energy_max: float,
    bins: int,
) -> dict[int, dict[str, object]]:
    """Validate and open either current or v0.2.2 cached spectra."""
    manifest = read_yaml(spectra_manifest)
    expected_binning = {
        "energy_min_gev": energy_min,
        "energy_max_gev": energy_max,
        "bins": bins,
    }
    if manifest.get("format") not in (FORMAT, LEGACY_FORMAT):
        raise RuntimeError(f"invalid flux spectra manifest: {spectra_manifest}")
    if manifest.get("binning") != expected_binning:
        raise RuntimeError("cached flux spectra use different energy binning")
    canonical = read_yaml(canonical_manifest)
    if manifest.get("canonical_flux_sha256") != canonical.get("output", {}).get(
        "sha256"
    ):
        raise RuntimeError("cached spectra do not match the canonical flux table")
    result: dict[int, dict[str, object]] = {}
    for pdg_text, record in manifest.get("flavors", {}).items():
        pdg = int(pdg_text)
        path = spectra_manifest.parent / record["path"]
        if checksum(path) != record["sha256"]:
            raise RuntimeError(f"cached flux spectrum checksum mismatch: {path}")
        result[pdg] = {
            "path": path,
            "integral_per_cm2": float(record["integral_per_cm2"]),
        }
    if not result:
        raise RuntimeError("cached flux spectra contain no flavors")
    return result
