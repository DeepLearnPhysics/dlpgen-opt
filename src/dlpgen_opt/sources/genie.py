from __future__ import annotations

import glob
import hashlib
from pathlib import Path

from ..artifacts import InputArtifact
from ..config import GenieSource, ProductionConfig
from ..layout import JobLayout
from ..validation import validate_root
from .base import SourceBackend


def flux_files(pattern: Path) -> list[Path]:
    return [Path(path).resolve() for path in sorted(glob.glob(str(pattern)))]


class GenieBackend(SourceBackend):
    def __init__(self) -> None:
        self._catalog: tuple[Path, ...] | None = None

    def _settings(self, config: ProductionConfig) -> GenieSource:
        source = config.source
        if not isinstance(source, GenieSource):
            raise TypeError("GENIE backend requires a GENIE source configuration")
        return source

    def command(self, config: ProductionConfig, job: int, layout: JobLayout) -> list[str]:
        source = self._settings(config)
        selected = self.selected_flux(config, job)
        command = [
            source.executable,
            "--gevgen",
            source.gevgen_executable,
            "--converter",
            source.converter_executable,
            "--flux-pattern",
            str(selected),
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
        if source.flux.stage_to_local:
            command.append("--stage-flux")
        return command

    def catalog(self, config: ProductionConfig) -> tuple[Path, ...]:
        if self._catalog is None:
            source = self._settings(config)
            files = tuple(flux_files(source.flux.file_pattern))
            if not files:
                raise RuntimeError(
                    f"GENIE flux pattern matched no files: {source.flux.file_pattern}"
                )
            self._catalog = files
        return self._catalog

    def selected_flux(self, config: ProductionConfig, job: int) -> Path:
        files = self.catalog(config)
        index = (config.production.base_seed + job) % len(files)
        return files[index]

    def catalog_metadata(self, config: ProductionConfig) -> dict[str, object]:
        source = self._settings(config)
        files = self.catalog(config)
        digest = hashlib.sha256()
        for path in files:
            digest.update(str(path).encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
        return {
            "pattern": str(source.flux.file_pattern),
            "files": len(files),
            "paths_sha256": digest.hexdigest(),
            "selection": "seeded-round-robin-one-file-per-job",
            "first_job_index": config.production.base_seed % len(files),
            "checksum_files": source.flux.checksum_files,
            "stage_to_local": source.flux.stage_to_local,
        }

    def output(self, layout: JobLayout) -> Path:
        return layout.rootracker

    def outputs(self, layout: JobLayout) -> list[Path]:
        return [layout.genie_flux_config, layout.genie_ghep, layout.rootracker]

    def inputs(
        self, config: ProductionConfig, job: int | None = None
    ) -> list[Path | InputArtifact]:
        source = self._settings(config)
        if job is None:
            raise ValueError("GENIE stage inputs require a production job index")
        selected = self.selected_flux(config, job)
        source_config = [source.config] if source.config is not None else []
        return [
            *source_config,
            InputArtifact(selected, checksum=source.flux.checksum_files),
            source.spline,
        ]

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
