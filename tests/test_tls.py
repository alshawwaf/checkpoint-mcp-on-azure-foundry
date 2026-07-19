"""Org-policy guard: TLS verification is NEVER disabled. Two layers:
a source scan of the whole package for `verify=False`, and a behavioral
check that the guardrail's requests.post never even passes a `verify`
kwarg (the requests default -- verify on -- must be left alone).
"""

import re
from pathlib import Path
from types import SimpleNamespace

import chkpmcpaz

PKG_DIR = Path(chkpmcpaz.__file__).parent


def test_no_tls_verification_disabled_anywhere():
    pattern = re.compile(r"verify\s*=\s*False")
    offenders = [p.name for p in sorted(PKG_DIR.rglob("*.py"))
                 if pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == [], f"TLS verification disabled in: {offenders}"


def test_guardrail_post_never_passes_a_verify_kwarg(monkeypatch):
    import chkpmcpaz.guardrail as guardrail

    recorded = {}

    def _post(url, **kwargs):
        recorded.update(kwargs)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"userPromptAnalysis": {"attackDetected": False},
                          "documentsAnalysis": []},
            raise_for_status=lambda: None)

    monkeypatch.setattr(guardrail.requests, "post", _post, raising=False)
    monkeypatch.setattr(guardrail, "post", _post, raising=False)
    cred = SimpleNamespace(get_token=lambda *s, **k: SimpleNamespace(
        token="t", expires_on=4102444800))
    guardrail.screen_input("https://acct.cognitiveservices.azure.com",
                           "benign", credential=cred)
    assert "verify" not in recorded
