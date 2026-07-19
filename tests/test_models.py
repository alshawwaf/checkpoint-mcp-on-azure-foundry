"""Pure-logic + dispatch tests for the `models {status,enable,disable}` command
(the Azure analogue of the AWS Bedrock model-access surface). No Azure calls.
"""

import chkpmcpaz.models as models
from chkpmcpaz.config import StackConfig


def test_classify_matrix():
    assert models.classify(present=True, callable_=True) == "callable"
    assert models.classify(present=True, callable_=False) == \
        "present (not callable by this identity)"
    assert models.classify(present=False, callable_=False) == "missing"
    # callable implies present -- callable wins even if 'present' were mis-set
    assert models.classify(present=False, callable_=True) == "callable"


def test_run_models_without_stack_points_at_deploy(monkeypatch, capsys):
    monkeypatch.setattr(models, "azd_env_values", lambda prefix=None: {})
    monkeypatch.setattr(models, "hydrate_config", lambda cfg, env: cfg)
    assert models.run_models(StackConfig(), "status") == 1
    assert "deploy" in capsys.readouterr().out


def test_run_models_unknown_action(monkeypatch):
    env = {"FOUNDRY_ACCOUNT_NAME": "acct", "AZURE_RESOURCE_GROUP": "rg-chkpmcp"}
    monkeypatch.setattr(models, "azd_env_values", lambda prefix=None: env)
    monkeypatch.setattr(models, "hydrate_config", lambda cfg, env: cfg)
    assert models.run_models(StackConfig(), "bogus") == 2


def test_enable_reports_missing_deployments(monkeypatch, capsys):
    env = {"FOUNDRY_ACCOUNT_NAME": "acct", "AZURE_RESOURCE_GROUP": "rg-chkpmcp"}
    monkeypatch.setattr(models, "azd_env_values", lambda prefix=None: env)
    monkeypatch.setattr(models, "hydrate_config", lambda cfg, env: cfg)
    # only the fallback deployment exists -> primary reported missing, non-fatal
    monkeypatch.setattr(models, "_deployment_names", lambda env: {"claude-haiku-4-5"})
    assert models.run_models(StackConfig(), "enable") == 0
    out = capsys.readouterr().out
    assert "claude-sonnet-4-6" in out and "deploy" in out


def test_enable_noop_when_all_present(monkeypatch, capsys):
    from chkpmcpaz.config import MODEL_PREFERENCE
    env = {"FOUNDRY_ACCOUNT_NAME": "acct", "AZURE_RESOURCE_GROUP": "rg-chkpmcp"}
    monkeypatch.setattr(models, "azd_env_values", lambda prefix=None: env)
    monkeypatch.setattr(models, "hydrate_config", lambda cfg, env: cfg)
    monkeypatch.setattr(models, "_deployment_names", lambda env: set(MODEL_PREFERENCE))
    assert models.run_models(StackConfig(), "enable") == 0
    assert "already present" in capsys.readouterr().out
