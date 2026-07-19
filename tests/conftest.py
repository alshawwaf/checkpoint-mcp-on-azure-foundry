"""Shared test isolation.

Some tests install a fake ``azure.ai.projects`` SDK into ``sys.modules`` (the
hosted-agent extras are lazy-imported) and others exercise code that caches a
credential in :mod:`chkpmcpaz.azutil`. ``monkeypatch`` reverts its own edits,
but a fake sub-module that gets re-imported under a different name, or a global
cache the product code itself populates, can leak into a later test and make an
otherwise-passing test fail depending on file order (e.g. ``test_creds`` before
``test_hosting_logic``). This autouse fixture snapshots and restores the two
leak vectors around every test so the suite is order-independent.
"""

import os
import sys

import pytest

import chkpmcpaz.azutil as azutil
from chkpmcpaz import cli


@pytest.fixture(autouse=True)
def _isolate_global_state(monkeypatch):
    azure_modules = {k: v for k, v in sys.modules.items()
                     if k == "azure.ai" or k.startswith("azure.ai.")}
    saved_credential = getattr(azutil, "_CREDENTIAL", None)
    saved_log_sink = getattr(azutil, "_LOG_SINK", None)
    # A developer's real local .env / exports must never steer a unit test into
    # a live Guard API call: neutralise the CLI's .env autoload, and clear any
    # ambient guardrail env for the duration of the test (both auto-restored).
    monkeypatch.setattr(cli, "load_env_file", lambda *a, **k: [], raising=False)
    for k in [k for k in os.environ
              if k == "CHKP_GUARDRAIL_PROVIDER" or k.startswith("LAKERA")]:
        monkeypatch.delenv(k, raising=False)
    try:
        yield
    finally:
        # Drop any azure.ai* modules a test introduced, and restore the exact
        # objects that were present before it ran.
        for key in [k for k in sys.modules
                    if k == "azure.ai" or k.startswith("azure.ai.")]:
            if key not in azure_modules:
                del sys.modules[key]
        sys.modules.update(azure_modules)
        azutil._CREDENTIAL = saved_credential
        azutil._LOG_SINK = saved_log_sink
