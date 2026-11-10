from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal

from .batch_runtime import container_command, container_reference
from .scheduling import stages, submit_chain

from .config import ProductionConfig
from .slurm_render import render_script, render_prepare_script


class SlurmProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduler: Literal["slurm"] = "slurm"
    account: str = Field(pattern=r"^[A-Za-z0-9_.:-]+$")
    partition: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    qos: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    constraint: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_&|.!-]+$")
    use_srun: bool = False
    jobs_per_task: int = Field(default=1, gt=0)
    modules: list[str] = Field(default_factory=list)
    shifter_modules: str = Field(default="none", pattern=r"^[A-Za-z0-9_,.-]+$")
    container_runtime: str = Field(default="singularity", pattern=r"^(singularity|apptainer|shifter)$")
    cpus_per_task: int = Field(default=1, gt=0)
    prepare_cpus_per_task: int | None = Field(default=None, gt=0)
    mem_per_cpu: str | None = Field(default="4G", pattern=r"^[0-9]+[KMGTP]?[bB]?$")
    time: str = Field(
        default="02:00:00",
        pattern=r"^(?:[0-9]+-[0-9]{2}:[0-9]{2}:[0-9]{2}|[0-9]+:[0-9]{2}:[0-9]{2})$",
    )
    prepare_time: str | None = Field(
        default=None,
        pattern=r"^(?:[0-9]+-[0-9]{2}:[0-9]{2}:[0-9]{2}|[0-9]+:[0-9]{2}:[0-9]{2})$",
    )
    max_array_size: int = Field(default=99, gt=0)
    bind_paths: list[Path] = Field(default_factory=lambda: [Path("/sdf")])


    @model_validator(mode="after")
    def valid_resources(self):
        if self.partition is None and self.qos is None:
            raise ValueError("Slurm profiles require partition or qos")
        if self.jobs_per_task > self.cpus_per_task:
            raise ValueError("jobs_per_task cannot exceed cpus_per_task")
        if any(not re.fullmatch(r"[A-Za-z0-9_./+-]+", name) for name in self.modules):
            raise ValueError("invalid module name")
        return self


def load_profile(path: str | Path, name: str) -> SlurmProfile:
    profile_path = Path(path).expanduser().resolve()
    with profile_path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), dict):
        raise ValueError(f"SLURM profile file must contain a profiles mapping: {profile_path}")
    if name not in raw["profiles"]:
        choices = ", ".join(sorted(raw["profiles"]))
        raise ValueError(f"unknown SLURM profile {name!r}; available profiles: {choices}")
    return SlurmProfile.model_validate(raw["profiles"][name])


def submit_arrays(
    config: ProductionConfig,
    profile: SlurmProfile,
    *,
    container: str | Path,
    max_concurrent: int | None = None,
    job_name: str | None = None,
    extra_bind_paths: list[str | Path] | None = None,
    dry_run: bool = False,
    prepare_round: int = 0,
) -> list[tuple[Path, str | None]]:
    container_path = container_reference(container, profile.container_runtime, dry_run)
    if max_concurrent is not None and max_concurrent <= 0:
        raise ValueError("max_concurrent must be positive")
    if prepare_round < 0:
        raise ValueError("prepare-round must be nonnegative")

    resolved_name = job_name or config.production.name
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", resolved_name):
        raise ValueError("SLURM job name may contain only letters, numbers, '.', '_' and '-'")

    slurm_dir = config.production.output_dir / "slurm"
    log_dir = slurm_dir / "logs"
    bind_paths = [path.expanduser().resolve() for path in profile.bind_paths]
    bind_paths.extend(Path(path).expanduser().resolve() for path in extra_bind_paths or [])
    bind_paths = list(dict.fromkeys(bind_paths))

    plan = stages(config, profile.max_array_size, prepare_round, profile.jobs_per_task)
    def render(stage):
        options = dict(container=container_path, job_name=resolved_name,
                       log_dir=log_dir, bind_paths=bind_paths)
        if stage.kind == "run":
            return render_script(config, profile, first_job=stage.first,
                last_job=stage.last, max_concurrent=max_concurrent, **options)
        return render_prepare_script(config, profile,
            phase=None if stage.kind == "prepare" else stage.kind,
            round_index=stage.round_index, first_task=stage.first,
            last_task=stage.last, max_concurrent=max_concurrent, **options)
    return submit_chain(plan, slurm_dir, render, "slurm", dry_run)


def default_container() -> str | None:
    return os.environ.get("DLPGEN_OPT_CONTAINER_PATH")


def default_profiles_path() -> Path:
    roots = []
    if os.environ.get("DLPGEN_OPT_ROOT"):
        roots.append(Path(os.environ["DLPGEN_OPT_ROOT"]))
    roots.extend((Path(__file__).resolve().parents[2], Path.cwd()))
    for root in roots:
        candidate = root / "configs" / "batch" / "sites.yaml"
        if candidate.is_file():
            return candidate
    return roots[0] / "configs" / "batch" / "sites.yaml"
