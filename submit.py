#!/usr/bin/env python3
"""Lightweight Slurm/PBS launcher for dlpgen-opt productions.

This file requires only the site-provided PyYAML package. The complete project
configuration is validated and executed inside the production container; the
launcher reads only scheduling fields and submits preparation as compute jobs.
"""

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

# Import only dependency-free helpers from this checkout, never the physics stack.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from dlpgen_opt.scheduling import stages, submit_chain
from dlpgen_opt.slurm_render import render_script as render_run, render_prepare_script

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
    """Read production scheduling fields and resolve GiBUU source overrides."""
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
    source = raw.get("source", {})
    if not isinstance(source, dict):
        raise ValueError("source must be a mapping")
    if source.get("type") == "gibuu" and source.get("config"):
        source_path = Path(source["config"]).expanduser()
        if not source_path.is_absolute():
            source_path = config_path.parent / source_path
        with source_path.open(encoding="utf-8") as stream:
            settings = yaml.safe_load(stream)
        if not isinstance(settings, dict):
            raise ValueError("GiBUU source configuration must be a mapping")
        source = dict(settings, **source)
    cache = source.get("candidate_cache", {})
    if not isinstance(cache, dict):
        raise ValueError("candidate_cache must be a mapping")
    source = SimpleNamespace(**dict(source, candidate_cache=SimpleNamespace(**cache)))
    return {
        "source": source,
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
    if profile.get("scheduler") == "pbs":
        from dlpgen_opt.pbs_profile import normalize_pbs_profile
        return normalize_pbs_profile(profile)
    if profile.get("scheduler", "slurm") != "slurm" or profile.get("container_runtime") == "shifter":
        raise ValueError("submit.py supports Slurm/PBS with Apptainer/Singularity; use dlpgen-opt submit for Shifter")
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
    for key, value in dict(prepare_cpus_per_task=None, prepare_time=None,
                          jobs_per_task=1, qos=None, constraint=None, modules=[],
                          shifter_modules="none", use_srun=False).items():
        profile.setdefault(key, value)
    return profile


def choose_runtime(requested, dry_run=False):
    if requested not in ("auto", "apptainer", "singularity"):
        raise ValueError("unsupported container runtime: " + str(requested))
    if requested != "auto":
        return requested
    for candidate in ("apptainer", "singularity"):
        if shutil.which(candidate):
            return candidate
    if dry_run:
        return "apptainer"
    raise RuntimeError("neither apptainer nor singularity is available on PATH")


def validate_profile(profile):
    if profile.get("scheduler") == "pbs":
        from dlpgen_opt.pbs_profile import validate_pbs_profile
        validate_pbs_profile(profile)
        return
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
    for key in ("prepare_cpus_per_task", "jobs_per_task"):
        value = profile.get(key)
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError(key + " must be a positive integer")
    if profile.get("jobs_per_task", 1) > profile["cpus_per_task"]:
        raise ValueError("jobs_per_task cannot exceed cpus_per_task")
    if profile.get("prepare_time") and not SAFE_TIME.fullmatch(profile["prepare_time"]):
        raise ValueError("invalid prepare_time")
    for key in ("qos", "constraint"):
        if profile.get(key) and not SAFE_SLURM_NAME.fullmatch(str(profile[key])):
            raise ValueError("invalid " + key)
    if not SAFE_TIME.fullmatch(str(profile["time"])):
        raise ValueError("invalid SLURM time value")


def scheduler_config(production):
    source = production.get("source", SimpleNamespace(type="dlpgen"))
    # Renderers only need this flag; the shared planner remains authoritative.
    from dlpgen_opt.scheduling import preparation_settings
    prepare, tasks = preparation_settings(source, production["jobs"])
    if getattr(source, "type", None) == "gibuu":
        source = SimpleNamespace(type="gibuu", mode=getattr(source, "mode", "import"),
            candidate_cache=SimpleNamespace(enabled=prepare, distributed_tasks=tasks))
    return SimpleNamespace(config_path=production["config"], source=source,
        production=SimpleNamespace(name=production["name"], jobs=production["jobs"],
                                   output_dir=production["output_dir"]))


def submit(production, profile, options):
    config = scheduler_config(production)
    runtime = SimpleNamespace(**dict(profile, container_runtime=options.runtime))
    if profile.get("scheduler") == "pbs":
        from dlpgen_opt.pbs_runtime import submit_arrays
        runtime.bind_paths = [Path(path) for path in runtime.bind_paths]
        return submit_arrays(config, runtime, container=options.container,
            max_concurrent=options.max_concurrent, extra_bind_paths=options.bind,
            dry_run=options.dry_run, prepare_round=getattr(options, "prepare_round", 0))
    slurm_dir = production["output_dir"] / "slurm"
    binds = list(dict.fromkeys([str(Path(path).expanduser().resolve())
                               for path in profile["bind_paths"] + options.bind]))
    plan = stages(config, profile["max_array_size"],
                  getattr(options, "prepare_round", 0), profile["jobs_per_task"])
    def render(stage):
        common = dict(container=options.container, job_name=production["name"],
                      log_dir=slurm_dir / "logs", bind_paths=binds)
        if stage.kind == "run":
            return render_run(config, runtime, first_job=stage.first,
                last_job=stage.last, max_concurrent=options.max_concurrent, **common)
        return render_prepare_script(config, runtime,
            phase=None if stage.kind == "prepare" else stage.kind,
            round_index=stage.round_index, first_task=stage.first,
            last_task=stage.last, max_concurrent=options.max_concurrent, **common)
    return submit_chain(plan, slurm_dir, render, "slurm", options.dry_run)


def parser():
    command = argparse.ArgumentParser(
        description="Submit Slurm/PBS jobs without installing the project (Python 3.6+ and PyYAML)."
    )
    command.add_argument("config", help="production YAML file")
    command.add_argument("--profile", default="s3df_milano")
    command.add_argument(
        "--profiles",
        default=str(Path(__file__).resolve().parent / "configs" / "batch" / "sites.yaml"),
        help="site profile YAML file",
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
    command.add_argument("--queue")
    command.add_argument("--cpus-per-task", type=int)
    command.add_argument("--jobs-per-task", type=int, help="production jobs packed into each allocation")
    command.add_argument("--prepare-cpus-per-task", type=int)
    command.add_argument("--prepare-time")
    command.add_argument("--prepare-round", type=int, default=0)
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
        profile = read_profile(options.profiles, options.profile)
        requested_runtime = options.runtime
        if requested_runtime == "auto":
            requested_runtime = profile.get("container_runtime", "auto")
        # PBS initializes the runtime via compute-node modules; it need not
        # exist on the login node. Slurm auto detection retains legacy behavior.
        options.runtime = (requested_runtime if profile.get("scheduler") == "pbs"
                           else choose_runtime(requested_runtime, options.dry_run))
        for argument, key in (
            (options.account, "account"),
            (options.partition, "partition"),
            (options.queue, "queue"),
            (options.cpus_per_task, "cpus_per_task"),
            (options.jobs_per_task, "jobs_per_task"),
            (options.mem_per_cpu, "mem_per_cpu"),
            (options.prepare_cpus_per_task, "prepare_cpus_per_task"),
            (options.prepare_time, "prepare_time"),
            (options.time, "time"),
            (options.max_array_size, "max_array_size"),
        ):
            if argument is not None:
                profile[key] = argument
        validate_profile(profile)
        if profile["account"] == "REPLACE_WITH_PROJECT" and not options.dry_run:
            raise ValueError("Replace REPLACE_WITH_PROJECT with your allocation")

        submit(production, profile, options)
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print("error: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
