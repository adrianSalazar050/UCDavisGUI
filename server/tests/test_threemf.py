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


def test_parses_printer_model_id():
    # Verified against a real A1 file (2026-07-21): slice_info.config carries
    # <metadata key="printer_model_id" value="N2S"/>, where N2S is the A1.
    src = SLICE_INFO.replace('<metadata key="index" value="1"/>',
        '<metadata key="index" value="1"/>'
        '<metadata key="printer_model_id" value="N2S"/>')
    assert parse_slice_info(_zip({SLICE_INFO_PATH: src}))["printer_model_id"] == "N2S"


def test_printer_model_id_is_none_when_absent():
    # Older/other slicers may not write it. Absent means "unknown", which the
    # model check treats as "do not block" -- so it must be None, not "".
    got = parse_slice_info(_zip({SLICE_INFO_PATH: SLICE_INFO}))
    assert got["printer_model_id"] is None


def test_printer_model_id_none_on_corrupt_file():
    assert parse_slice_info(b"not a zip")["printer_model_id"] is None


def test_printer_model_id_takes_the_first_plate():
    # Every plate in one file is sliced for the same printer, so the first is
    # authoritative; a second plate must not overwrite it with a blank.
    src = SLICE_INFO.replace('<metadata key="index" value="1"/>',
        '<metadata key="printer_model_id" value="N2S"/>').replace(
        "</config>", '<plate><metadata key="prediction" value="5"/></plate></config>')
    assert parse_slice_info(_zip({SLICE_INFO_PATH: src}))["printer_model_id"] == "N2S"


def test_sums_multiple_plates():
    two = SLICE_INFO.replace("</config>",
        '<plate><metadata key="prediction" value="100"/>'
        '<metadata key="weight" value="2.0"/></plate></config>')
    got = parse_slice_info(_zip({SLICE_INFO_PATH: two}))
    assert got["seconds"] == 1017      # 917 + 100
    assert round(got["grams"], 2) == 3.69


# Exact-dict comparisons on purpose: the queue and the model check both read
# this shape, so an accidentally added or renamed key should fail here.
ALL_NONE = {"seconds": None, "grams": None, "filaments": [],
            "printer_model_id": None}


def test_missing_slice_info_is_all_none():
    got = parse_slice_info(_zip({"3D/3dmodel.model": "<model/>"}))
    assert got == ALL_NONE


def test_not_a_zip_is_all_none():
    assert parse_slice_info(b"not a zip") == ALL_NONE


def test_malformed_xml_is_all_none():
    got = parse_slice_info(_zip({SLICE_INFO_PATH: "<config><plate"}))
    assert got == ALL_NONE
