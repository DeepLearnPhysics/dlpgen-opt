from __future__ import annotations

import argparse
import json
import sys

from pydantic import ValidationError

from .config import load_config
from .pipeline import Pipeline
from .slurm import default_container, default_profiles_path, load_profile, submit_arrays


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="dlpgen-opt",
        description="Run reproducible DLPGenerator detector-simulation productions.",
    )
    root.add_argument("--version", action="version", version="%(prog)s 0.1.3")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("run", "generate", "edep-sim", "supera", "validate"):
        command = commands.add_parser(name)
        command.add_argument("config", help="production YAML file")
        command.add_argument("--job", type=int, help="run only this zero-based job")
        if name != "validate":
            command.add_argument("--dry-run", action="store_true")
            command.add_argument(
                "--force", action="store_true", help="replace an incomplete stage output"
            )
    submit = commands.add_parser("submit", help="submit the production as S3DF SLURM arrays")
    submit.add_argument("config", help="production YAML file")
    submit.add_argument("--profile", default="s3df_milano")
    submit.add_argument("--profiles", default=default_profiles_path())
    submit.add_argument(
        "--container",
        default=default_container(),
        help="pre-staged Singularity image (or set DLPGEN_OPT_CONTAINER_PATH)",
    )
    submit.add_argument("--max-concurrent", type=int)
    submit.add_argument("--job-name")
    submit.add_argument("--bind", action="append", default=[], help="additional bind path")
    submit.add_argument(
        "--dry-run", action="store_true", help="print scripts without writing or submitting"
    )
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "submit":
            if not args.container:
                raise ValueError(
                    "--container or DLPGEN_OPT_CONTAINER_PATH is required; "
                    "use a pre-staged .sif on /sdf"
                )
            profile = load_profile(args.profiles, args.profile)
            submit_arrays(
                config,
                profile,
                container=args.container,
                max_concurrent=args.max_concurrent,
                job_name=args.job_name,
                extra_bind_paths=args.bind,
                dry_run=args.dry_run,
            )
            return 0
        pipeline = Pipeline(config)
        jobs = [args.job] if args.job is not None else list(range(config.production.jobs))
        if any(job < 0 or job >= config.production.jobs for job in jobs):
            raise ValueError(f"job must be in [0, {config.production.jobs - 1}]")
        dry_run = getattr(args, "dry_run", False)
        if not dry_run:
            pipeline.initialize()
        for job in jobs:
            if args.command == "validate":
                print(json.dumps(pipeline.validate(job), indent=2, sort_keys=True))
            else:
                method = getattr(pipeline, args.command.replace("-", "_"))
                method(job, dry_run=dry_run, force=args.force)
        return 0
    except (OSError, RuntimeError, ValueError, ValidationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
