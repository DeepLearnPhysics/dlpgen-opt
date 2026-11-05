"""Human-readable progress, kept separate from machine-readable stdout."""

import sys


def progress(stage: str, message: str) -> None:
    print(f"[{stage}] {message}", file=sys.stderr, flush=True)
