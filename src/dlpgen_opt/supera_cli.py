from __future__ import annotations

import argparse
import os
import sys
import traceback


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="dlpgen-opt-supera",
        description="Run edep2supera and exit after its output manager is finalized.",
    )
    command.add_argument("-o", "--output", required=True)
    command.add_argument("-c", "--config", required=True)
    command.add_argument("-n", "--num-events", type=int, default=-1)
    command.add_argument("-s", "--skip", type=int, default=0)
    command.add_argument("inputs", nargs="+")
    return command


def _exit(code: int) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    try:
        from edep2supera import utils

        utils.run_supera(
            out_file=args.output,
            in_files=args.inputs,
            config_key=args.config,
            num_events=args.num_events,
            num_skip=args.skip,
        )
    except BaseException:
        traceback.print_exc()
        _exit(1)
    _exit(0)
