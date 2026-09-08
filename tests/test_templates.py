"""
The templates carry a lot of inline JavaScript, and a syntax error in it is
invisible server-side: the page returns 200 and every handler silently fails to
exist. That shipped once (a mangled escape split a string across lines and took
the whole commissioner tab with it), so check it here.

Uses node when available and skips when it isn't, so the suite still runs on a
machine without it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "joyce_ff" / "webapp" / "templates"
NODE = shutil.which("node")


def _inline_scripts(html: str) -> list[str]:
    """Inline <script> bodies only — skip any with a src=."""
    return [m.group(1) for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                            html, re.S)]


@pytest.mark.parametrize("name", ["dashboard.html", "otblitz.html"])
def test_template_javascript_parses(name, tmp_path):
    if not NODE:
        pytest.skip("node not installed")
    html = (TEMPLATES / name).read_text(encoding="utf-8")
    scripts = _inline_scripts(html)
    assert scripts, f"{name}: expected inline JavaScript"
    for i, js in enumerate(scripts):
        f = tmp_path / f"{name}.{i}.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{name} script #{i} has a syntax error:\n{r.stderr}"


@pytest.mark.parametrize("name", ["dashboard.html", "otblitz.html"])
def test_no_stray_unlock_wording(name):
    """'Unlock' meant two unrelated things on one screen and confused the
    commissioner; the access button is 'Sign in' now. Function names are fine."""
    html = (TEMPLATES / name).read_text(encoding="utf-8")
    visible = re.sub(r"function\s+\w*[Uu]nlock\w*|\w*[Uu]nlock\w*\s*\(", "", html)
    assert "Unlock" not in visible, f"{name}: user-facing 'Unlock' is back"
