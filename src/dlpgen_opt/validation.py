from __future__ import annotations

import csv
import warnings
from pathlib import Path

from .provenance import checksum


def validate_nonempty(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise RuntimeError(f"expected output does not exist: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise RuntimeError(f"expected output is empty: {path}")
    return {"path": str(path), "bytes": size, "sha256": checksum(path)}


def validate_source_csv(path: Path, expected_calls: int) -> dict[str, object]:
    result = validate_nonempty(path)
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    required = {
        "call_id", "interaction_id", "status_code", "pdg_code", "px", "py",
        "pz", "energy", "mass", "x", "y", "z", "t",
    }
    if not rows or not required.issubset(rows[0]):
        raise RuntimeError(f"DLPGenerator CSV is missing required data: {path}")
    calls = {int(row["call_id"]) for row in rows}
    if calls != set(range(expected_calls)):
        raise RuntimeError(f"expected calls 0..{expected_calls - 1}, found {sorted(calls)}")
    interactions = {(int(r["call_id"]), int(r["interaction_id"])) for r in rows}
    result.update({"rows": len(rows), "calls": len(calls), "interactions": len(interactions)})
    return result


def validate_root(path: Path, tree: str | None = None) -> dict[str, object]:
    result = validate_nonempty(path)
    try:
        import ROOT  # type: ignore
    except ImportError as error:
        raise RuntimeError("ROOT Python bindings are required for ROOT-file validation") from error
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        root_file = ROOT.TFile.Open(str(path), "READ")
    if not root_file or root_file.IsZombie():
        raise RuntimeError(f"corrupt ROOT file: {path}")
    keys = int(root_file.GetNkeys())
    if keys < 1:
        root_file.Close()
        raise RuntimeError(f"ROOT file contains no keys: {path}")
    result["keys"] = keys
    if tree:
        value = root_file.Get(tree)
        if not value:
            root_file.Close()
            raise RuntimeError(f"ROOT file does not contain {tree}: {path}")
        entries = int(value.GetEntries())
        if entries < 1:
            root_file.Close()
            raise RuntimeError(f"ROOT tree {tree} is empty: {path}")
        result.update({"tree": tree, "entries": entries})
    root_file.Close()
    return result
