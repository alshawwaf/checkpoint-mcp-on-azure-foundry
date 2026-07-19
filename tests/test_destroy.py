"""destroy: an empty stack is a clean one-line no-op (AWS-port parity).

When there are no real Azure resources, `run_destroy` must NOT render the
destroy-plan window, must NOT run the teardown, must NOT report "DESTROYED
INCOMPLETE" -- it prints one "nothing to destroy" line and exits 0, exactly like
the AWS port. A lingering LOCAL azd environment is harmless and never fabricates
work.
"""

import chkpmcpaz.destroy as destroy
from chkpmcpaz.config import StackConfig


def test_destroy_empty_stack_is_a_clean_noop(monkeypatch, capsys):
    calls = {"plan": 0, "teardown": 0}
    monkeypatch.setattr(destroy, "azd_env_values", lambda prefix=None: {})
    monkeypatch.setattr(destroy, "hydrate_config", lambda cfg, env: cfg)
    monkeypatch.setattr(destroy, "inventory", lambda cfg, env: [])   # no real resources
    monkeypatch.setattr(destroy.ui, "render_destroy_plan",
                        lambda *a, **k: calls.__setitem__("plan", calls["plan"] + 1))
    monkeypatch.setattr(destroy, "_destroy",
                        lambda *a, **k: calls.__setitem__("teardown", calls["teardown"] + 1) or 0)

    rc = destroy.run_destroy(StackConfig(), yes=True)

    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing to destroy" in out.lower()
    assert "INCOMPLETE" not in out            # never a scary failure banner
    assert calls["plan"] == 0                 # no destroy-plan window
    assert calls["teardown"] == 0             # no teardown / azd down
