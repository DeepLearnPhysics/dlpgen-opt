from pathlib import Path
from unittest.mock import patch

from dlpgen_opt.provenance import git_commit


def test_git_commit_rejects_parent_repository(tmp_path: Path) -> None:
    dependency = tmp_path / "dependencies" / "example"
    dependency.mkdir(parents=True)

    with patch(
        "dlpgen_opt.provenance.subprocess.check_output",
        return_value=f"{tmp_path}\n",
    ) as check_output:
        assert git_commit(dependency) is None

    check_output.assert_called_once()


def test_git_commit_accepts_exact_repository(tmp_path: Path) -> None:
    dependency = tmp_path / "dependencies" / "example"
    dependency.mkdir(parents=True)

    with patch(
        "dlpgen_opt.provenance.subprocess.check_output",
        side_effect=[f"{dependency}\n", "abc123\n"],
    ) as check_output:
        assert git_commit(dependency) == "abc123"

    assert [item.args[0] for item in check_output.call_args_list] == [
        ["git", "-C", str(dependency), "rev-parse", "--show-toplevel"],
        ["git", "-C", str(dependency), "rev-parse", "HEAD"],
    ]
