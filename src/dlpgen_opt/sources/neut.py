from __future__ import annotations

import json
import sys
from pathlib import Path

from ..config import NeutSource, ProductionConfig
from ..layout import JobLayout
from ..validation import validate_nonempty, validate_root
from .base import SourceBackend


class NeutBackend(SourceBackend):
    """Generate NEUT events from cached, mixed-flavor dk2nu spectra."""

    def _settings(self, config: ProductionConfig) -> NeutSource:
        source = config.source
        if not isinstance(source, NeutSource):
            raise TypeError("NEUT backend requires a NEUT source configuration")
        return source

    def _common(self, config: ProductionConfig, layout: JobLayout) -> list[str]:
        source = self._settings(config)
        return [
            sys.executable,
            "-m",
            "dlpgen_opt.neut_cli",
            "--flux-manifest",
            str(config.production.output_dir / "flux" / "canonical.yaml"),
            "--flux-spectra-manifest",
            str(config.production.output_dir / "flux" / "spectra.yaml"),
            "--work-dir",
            str(layout.source_dir),
            "--runtime",
            str(source.runtime),
            "--card",
            str(source.card),
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
            "--mdlqe",
            str(source.mdlqe),
            "--mdl2p2h",
            str(source.mdl2p2h),
            "--processes",
            *source.processes,
        ]

    def command(
        self, config: ProductionConfig, job: int, layout: JobLayout
    ) -> list[str]:
        source = self._settings(config)
        return [
            *self._common(config, layout),
            "--normalization",
            str(config.production.output_dir / "neut" / "normalization.yaml"),
            "--native-archive",
            str(layout.neut_native_archive),
            "--resolved-cards",
            str(layout.neut_cards),
            "--output",
            str(layout.rootracker),
            "--metadata-output",
            str(layout.source_conversion_metadata),
            "--events",
            str(config.production.generator_calls_per_job),
            "--seed",
            str(config.seed(job, 0)),
            "--vertex-cm",
            *(str(value) for value in source.vertex_cm),
        ]

    def prepare_command(
        self, config: ProductionConfig, layout: JobLayout
    ) -> list[str]:
        return [
            *self._common(config, layout),
            "--normalization",
            str(config.production.output_dir / "neut" / "normalization.yaml"),
            "--native-archive",
            str(config.production.output_dir / "neut" / "normalization-probes.tar.gz"),
            "--seed",
            str(config.production.base_seed),
            "--prepare-only",
        ]

    def output(self, layout: JobLayout) -> Path:
        return layout.rootracker

    def outputs(self, config: ProductionConfig, layout: JobLayout) -> list[Path]:
        return [
            layout.neut_native_archive,
            layout.neut_cards,
            layout.rootracker,
            layout.source_conversion_metadata,
        ]

    def inputs(self, config: ProductionConfig, job: int | None = None) -> list[Path]:
        source = self._settings(config)
        inputs = [
            config.production.output_dir / "flux" / "canonical.yaml",
            config.production.output_dir / "flux" / "spectra.yaml",
            source.card,
        ]
        if job is not None:
            inputs.append(config.production.output_dir / "neut" / "normalization.yaml")
        if source.config is not None:
            inputs.append(source.config)
        return inputs

    def finalize(
        self, config: ProductionConfig, layout: JobLayout
    ) -> dict[str, object]:
        source = self._settings(config)
        rootracker = validate_root(layout.rootracker, "gRooTracker")
        archive = validate_nonempty(layout.neut_native_archive)
        cards = validate_nonempty(layout.neut_cards)
        validate_nonempty(layout.source_conversion_metadata)
        with layout.source_conversion_metadata.open(encoding="utf-8") as stream:
            conversion = json.load(stream)
        expected = config.production.generator_calls_per_job
        if conversion.get("events") != expected:
            raise RuntimeError(
                f"expected {expected} converted NEUT events, "
                f"found {conversion.get('events')}"
            )
        tools = conversion.get("generator_tools", [])
        if tools and not any(
            "neut" in str(tool.get("name", "")).lower() for tool in tools
        ):
            raise RuntimeError("NuHepMC generator metadata does not identify NEUT")
        return {
            "format": "NEUT-NuHepMC",
            "generator_version": source.generator_version,
            "rootracker": rootracker,
            "conversion": conversion,
            "native_archive": archive,
            "resolved_cards": cards,
        }

    def edep_macro_lines(
        self, config: ProductionConfig, layout: JobLayout
    ) -> list[str]:
        return [
            "/generator/kinematics/rooTracker/input " + str(layout.rootracker),
            "/generator/kinematics/rooTracker/generator NEUT",
            "/generator/kinematics/set rooTracker",
        ]
