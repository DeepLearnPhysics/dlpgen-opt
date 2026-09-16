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


def validate_spine_hdf5(path: Path, expected_entries: int) -> dict[str, object]:
    """Validate the event boundary and core truth products in SPINE output."""
    result = validate_nonempty(path)
    try:
        import h5py
    except ImportError as error:
        raise RuntimeError("h5py is required for SPINE HDF5 validation") from error

    required = {
        "events",
        "index",
        "meta",
        "points_label",
        "depositions_label",
        "truth_particles",
        "truth_interactions",
    }
    with h5py.File(path, "r") as hdf5_file:
        missing = sorted(required.difference(hdf5_file.keys()))
        if missing:
            raise RuntimeError(f"SPINE HDF5 output is missing products: {missing}")
        entries = len(hdf5_file["events"])
        if entries != expected_entries:
            raise RuntimeError(
                f"expected {expected_entries} SPINE events, found {entries}: {path}"
            )
        interaction_refs = hdf5_file["events"]["truth_interactions"]
        interactions_per_event = [
            len(hdf5_file["truth_interactions"][reference])
            for reference in interaction_refs
        ]
        if interactions_per_event != [1] * expected_entries:
            raise RuntimeError(
                "expected exactly one truth interaction per SPINE event, found "
                f"{interactions_per_event}: {path}"
            )
        result.update(
            {
                "entries": entries,
                "truth_particles": len(hdf5_file["truth_particles"]),
                "truth_interactions": len(hdf5_file["truth_interactions"]),
                "interactions_per_event": interactions_per_event,
            }
        )
    return result
