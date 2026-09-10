from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import fcntl

import yaml

from .config import DLPGeneratorSource, GenieSource, GiBUUSource, ProductionConfig
from .dlpgen_build import (
    CheckoutSnapshot,
    build_cache_path,
    ensure_custom_build,
    inspect_checkout,
)
from .flux_cli import ALGORITHM as FLUX_ALGORITHM
from .flux_cli import catalog_digest, materialize as materialize_flux
from .gibuu_cli import materialize_flux_spectra
from .layout import JobLayout
from .provenance import (
    checksum,
    dependency_commits,
    host_info,
    read_yaml,
    write_yaml,
)
from .runner import execute_stage
from .sources import DLPGeneratorBackend, GenieBackend, GiBUUBackend, SourceBackend
from .validation import validate_nonempty, validate_root


STAGES = ("generate", "edep-sim", "supera")


@contextmanager
def _initialization_lock(root: Path) -> Iterator[None]:
    """Serialize shared production metadata writes across array tasks."""
    with (root / ".initialize.lock").open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Pipeline:
    def __init__(self, config: ProductionConfig, repository: Path | None = None):
        self.config = config
        self.repository = repository or Path(
            os.environ.get("DLPGEN_OPT_ROOT", Path(__file__).resolve().parents[2])
        )
        if isinstance(config.source, DLPGeneratorSource):
            self.source = DLPGeneratorBackend()
        elif isinstance(config.source, GenieSource):
            self.source = GenieBackend()
        else:
            self.source = GiBUUBackend()
        self.dlpgen_checkout: CheckoutSnapshot | None = None
        if isinstance(config.source, DLPGeneratorSource) and config.source.checkout:
            self.dlpgen_checkout = inspect_checkout(config.source.checkout)

    def _dependency_commits(self) -> dict[str, str | None]:
        commits = dependency_commits(self.repository)
        if self.dlpgen_checkout:
            commits["DLPGenerator"] = self.dlpgen_checkout.commit
        return commits

    def _metadata(self, job: int) -> dict[str, object]:
        metadata: dict[str, object] = {
            "job": job,
            "seeds": {
                "source": self.config.seed(job, 0),
                "edep_sim": self.config.seed(job, 1),
                "supera": self.config.seed(job, 2),
            },
            "container_image": self.config.software.container_image,
            "dependency_commits": self._dependency_commits(),
            "host": host_info(),
        }
        if self.dlpgen_checkout:
            metadata["dlpgen_checkout"] = self.dlpgen_checkout.metadata()
        return metadata

    def initialize(self) -> None:
        root = self.config.production.output_dir
        root.mkdir(parents=True, exist_ok=True)
        with _initialization_lock(root):
            self._initialize_locked(root)

    def _initialize_locked(self, root: Path) -> None:
        resolved = root / "resolved_config.yaml"
        current = self.config.resolved_dict()
        if resolved.exists():
            previous = read_yaml(resolved)
            if previous != current:
                raise RuntimeError(
                    f"production directory already contains a different configuration: {root}"
                )
        else:
            write_yaml(resolved, current)
        if (
            isinstance(self.config.source, GiBUUSource)
            and self.config.source.mode == "generate"
        ):
            self._prepare_gibuu_flux(root)
        manifest_path = root / "manifest.yaml"
        # Referenced generator inputs are fully expanded in resolved_config.yaml.
        # Once their immutable metadata is recorded, subsequent array tasks can
        # avoid rescanning remote catalogs or re-hashing large event vectors.
        if (
            isinstance(self.config.source, (GenieSource, GiBUUSource))
            and manifest_path.exists()
        ):
            return
        commits = self._dependency_commits()
        expected = {
            "edep-sim": self.config.software.edep_sim.expected_commit,
            "SuperaAtomic": self.config.software.supera_atomic.expected_commit,
            "edep2supera": self.config.software.edep2supera.expected_commit,
        }
        if isinstance(self.config.source, DLPGeneratorSource):
            expected["DLPGenerator"] = self.config.source.expected_commit
        elif isinstance(self.config.source, GenieSource):
            expected["GENIE"] = self.config.source.expected_commit
            expected["dk2nu"] = self.config.source.dk2nu_expected_commit
        elif (
            isinstance(self.config.source, GiBUUSource)
            and self.config.source.mode == "generate"
        ):
            expected["dk2nu"] = self.config.source.dk2nu_expected_commit
        mismatches = {
            name: {"expected": pin, "actual": commits.get(name)}
            for name, pin in expected.items()
            if pin and pin != commits.get(name)
        }
        if mismatches:
            raise RuntimeError(f"dependency pin mismatch: {mismatches}")
        manifest: dict[str, object] = {
            "production": self.config.production.name,
            "configuration": str(self.config.config_path),
            "container_image": self.config.software.container_image,
            "dependency_commits": commits,
            "geometry_sha256": validate_nonempty(self.config.detector.geometry)["sha256"],
            "supera_config_sha256": validate_nonempty(
                self.config.detector.supera_config
            )["sha256"],
        }
        if isinstance(self.config.source, DLPGeneratorSource):
            manifest["source_config_sha256"] = validate_nonempty(
                self.config.source.config
            )["sha256"]
            if self.dlpgen_checkout:
                manifest["dlpgen_checkout"] = self.dlpgen_checkout.metadata()
        elif isinstance(self.config.source, GenieSource):
            if self.config.source.config is not None:
                manifest["source_config_sha256"] = validate_nonempty(
                    self.config.source.config
                )["sha256"]
            if not isinstance(self.source, GenieBackend):
                raise TypeError("GENIE source requires the GENIE backend")
            manifest["genie"] = {
                "tune": self.config.source.tune,
                "target_pdg": self.config.source.target_pdg,
                "flux_catalog": self.source.catalog_metadata(self.config),
                "spline": validate_nonempty(self.config.source.spline),
            }
        else:
            source = self.config.source
            if not isinstance(source, GiBUUSource):
                raise TypeError("unsupported source configuration")
            if source.config is not None:
                manifest["source_config_sha256"] = validate_nonempty(
                    source.config
                )["sha256"]
            if source.mode == "generate":
                manifest["gibuu"] = {
                    "mode": "generate",
                    "generator_version": source.generator_version,
                    "flux": read_yaml(root / "flux" / "canonical.yaml"),
                    "jobcard": validate_nonempty(source.jobcard),
                    "target": {"a": source.target_a, "z": source.target_z},
                    "processes": source.processes,
                    "events_per_job": self.config.production.generator_calls_per_job,
                }
            else:
                if source.input is None:
                    raise RuntimeError("GiBUU import mode has no native input")
                native_input: dict[str, object] = {
                    "path": str(source.input),
                    "bytes": source.input.stat().st_size,
                }
                if source.checksum_input:
                    native_input = validate_nonempty(source.input)
                else:
                    native_input["checksum"] = "skipped"
                manifest["gibuu"] = {
                    "mode": "import",
                    "generator_version": source.generator_version,
                    "native_format": "NuHepMC",
                    "native_input": native_input,
                    "jobcard": validate_nonempty(source.jobcard),
                    "event_selection": "contiguous-job-indexed-ranges",
                    "events_per_job": self.config.production.generator_calls_per_job,
                }
        if self.dlpgen_checkout and manifest_path.exists():
            if read_yaml(manifest_path) != manifest:
                raise RuntimeError(
                    "production directory was initialized with different inputs: {}".format(
                        root
                    )
                )
        else:
            write_yaml(manifest_path, manifest)

    def _prepare_gibuu_flux(self, root: Path) -> None:
        source = self.config.source
        if not isinstance(source, GiBUUSource) or source.flux is None:
            raise TypeError("native GiBUU generation requires flux settings")
        directory = root / "flux"
        table = directory / "canonical.root"
        manifest = directory / "canonical.yaml"
        spectra = directory / "spectra.yaml"
        if table.exists() and manifest.exists() and spectra.exists():
            validate_nonempty(table)
            validate_nonempty(manifest)
            validate_nonempty(spectra)
            return
        if table.exists() or manifest.exists() or spectra.exists():
            raise RuntimeError("incomplete canonical GiBUU flux product")
        from .sources.genie import flux_files

        paths = flux_files(source.flux.file_pattern)
        if not paths:
            raise RuntimeError(
                f"flux pattern matched no files: {source.flux.file_pattern}"
            )
        cache_contract = {
            "algorithm": FLUX_ALGORITHM,
            "catalog_paths_sha256": catalog_digest(paths),
            "catalog_files": len(paths),
            "window": {
                "distance_m": source.flux.distance_m,
                "center_m": list(source.flux.center_m),
                "size_m": list(source.flux.window_size_m),
            },
            "flavors": list(source.flux.flavors),
            "seed": self.config.production.base_seed,
            "max_files": source.flux.max_files,
            "target_pot": source.flux.target_pot,
            "checksum_inputs": source.flux.checksum_files,
            "gibuu_binning": {
                "energy_min_gev": source.energy_min_gev,
                "energy_max_gev": source.energy_max_gev,
                "bins": source.energy_bins,
            },
        }
        encoded = json.dumps(cache_contract, sort_keys=True, separators=(",", ":"))
        cache_key = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        cache_root = source.flux.cache_dir or (
            self.config.production.output_dir.parent / ".dlpgen-opt-flux-cache"
        )
        cache_entry = cache_root / cache_key
        with _exclusive_lock(cache_root / f".{cache_key}.lock"):
            cache_record = cache_entry / "cache.yaml"
            if not cache_record.exists():
                if cache_entry.exists():
                    raise RuntimeError(f"incomplete shared flux cache entry: {cache_entry}")
                cache_root.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(
                    prefix=f".{cache_key}.", dir=cache_root
                ) as temporary:
                    staging = Path(temporary)
                    cached_table = staging / "canonical.root"
                    cached_manifest = staging / "canonical.yaml"
                    cached_spectra = staging / "spectra.yaml"
                    materialize_flux(
                        flux_pattern=source.flux.file_pattern,
                        output=cached_table,
                        manifest_output=cached_manifest,
                        distance_m=source.flux.distance_m,
                        center_m=source.flux.center_m,
                        window_size_m=source.flux.window_size_m,
                        flavors=list(source.flux.flavors),
                        seed=self.config.production.base_seed,
                        max_files=source.flux.max_files,
                        target_pot=source.flux.target_pot,
                        checksum_inputs=source.flux.checksum_files,
                        input_paths=paths,
                    )
                    materialize_flux_spectra(
                        flux_table=cached_table,
                        flux_manifest=cached_manifest,
                        output_manifest=cached_spectra,
                        energy_min=source.energy_min_gev,
                        energy_max=source.energy_max_gev,
                        bins=source.energy_bins,
                    )
                    write_yaml(
                        staging / "cache.yaml",
                        {"status": "complete", "key": cache_key, **cache_contract},
                    )
                    staging.rename(cache_entry)
                cached_manifest_data = read_yaml(cache_entry / "canonical.yaml")
                cached_manifest_data["output"]["path"] = str(
                    cache_entry / "canonical.root"
                )
                write_yaml(cache_entry / "canonical.yaml", cached_manifest_data)
            record = read_yaml(cache_record)
            if record.get("status") != "complete" or record.get("key") != cache_key:
                raise RuntimeError(f"invalid shared flux cache entry: {cache_entry}")
            validate_nonempty(cache_entry / "canonical.root")
            validate_nonempty(cache_entry / "canonical.yaml")
            canonical_record = read_yaml(cache_entry / "canonical.yaml")
            if checksum(cache_entry / "canonical.root") != canonical_record.get(
                "output", {}
            ).get("sha256"):
                raise RuntimeError(f"cached canonical flux checksum mismatch: {cache_entry}")
            if source.flux.checksum_files:
                for input_record in canonical_record.get("inputs", []):
                    path = Path(input_record["path"])
                    if checksum(path) != input_record.get("sha256"):
                        raise RuntimeError(f"cached dk2nu input changed: {path}")
            spectra_record = read_yaml(cache_entry / "spectra.yaml")
            for flavor in spectra_record.get("flavors", {}).values():
                validate_nonempty(cache_entry / flavor["path"])

        directory.mkdir(parents=True, exist_ok=True)
        cache_artifacts = [
            cache_entry / "canonical.root",
            cache_entry / "canonical.yaml",
            cache_entry / "spectra.yaml",
            *(
                cache_entry / record["path"]
                for record in spectra_record["flavors"].values()
            ),
        ]
        for cached in cache_artifacts:
            destination = directory / cached.name
            try:
                os.link(cached, destination)
            except OSError:
                shutil.copy2(cached, destination)

    def _completed(self, layout: JobLayout, stage: str) -> bool:
        marker = layout.status(stage)
        return bool(
            self.config.execution.resume
            and marker.exists()
            and read_yaml(marker).get("status") == "completed"
        )

    def generate(self, job: int, *, dry_run: bool = False, force: bool = False) -> None:
        layout = JobLayout.for_job(self.config, job)
        command = self.source.command(self.config, job, layout)
        if dry_run:
            if self.dlpgen_checkout:
                command[0] = str(
                    build_cache_path(
                        self.config.production.output_dir, self.dlpgen_checkout
                    )
                    / "bin"
                    / "dlpgen"
                )
            self._print_plan(job, "generate", command, self.source.output(layout))
            return
        layout.create()
        if self._completed(layout, "generate") and not force:
            self.source.finalize(self.config, layout)
            return
        existing = [
            path for path in self.source.outputs(self.config, layout) if path.exists()
        ]
        if existing:
            if not force:
                raise RuntimeError(
                    f"refusing to overwrite incomplete source output(s): {existing}"
                )
            for path in existing:
                path.unlink()

        environment = None
        inputs = self.source.inputs(self.config, job)
        if self.dlpgen_checkout:
            runtime = ensure_custom_build(
                self.config.production.output_dir, self.dlpgen_checkout
            )
            command[0] = str(runtime.executable)
            environment = runtime.environment
            inputs = [*inputs, runtime.build_manifest]

        def validator() -> dict[str, object]:
            return self.source.finalize(self.config, layout)

        execute_stage(
            stage="generate",
            command=command,
            status_path=layout.status("generate"),
            stdout_path=layout.logs_dir / "generate.stdout.log",
            stderr_path=layout.logs_dir / "generate.stderr.log",
            validator=validator,
            inputs=inputs,
            outputs=self.source.outputs(self.config, layout),
            metadata=self._metadata(job),
            environment=environment,
        )

    def _write_edep_macro(self, job: int, layout: JobLayout) -> None:
        layout.edep_macro.write_text(
            "\n".join(
                [
                    f"/edep/random/randomSeed {self.config.seed(job, 1)}",
                    *self.source.edep_macro_lines(self.config, layout),
                    "/generator/count/fixed/number 1",
                    "/generator/count/set fixed",
                    "/generator/add",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def edep_sim(self, job: int, *, dry_run: bool = False, force: bool = False) -> None:
        layout = JobLayout.for_job(self.config, job)
        command = [
            self.config.software.edep_sim.executable,
            "-C",
            "-g",
            str(self.config.detector.geometry),
            "-p",
            self.config.detector.physics_list,
            "-o",
            str(layout.edep_output),
            "-u",
            "-e",
            str(self.config.production.generator_calls_per_job),
            str(layout.edep_macro),
        ]
        if dry_run:
            self._print_plan(job, "edep-sim", command, layout.edep_output)
            return
        layout.create()
        validate_nonempty(self.source.output(layout))
        if self._completed(layout, "edep-sim") and not force:
            validate_root(layout.edep_output, "EDepSimEvents")
            return
        if layout.edep_output.exists():
            if not force:
                raise RuntimeError(f"refusing to overwrite untracked output: {layout.edep_output}")
            layout.edep_output.unlink()
        self._write_edep_macro(job, layout)
        execute_stage(
            stage="edep-sim",
            command=command,
            status_path=layout.status("edep-sim"),
            stdout_path=layout.logs_dir / "edep-sim.stdout.log",
            stderr_path=layout.logs_dir / "edep-sim.stderr.log",
            validator=lambda: validate_root(layout.edep_output, "EDepSimEvents"),
            inputs=[
                self.source.output(layout),
                layout.edep_macro,
                self.config.detector.geometry,
            ],
            outputs=[layout.edep_output],
            metadata=self._metadata(job),
        )

    def supera(self, job: int, *, dry_run: bool = False, force: bool = False) -> None:
        layout = JobLayout.for_job(self.config, job)
        command = [
            self.config.software.edep2supera.executable,
            "--output",
            str(layout.supera_output),
            "--config",
            str(layout.resolved_supera_config),
            str(layout.edep_output),
        ]
        if dry_run:
            self._print_plan(job, "supera", command, layout.supera_output)
            return
        layout.create()
        validate_root(layout.edep_output, "EDepSimEvents")
        if self._completed(layout, "supera") and not force:
            validate_root(layout.supera_output)
            return
        if layout.supera_output.exists():
            if not force:
                raise RuntimeError(f"refusing to overwrite untracked output: {layout.supera_output}")
            layout.supera_output.unlink()
        with self.config.detector.supera_config.open(encoding="utf-8") as stream:
            supera_config = yaml.safe_load(stream)
        if not isinstance(supera_config, dict):
            raise RuntimeError("Supera configuration must contain a YAML mapping")
        supera_config.setdefault("BBoxConfig", {})["Seed"] = self.config.seed(job, 2)
        write_yaml(layout.resolved_supera_config, supera_config)
        execute_stage(
            stage="supera",
            command=command,
            status_path=layout.status("supera"),
            stdout_path=layout.logs_dir / "supera.stdout.log",
            stderr_path=layout.logs_dir / "supera.stderr.log",
            validator=lambda: validate_root(
                layout.supera_output, "sparse3d_pcluster_tree"
            ),
            inputs=[layout.edep_output, layout.resolved_supera_config],
            outputs=[layout.supera_output],
            metadata=self._metadata(job),
        )

    def validate(self, job: int) -> dict[str, object]:
        layout = JobLayout.for_job(self.config, job)
        return {
            "job": job,
            "source": self.source.finalize(self.config, layout),
            "edep_sim": validate_root(layout.edep_output, "EDepSimEvents"),
            "supera": validate_root(layout.supera_output, "sparse3d_pcluster_tree"),
        }

    def run(self, job: int, *, dry_run: bool = False, force: bool = False) -> None:
        self.generate(job, dry_run=dry_run, force=force)
        self.edep_sim(job, dry_run=dry_run, force=force)
        self.supera(job, dry_run=dry_run, force=force)

    @staticmethod
    def _print_plan(job: int, stage: str, command: list[str], output: Path) -> None:
        print(
            json.dumps(
                {
                    "job": job,
                    "stage": stage,
                    "command": shlex.join(command),
                    "output": str(output),
                },
                sort_keys=True,
            )
        )
