from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from dlpgen_opt.config import load_config
from dlpgen_opt.dlpgen_build import ensure_custom_build, inspect_checkout
from dlpgen_opt.pipeline import Pipeline


def make_checkout(root: Path) -> Path:
    checkout = root / "DLPGenerator"
    (checkout / "bin").mkdir(parents=True)
    (checkout / "python" / "dlp_generator").mkdir(parents=True)
    (checkout / "GNUmakefile").write_text("all:\n\t@true\n", encoding="utf-8")
    executable = checkout / "bin" / "dlpgen"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    (checkout / "python" / "dlp_generator" / "__init__.py").write_text(
        "VERSION = 1\n", encoding="utf-8"
    )
    return checkout


def config_with_checkout(production_config, checkout: Path):
    raw = production_config.resolved_dict()
    raw["source"]["checkout"] = str(checkout)
    raw["production"]["output_dir"] = str(
        production_config.production.output_dir.parent / "custom_v001"
    )
    path = production_config.config_path.parent / "production-custom.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(path)


def test_checkout_path_is_resolved(production_config, tmp_path):
    checkout = make_checkout(tmp_path)
    config = config_with_checkout(production_config, checkout)
    assert config.source.checkout == checkout.resolve()


def test_fingerprint_includes_dirty_source_changes(tmp_path):
    checkout = make_checkout(tmp_path)
    first = inspect_checkout(checkout)
    (checkout / "python" / "dlp_generator" / "__init__.py").write_text(
        "VERSION = 2\n", encoding="utf-8"
    )
    second = inspect_checkout(checkout)
    assert second.fingerprint != first.fingerprint


def test_custom_build_is_cached_and_prepares_environment(tmp_path):
    checkout = make_checkout(tmp_path)
    snapshot = inspect_checkout(checkout)
    production = tmp_path / "production"

    def fake_make(command, log, environment):
        cache = Path(command[2])
        library = cache / "build" / "lib" / "libLiteFMWK_ParticleBomb.so"
        library.parent.mkdir(parents=True)
        library.write_bytes(b"library")
        return subprocess.CompletedProcess(command, 0)

    with patch("dlpgen_opt.dlpgen_build._run_build", side_effect=fake_make) as run:
        first = ensure_custom_build(production, snapshot)
        second = ensure_custom_build(production, snapshot)

    assert run.call_count == 1
    assert first == second
    assert first.executable.is_file()
    assert first.build_manifest.is_file()
    assert first.environment["PATH"].startswith(str(first.executable.parent))
    assert first.environment["PYTHONPATH"].startswith(str(first.executable.parents[1] / "python"))
    assert first.environment["LD_LIBRARY_PATH"].startswith(
        str(first.executable.parents[1] / "build" / "lib")
    )


def test_manifest_prevents_mixing_custom_source_versions(production_config, tmp_path):
    checkout = make_checkout(tmp_path)
    config = config_with_checkout(production_config, checkout)
    pipeline = Pipeline(config)
    pipeline.initialize()
    manifest = yaml.safe_load(
        (config.production.output_dir / "manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["dlpgen_checkout"]["fingerprint"] == pipeline.dlpgen_checkout.fingerprint

    (checkout / "python" / "dlp_generator" / "__init__.py").write_text(
        "VERSION = 2\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="different inputs"):
        Pipeline(config).initialize()


def test_standard_configuration_uses_embedded_generator(production_config, capsys):
    assert production_config.source.checkout is None
    assert "checkout" not in production_config.resolved_dict()["source"]
    Pipeline(production_config).generate(0, dry_run=True)
    record = yaml.safe_load(capsys.readouterr().out)
    assert "dlpgen " in record["command"]
    assert ".dlpgen-cache" not in record["command"]
