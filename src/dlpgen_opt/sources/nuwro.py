from __future__ import annotations

import json
import sys
from pathlib import Path

from ..config import NuWroSource, ProductionConfig
from ..layout import JobLayout
from ..validation import validate_nonempty, validate_root
from .base import SourceBackend


class NuWroBackend(SourceBackend):
    """Generate NuWro events from cached, mixed-flavor dk2nu spectra."""

    def _settings(self, config: ProductionConfig) -> NuWroSource:
        source = config.source
        if not isinstance(source, NuWroSource):
            raise TypeError("NuWro backend requires a NuWro source configuration")
        return source

    def command(
        self, config: ProductionConfig, job: int, layout: JobLayout
    ) -> list[str]:
        source = self._settings(config)
        return [
            sys.executable,
            "-m",
            "dlpgen_opt.nuwro_cli",
            "--flux-manifest",
            str(config.production.output_dir / "flux" / "canonical.yaml"),
            "--flux-spectra-manifest",
            str(config.production.output_dir / "flux" / "spectra.yaml"),
            "--work-dir",
            str(layout.source_dir),
            "--native-output",
            str(layout.nuwro_native),
            "--params-output",
            str(layout.nuwro_params),
            "--output",
            str(layout.rootracker),
            "--metadata-output",
            str(layout.source_conversion_metadata),
            "--events",
            str(config.production.generator_calls_per_job),
            "--test-events",
            str(source.test_events),
            "--seed",
            str(config.seed(job, 0)),
            "--executable",
            source.executable,
            "--converter",
            source.converter_executable,
            "--target-a",
            str(source.target_a),
            "--target-z",
            str(source.target_z),
            "--energy-min-gev",
            str(source.energy_min_gev),
            "--energy-max-gev",
            str(source.energy_max_gev),
            "--energy-bins",
            str(source.energy_bins),
            "--processes",
            *source.processes,
            "--vertex-cm",
            *(str(value) for value in source.vertex_cm),
        ]

    def output(self, layout: JobLayout) -> Path:
        return layout.rootracker

    def outputs(self, config: ProductionConfig, layout: JobLayout) -> list[Path]:
        return [
            layout.nuwro_native,
            layout.nuwro_params,
            layout.rootracker,
            layout.source_conversion_metadata,
        ]

    def inputs(self, config: ProductionConfig, job: int | None = None) -> list[Path]:
        source = self._settings(config)
        result = [
            config.production.output_dir / "flux" / "canonical.yaml",
            config.production.output_dir / "flux" / "spectra.yaml",
        ]
        if source.config is not None:
            result.append(source.config)
        return result

    def finalize(
        self, config: ProductionConfig, layout: JobLayout
    ) -> dict[str, object]:
        source = self._settings(config)
        native = validate_root(layout.nuwro_native, "treeout")
        params = validate_nonempty(layout.nuwro_params)
        rootracker = validate_root(layout.rootracker, "gRooTracker")
        validate_nonempty(layout.source_conversion_metadata)
        with layout.source_conversion_metadata.open(encoding="utf-8") as stream:
            conversion = json.load(stream)
        expected = config.production.generator_calls_per_job
        if conversion.get("events") != expected:
            raise RuntimeError(
                f"expected {expected} converted NuWro events, "
                f"found {conversion.get('events')}"
            )
        return {
            "format": "NuWro-RooTracker",
            "generator_version": source.generator_version,
            "native": native,
            "params": params,
            "rootracker": rootracker,
            "conversion": conversion,
        }

    def edep_macro_lines(
        self, config: ProductionConfig, layout: JobLayout
    ) -> list[str]:
        return [
            "/generator/kinematics/rooTracker/input " + str(layout.rootracker),
            "/generator/kinematics/rooTracker/generator NuWro",
            "/generator/kinematics/set rooTracker",
        ]
