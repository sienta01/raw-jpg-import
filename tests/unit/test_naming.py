from datetime import date

import pytest

from raw_jpg_import.naming import (
    event_folder_name,
    key,
    parse_event_folder,
    parse_user_date,
    sanitize_component,
)


def test_illegal_characters_become_dashes():
    clean, reasons = sanitize_component("Sunset: Beach/Walk?")
    assert clean == "Sunset- Beach-Walk"
    assert reasons


@pytest.mark.parametrize("raw, expected", [
    ("CON", "CON_"),
    ("con", "con_"),
    ("com1.trip", "com1_.trip"),
    ("LPT9", "LPT9_"),
    ("Console", "Console"),
])
def test_reserved_windows_names(raw, expected):
    assert sanitize_component(raw)[0] == expected


def test_trailing_dots_and_spaces_stripped():
    assert sanitize_component("Trip. ")[0] == "Trip"
    assert sanitize_component("  Trip...")[0] == "Trip"


def test_collapses_repeats_and_control_chars():
    assert sanitize_component("a\x01\x02b")[0] == "a-b"
    assert sanitize_component("a    b")[0] == "a b"


def test_nfd_becomes_nfc():
    nfd = "Café"
    assert sanitize_component(nfd)[0] == "Café"


def test_truncates_to_80():
    clean, reasons = sanitize_component("x" * 200)
    assert len(clean) == 80
    assert any("80" in r for r in reasons)


@pytest.mark.parametrize("raw", ["", ".", "..", "   ", "???"])
def test_empty_results(raw):
    assert sanitize_component(raw)[0] == ""


def test_plain_name_unchanged_without_reasons():
    assert sanitize_component("Sunset Beachwalk") == ("Sunset Beachwalk", [])


def test_event_folder_name():
    d = date(2026, 1, 23)
    assert event_folder_name(d, "Sunset Beachwalk") == "2026.01.23 - Sunset Beachwalk"
    assert event_folder_name(d, "") == "2026.01.23"
    assert event_folder_name(d, "  ") == "2026.01.23"
    assert event_folder_name(d, "a/b") == "2026.01.23 - a-b"


def test_parse_event_folder_round_trip():
    d = date(2026, 1, 23)
    assert parse_event_folder(event_folder_name(d, "Sunset")) == (d, "Sunset")
    assert parse_event_folder("2026.01.23") == (d, "")
    assert parse_event_folder("2026-01-23 x") is None
    assert parse_event_folder("2026.02.30 - bad date") is None
    assert parse_event_folder("Holiday") is None


def test_key_is_case_and_normalisation_insensitive():
    assert key("DSC_0001.NEF") == key("dsc_0001.nef")
    assert key("Café") == key("CAFÉ")


def test_parse_user_date():
    assert parse_user_date("2026-01-23") == date(2026, 1, 23)
    assert parse_user_date("2026.1.3") == date(2026, 1, 3)
    with pytest.raises(ValueError):
        parse_user_date("23/01/2026")
