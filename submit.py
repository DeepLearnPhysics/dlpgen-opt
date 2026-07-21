#!/usr/bin/env python3
"""Minimal S3DF SLURM launcher for dlpgen-opt productions.

This file requires only the site-provided PyYAML package. The complete project
configuration is validated and executed inside the production container; the
launcher reads only the three fields needed to construct scheduler arrays.
"""

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("error: submit.py requires the site-provided PyYAML package", file=sys.stderr)
    sys.exit(2)


SAFE_SLURM_NAME = re.compile(r"^[A-Za-z0-9_.:-]+$")
SAFE_MEMORY = re.compile(r"^[0-9]+[KMGTP]?[bB]?$")
SAFE_TIME = re.compile(
    r"^(?:[0-9]+-[0-9]{2}:[0-9]{2}:[0-9]{2}|[0-9]+:[0-9]{2}:[0-9]{2})$"
)


def read_production(path):
    """Read only production.name/output_dir/jobs from the strict project YAML."""
    config_path = Path(path).expanduser().resolve()
    with config_path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict) or not isinstance(raw.get("production"), dict):
        raise ValueError("production YAML must contain a production mapping")
    values = raw["production"]
    missing = sorted(set(("name", "output_dir", "jobs")) - set(values))
    if missing:
        raise ValueError(
            "production section is missing scheduler field(s): {}".format(
                ", ".join(missing)
            )
        )
    name = values["name"]
    if not isinstance(name, str):
        raise ValueError("production.name must be a string")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", name):
        raise ValueError("invalid production.name: {!r}".format(name))
    jobs = values["jobs"]
    if isinstance(jobs, bool) or not isinstance(jobs, int):
        raise ValueError("production.jobs must be an integer")
    if jobs <= 0:
        raise ValueError("production.jobs must be positive")
    if not isinstance(values["output_dir"], str):
        raise ValueError("production.output_dir must be a string")
    output_dir = Path(values["output_dir"]).expanduser()
    if not output_dir.is_absolute():
        output_dir = (config_path.parent / output_dir).resolve()
    return {
        "config": config_path,
        "name": name,
        "output_dir": output_dir,
        "jobs": jobs,
    }


def read_profile(path, name):
    profile_path = Path(path).expanduser().resolve()
    with profile_path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    profiles = raw.get("profiles") if isinstance(raw, dict) else None
    if not isinstance(profiles, dict) or name not in profiles:
        choices = ", ".join(sorted(profiles or {}))
        raise ValueError("unknown profile {!r}; available profiles: {}".format(name, choices))
    profile = dict(profiles[name])
    required = (
        "account",
        "partition",
        "cpus_per_task",
        "mem_per_cpu",
        "time",
        "max_array_size",
    )
    missing = [key for key in required if key not in profile]
    if missing:
        raise ValueError("profile is missing: {}".format(", ".join(missing)))
    profile.setdefault("bind_paths", ["/sdf"])
    return profile


def choose_runtime(requested, dry_run=False):
    if requested != "auto":
        return requested
    for candidate in ("apptainer", "singularity"):
        if shutil.which(candidate):
            return candidate
    if dry_run:
        return "apptainer"
    raise RuntimeError("neither apptainer nor singularity is available on PATH")


def validate_profile(profile):
    profile["cpus_per_task"] = int(profile["cpus_per_task"])
    profile["max_array_size"] = int(profile["max_array_size"])
    if profile["cpus_per_task"] <= 0 or profile["max_array_size"] <= 0:
        raise ValueError("CPU and array-size values must be positive")
    if not SAFE_SLURM_NAME.fullmatch(str(profile["account"])):
        raise ValueError("invalid SLURM account")
    if not SAFE_SLURM_NAME.fullmatch(str(profile["partition"])):
        raise ValueError("invalid SLURM partition")
    if not SAFE_MEMORY.fullmatch(str(profile["mem_per_cpu"])):
        raise ValueError("invalid --mem-per-cpu value")
    if not SAFE_TIME.fullmatch(str(profile["time"])):
        raise ValueError("invalid SLURM time value")


def render_script(production, profile, options, first_job, last_job):
    array = "{}-{}".format(first_job, last_job)
    task_count = last_job - first_job + 1
    if options.max_concurrent and options.max_concurrent < task_count:
        array += "%{}".format(options.max_concurrent)
    slurm_dir = production["output_dir"] / "slurm"
    log_dir = slurm_dir / "logs"
    binds = list(
        dict.fromkeys([str(path) for path in profile["bind_paths"]] + options.bind)
    )
    tokens = [
        options.runtime,
        "exec",
        "--cleanenv",
        "--env",
        "PYTHONNOUSERSITE=1",
        "--bind",
        ",".join(binds),
        str(options.container),
        "/usr/local/bin/dlpgen-opt-entrypoint",
        "run",
        str(production["config"]),
        "--job",
    ]
    command = " ".join(shlex.quote(token) for token in tokens)
    command += ' "${SLURM_ARRAY_TASK_ID}"'
    return """#!/bin/bash
# Generated by the minimal dlpgen-opt S3DF launcher.
#SBATCH --account={account}
#SBATCH --partition={partition}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={cpus}
#SBATCH --mem-per-cpu={memory}
#SBATCH --time={time}
#SBATCH --array={array}
#SBATCH --job-name={name}
#SBATCH --output={log_dir}/{name}_%A_%a.out
#SBATCH --error={log_dir}/{name}_%A_%a.err

set -euo pipefail

echo "dlpgen-opt production: {name}"
echo "SLURM job/task: ${{SLURM_JOB_ID}}/${{SLURM_ARRAY_TASK_ID}}"
echo "Node: ${{SLURM_NODELIST}}"
echo "Started: $(date --iso-8601=seconds)"

test -r {container}
test -r {config}

{command}
""".format(
        account=profile["account"],
        partition=profile["partition"],
        cpus=profile["cpus_per_task"],
        memory=profile["mem_per_cpu"],
        time=profile["time"],
        array=array,
        name=production["name"],
        log_dir=log_dir,
        container=shlex.quote(str(options.container)),
        config=shlex.quote(str(production["config"])),
        command=command,
    )


def submit(production, profile, options):
    slurm_dir = production["output_dir"] / "slurm"
    log_dir = slurm_dir / "logs"
    dependency = None
    results = []
    for first in range(0, production["jobs"], profile["max_array_size"]):
        last = min(first + profile["max_array_size"], production["jobs"]) - 1
        script = render_script(production, profile, options, first, last)
        script_path = slurm_dir / "submit_{:05d}_{:05d}.sbatch".format(first, last)
        if options.dry_run:
            print("# {}\n{}".format(script_path, script), end="")
            results.append((script_path, None))
            continue

        log_dir.mkdir(parents=True, exist_ok=True)
        script_path.write_text(script, encoding="utf-8")
        script_path.chmod(0o755)
        command = ["sbatch", "--parsable"]
        if dependency:
            command.extend(("--dependency", "afterok:{}".format(dependency)))
        command.append(str(script_path))
        try:
            completed = subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or str(error)).strip()
            raise RuntimeError("sbatch failed for {}: {}".format(script_path, detail))
        job_id = completed.stdout.strip().split(";", 1)[0]
        if not job_id:
            raise RuntimeError("sbatch returned no job ID for {}".format(script_path))
        print("submitted {}: {}".format(script_path, job_id))
        results.append((script_path, job_id))
        dependency = job_id
    return results


def parser():
    command = argparse.ArgumentParser(
        description="Submit dlpgen-opt jobs to S3DF without installing the project."
    )
    command.add_argument("config", help="production YAML file")
    command.add_argument("--profile", default="s3df_milano")
    command.add_argument(
        "--profiles",
        default=str(Path(__file__).resolve().parent / "configs" / "slurm" / "s3df.yaml"),
        help="SLURM profile YAML file",
    )
    command.add_argument(
        "--container",
        default=os.environ.get("DLPGEN_OPT_CONTAINER_PATH"),
        help="pre-staged SIF image (or set DLPGEN_OPT_CONTAINER_PATH)",
    )
    command.add_argument(
        "--runtime",
        choices=("auto", "apptainer", "singularity"),
        default="auto",
    )
    command.add_argument("--account")
    command.add_argument("--partition")
    command.add_argument("--cpus-per-task", type=int)
    command.add_argument("--mem-per-cpu")
    command.add_argument("--time")
    command.add_argument("--max-array-size", type=int)
    command.add_argument("--max-concurrent", type=int)
    command.add_argument("--bind", action="append", default=[])
    command.add_argument("--dry-run", action="store_true")
    return command


def main(argv=None):
    options = parser().parse_args(argv)
    try:
        production = read_production(options.config)
        if not options.container:
            raise ValueError(
                "--container or DLPGEN_OPT_CONTAINER_PATH is required"
            )
        options.container = Path(options.container).expanduser().resolve()
        if not options.dry_run and not options.container.is_file():
            raise ValueError("SIF image does not exist: {}".format(options.container))
        if options.max_concurrent is not None and options.max_concurrent <= 0:
            raise ValueError("--max-concurrent must be positive")
        options.runtime = choose_runtime(options.runtime, options.dry_run)

        profile = read_profile(options.profiles, options.profile)
        for argument, key in (
            (options.account, "account"),
            (options.partition, "partition"),
            (options.cpus_per_task, "cpus_per_task"),
            (options.mem_per_cpu, "mem_per_cpu"),
            (options.time, "time"),
            (options.max_array_size, "max_array_size"),
        ):
            if argument is not None:
                profile[key] = argument
        validate_profile(profile)

        submit(production, profile, options)
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print("error: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
