from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_default_generation_and_image_match_sensitive_vat():
    geometry = ElementTree.parse(ROOT / "configs/geometry/lar_vat.gdml")
    lar_box = geometry.find("./solids/box[@name='LArVatBox']")
    assert lar_box is not None
    assert lar_box.attrib["lunit"] == "cm"
    vat_size_cm = [float(lar_box.attrib[axis]) for axis in ("x", "y", "z")]

    with (ROOT / "configs/supera/lar_vat.yaml").open(encoding="utf-8") as stream:
        bbox = yaml.safe_load(stream)["BBoxConfig"]
    assert bbox["BBoxSize"] == vat_size_cm
    assert bbox["BBoxBottom"] == [-length / 2 for length in vat_size_cm]

    with (ROOT / "configs/dlpgen/baseline.yaml").open(encoding="utf-8") as stream:
        source = yaml.safe_load(stream)["InclusiveLArTPC"]
    assert source["XRange"] == source["YRange"] == source["ZRange"] == [0, 0]
