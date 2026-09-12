from __future__ import annotations

import json
import sys
from pathlib import Path

from ..artifacts import InputArtifact
from ..config import GiBUUSource, ProductionConfig
from ..layout import JobLayout
from ..validation import validate_nonempty, validate_root
from .base import SourceBackend


class GiBUUBackend(SourceBackend):
    """Import native GiBUU 2025 NuHepMC output for detector simulation."""

    def _settings(self, config: ProductionConfig) -> GiBUUSource:
        source = config.source
        if not isinstance(source, GiBUUSource):
            raise TypeError("GiBUU backend requires a GiBUU source configuration")
        return source

    def command(
        self, config: ProductionConfig, job: int, layout: JobLayout
    ) -> list[str]:
        source = self._settings(config)
        events = config.production.generator_calls_per_job
        if source.mode == "generate":
            flux_manifest = config.production.output_dir / "flux" / "canonical.yaml"
            spectra_manifest = config.production.output_dir / "flux" / "spectra.yaml"
            command = [
                sys.executable,
                "-m",
                "dlpgen_opt.gibuu_cli",
                "--flux-manifest",
                str(flux_manifest),
                "--flux-spectra-manifest",
                str(spectra_manifest),
                "--jobcard",
                str(source.jobcard),
                "--work-dir",
                str(layout.source_dir),
                "--output",
                str(layout.rootracker),
                "--metadata-output",
                str(layout.source_conversion_metadata),
                "--native-archive",
                str(layout.gibuu_native_archive),
                "--resolved-jobcards",
                str(layout.gibuu_jobcard),
                "--events",
                str(events),
                "--seed",
                str(
                    config.production.base_seed
                    if source.candidate_cache.enabled
                    else config.seed(job, 0)
                ),
                "--executable",
                source.executable,
                "--input-tables",
                str(source.input_tables),
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
                "--ensembles",
                str(source.ensembles),
                "--runs",
                str(source.runs),
                "--time-steps",
                str(source.time_steps),
                "--processes",
                *source.processes,
                "--vertex-cm",
                *(str(value) for value in source.vertex_cm),
            ]
            if source.candidate_cache.enabled:
                cache_root = source.candidate_cache.directory or (
                    config.production.output_dir.parent
                    / ".dlpgen-opt-gibuu-cache"
                )
                command.extend(
                    [
                        "--candidate-cache-dir",
                        str(cache_root),
                        "--campaign-dir",
                        str(config.production.output_dir / "gibuu"),
                        "--total-events",
                        str(config.production.jobs * events),
                        "--job-index",
                        str(job),
                        "--cache-sizing",
                        source.candidate_cache.sizing,
                        "--reserve-fraction",
                        str(source.candidate_cache.reserve_fraction),
                        "--max-cache-shards",
                        str(source.candidate_cache.max_shards),
                        "--software-identity",
                        config.software.container_image,
                    ]
                )
                if source.candidate_cache.shards is not None:
                    command.extend(
                        ["--cache-shards", str(source.candidate_cache.shards)]
                    )
            return command
        if source.input is None:
            raise RuntimeError("GiBUU import mode has no native input")
        offset = job * events
        return [
            sys.executable,
            "-m",
            "dlpgen_opt.nuhepmc_cli",
            str(source.input),
            "--output",
            str(layout.rootracker),
            "--metadata-output",
            str(layout.source_conversion_metadata),
            "--events",
            str(events),
            "--skip",
            str(offset),
            "--vertex-cm",
            *(str(value) for value in source.vertex_cm),
        ]

    def prepare_command(
        self, config: ProductionConfig, layout: JobLayout
    ) -> list[str]:
        source = self._settings(config)
        if source.mode != "generate" or not source.candidate_cache.enabled:
            raise RuntimeError("GiBUU candidate preparation is not enabled")
        return [*self.command(config, 0, layout), "--prepare-only"]

    def output(self, layout: JobLayout) -> Path:
        return layout.rootracker

    def outputs(self, config: ProductionConfig, layout: JobLayout) -> list[Path]:
        outputs = [layout.rootracker, layout.source_conversion_metadata]
        source = self._settings(config)
        if source.mode == "generate" and not source.candidate_cache.enabled:
            outputs.extend([layout.gibuu_native_archive, layout.gibuu_jobcard])
        return outputs

    def inputs(
        self, config: ProductionConfig, job: int | None = None
    ) -> list[Path | InputArtifact]:
        source = self._settings(config)
        if source.mode == "generate":
            return [
                config.production.output_dir / "flux" / "canonical.yaml",
                config.production.output_dir / "flux" / "spectra.yaml",
                source.jobcard,
            ]
        if source.input is None:
            raise RuntimeError("GiBUU import mode has no native input")
        inputs: list[Path | InputArtifact] = [
            InputArtifact(source.input, checksum=source.checksum_input)
        ]
        inputs.append(source.jobcard)
        return inputs

    def finalize(
        self, config: ProductionConfig, layout: JobLayout
    ) -> dict[str, object]:
        source = self._settings(config)
        rootracker = validate_root(layout.rootracker, "gRooTracker")
        validate_nonempty(layout.source_conversion_metadata)
        with layout.source_conversion_metadata.open(encoding="utf-8") as stream:
            conversion = json.load(stream)
        expected = config.production.generator_calls_per_job
        if conversion.get("events") != expected:
            raise RuntimeError(
                f"expected {expected} converted GiBUU events, "
                f"found {conversion.get('events')}"
            )
        tools = conversion.get("generator_tools", [])
        if tools and not any(
            "gibuu" in str(tool.get("name", "")).lower() for tool in tools
        ):
            raise RuntimeError("NuHepMC generator metadata does not identify GiBUU")
        native_archive = None
        resolved_jobcards = None
        if source.mode == "generate" and not source.candidate_cache.enabled:
            native_archive = validate_nonempty(layout.gibuu_native_archive)
            resolved_jobcards = validate_nonempty(layout.gibuu_jobcard)
        return {
            "format": (
                "GiBUU-generated"
                if source.mode == "generate"
                else "GiBUU-NuHepMC"
            ),
            "generator_version": source.generator_version,
            "native_input": str(source.input) if source.input else None,
            "jobcard": str(source.jobcard),
            "rootracker": rootracker,
            "conversion": conversion,
            "native_archive": native_archive,
            "resolved_jobcards": resolved_jobcards,
        }

    def edep_macro_lines(
        self, config: ProductionConfig, layout: JobLayout
    ) -> list[str]:
        return [
            "/generator/kinematics/rooTracker/input " + str(layout.rootracker),
            "/generator/kinematics/rooTracker/generator GiBUU",
            "/generator/kinematics/set rooTracker",
        ]
