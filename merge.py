#!/usr/bin/env python3
"""Prepare and submit deterministic train/test ROOT merges on Slurm or ALCF PBS.

This standalone launcher needs only the site-provided PyYAML package. ROOT and
``hadd`` run inside the same production SIF used for simulation jobs.
"""

import argparse
import importlib.util
import os
import random
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

try:
    import yaml
except ImportError:
    print("error: merge.py requires the site-provided PyYAML package", file=sys.stderr)
    sys.exit(2)


def load_production_submit():
    """Load the sibling launcher without requiring a project installation."""
    path = Path(__file__).resolve().with_name("submit.py")
    spec = importlib.util.spec_from_file_location("dlpgen_opt_submit", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load standalone production launcher: {}".format(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


production_submit = load_production_submit()


SIZE_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?I?B)?$", re.IGNORECASE)
SIZE_MULTIPLIERS = {
    "B": 1,
    "KB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
    "KIB": 1 << 10,
    "MIB": 1 << 20,
    "GIB": 1 << 30,
    "TIB": 1 << 40,
}
LARCV_PRELOAD = (
    "/usr/lib/x86_64-linux-gnu/libpython3.10.so:"
    "/app/larcv2/build/lib/liblarcv.so"
)


def parse_size(value):
    match = SIZE_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError("invalid size {!r}; use e.g. 80GB or 75GiB".format(value))
    number = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    size = int(number * SIZE_MULTIPLIERS[unit])
    if size <= 0:
        raise ValueError("maximum file size must be positive")
    return size


def read_yaml(path):
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ValueError("expected a YAML mapping in {}".format(path))
    return value


def read_production(directory, allow_missing=False):
    root = Path(directory).expanduser().resolve()
    manifest_path = root / "manifest.yaml"
    resolved_path = root / "resolved_config.yaml"
    if not manifest_path.is_file() or not resolved_path.is_file():
        raise ValueError("production directory needs manifest.yaml and resolved_config.yaml")

    manifest = read_yaml(manifest_path)
    resolved = read_yaml(resolved_path)
    name = manifest.get("production")
    production = resolved.get("production")
    if not isinstance(name, str) or not isinstance(production, dict):
        raise ValueError("production metadata is incomplete in {}".format(root))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError("invalid production name in manifest")
    jobs = production.get("jobs")
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs <= 0:
        raise ValueError("resolved production.jobs must be a positive integer")

    inputs = []
    missing = []
    for job in range(jobs):
        job_root = root / "jobs" / "{:05d}".format(job)
        status_path = job_root / "supera.yaml"
        output_path = job_root / "supera" / "supera.root"
        completed = False
        if status_path.is_file():
            completed = read_yaml(status_path).get("status") == "completed"
        if not completed or not output_path.is_file() or output_path.stat().st_size <= 0:
            missing.append(job)
            continue
        inputs.append(output_path.resolve())

    if missing and not allow_missing:
        preview = ", ".join(str(job) for job in missing[:10])
        suffix = " ..." if len(missing) > 10 else ""
        raise ValueError(
            "{} of {} jobs lack completed Supera output: {}{}; "
            "use --allow-missing to merge the completed subset".format(
                len(missing), jobs, preview, suffix
            )
        )
    if len(inputs) < 2:
        raise ValueError("at least two completed Supera files are required")
    return {"root": root, "name": name, "inputs": inputs, "missing": missing}


def split_inputs(inputs, train_fraction, seed):
    if not 0.0 < train_fraction < 1.0:
        raise ValueError("train fraction must be strictly between zero and one")
    shuffled = list(inputs)
    random.Random(seed).shuffle(shuffled)
    train_count = max(1, min(len(shuffled) - 1, int(len(shuffled) * train_fraction)))
    return {"train": shuffled[:train_count], "test": shuffled[train_count:]}


def chunk_inputs(inputs, max_bytes):
    chunks = []
    current = []
    current_bytes = 0
    for path in inputs:
        size = path.stat().st_size
        if size > max_bytes:
            raise ValueError(
                "input {} is {} bytes, larger than the configured limit {}".format(
                    path, size, max_bytes
                )
            )
        if current and current_bytes + size > max_bytes:
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(path)
        current_bytes += size
    if current:
        chunks.append(current)
    return chunks


def build_tasks(production, split_files, max_bytes, output_dir):
    tasks = []
    for split_name in ("train", "test"):
        chunks = chunk_inputs(split_files[split_name], max_bytes)
        for chunk_index, files in enumerate(chunks):
            stem = "{}_{}".format(production["name"], split_name)
            if len(chunks) > 1:
                stem += "_{}".format(chunk_index)
            output = output_dir / "{}.root".format(stem)
            tasks.append(
                {
                    "split": split_name,
                    "chunk": chunk_index,
                    "inputs": files,
                    "input_bytes": sum(path.stat().st_size for path in files),
                    "output": output,
                }
            )
    return tasks


def prepare_plan(production, tasks, output_dir, train_fraction, seed, max_bytes, scheduler="slurm"):
    filelist_dir = output_dir / "filelists"
    slurm_dir = output_dir / scheduler
    log_dir = slurm_dir / "logs"
    for directory in (output_dir, filelist_dir, slurm_dir, log_dir):
        directory.mkdir(parents=True, exist_ok=True)

    for task in tasks:
        filelist = filelist_dir / "{}_{}_{}.txt".format(
            production["name"], task["split"], task["chunk"]
        )
        filelist.write_text(
            "".join("{}\n".format(path) for path in task["inputs"]),
            encoding="utf-8",
        )
        task["filelist"] = filelist

    task_manifest = output_dir / "merge_tasks.tsv"
    task_manifest.write_text(
        "".join("{}\t{}\n".format(task["output"], task["filelist"]) for task in tasks),
        encoding="utf-8",
    )
    plan = {
        "production": production["name"],
        "production_dir": str(production["root"]),
        "train_fraction": train_fraction,
        "shuffle_seed": seed,
        "max_input_bytes_per_output": max_bytes,
        "missing_jobs": production["missing"],
        "tasks": [
            {
                "split": task["split"],
                "chunk": task["chunk"],
                "input_count": len(task["inputs"]),
                "input_bytes": task["input_bytes"],
                "filelist": str(task["filelist"]),
                "output": str(task["output"]),
            }
            for task in tasks
        ],
    }
    with (output_dir / "merge_plan.yaml").open("w", encoding="utf-8") as stream:
        # S3DF login nodes currently provide a PyYAML version predating the
        # ``sort_keys`` keyword. Plan key ordering is cosmetic, so retain
        # compatibility with that site-provided package.
        yaml.safe_dump(plan, stream)
    return task_manifest, slurm_dir, log_dir


def render_script(production, profile, options, task_manifest, log_dir, first, last):
    task_count = last - first + 1
    array = "0-{}".format(task_count - 1)
    if options.max_concurrent and options.max_concurrent < task_count:
        array += "%{}".format(options.max_concurrent)
    command = merge_command(production, profile, options, task_manifest)
    if profile.get('use_srun'):
        command = 'srun --nodes=1 --ntasks=1 --cpus-per-task={} --cpu-bind=cores '.format(profile['cpus_per_task']) + command
    directives = []
    for key in ('partition', 'mem_per_cpu', 'qos', 'constraint'):
        if profile.get(key) is not None:
            directives.append('#SBATCH --{}={}'.format(key.replace('_', '-'), profile[key]))
    modules = '\n'.join('module load '+shlex.quote(str(name)) for name in profile.get('modules', []))
    return """#!/bin/bash
# Generated by the standalone dlpgen-opt merge launcher.
#SBATCH --account={account}
{directives}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={cpus}
#SBATCH --time={time}
#SBATCH --array={array}
#SBATCH --job-name={name}_hadd
#SBATCH --output={log_dir}/{name}_hadd_%A_%a.out
#SBATCH --error={log_dir}/{name}_hadd_%A_%a.err

set -euo pipefail
{modules}

TASK_INDEX=$((SLURM_ARRAY_TASK_ID + {first}))
TASK_LINE=$(sed -n "$((TASK_INDEX + 1))p" {task_manifest})
IFS=$'\t' read -r OUTPUT FILE_LIST <<< "$TASK_LINE"
test -n "$OUTPUT"
test -s "$FILE_LIST"
mkdir -p "$(dirname "$OUTPUT")"

echo "dlpgen-opt merge task: ${{SLURM_JOB_ID}}/${{SLURM_ARRAY_TASK_ID}}"
echo "Merge task index: $TASK_INDEX"
echo "File list: $FILE_LIST"
echo "Output: $OUTPUT"
echo "Started: $(date --iso-8601=seconds)"

{merge_command}

test -s "$OUTPUT"
echo "Merge task $TASK_INDEX completed successfully"
echo "Completed: $(date --iso-8601=seconds)"
""".format(
        account=profile["account"],
        directives="\n".join(directives),
        modules=modules,
        merge_command=command,
        cpus=profile["cpus_per_task"],
        time=profile["time"],
        array=array,
        name=production["name"],
        log_dir=shlex.quote(str(log_dir)),
        first=first,
        task_manifest=shlex.quote(str(task_manifest)),
    )


def merge_command(production, profile, options, task_manifest):
    from dlpgen_opt.batch_runtime import container_command
    cpus = profile['cpus_per_task']
    # The input paths are resolved, including symlinks outside the production.
    binds = list(map(str, profile['bind_paths']))
    binds += [str(production['root']), str(task_manifest.parent)]
    binds += sorted(set(str(path.parent) for path in production['inputs']
                        if production['root'] not in path.parents))
    binds += options.bind
    binds = list(dict.fromkeys(binds))
    command = container_command(options.runtime, options.container, binds,
                                cpus=str(cpus), fakeroot=profile.get('fakeroot', False),
                                shifter_modules=profile.get('shifter_modules', 'none'))
    # One output at a time: hadd parallelizes within the node CPU budget.
    # Temporary partial outputs live on the output filesystem, not node /tmp.
    inner = ('set -euo pipefail; '
             'work=$(mktemp -d "$(dirname "$1")/.hadd.XXXXXX"); '
             'trap \'rm -rf -- "$work"\' EXIT; '
             'export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1; '
             'LD_PRELOAD=' + LARCV_PRELOAD + ' hadd ' + ('-f ' if options.force else '') +
             '-j ' + str(cpus) + ' -d "$work" "$1" "@$2"')
    command += ' bash -lc ' + shlex.quote(inner) + ' _ "$OUTPUT" "$FILE_LIST"'
    return command


def render_pbs_merge(production, profile, options, task_manifest, log_dir):
    from dlpgen_opt.pbs_runtime import pbs_directives
    runtime = SimpleNamespace(**profile)
    cpus = profile['cpus_per_task']
    lines = pbs_directives(runtime, production['name'] + '_hadd', cpus, profile['time'])
    lines += ['set -euo pipefail',
              'cd ' + shlex.quote(str(task_manifest.parent)),
              'exec >' + shlex.quote(str(log_dir)) + '/"merge_${PBS_JOBID}.out" '
              '2>' + shlex.quote(str(log_dir)) + '/"merge_${PBS_JOBID}.err"']
    lines += ['module use ' + shlex.quote(str(path)) for path in profile['module_paths']]
    lines += ['module load ' + shlex.quote(name) for name in profile['modules']]
    command = merge_command(production, profile, options, task_manifest)
    lines += ['TASK_INDEX=0',
              "while IFS=$'\\t' read -r OUTPUT FILE_LIST; do",
              '  test -n "$OUTPUT"', '  test -s "$FILE_LIST"',
              '  mkdir -p "$(dirname "$OUTPUT")"',
              '  echo "[merge] Starting task $TASK_INDEX: $OUTPUT"',
              '  mpiexec -n 1 --ppn 1 --depth ' + str(cpus) +
              ' --cpu-bind ' + profile['cpu_bind'] + ' ' + command + ' < /dev/null',
              '  test -s "$OUTPUT"',
              '  echo "[merge] Completed task $TASK_INDEX: $OUTPUT"',
              '  TASK_INDEX=$((TASK_INDEX + 1))',
              'done < ' + shlex.quote(str(task_manifest)),
              'echo "[merge] Completed $TASK_INDEX merge tasks"']
    return '\n'.join(lines) + '\n'


def submit_pbs_merge(production, profile, options, task_manifest, directory, log_dir, task_count):
    from dlpgen_opt.scheduling import Stage, submit_chain
    stage = Stage('run', 0, task_count-1, jobs_per_task=task_count)
    render = lambda _: render_pbs_merge(production, profile, options, task_manifest, log_dir)
    if options.prepare_only:
        path = directory / (stage.name + '.pbs')
        path.write_text(render(stage), encoding='utf-8')
        print('prepared {}'.format(path))
        return [(path, None)]
    return submit_chain([stage], directory, render, 'pbs')


def submit_tasks(production, profile, options, task_manifest, slurm_dir, log_dir, task_count):
    if profile.get("scheduler") == "pbs":
        return submit_pbs_merge(production, profile, options, task_manifest,
                                slurm_dir, log_dir, task_count)
    results = []
    dependency = None
    maximum = profile["max_array_size"]
    for first in range(0, task_count, maximum):
        last = min(first + maximum, task_count) - 1
        script = render_script(
            production, profile, options, task_manifest, log_dir, first, last
        )
        script_path = slurm_dir / "merge_{:05d}_{:05d}.sbatch".format(first, last)
        script_path.write_text(script, encoding="utf-8")
        script_path.chmod(0o755)
        if options.prepare_only:
            print("prepared {}".format(script_path))
            results.append((script_path, None))
            continue
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
        description="Split and hadd a completed dlpgen-opt production on S3DF, Aurora, Polaris or NERSC."
    )
    command.add_argument("production_dir")
    command.add_argument("--train-fraction", type=float, default=0.8)
    command.add_argument("--seed", type=int, default=12345)
    command.add_argument("--max-file-size", default="80GB")
    command.add_argument("--output-dir")
    command.add_argument("--allow-missing", action="store_true")
    command.add_argument("--force", action="store_true")
    command.add_argument("--prepare-only", action="store_true")
    command.add_argument("--profile", default="s3df_milano")
    command.add_argument(
        "--profiles",
        default=str(Path(__file__).resolve().parent / "configs" / "batch" / "sites.yaml"),
    )
    command.add_argument(
        "--container",
        default=os.environ.get("DLPGEN_OPT_CONTAINER_PATH"),
    )
    command.add_argument(
        "--runtime", choices=("auto", "apptainer", "singularity", "shifter"), default="auto"
    )
    command.add_argument("--account")
    command.add_argument("--partition")
    command.add_argument("--queue")
    command.add_argument("--cpus-per-task", type=int)
    command.add_argument("--mem-per-cpu")
    command.add_argument("--time")
    command.add_argument("--max-array-size", type=int)
    command.add_argument("--max-concurrent", type=int)
    command.add_argument("--bind", action="append", default=[])
    return command


def main(argv=None):
    options = parser().parse_args(argv)
    try:
        if not options.container:
            raise ValueError("--container or DLPGEN_OPT_CONTAINER_PATH is required")
        if options.max_concurrent is not None and options.max_concurrent <= 0:
            raise ValueError("--max-concurrent must be positive")
        profile = production_submit.read_profile(options.profiles, options.profile, allow_shifter=True)
        requested_runtime = options.runtime
        if profile.get("container_runtime") == "shifter":
            if requested_runtime not in ('auto', 'shifter'):
                raise ValueError('the NERSC Shifter profile requires --runtime shifter or auto')
            options.runtime = 'shifter'
        elif requested_runtime == 'shifter':
            raise ValueError('use the nersc_cpu profile or a Shifter site profile')
        elif profile.get("scheduler") == "pbs":
            options.runtime = (profile["container_runtime"] if requested_runtime == "auto"
                               else requested_runtime)
        else:
            options.runtime = production_submit.choose_runtime(
                requested_runtime, dry_run=options.prepare_only)

        from dlpgen_opt.batch_runtime import container_reference
        options.container = container_reference(options.container, options.runtime, options.prepare_only)
        for argument, key in (
            (options.account, "account"),
            (options.partition, "partition"),
            (options.queue, "queue"),
            (options.cpus_per_task, "cpus_per_task"),
            (options.mem_per_cpu, "mem_per_cpu"),
            (options.time, "time"),
            (options.max_array_size, "max_array_size"),
        ):
            if argument is not None:
                profile[key] = argument
        # Merge tasks have their own scheduling; production packing is irrelevant.
        profile["jobs_per_task"] = 1
        production_submit.validate_profile(profile)

        production = read_production(options.production_dir, options.allow_missing)
        split_files = split_inputs(
            production["inputs"], options.train_fraction, options.seed
        )
        max_bytes = parse_size(options.max_file_size)
        output_dir = (
            Path(options.output_dir).expanduser().resolve()
            if options.output_dir
            else production["root"] / "merged"
        )
        tasks = build_tasks(production, split_files, max_bytes, output_dir)
        task_manifest, slurm_dir, log_dir = prepare_plan(
            production,
            tasks,
            output_dir,
            options.train_fraction,
            options.seed,
            max_bytes,
            scheduler=profile.get("scheduler", "slurm"),
        )
        print(
            "prepared {} merge task(s): {} train file(s), {} test file(s)".format(
                len(tasks), len(split_files["train"]), len(split_files["test"])
            )
        )
        submit_tasks(
            production,
            profile,
            options,
            task_manifest,
            slurm_dir,
            log_dir,
            len(tasks),
        )
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print("error: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
