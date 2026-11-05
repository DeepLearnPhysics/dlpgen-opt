from pathlib import Path
from unittest.mock import Mock

import fcntl
import pytest

from dlpgen_opt.pipeline import _acquire_lock
from dlpgen_opt.progress import progress


def test_progress_flushes_stderr(monkeypatch):
    stream = Mock()
    monkeypatch.setattr("sys.stderr", stream)
    progress("flux", "Scanning file 1/32")
    assert "".join(call.args[0] for call in stream.write.call_args_list) == (
        "[flux] Scanning file 1/32\n"
    )
    stream.flush.assert_called_once()


def test_contended_lock_reports_wait_and_acquisition(monkeypatch, capsys):
    flock = Mock(side_effect=[BlockingIOError(), None])
    monkeypatch.setattr("dlpgen_opt.pipeline.fcntl.flock", flock)
    _acquire_lock(7, Path("cache.lock"))
    assert [call.args for call in flock.call_args_list] == [
        (7, fcntl.LOCK_EX | fcntl.LOCK_NB), (7, fcntl.LOCK_EX),
    ]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "[lock] Waiting for lock: cache.lock",
        "[lock] Acquired lock: cache.lock",
    ]


def test_lock_permission_error_is_not_reported_as_waiting(monkeypatch, capsys):
    monkeypatch.setattr(
        "dlpgen_opt.pipeline.fcntl.flock", Mock(side_effect=PermissionError()),
    )
    with pytest.raises(PermissionError):
        _acquire_lock(7, Path("cache.lock"))
    assert capsys.readouterr().err == ""
