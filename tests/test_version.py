from importlib.metadata import version

from dlpgen_opt import __version__


def test_installed_package_version_matches_runtime_version() -> None:
    assert version("dlpgen-opt") == __version__
