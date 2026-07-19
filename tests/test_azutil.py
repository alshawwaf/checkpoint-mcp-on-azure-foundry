"""azd/az plumbing -- the pure parser for `azd env get-values` output
(KEY="VALUE" lines: quotes stripped, '=' allowed inside values, empty values
kept), the AzCliError shape the credential classifier depends on, and the
provider-aware overlays hydrate_config applies to a flag-built StackConfig.
"""

from chkpmcpaz.azutil import AzCliError, have, hydrate_config, parse_env_values
from chkpmcpaz.config import PROVIDER_ANTHROPIC, PROVIDER_AZURE_OPENAI, StackConfig


def test_parse_env_values_strips_quotes():
    out = parse_env_values('AZURE_LOCATION="eastus2"\nKEY_VAULT_NAME="kv-chkpmcp-x"\n')
    assert out == {"AZURE_LOCATION": "eastus2", "KEY_VAULT_NAME": "kv-chkpmcp-x"}


def test_parse_env_values_equals_inside_value():
    out = parse_env_values(
        'APPLICATIONINSIGHTS_CONNECTION_STRING="InstrumentationKey=abc;IngestionEndpoint=https://x/"\n')
    assert out["APPLICATIONINSIGHTS_CONNECTION_STRING"] == \
        "InstrumentationKey=abc;IngestionEndpoint=https://x/"


def test_parse_env_values_empty_value_and_blank_lines():
    out = parse_env_values('CLAUDE_FALLBACK_DEPLOYMENT=""\n\nFOO="bar"\n')
    assert out["CLAUDE_FALLBACK_DEPLOYMENT"] == ""
    assert out["FOO"] == "bar"


def test_parse_env_values_empty_text():
    assert parse_env_values("") == {}


def test_azclierror_carries_command_context():
    e = AzCliError("azd", 3, "ERROR: provisioning failed")
    assert isinstance(e, RuntimeError)


def test_have_uses_path_lookup():
    assert have("ls") is True
    assert have("definitely-not-a-real-command-xyz123") is False


# --- hydrate_config: provider-aware overlays (CONTRACT section 6f) -------------

def test_hydrate_config_overlays_openai_and_preserves_provider():
    # A gpt test stack: hydrate must fill the Azure OpenAI endpoint/deployment
    # from the azd outputs and KEEP the provider the CLI already resolved.
    cfg = StackConfig(provider=PROVIDER_AZURE_OPENAI)
    env = {
        "AZURE_LOCATION": "eastus2",
        "OPENAI_BASE_URL": "https://acct.services.ai.azure.com",
        "OPENAI_MODEL_DEPLOYMENT": "gpt-5-mini",
    }
    out = hydrate_config(cfg, env)
    assert out.provider == PROVIDER_AZURE_OPENAI          # preserved, not reset
    assert out.openai_base_url == "https://acct.services.ai.azure.com"
    assert out.openai_deployment == "gpt-5-mini"
    # the active-provider properties now resolve to the gpt endpoint
    assert out.model_base_url == "https://acct.services.ai.azure.com"
    assert out.configured_deployment == "gpt-5-mini"


def test_hydrate_config_claude_path_still_overlays_and_keeps_provider():
    # The Claude production path is unchanged: claude_* fill from outputs and the
    # provider stays anthropic; openai_* remain unset.
    cfg = StackConfig(provider=PROVIDER_ANTHROPIC)
    env = {
        "CLAUDE_BASE_URL": "https://acct.services.ai.azure.com/anthropic",
        "CLAUDE_MODEL_DEPLOYMENT": "claude-sonnet-4-6",
        "OPENAI_BASE_URL": "",              # emitted empty when gpt not deployed
        "OPENAI_MODEL_DEPLOYMENT": "",
    }
    out = hydrate_config(cfg, env)
    assert out.provider == PROVIDER_ANTHROPIC
    assert out.claude_base_url == "https://acct.services.ai.azure.com/anthropic"
    assert out.claude_deployment == "claude-sonnet-4-6"
    assert out.openai_base_url is None and out.openai_deployment is None


def test_hydrate_config_flag_openai_deployment_wins_over_output():
    # An explicit --model/deployment on the cfg wins over the azd output.
    cfg = StackConfig(provider=PROVIDER_AZURE_OPENAI, openai_deployment="gpt-4o")
    out = hydrate_config(cfg, {"OPENAI_MODEL_DEPLOYMENT": "gpt-5-mini"})
    assert out.openai_deployment == "gpt-4o"


def test_hydrate_config_empty_env_is_a_noop():
    cfg = StackConfig(provider=PROVIDER_AZURE_OPENAI, openai_deployment="gpt-5-mini")
    assert hydrate_config(cfg, {}) is cfg
