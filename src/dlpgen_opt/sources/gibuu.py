from __future__ import annotations

import json
import sys
from pathlib import Path

from ..artifacts import InputArtifact
from ..config import GiBUUSource, ProductionConfig
from ..layout import JobLayout
from ..validation import validate_nonempty
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
            return [
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
                str(layout.hepevt),
                "--metadata-output",
                str(layout.source_conversion_metadata),
                "--native-archive",
                str(layout.gibuu_native_archive),
                "--resolved-jobcards",
                str(layout.gibuu_jobcard),
                "--events",
                str(events),
                "--seed",
                str(config.seed(job, 0)),
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
        if source.input is None:
            raise RuntimeError("GiBUU import mode has no native input")
        offset = job * events
        return [
            sys.executable,
            "-m",
            "dlpgen_opt.nuhepmc_cli",
            str(source.input),
            "--output",
            str(layout.hepevt),
            "--metadata-output",
            str(layout.source_conversion_metadata),
            "--events",
            str(events),
            "--skip",
            str(offset),
            "--vertex-cm",
            *(str(value) for value in source.vertex_cm),
        ]

    def output(self, layout: JobLayout) -> Path:
        return layout.hepevt

    def outputs(self, config: ProductionConfig, layout: JobLayout) -> list[Path]:
        outputs = [layout.hepevt, layout.source_conversion_metadata]
        if self._settings(config).mode == "generate":
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
        hepevt = validate_nonempty(layout.hepevt)
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
        if source.mode == "generate":
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
            "hepevt": hepevt,
            "conversion": conversion,
            "native_archive": native_archive,
            "resolved_jobcards": resolved_jobcards,
        }

    def edep_macro_lines(
        self, config: ProductionConfig, layout: JobLayout
    ) -> list[str]:
        return [
            "/generator/kinematics/hepevt/input " + str(layout.hepevt),
            "/generator/kinematics/hepevt/flavor pbomb",
            "/generator/kinematics/hepevt/verbose 0",
            "/generator/kinematics/set hepevt",
        ]
