"""Tests for parsing the Lua table literal in clientReport messages."""

import sys
import types
from pathlib import Path

import pytest

_PACKAGE_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "homeassistant_grenton"
)

for _name, _path in (
    ("custom_components", _PACKAGE_DIR.parent),
    ("custom_components.homeassistant_grenton", _PACKAGE_DIR),
):
    if _name not in sys.modules:
        _module = types.ModuleType(_name)
        _module.__path__ = [str(_path)]
        sys.modules[_name] = _module

from custom_components.homeassistant_grenton.domain.api.clu_messages import (  # noqa: E402
    GrentonCluApiClientRegisterResponse,
    GrentonCluApiClientReportNotification,
)
from custom_components.homeassistant_grenton.domain.api.clu_messages.lua_literal import (  # noqa: E402
    LuaLiteralError,
    parse_lua_table,
)

# Shape of a real CLU answer, from an Object Manager capture (opengr8on docs).
CAPTURE = (
    '{27,nil,1,true,"2023-12-05","00:20:47",5,12,2023,2,0,20,1701735647,'
    '"05.12.01-2330",false,false,50,230,"tempus1.gum.gov.pl",0,0,'
    '"8.8.8.8","8.8.4.4",24.09,0.50,25,23,0}'
)


def test_capture_has_one_value_per_entry():
    values = parse_lua_table(CAPTURE)
    assert len(values) == 28
    assert values[:4] == [27, None, 1, True]
    assert values[4] == "2023-12-05"
    assert values[13] == "05.12.01-2330"
    assert values[14] is False
    assert values[18] == "tempus1.gum.gov.pl"
    assert values[23] == 24.09
    assert values[24] == 0.5


def test_comma_inside_string_stays_one_value():
    assert parse_lua_table('{"a,b",1,0}') == ["a,b", 1, 0]


def test_braces_inside_string_stay_one_value():
    assert parse_lua_table('{"}{",1}') == ["}{", 1]


def test_nested_table_is_one_value():
    values = parse_lua_table("{1,{2,3,{4}},0}")
    assert values == [1, "{2,3,{4}}", 0]


def test_escapes_are_decoded():
    values = parse_lua_table(r'{"say \"hi\"","a\\b","line\nnext","\196\133"}')
    assert values == ['say "hi"', "a\\b", "line\nnext", "ą"]


def test_single_quoted_string():
    assert parse_lua_table("{'x,y'}") == ["x,y"]


def test_numeric_strings_keep_their_number_typing():
    # The CLU stores values written with setVar("x","1") as strings, and
    # entities read them back as numbers, as before.
    assert parse_lua_table('{"1","22.5","true"}') == [1, 22.5, True]


def test_text_is_returned_unchanged():
    assert parse_lua_table('{" padded ","\\"quoted\\""}') == [" padded ", '"quoted"']


def test_whitespace_and_empty_table():
    assert parse_lua_table(" { 1 , 2 }\n") == [1, 2]
    assert parse_lua_table("{}") == []


def test_negative_and_exponent_numbers():
    assert parse_lua_table("{-255,1e+15,-0.5}") == [-255, 1e15, -0.5]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1,2",
        "{1,2",
        '{"open}',
        "{1 2}",
        "{1,2}x",
        "{[1]=2}",
        "{a=1}",
    ],
)
def test_malformed_tables_raise(text):
    with pytest.raises(LuaLiteralError):
        parse_lua_table(text)


def test_report_notification_uses_the_parser():
    wire = 'resp:192.168.1.10:00000000:clientReport:7:{"a,b",1,nil}'
    assert GrentonCluApiClientReportNotification(wire).values == ["a,b", 1, None]


def test_register_answer_that_is_not_a_report_raises():
    with pytest.raises(ValueError):
        GrentonCluApiClientRegisterResponse("resp:192.168.1.10:12345678:nil")


def test_unparseable_report_raises():
    with pytest.raises(ValueError):
        GrentonCluApiClientReportNotification('resp:192.168.1.10:00000000:clientReport:7:{"a,1')
