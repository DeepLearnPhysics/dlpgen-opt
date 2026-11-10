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

from .config import GiBUUSource, ProductionConfig


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


def _directive(value: str, field: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError(f"SLURM {field} must be a single line")
    return value


def _resource_directives(profile):
    lines = ["#SBATCH --nodes=1"]
    for flag, value in (("partition",profile.partition),("qos",profile.qos),
                        ("constraint",profile.constraint),("mem-per-cpu",profile.mem_per_cpu)):
        if value is not None:
            lines.append(f"#SBATCH --{flag}={_directive(str(value),flag)}")
    return "\n".join(lines)


def _module_setup(profile):
    return "\n".join("module load " + shlex.quote(name) for name in profile.modules)


def render_script(
    config: ProductionConfig,
    profile: SlurmProfile,
    *,
    container: Path,
    first_job: int,
    last_job: int,
    max_concurrent: int | None,
    job_name: str,
    log_dir: Path,
    bind_paths: list[Path],
) -> str:
    import math
    task_count = math.ceil((last_job - first_job + 1) / profile.jobs_per_task)
    array = f"0-{task_count - 1}"
    if max_concurrent is not None and max_concurrent < task_count:
        array += f"%{max_concurrent}"
    config_path = str(config.config_path)
    command = container_command(profile.container_runtime, container, bind_paths,
        shifter_modules=profile.shifter_modules)
    if profile.jobs_per_task == 1:
        command += f' run {shlex.quote(config_path)} --job "$((SLURM_ARRAY_TASK_ID + {first_job}))"'
    else:
        command += (f' python3 -m dlpgen_opt.batch_worker {shlex.quote(config_path)}'
            f' --first "$((SLURM_ARRAY_TASK_ID * {profile.jobs_per_task} + {first_job}))"'
            f' --last {last_job} --count {profile.jobs_per_task} --cpus {profile.cpus_per_task}'
            f' --log-dir {shlex.quote(str(log_dir / "packed"))}')
    if profile.use_srun or profile.container_runtime == "shifter":
        command = f"srun --nodes=1 --ntasks=1 --cpus-per-task={profile.cpus_per_task} --cpu-bind=cores " + command
    elif profile.jobs_per_task > 1:
        command = f"env DLPGEN_OPT_CPUS_PER_TASK={profile.cpus_per_task} " + command
    resources = _resource_directives(profile)
    setup = _module_setup(profile)
    image_check = "" if profile.container_runtime == "shifter" else f"test -r {shlex.quote(str(container))}"
    invalid_dependency = ("#SBATCH --kill-on-invalid-dep=yes\n"
                          if isinstance(config.source, GiBUUSource) and config.source.candidate_cache.distributed_tasks else "")
    return f"""#!/bin/bash
# Generated by dlpgen-opt for Slurm.
#SBATCH --account={_directive(profile.account, "account")}
{resources}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={profile.cpus_per_task}
#SBATCH --time={profile.time}
#SBATCH --array={array}
{invalid_dependency}#SBATCH --job-name={_directive(job_name, "job name")}
#SBATCH --output={log_dir}/{job_name}_%A_%a.out
#SBATCH --error={log_dir}/{job_name}_%A_%a.err

set -euo pipefail
{setup}

echo "dlpgen-opt production: {config.production.name}"
echo "SLURM job/task: ${{SLURM_JOB_ID}}/${{SLURM_ARRAY_TASK_ID}}"
echo "First production job index: $((SLURM_ARRAY_TASK_ID * {profile.jobs_per_task} + {first_job}))"
echo "Node: ${{SLURM_NODELIST}}"
echo "Started: $(date --iso-8601=seconds)"

{image_check}
test -r {shlex.quote(config_path)}

{command}

echo "dlpgen-opt job ${{SLURM_JOB_ID}}/${{SLURM_ARRAY_TASK_ID}} completed successfully"
echo "Completed: $(date --iso-8601=seconds)"
"""


def render_prepare_script(
    config: ProductionConfig,
    profile: SlurmProfile,
    *,
    container: Path,
    job_name: str,
    log_dir: Path,
    bind_paths: list[Path],
    phase: str | None = None,
    round_index: int = 0,
    first_task: int = 0,
    last_task: int | None = None,
    max_concurrent: int | None = None,
) -> str:
    prepare_cpus = profile.prepare_cpus_per_task or profile.cpus_per_task
    command = container_command(profile.container_runtime, container, bind_paths,
        shifter_modules=profile.shifter_modules)
    # Expand allocation outside the clean container environment.
    cpu_env = f'--env "SLURM_CPUS_PER_TASK=${{SLURM_CPUS_PER_TASK:-{prepare_cpus}}}"'
    command = command.replace("--env PYTHONNOUSERSITE=1", f"--env PYTHONNOUSERSITE=1 {cpu_env}", 1) if profile.container_runtime != "shifter" else command.replace("--env=PYTHONNOUSERSITE=1", f"--env=PYTHONNOUSERSITE=1 {cpu_env}", 1)
    command += " " + ("prepare-gibuu-distributed" if phase else "prepare") + " " + shlex.quote(str(config.config_path))
    if profile.use_srun or profile.container_runtime == "shifter":
        command = f"srun --nodes=1 --ntasks=1 --cpus-per-task={prepare_cpus} --cpu-bind=cores " + command
    resources = _resource_directives(profile)
    setup = _module_setup(profile)
    array_directive = ""
    suffix = "prepare"
    log_id = "%j"
    if phase:
        array_directive = "#SBATCH --kill-on-invalid-dep=yes\n"
        suffix = f"prepare_r{round_index}_{phase}_{first_task:05d}"
        command += f" --phase {shlex.quote(phase)} --round {round_index}"
        if phase == "worker":
            count = last_task - first_task + 1
            array = f"0-{count - 1}"
            if max_concurrent is not None and max_concurrent < count:
                array += f"%{max_concurrent}"
            array_directive += f"#SBATCH --array={array}\n"
            command += f' --task "$((SLURM_ARRAY_TASK_ID + {first_task}))"'
            log_id = "%A_%a"
    return f"""#!/bin/bash
# Generated GiBUU candidate-cache preparation job.
#SBATCH --account={_directive(profile.account, "account")}
{resources}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={prepare_cpus}
#SBATCH --time={profile.prepare_time or profile.time}
{array_directive}#SBATCH --job-name={_directive(job_name + "_" + suffix, "job name")}
#SBATCH --output={log_dir}/{job_name}_{suffix}_{log_id}.out
#SBATCH --error={log_dir}/{job_name}_{suffix}_{log_id}.err

set -euo pipefail
{setup}

{command}
"""


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
        candidate = root / "configs" / "slurm" / "s3df.yaml"
        if candidate.is_file():
            return candidate
    return roots[0] / "configs" / "slurm" / "s3df.yaml"
