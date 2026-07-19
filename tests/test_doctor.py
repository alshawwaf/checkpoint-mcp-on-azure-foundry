"""Unit tests for the doctor subscription-eligibility classifier.

`_offer_is_credit` is the pure decision behind the preflight FAIL that stops a
deploy onto a subscription that cannot buy the Anthropic Marketplace models
(MSDN/Visual Studio, Dev/Test, free, student, sponsored, CSP, or any
credit-capped offer). Keeping it pure means we can assert the policy without
touching az.

`_check_subscription_eligibility` is PROVIDER-AWARE (CONTRACT section 5b): the
credit/MSDN FAIL fires only for the anthropic (Claude) provider; the
azure-openai (gpt-5-mini) provider is first-party, so on the very same MSDN
Dev/Test subscription it does NOT fail and instead just checks the gpt
deployment quota. That is exactly what lets the cheap test model deploy where
Claude is blocked. The ARM probes are stubbed so no az is touched.
"""

import pytest

from chkpmcpaz import doctor
from chkpmcpaz.config import PROVIDER_ANTHROPIC, PROVIDER_AZURE_OPENAI
from chkpmcpaz.doctor import _offer_is_credit


@pytest.mark.parametrize(
    "quota_id,spending_limit,expected",
    [
        # --- real offers seen in the field (both of the user's subscriptions) ---
        ("MSDN_2014-09-01", "On", True),            # Visual Studio Professional
        ("MSDNDevTest_2014-09-01", "Off", True),    # MSDN Dev/Test (Marketplace-blocked)
        # --- other credit / benefit / restricted offers ---
        ("VisualStudioEnterprise_2014-09-01", "Off", True),
        ("FreeTrial_2014-09-01", "On", True),
        ("AzureForStudents_2018-01-01", "Off", True),   # matched via "Student"
        ("Sponsored_2016-01-01", "Off", True),
        ("CSP_2015-05-01", "Off", True),
        ("MPN_2014-09-01", "Off", True),
        # --- an otherwise-billable offer becomes ineligible if credit-capped ---
        ("PayAsYouGo_2014-09-01", "On", True),
        # --- eligible: real billing, no spending cap ---
        ("PayAsYouGo_2014-09-01", "Off", False),
        ("EnterpriseAgreement_2014-09-01", "Off", False),
        ("MSAZR0003P_2014-09-01", "Off", False),        # legacy pay-as-you-go
        # --- unknown/empty: not asserted as credit (doctor downgrades to WARN) ---
        ("", "", False),
        (None, None, False),
    ],
)
def test_offer_is_credit(quota_id, spending_limit, expected):
    assert _offer_is_credit(quota_id, spending_limit) is expected


def test_spending_limit_on_always_blocks():
    """A spending limit is a hard credit cap regardless of offer string."""
    assert _offer_is_credit("SomeFutureOffer_2099-01-01", "On") is True


def test_eligible_offer_not_flagged_by_substring_accident():
    """Guard against false positives: common billable offers must pass."""
    for q in ("PayAsYouGo_2014-09-01", "EnterpriseAgreement_2014-09-01"):
        assert _offer_is_credit(q, "Off") is False


# =============================================================================
# Provider-aware eligibility gate (CONTRACT section 5b). Stub the ARM probes
# (_az_json) and capture every _say() so we can assert the status/label/detail
# without touching az.
# =============================================================================

# The user's MSDN Dev/Test subscription (the one where Claude is blocked but
# first-party gpt-5-mini deploys fine).
_MSDN_ACCOUNT = {"id": "7bb32d3c-a24c-40e7-b7c9-70c285923b0b",
                 "name": "Visual Studio Enterprise Subscription – MPN"}


def _fake_az_json(quota, spending, gpt_limit, claude_limit):
    """A stub for doctor._az_json that answers the subscription-policies REST
    call and the cognitiveservices usage list from fixed values."""
    def _inner(args, timeout=45):
        if "rest" in args:                       # subscription policies probe
            return {"subscriptionPolicies": {"quotaId": quota, "spendingLimit": spending}}
        if "usage" in args:                      # live model quota probe (eastus2)
            return [
                {"name": {"value": f"OpenAI.GlobalStandard.gpt-5-mini"}, "limit": gpt_limit},
                {"name": {"value": f"AI.Standard.claude-sonnet-4-6"}, "limit": claude_limit},
                {"name": {"value": f"AI.Standard.claude-haiku-4-5"}, "limit": claude_limit},
            ]
        return None
    return _inner


def _capture_say(monkeypatch):
    """Patch doctor._say to record (status, label, detail); it still returns the
    status so the results list behaves exactly as in production."""
    rec = []

    def _fake_say(status, label, detail=""):
        rec.append((status, label, detail))
        return status

    monkeypatch.setattr(doctor, "_say", _fake_say)
    return rec


def _run_eligibility(monkeypatch, provider, *, quota="MSDNDevTest_2014-09-01",
                     spending="Off", gpt_limit=2000, claude_limit=0,
                     account=_MSDN_ACCOUNT, location="eastus2"):
    rec = _capture_say(monkeypatch)
    monkeypatch.setattr(doctor, "_az_json",
                        _fake_az_json(quota, spending, gpt_limit, claude_limit))
    results = []
    doctor._check_subscription_eligibility(results, account, location,
                                           provider=provider)
    return results, rec


def test_azure_openai_on_msdn_does_not_fail_and_checks_gpt_quota(monkeypatch):
    # MSDN Dev/Test + gpt-5-mini: NOT a FAIL (first-party), and the gpt quota
    # (2000 GlobalStandard in eastus2) is checked and reported OK.
    results, rec = _run_eligibility(monkeypatch, PROVIDER_AZURE_OPENAI)
    assert "fail" not in results                       # the whole point: no FAIL
    assert any(s == "ok" and "first-party" in d for s, _, d in rec)
    assert any(s == "ok" and "gpt-5-mini quota" in lbl for s, lbl, _ in rec)


def test_anthropic_on_msdn_still_fails(monkeypatch):
    # The SAME MSDN subscription, but the anthropic (Claude) provider: the
    # credit/Marketplace FAIL must still fire.
    results, rec = _run_eligibility(monkeypatch, PROVIDER_ANTHROPIC,
                                    claude_limit=50)
    assert "fail" in results
    assert any(s == "fail" and "cannot buy Claude" in d for s, _, d in rec)


def test_eligibility_defaults_to_anthropic_when_provider_omitted(monkeypatch):
    # Backward compatibility: the provider kwarg defaults to anthropic, so an
    # existing caller that omits it keeps the Claude credit gate.
    rec = _capture_say(monkeypatch)
    monkeypatch.setattr(doctor, "_az_json",
                        _fake_az_json("MSDNDevTest_2014-09-01", "Off", 2000, 50))
    results = []
    doctor._check_subscription_eligibility(results, _MSDN_ACCOUNT, "eastus2")
    assert "fail" in results


def test_azure_openai_zero_gpt_quota_warns_not_fails(monkeypatch):
    # gpt-5-mini with 0 GlobalStandard allocation: a WARN (request capacity),
    # never a FAIL -- deploy still validates live.
    results, rec = _run_eligibility(monkeypatch, PROVIDER_AZURE_OPENAI, gpt_limit=0)
    assert "fail" not in results and "warn" in results
    assert any(s == "warn" and "gpt-5-mini quota" in lbl for s, lbl, _ in rec)


def test_not_logged_in_skips_for_both_providers(monkeypatch):
    for provider in (PROVIDER_ANTHROPIC, PROVIDER_AZURE_OPENAI):
        rec = _capture_say(monkeypatch)
        results = []
        doctor._check_subscription_eligibility(results, None, "eastus2",
                                               provider=provider)
        assert results == ["warn"]                     # skipped, never a FAIL
        assert any("not logged in" in d for _, _, d in rec)
