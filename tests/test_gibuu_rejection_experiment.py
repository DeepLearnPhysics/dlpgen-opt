"""The experimental runner must never accept truncated or over-bound samples."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    'gibuu_rejection_experiment',
    Path(__file__).parent / 'integration' / 'gibuu_rejection.py',
)
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)


def test_bound_overflow_rejected_even_with_success_exit(tmp_path):
    native = tmp_path / 'events'
    native.write_text('HepMC::Asciiv3-END_EVENT_LISTING\n')
    with pytest.raises(RuntimeError, match='bound .* exceeded'):
        experiment.check_native_result(0, 'totalWeight > equalWeights_Max: 20 10', native, 10.)


def test_partial_native_file_rejected(tmp_path):
    native = tmp_path / 'events'
    native.write_text('HepMC::Asciiv3-START_EVENT_LISTING\n')
    with pytest.raises(RuntimeError, match='incomplete'):
        experiment.check_native_result(0, '', native, 10.)


def test_failed_native_run_rejected(tmp_path):
    with pytest.raises(RuntimeError, match='native run failed'):
        experiment.check_native_result(-6, '', tmp_path / 'missing', 10.)


def test_summary_audits_signed_weights_before_candidate_filter(tmp_path, monkeypatch):
    (tmp_path / 'EventOutput.Pert.hepmc3').write_text(
        'W CV\nE 0\nW 2.0\nE 1\nW -0.5\nHepMC::Asciiv3-END_EVENT_LISTING\n'
    )
    (tmp_path / 'fort.87').write_text('8.0\n')
    monkeypatch.setattr(experiment, '_read_candidates', lambda *a, **k: [
        SimpleNamespace(native_weight=2., incoming_neutrino=(14, 0, 0, 1, 1.), particles=[])
    ])
    experiment.summarize(tmp_path, SimpleNamespace(mode='weighted', seed=1, runs=1, bound=None), 10.)
    result = json.loads((tmp_path / 'result.json').read_text())
    assert result['negative_events'] == 1
    assert result['negative_weight'] == -.5
    assert result['signed_weight'] == 1.5
    assert result['positive_weight'] == 2.
