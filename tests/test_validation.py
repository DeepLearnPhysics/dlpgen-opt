from __future__ import annotations

import h5py
import numpy as np
import pytest

from dlpgen_opt.validation import validate_spine_hdf5


def _spine_file(path, interaction_counts):
    with h5py.File(path, "w") as output:
        interactions = output.create_dataset(
            "truth_interactions", (sum(interaction_counts),), dtype=np.int64
        )
        event_dtype = np.dtype(
            [("truth_interactions", h5py.regionref_dtype)]
        )
        events = output.create_dataset("events", (len(interaction_counts),), dtype=event_dtype)
        offset = 0
        for index, count in enumerate(interaction_counts):
            events[index] = (interactions.regionref[offset : offset + count],)
            offset += count
        for name in (
            "index",
            "meta",
            "points_label",
            "depositions_label",
            "truth_particles",
        ):
            output.create_dataset(name, (0,), dtype=np.int64)


def test_spine_validation_accepts_empty_detector_events(tmp_path):
    path = tmp_path / "spine.h5"
    _spine_file(path, [1, 0, 1])

    result = validate_spine_hdf5(path, 3)

    assert result["interactions_per_event"] == [1, 0, 1]
    assert result["empty_events"] == 1


def test_spine_validation_rejects_multiple_interactions(tmp_path):
    path = tmp_path / "spine.h5"
    _spine_file(path, [1, 2])

    with pytest.raises(RuntimeError, match="at most one truth interaction"):
        validate_spine_hdf5(path, 2)
