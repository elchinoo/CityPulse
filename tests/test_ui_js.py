"""The dashboard's JavaScript: syntax, no HTML injection, and the delete-city confirmation against a fake DOM (needs node)."""
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


def test_the_page_stays_under_a_strict_csp():
    html = (APP_STATIC / "index.html").read_text()
    assert "<script>" not in html and " onclick=" not in html and "style=" not in html
