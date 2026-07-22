from __future__ import annotations

import json
import os
import shlex
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import fcntl

import yaml

from .config import DLPGeneratorSource, GenieSource, ProductionConfig
from .dlpgen_build import (
    CheckoutSnapshot,
    build_cache_path,
    ensure_custom_build,
    inspect_checkout,
)
from .layout import JobLayout
from .provenance import (
    dependency_commits,
    host_info,
    read_yaml,
    write_yaml,
)
from .runner import execute_stage
from .sources import DLPGeneratorBackend, GenieBackend, SourceBackend
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


class Pipeline:
    def __init__(self, config: ProductionConfig, repository: Path | None = None):
        self.config = config
        self.repository = repository or Path(
            os.environ.get("DLPGEN_OPT_ROOT", Path(__file__).resolve().parents[2])
        )
        self.source: SourceBackend = (
            DLPGeneratorBackend()
            if isinstance(config.source, DLPGeneratorSource)
            else GenieBackend()
        )
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
        manifest_path = root / "manifest.yaml"
        # A referenced GENIE configuration is fully expanded in resolved_config.yaml.
        # Once its immutable catalog summary is recorded, subsequent array tasks can
        # skip both catalog enumeration and remote flux-file access.
        if isinstance(self.config.source, GenieSource) and manifest_path.exists():
            return
        commits = self._dependency_commits()
        expected = {
            "edep-sim": self.config.software.edep_sim.expected_commit,
            "SuperaAtomic": self.config.software.supera_atomic.expected_commit,
            "edep2supera": self.config.software.edep2supera.expected_commit,
        }
        if isinstance(self.config.source, DLPGeneratorSource):
            expected["DLPGenerator"] = self.config.source.expected_commit
        else:
            expected["GENIE"] = self.config.source.expected_commit
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
        else:
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
        if self.dlpgen_checkout and manifest_path.exists():
            if read_yaml(manifest_path) != manifest:
                raise RuntimeError(
                    "production directory was initialized with different inputs: {}".format(
                        root
                    )
                )
        else:
            write_yaml(manifest_path, manifest)

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
        existing = [path for path in self.source.outputs(layout) if path.exists()]
        if existing and not force:
            raise RuntimeError(f"refusing to overwrite incomplete source output(s): {existing}")

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
            outputs=self.source.outputs(layout),
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
