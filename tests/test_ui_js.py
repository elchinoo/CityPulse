"""The dashboard's JavaScript: syntax, no HTML injection, the delete-city confirmation and the refresh behaviour against a fake DOM (needs node)."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import apgkit
import pytest

APP_STATIC = Path(__file__).resolve().parents[1] / "src" / "citypulse" / "static"
KIT_STATIC = Path(apgkit.__file__).resolve().parent / "static"
FAKEDOM = Path(__file__).resolve().parent / "fakedom"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")
SCRIPTS = sorted(APP_STATIC.glob("*.js"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_script_parses_as_a_module(script):
    r = subprocess.run([NODE, "--input-type=module", "--check"], input=script.read_text(), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_never_inject_html(script):
    """City ids and counts are data: strict CSP, DOM built with text nodes only."""
    src = script.read_text()
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function|setAttribute\(\s*[\"']on", src)


def test_delete_city_confirmation_in_a_fake_dom():
    env = {**os.environ, "APP_STATIC": str(APP_STATIC), "KIT_STATIC": str(KIT_STATIC)}
    r = subprocess.run([NODE, "--import", str(FAKEDOM / "register.mjs"), str(FAKEDOM / "delete_city.mjs")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stdout + r.stderr


def test_pages_keep_what_is_typed_when_they_refresh():
    """Overview (delete-city confirmation), Burst, Sensors, City picker and Settings keep their inputs, values and focus across a refresh."""
    env = {**os.environ, "APP_STATIC": str(APP_STATIC), "KIT_STATIC": str(KIT_STATIC)}
    r = subprocess.run([NODE, "--import", str(FAKEDOM / "register.mjs"), str(FAKEDOM / "refresh_typing.mjs")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stdout + r.stderr


def test_first_paint_states_durations_and_the_simulator_tile():
    """Loading... until the first data, a readable error if it fails, humanized durations, no simulator tile without a built-in simulator."""
    env = {**os.environ, "APP_STATIC": str(APP_STATIC), "KIT_STATIC": str(KIT_STATIC)}
    r = subprocess.run([NODE, "--import", str(FAKEDOM / "register.mjs"), str(FAKEDOM / "states.mjs")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stdout + r.stderr


def test_revoking_a_token_asks_through_the_kit_dialog():
    """Settings uses confirmDialog under <body>: Cancel/Escape send nothing, Revoke sends one DELETE, no native dialog."""
    env = {**os.environ, "APP_STATIC": str(APP_STATIC), "KIT_STATIC": str(KIT_STATIC)}
    r = subprocess.run([NODE, "--import", str(FAKEDOM / "register.mjs"), str(FAKEDOM / "revoke_dialog.mjs")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stdout + r.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_never_use_native_dialogs(script):
    """window.confirm/alert/prompt cannot be styled and freeze browser tests: the kit's confirmDialog/promptDialog instead."""
    assert not re.search(r"(?<![\w.])(?:window\.)?(?:confirm|alert|prompt)\s*\(", script.read_text())


def test_the_page_stays_under_a_strict_csp():
    html = (APP_STATIC / "index.html").read_text()
    assert "<script>" not in html and " onclick=" not in html and "style=" not in html


def test_map_tab_in_a_fake_dom():
    """Inline SVG from GeoJSON: no inline style, classes for health, keyboard operable districts, a table as text alternative, a not-available card."""
    env = {**os.environ, "APP_STATIC": str(APP_STATIC), "KIT_STATIC": str(KIT_STATIC)}
    r = subprocess.run([NODE, "--import", str(FAKEDOM / "register.mjs"), str(FAKEDOM / "map.mjs")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stdout + r.stderr


def test_map_and_insights_tabs_are_wired_with_the_right_permissions():
    html, app = (APP_STATIC / "index.html").read_text(), (APP_STATIC / "app.js").read_text()
    for tab in ("map", "insights"):
        assert f'data-tab="{tab}"' in html and f'id="view-{tab}"' in html
    assert 'from "/kit/insights.js"' in app and "initInsights($(\"view-insights\"), api)" in app
    assert 'insights: "settings:manage"' in app and 'map: "city:read"' in app       # statement text is admin material; the map is plain dashboard data
    assert "stopMap" in app
    css = (APP_STATIC / "app.css").read_text()
    assert ".map-svg .district.h0" in css and ".map-svg .pt.st-fault" in css


def test_map_script_builds_svg_without_style_attributes_or_foreign_requests():
    src = (APP_STATIC / "map.js").read_text()
    assert "createElementNS" in src and not re.search(r"""["']style["']|\.style\b|https?://(?!www\.w3\.org/2000/svg)""", src)
