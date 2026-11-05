from pathlib import Path
from types import SimpleNamespace
import math
import sys

import pytest

from dlpgen_opt.flux_spectra import _flux_histograms


def project(monkeypatch, tmp_path, rows):
    events = [
        SimpleNamespace(pdg=pdg, energy_gev=energy, flux_weight_per_cm2=weight)
        for pdg, energy, weight in rows
    ]
    closed = []
    root_file = SimpleNamespace(
        IsZombie=lambda: False,
        Get=lambda name: events,
        Close=lambda: closed.append(True),
    )
    monkeypatch.setitem(
        sys.modules, "ROOT",
        SimpleNamespace(TFile=SimpleNamespace(Open=lambda path, mode: root_file)),
    )
    try:
        return _flux_histograms(
            Path("canonical.root"), tmp_path,
            energy_min=0.0, energy_max=2.0, bins=2,
        )
    finally:
        assert closed == [True]


def test_signed_contributions_are_summed_before_sampling(monkeypatch, tmp_path):
    result = project(monkeypatch, tmp_path, [
        (14, 0.5, 3.0), (14, 0.5, -1.0), (14, 2.0, 4.0),
    ])
    assert result[14]["integral_per_cm2"] == 6.0
    assert (tmp_path / "flux-14.dat").read_text().splitlines()[1:] == [
        "0.5 2", "1.5 4",
    ]


def test_negative_bin_rejected_even_with_positive_total(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="pdg=14, bin=0"):
        project(monkeypatch, tmp_path, [
            (12, 0.5, 1.0), (14, 0.5, -1.0), (14, 1.5, 3.0),
        ])
    assert not list(tmp_path.glob("*.dat"))


def test_exact_cancellation_produces_zero_bin(monkeypatch, tmp_path):
    result = project(monkeypatch, tmp_path, [
        (14, 0.5, 1.0), (14, 0.5, -1.0), (14, 1.5, 2.0),
    ])
    assert result[14]["integral_per_cm2"] == 2.0


@pytest.mark.parametrize("weights", [[-1.0], [1.0, -1.0]])
def test_outside_signed_weights_cannot_evade_range_check(monkeypatch, tmp_path, weights):
    with pytest.raises(RuntimeError, match="outside configured energy range"):
        project(monkeypatch, tmp_path, [(14, 3.0, w) for w in weights])


@pytest.mark.parametrize("energy, weight", [
    (math.nan, 1.0), (math.inf, 1.0), (0.0, 1.0), (-1.0, 1.0),
    (0.5, math.nan), (0.5, math.inf),
])
def test_nonphysical_throw_still_rejected(monkeypatch, tmp_path, energy, weight):
    with pytest.raises(RuntimeError, match="invalid throw"):
        project(monkeypatch, tmp_path, [(14, energy, weight)])
