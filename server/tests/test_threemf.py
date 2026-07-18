import io
import zipfile

from server.threemf import parse_slice_info, SLICE_INFO_PATH

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header><header_item key="X-BBL-Client-Type" value="slicer"/></header>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="917"/>
    <metadata key="weight" value="1.69"/>
    <filament id="1" type="PLA" color="#000000" used_g="1.69" used_m="0.57"/>
  </plate>
</config>"""


def _zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_parses_prediction_and_weight():
    got = parse_slice_info(_zip({SLICE_INFO_PATH: SLICE_INFO}))
    assert got["seconds"] == 917
    assert got["grams"] == 1.69
    assert got["filaments"][0]["type"] == "PLA"


def test_sums_multiple_plates():
    two = SLICE_INFO.replace("</config>",
        '<plate><metadata key="prediction" value="100"/>'
        '<metadata key="weight" value="2.0"/></plate></config>')
    got = parse_slice_info(_zip({SLICE_INFO_PATH: two}))
    assert got["seconds"] == 1017      # 917 + 100
    assert round(got["grams"], 2) == 3.69


def test_missing_slice_info_is_all_none():
    got = parse_slice_info(_zip({"3D/3dmodel.model": "<model/>"}))
    assert got == {"seconds": None, "grams": None, "filaments": []}


def test_not_a_zip_is_all_none():
    assert parse_slice_info(b"not a zip") == {"seconds": None, "grams": None,
                                              "filaments": []}


def test_malformed_xml_is_all_none():
    got = parse_slice_info(_zip({SLICE_INFO_PATH: "<config><plate"}))
    assert got == {"seconds": None, "grams": None, "filaments": []}
