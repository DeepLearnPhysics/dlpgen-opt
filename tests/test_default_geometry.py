from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("profile", ("lar_sbn.yaml", "lar_dune.yaml"))
def test_default_generation_and_image_match_sensitive_vat(profile):
    geometry = ElementTree.parse(ROOT / "configs/geometry/lar_vat.gdml")
    lar_box = geometry.find("./solids/box[@name='LArVatBox']")
    assert lar_box is not None
    assert lar_box.attrib["lunit"] == "cm"
    vat_size_cm = [float(lar_box.attrib[axis]) for axis in ("x", "y", "z")]

    with (ROOT / "configs/supera" / profile).open(encoding="utf-8") as stream:
        bbox = yaml.safe_load(stream)["BBoxConfig"]
    assert bbox["BBoxSize"] == vat_size_cm
    assert bbox["BBoxBottom"] == [-length / 2 for length in vat_size_cm]

    with (ROOT / "configs/dlpgen/baseline.yaml").open(encoding="utf-8") as stream:
        source = yaml.safe_load(stream)["InclusiveLArTPC"]
    assert source["XRange"] == source["YRange"] == source["ZRange"] == [0, 0]


@pytest.mark.parametrize("profile", ("lar_sbn.yaml", "lar_dune.yaml"))
def test_supera_profiles_configure_all_logger_levels(profile):
    with (ROOT / "configs/supera" / profile).open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)

    assert "LogLevel" not in config
    assert config["SuperaDriver"] == {
        "LogLevel": "WARNING",
        "AssertInOutVoxelCount": False,
    }
    assert config["BBoxConfig"]["LogLevel"] == "WARNING"
    assert config["LabelConfig"]["LogLevel"] == "WARNING"


@pytest.mark.parametrize("volume", ("LArVat", "World"))
def test_sensitive_vat_has_nominal_lartpc_drift_field(volume):
    geometry = ElementTree.parse(ROOT / "configs/geometry/lar_vat.gdml")
    field = geometry.find(
        f"./structure/volume[@name='{volume}']/auxiliary[@auxtype='EField']"
    )
    assert field is not None
    assert field.attrib["auxvalue"] == "(500 V/cm, 0 V/cm, 0 V/cm)"
