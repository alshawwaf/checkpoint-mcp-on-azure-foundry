"""Static guard: no undefined names (pyflakes F821) anywhere in the package.

The deploy / destroy / hosting paths shell out to azd / az and are largely
stubbed in unit tests (e.g. `_deploy` is replaced), so a stray undefined name
(like the `guardrail_provider_sel` typo that only surfaced on a live deploy)
compiles fine under py_compile and only explodes at runtime. pyflakes does the
scope analysis that catches that whole class cheaply. Skips when pyflakes isn't
installed (it lives in the `dev` extra)."""
import os
import subprocess
import sys

import pytest

pytest.importorskip("pyflakes")


def test_no_undefined_names_in_package():
    import chkpmcpaz

    pkg_dir = os.path.dirname(chkpmcpaz.__file__)
    out = subprocess.run([sys.executable, "-m", "pyflakes", pkg_dir],
                         capture_output=True, text=True)
    undefined = [ln for ln in out.stdout.splitlines() if "undefined name" in ln]
    assert not undefined, "pyflakes found undefined names:\n" + "\n".join(undefined)
