"""en and nl translations cover exactly what strings.json defines."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

BASE = Path(__file__).parent.parent / "custom_components" / "ev_trip_planner"


def keys(node, prefix=""):
    if isinstance(node, dict):
        out = set()
        for k, v in node.items():
            out |= keys(v, f"{prefix}.{k}" if prefix else k)
        return out
    return {prefix}


def placeholders(text: str) -> set[str]:

    return set(re.findall(r"{(\w+)}", text))


def flat(node, prefix=""):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            out.update(flat(v, f"{prefix}.{k}" if prefix else k))
        return out
    return {prefix: node}


@pytest.mark.parametrize("lang", ["en", "nl"])
def test_translation_complete(lang: str) -> None:
    strings = json.loads((BASE / "strings.json").read_text())
    trans = json.loads((BASE / "translations" / f"{lang}.json").read_text())
    assert keys(trans) == keys(strings)
    s, t = flat(strings), flat(trans)
    for key, text in s.items():
        assert placeholders(t[key]) == placeholders(text), key


def test_en_matches_strings() -> None:
    strings = json.loads((BASE / "strings.json").read_text())
    en = json.loads((BASE / "translations" / "en.json").read_text())
    assert en == strings
