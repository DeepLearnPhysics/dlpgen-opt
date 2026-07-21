from __future__ import annotations

import glob
from pathlib import Path

from ..config import GenieSource, ProductionConfig
from ..layout import JobLayout
from ..validation import validate_root
from .base import SourceBackend


def flux_files(pattern: Path) -> list[Path]:
    return [Path(path).resolve() for path in sorted(glob.glob(str(pattern)))]


class GenieBackend(SourceBackend):
    def _settings(self, config: ProductionConfig) -> GenieSource:
        source = config.source
        if not isinstance(source, GenieSource):
            raise TypeError("GENIE backend requires a GENIE source configuration")
        return source

    def command(self, config: ProductionConfig, job: int, layout: JobLayout) -> list[str]:
        source = self._settings(config)
        return [
            source.executable,
            "--gevgen",
            source.gevgen_executable,
            "--converter",
            source.converter_executable,
            "--flux-pattern",
            str(source.flux.file_pattern),
            "--flux-config",
            str(layout.genie_flux_config),
            "--ghep-prefix",
            str(layout.source_dir / "events"),
            "--rootracker-output",
            str(layout.rootracker),
            "--events",
            str(config.production.generator_calls_per_job),
            "--run",
            str(job),
            "--seed",
            str(config.seed(job, 0)),
            "--distance-m",
            str(source.flux.distance_m),
            "--center-m",
            *(str(value) for value in source.flux.center_m),
            "--window-size-m",
            *(str(value) for value in source.flux.window_size_m),
            "--flavors",
            *(str(value) for value in source.flux.flavors),
            "--max-energy-gev",
            str(source.flux.max_energy_gev),
            "--max-weight-scan-entries",
            str(source.flux.max_weight_scan_entries),
            "--target-pdg",
            str(source.target_pdg),
            "--tune",
            source.tune,
            "--spline",
            str(source.spline),
        ]

    def output(self, layout: JobLayout) -> Path:
        return layout.rootracker

    def outputs(self, layout: JobLayout) -> list[Path]:
        return [layout.genie_flux_config, layout.genie_ghep, layout.rootracker]

    def inputs(self, config: ProductionConfig) -> list[Path]:
        source = self._settings(config)
        files = flux_files(source.flux.file_pattern)
        if not files:
            raise RuntimeError(
                f"GENIE flux pattern matched no files: {source.flux.file_pattern}"
            )
        source_config = [source.config] if source.config is not None else []
        return [*source_config, *files, source.spline]

    def finalize(self, config: ProductionConfig, layout: JobLayout) -> dict[str, object]:
        ghep = validate_root(layout.genie_ghep, "gtree")
        rootracker = validate_root(layout.rootracker, "gRooTracker")
        return {
            "format": "GENIE-RooTracker",
            "ghep": ghep,
            "rootracker": rootracker,
        }

    def edep_macro_lines(
        self, config: ProductionConfig, layout: JobLayout
    ) -> list[str]:
        source = self._settings(config)
        x, y, z = source.vertex_cm
        return [
            "/generator/kinematics/rooTracker/input " + str(layout.rootracker),
            "/generator/kinematics/set rooTracker",
            f"/generator/position/fixed/position {x} {y} {z} cm",
            "/generator/position/set fixed",
        ]
