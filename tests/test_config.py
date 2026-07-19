"""Catalog + name-derivation contract (architect-owned chkpmcpaz/config.py).

With the default prefix, derived names must reproduce the EXACT canonical
stack names, or status/destroy lose already-deployed stacks. The pins mirror
the AWS repo's catalog of 2026-07-15 -- a silent pin drift would make a deploy
non-reproducible, so every one is asserted individually.
"""

import re

import pytest

from chkpmcpaz import config
from chkpmcpaz.config import (
    CRED_SHAPE,
    DEFAULT_SERVERS,
    PLACEHOLDER_VALUE,
    SERVERS,
    StackConfig,
    agent_name,
    parse_servers,
    resource_group_name,
    sanitize_id,
    secret_name,
    split_namespaced,
    target_name,
    tool_namespace,
    validate_prefix,
)

# --- catalog invariants -------------------------------------------------------

# Frozen npm pins (CONTRACT section 12 / AWS catalog of 2026-07-15).
PINS = {
    "quantum-management": "1.4.7",
    "management-logs": "1.4.6",
    "threat-prevention": "1.5.4",
    "https-inspection": "1.4.6",
    "policy-insights": "0.3.5",
    "quantum-gw-cli": "1.4.8",
    "reputation-service": "1.3.1",
    "threat-emulation": "1.3.1",
    "documentation": "1.4.6",
    "cloudguard-waf": "0.1.0",
    "spark-management": "1.4.8",
    "argos-erm": "0.5.4",
    "harmony-sase": "1.3.1",
    "workforce-ai": "1.1.0",
    "quantum-gaia": "1.3.5",
}

EXCLUDED_FROM_ALL = {"argos-erm", "harmony-sase", "workforce-ai", "quantum-gaia"}


def test_catalog_has_exactly_fifteen_servers():
    assert set(SERVERS) == set(PINS)
    assert len(SERVERS) == 15


@pytest.mark.parametrize("server,version", sorted(PINS.items()))
def test_catalog_pin_is_frozen(server, version):
    spec = SERVERS[server]
    assert spec.version == version
    assert spec.package == f"@chkp/{server}-mcp"
    assert spec.pinned == f"@chkp/{server}-mcp@{version}"


def test_default_servers_are_the_nine():
    assert set(DEFAULT_SERVERS) == {
        "quantum-management", "management-logs", "threat-prevention",
        "https-inspection", "policy-insights", "quantum-gw-cli",
        "reputation-service", "threat-emulation", "documentation",
    }
    assert len(DEFAULT_SERVERS) == 9


def test_documentation_gets_region_args_automatically():
    assert SERVERS["documentation"].args == ("--region", "US")


def test_gaia_is_agent_side_only():
    spec = SERVERS["quantum-gaia"]
    assert spec.creds is None            # nothing injected into the child process
    assert spec.agent_creds == "gaia"    # the agent-side elicitation secret
    assert "GAIA_PASSWORD" in CRED_SHAPE["gaia"]


def test_every_credentialed_server_has_a_shape():
    for spec in SERVERS.values():
        if spec.creds is not None:
            assert spec.creds in CRED_SHAPE, spec.name
            assert CRED_SHAPE[spec.creds], spec.name


def test_management_shape_shares_keys_not_secrets():
    # management-shaped servers reuse the field NAMES but get separate secrets
    assert SERVERS["quantum-management"].creds == SERVERS["management-logs"].creds
    assert secret_name("quantum-management") != secret_name("management-logs")
    assert "MANAGEMENT_HOST" in CRED_SHAPE["management"]
    assert CRED_SHAPE["management"]["API_KEY"] == PLACEHOLDER_VALUE


# --- parse_servers ------------------------------------------------------------

def test_parse_servers_default_when_empty():
    assert parse_servers(None) == DEFAULT_SERVERS
    assert parse_servers("") == DEFAULT_SERVERS
    assert parse_servers("   ") == DEFAULT_SERVERS


def test_parse_servers_all_is_eleven_and_excludes_flagged():
    allset = parse_servers("all")
    assert len(allset) == 11
    assert set(allset).isdisjoint(EXCLUDED_FROM_ALL)
    assert set(allset) == set(SERVERS) - EXCLUDED_FROM_ALL
    assert parse_servers("ALL") == allset            # case-insensitive
    # every excluded server remains deployable explicitly by name
    for s in EXCLUDED_FROM_ALL:
        assert parse_servers(s) == [s]


def test_parse_servers_commas_spaces_and_dedupe():
    assert parse_servers("quantum-management, documentation") == [
        "quantum-management", "documentation"]
    assert parse_servers("quantum-management documentation") == [
        "quantum-management", "documentation"]
    # order preserved, duplicates dropped
    assert parse_servers("documentation quantum-management documentation") == [
        "documentation", "quantum-management"]


def test_parse_servers_unknown_names_the_bad_entry():
    with pytest.raises(ValueError, match="not-a-real-server"):
        parse_servers("quantum-management not-a-real-server")


# --- prefix validation --------------------------------------------------------

@pytest.mark.parametrize("good", ["chkpmcp", "a", "demo2", "a-b-c", "abcdefghijkl"])
def test_validate_prefix_accepts(good):
    assert validate_prefix(good) == good


@pytest.mark.parametrize("bad", ["", "UPPER", "-lead", "9lead", "sp ace",
                                 "under_score", "way-too-long-prefix", "a" * 13])
def test_validate_prefix_rejects(bad):
    with pytest.raises(ValueError):
        validate_prefix(bad)


# --- derived resource names ---------------------------------------------------

def test_canonical_names_exact():
    assert resource_group_name() == "rg-chkpmcp"
    assert agent_name() == "chkpmcp-agent"
    assert secret_name("quantum-management") == "chkpmcp-quantum-management"
    assert secret_name("quantum-gaia") == "chkpmcp-quantum-gaia"
    assert config.image_ref("acrchkpmcpx.azurecr.io") == "acrchkpmcpx.azurecr.io/chkp-agent:v1"


def test_prefixed_names_derive_and_stay_legal():
    assert resource_group_name("demo2") == "rg-demo2"
    assert agent_name("demo2") == "demo2-agent"
    # Key Vault secret names: [0-9a-zA-Z-] only
    for s in SERVERS:
        assert re.fullmatch(r"[0-9a-zA-Z-]+", secret_name(s, "demo2")), s


def test_secret_name_rejects_unknown_server():
    with pytest.raises(ValueError):
        secret_name("not-a-server")


# --- tool namespacing ---------------------------------------------------------

def test_target_name_strips_hyphens():
    assert target_name("quantum-management") == "quantummanagement"
    assert target_name("documentation") == "documentation"


def test_tool_namespace_and_split_round_trip():
    assert tool_namespace("quantum-management", "show_hosts") == "quantummanagement___show_hosts"
    for s in SERVERS:
        assert split_namespaced(tool_namespace(s, "show_hosts")) == (target_name(s), "show_hosts")


def test_split_namespaced_rejects_plain_names():
    with pytest.raises(ValueError):
        split_namespaced("show_hosts")


# --- StackConfig.from_env -----------------------------------------------------

def test_from_env_defaults_on_empty_environment():
    cfg = StackConfig.from_env({})
    assert cfg.prefix == "chkpmcp"
    assert cfg.location == "eastus2"
    assert cfg.subscription_id is None
    assert cfg.claude_deployment is None       # None -> auto-select
    assert cfg.servers == tuple(DEFAULT_SERVERS)


def test_from_env_reads_every_field():
    cfg = StackConfig.from_env({
        "CHKP_PREFIX": "demo2",
        "AZURE_LOCATION": "swedencentral",
        "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
        "FOUNDRY_PROJECT_ENDPOINT": "https://acct.services.ai.azure.com/api/projects/demo2-project",
        "CLAUDE_BASE_URL": "https://acct.services.ai.azure.com/anthropic",
        "CLAUDE_MODEL_DEPLOYMENT": "claude-haiku-4-5",
        "KEY_VAULT_URI": "https://kv-demo2-x.vault.azure.net/",
        "CONTENT_SAFETY_ENDPOINT": "https://acct.cognitiveservices.azure.com/",
        "CHKP_SERVERS": "quantum-management, documentation",
    })
    assert cfg.prefix == "demo2"
    assert cfg.location == "swedencentral"
    assert cfg.claude_base_url == "https://acct.services.ai.azure.com/anthropic"
    assert cfg.claude_deployment == "claude-haiku-4-5"
    assert cfg.key_vault_uri == "https://kv-demo2-x.vault.azure.net/"
    assert cfg.servers == ("quantum-management", "documentation")
    # bound helpers use the prefix
    assert cfg.secret_name("documentation") == "demo2-documentation"
    assert cfg.agent_name() == "demo2-agent"
    assert cfg.resource_group() == "rg-demo2"


def test_from_env_chkp_model_overrides_deployment_output():
    cfg = StackConfig.from_env({
        "CLAUDE_MODEL_DEPLOYMENT": "claude-sonnet-4-6",
        "CHKP_MODEL": "claude-haiku-4-5",
    })
    assert cfg.claude_deployment == "claude-haiku-4-5"


def test_from_env_empty_strings_mean_absent():
    cfg = StackConfig.from_env({"KEY_VAULT_URI": "", "CLAUDE_BASE_URL": ""})
    assert cfg.key_vault_uri is None
    assert cfg.claude_base_url is None


# --- sanitize_id --------------------------------------------------------------

def test_sanitize_id_charset_length_and_fallback():
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", sanitize_id("weird/id with spaces"))
    assert sanitize_id("my-session_1") == "my-session_1"     # legal ids untouched
    assert len(sanitize_id("x" * 400)) == 128
    assert sanitize_id("") == "default"
    assert sanitize_id("   ") == "default"


# --- constants the loop and infra depend on -----------------------------------

def test_model_and_loop_constants():
    assert config.MODEL_PREFERENCE == ["claude-sonnet-4-6", "claude-haiku-4-5"]
    assert config.CHEAPEST_MODEL == "claude-haiku-4-5"
    assert config.MAX_TURNS == 12
    assert config.MAX_TOKENS == 2048
    assert config.TOOL_RESULT_MAX_CHARS == 6000
    assert config.TOOL_DESCRIPTION_MAX_CHARS == 1000
    assert config.MEMORY_CONTEXT_MAX_CHARS == 1500
    assert config.AI_SCOPE == "https://ai.azure.com/.default"
    assert config.COGNITIVE_SCOPE == "https://cognitiveservices.azure.com/.default"
    assert config.CONTENT_SAFETY_API_VERSION == "2024-09-01"
    assert config.AGENT_PORT == 8088
    assert config.AGENT_PROTOCOL == ("responses", "2.0.0")
    assert config.DEFAULT_ACTOR == "chkp-analyst"
    assert config.IMAGE_REPO == "chkp-agent" and config.IMAGE_TAG == "v1"
    assert config.NAMESPACE_SEP == "___"
    assert config.DEFAULT_LOCATION == "eastus2"
    assert config.SUPPORTED_LOCATIONS == ("eastus2", "swedencentral")
    # deployment name == model name by design (deployment name is what the
    # agent passes as `model` to AnthropicFoundry)
    for deployment, model, version in config.CLAUDE_DEPLOYMENTS:
        assert deployment == model and version == "1"


def test_system_prompt_is_the_verbatim_aws_prompt():
    p = config.SYSTEM_PROMPT
    assert p.startswith("You are a Check Point security-operations assistant.")
    assert "quantummanagement___show_hosts" in p
    assert "GROUNDING RULES (follow exactly):" in p
    assert "TOOL FAILURE = NO ANSWER" in p
    assert "HOW-TO vs. MY-ESTATE" in p
    assert "documentation___" in p


def test_guardrail_injection_payload_verbatim():
    assert config.GUARDRAIL_TEST_INJECTION.startswith("Ignore all previous instructions.")
    assert "disable the" in config.GUARDRAIL_TEST_INJECTION


# =============================================================================
# Multi-provider surface (Claude on Foundry + first-party Azure OpenAI). These
# pure helpers are the SINGLE source of truth every builder resolves through
# (CONTRACT section 2); the Azure analogue of the AWS repo's claude-vs-nova name
# detection.
# =============================================================================

# --- provider constants -------------------------------------------------------

def test_provider_constants_and_default():
    assert config.PROVIDER_ANTHROPIC == "anthropic"
    assert config.PROVIDER_AZURE_OPENAI == "azure-openai"
    assert config.PROVIDERS == ("anthropic", "azure-openai")
    # production default stays anthropic (Claude) -- azure-openai is opt-in
    assert config.DEFAULT_PROVIDER == "anthropic"


def test_openai_model_constants_shape():
    # SAME tuple shape as CLAUDE_DEPLOYMENTS: (deployment, model, version).
    assert config.AZURE_OPENAI_DEPLOYMENTS == (("gpt-5-mini", "gpt-5-mini", "2025-08-07"),)
    assert config.OPENAI_MODEL_PREFERENCE == ["gpt-5-mini"]
    assert config.CHEAPEST_OPENAI_MODEL == "gpt-5-mini"
    # GA text version of gpt-5-mini used for the test path (AOAI-CONTRACT section 3)
    dep, model, version = config.AZURE_OPENAI_DEPLOYMENTS[0]
    assert dep == model == "gpt-5-mini" and version == "2025-08-07"
    # classic AzureOpenAI client data-plane version (AOAI-CONTRACT section 1)
    assert config.OPENAI_API_VERSION == "2024-10-21"


def test_new_env_name_strings_are_frozen():
    # frozen integration symbols (CONTRACT section 9) -- other builders + the
    # persisted azd env depend on these exact strings
    assert config.ENV_PROVIDER == "CHKP_PROVIDER"
    assert config.ENV_OPENAI_BASE_URL == "OPENAI_BASE_URL"
    assert config.ENV_OPENAI_DEPLOYMENT == "OPENAI_MODEL_DEPLOYMENT"


# --- provider_for: model name -> provider -------------------------------------

@pytest.mark.parametrize("model,provider", [
    ("gpt-5-mini", "azure-openai"),
    ("gpt-4o", "azure-openai"),
    ("GPT-4O-MINI", "azure-openai"),          # case-insensitive
    ("o1", "azure-openai"),                    # o[0-9] reasoning ids
    ("o3-mini", "azure-openai"),
    ("my-openai-proxy", "azure-openai"),       # substring 'openai'
    ("claude-sonnet-4-6", "anthropic"),
    ("claude-haiku-4-5", "anthropic"),
    ("", "anthropic"),                          # empty -> production default
    ("something-unknown", "anthropic"),         # unknown -> anthropic
    (None, "anthropic"),                        # None -> anthropic (no crash)
])
def test_provider_for_matrix(model, provider):
    assert config.provider_for(model) == provider


# --- resolve_provider: explicit precedence + auto-detect ----------------------

def test_resolve_provider_explicit_wins_over_model():
    # an explicit provider always overrides the model-name hint
    assert config.resolve_provider("anthropic", "gpt-5-mini") == "anthropic"
    assert config.resolve_provider("azure-openai", "claude-sonnet-4-6") == "azure-openai"


@pytest.mark.parametrize("explicit", [None, "", "auto", "AUTO"])
def test_resolve_provider_auto_detects_from_model(explicit):
    assert config.resolve_provider(explicit, "gpt-5-mini") == "azure-openai"
    assert config.resolve_provider(explicit, "claude-sonnet-4-6") == "anthropic"
    assert config.resolve_provider(explicit, None) == "anthropic"     # nothing -> default


def test_resolve_provider_unknown_raises_valueerror():
    # a typo must fail loudly instead of silently defaulting to production
    with pytest.raises(ValueError, match="unknown provider"):
        config.resolve_provider("azure-oai", "gpt-5-mini")


# --- preference_for -----------------------------------------------------------

def test_preference_for_returns_the_right_list_and_a_fresh_copy():
    assert config.preference_for("anthropic") == config.MODEL_PREFERENCE
    assert config.preference_for("azure-openai") == config.OPENAI_MODEL_PREFERENCE
    # a fresh copy: mutating the result must not corrupt the module constant
    got = config.preference_for("azure-openai")
    got.append("mutated")
    assert config.OPENAI_MODEL_PREFERENCE == ["gpt-5-mini"]


# --- StackConfig multi-provider fields + from_env resolution ------------------

def test_stackconfig_defaults_to_anthropic_production():
    cfg = StackConfig()
    assert cfg.provider == "anthropic"
    assert cfg.openai_base_url is None and cfg.openai_deployment is None
    # model_base_url/configured_deployment fall through to the Claude fields
    assert cfg.model_base_url is None
    assert cfg.configured_deployment is None


def test_from_env_gpt_model_routes_to_azure_openai_without_leaking():
    cfg = StackConfig.from_env({
        "CHKP_MODEL": "gpt-5-mini",
        "OPENAI_BASE_URL": "https://acct.services.ai.azure.com",
        "OPENAI_MODEL_DEPLOYMENT": "gpt-5-mini",
    })
    assert cfg.provider == "azure-openai"
    assert cfg.openai_deployment == "gpt-5-mini"
    assert cfg.openai_base_url == "https://acct.services.ai.azure.com"
    # a gpt-* CHKP_MODEL must NOT leak into the Claude deployment field
    assert cfg.claude_deployment is None
    # the active-provider properties resolve to the OpenAI target
    assert cfg.model_base_url == "https://acct.services.ai.azure.com"
    assert cfg.configured_deployment == "gpt-5-mini"


def test_from_env_detects_azure_openai_from_deployment_output_alone():
    # no CHKP_MODEL / CHKP_PROVIDER: the gpt deployment output alone routes it
    cfg = StackConfig.from_env({"OPENAI_MODEL_DEPLOYMENT": "gpt-5-mini"})
    assert cfg.provider == "azure-openai"
    assert cfg.openai_deployment == "gpt-5-mini"
    assert cfg.claude_deployment is None


def test_from_env_explicit_provider_wins_and_stamps_config():
    cfg = StackConfig.from_env({"CHKP_PROVIDER": "azure-openai"})
    assert cfg.provider == "azure-openai"


def test_from_env_claude_path_unchanged_by_multiprovider():
    # the existing Claude path must be byte-for-byte unaffected (production)
    cfg = StackConfig.from_env({
        "CLAUDE_BASE_URL": "https://acct.services.ai.azure.com/anthropic",
        "CLAUDE_MODEL_DEPLOYMENT": "claude-sonnet-4-6",
    })
    assert cfg.provider == "anthropic"
    assert cfg.claude_deployment == "claude-sonnet-4-6"
    assert cfg.openai_deployment is None
    assert cfg.model_base_url == "https://acct.services.ai.azure.com/anthropic"
    assert cfg.configured_deployment == "claude-sonnet-4-6"


# =============================================================================
# portal_links -- clickable browser links printed after deploy / in status.
# =============================================================================

_FULL_ENV = {
    "AZURE_SUBSCRIPTION_ID": "sub-123",
    "AZURE_RESOURCE_GROUP": "rg-chkpmcp",
    "AZURE_TENANT_ID": "tid-456",
    "FOUNDRY_ACCOUNT_NAME": "chkpmcp-foundry-tok",
    "FOUNDRY_PROJECT_NAME": "chkpmcp-project",
    "KEY_VAULT_NAME": "kv-chkpmcp-tok",
    "AZURE_CONTAINER_REGISTRY_NAME": "acrchkpmcptok",
    "APPLICATIONINSIGHTS_NAME": "appi-chkpmcp-tok",
}


def test_portal_links_full_env_builds_all_links_rg_first():
    links = config.portal_links(_FULL_ENV)
    labels = [l for l, _ in links]
    assert labels[0].startswith("Resource group")          # umbrella link first
    assert len(links) == 6
    urls = dict(links)
    rg = "/subscriptions/sub-123/resourceGroups/rg-chkpmcp"
    # tenant routes multi-tenant users to the right directory
    assert urls[labels[0]] == f"https://portal.azure.com/#@tid-456/resource{rg}/overview"
    joined = " ".join(u for _, u in links)
    assert f"{rg}/providers/Microsoft.KeyVault/vaults/kv-chkpmcp-tok/secrets" in joined
    assert f"{rg}/providers/Microsoft.ContainerRegistry/registries/acrchkpmcptok/overview" in joined
    assert f"{rg}/providers/Microsoft.Insights/components/appi-chkpmcp-tok/overview" in joined
    # Foundry portal deep link carries the project ARM id + tenant
    assert ("https://ai.azure.com/build/overview?wsid=" + rg
            + "/providers/Microsoft.CognitiveServices/accounts/chkpmcp-foundry-tok"
              "/projects/chkpmcp-project&tid=tid-456") in joined


def test_portal_links_missing_names_are_skipped_not_broken():
    env = dict(_FULL_ENV)
    del env["APPLICATIONINSIGHTS_NAME"]     # older env without the new output
    del env["FOUNDRY_PROJECT_NAME"]         # no project -> no Foundry portal link
    labels = [l for l, _ in config.portal_links(env)]
    assert not any("Insights" in l for l in labels)
    assert not any("Foundry portal" in l for l in labels)
    assert any("Foundry account" in l for l in labels)      # account link remains


def test_portal_links_empty_without_a_stack():
    assert config.portal_links({}) == []
    assert config.portal_links({"AZURE_SUBSCRIPTION_ID": "s"}) == []   # no RG


def test_portal_links_tenantless_env_still_links():
    env = dict(_FULL_ENV)
    del env["AZURE_TENANT_ID"]
    links = dict(config.portal_links(env))
    assert all("#@" not in u or "#@/" not in u for u in links.values())
    assert any(u.startswith("https://portal.azure.com/#resource/") for u in links.values())


def test_portal_links_lines_puts_each_url_on_its_own_line():
    lines = config.portal_links_lines(_FULL_ENV)
    assert lines[1].strip() == "Open in the browser:"
    # alternating label / URL pairs -- a URL line contains ONLY the url
    url_lines = [l for l in lines if "https://" in l]
    assert len(url_lines) == 6
    for l in url_lines:
        assert l.strip().startswith("https://")     # nothing shares the line
    label_lines = [l for l in lines if l.strip().startswith("•")]
    assert len(label_lines) == 6


def test_portal_links_lines_empty_without_a_stack():
    assert config.portal_links_lines({}) == []


# --------------------------------------------------------------------------
# Guardrail credential UX: LAKERA_GUARD_* aliases + local .env autoload
# (identical contract to the AWS repo -- same env-var names on both clouds).
# --------------------------------------------------------------------------

def test_lakera_env_canonical_names():
    env = {"LAKERA_API_KEY": "k", "LAKERA_PROJECT_ID": "p", "LAKERA_API_URL": "u"}
    assert config.lakera_env(env) == ("k", "p", "u")


def test_lakera_env_accepts_guard_aliases():
    # An operator's existing LAKERA_GUARD_* names must keep working.
    env = {"LAKERA_GUARD_API_KEY": "k", "LAKERA_GUARD_PROJECT_ID": "p",
           "LAKERA_GUARD_URL": "u"}
    assert config.lakera_env(env) == ("k", "p", "u")


def test_lakera_env_canonical_wins_over_alias():
    env = {"LAKERA_API_KEY": "canon", "LAKERA_GUARD_API_KEY": "old"}
    assert config.lakera_env(env)[0] == "canon"


def test_lakera_env_url_alias_variants():
    assert config.lakera_env({"LAKERA_GUARD_API_URL": "u"})[2] == "u"


def test_lakera_env_absent_is_empty_key_and_none():
    assert config.lakera_env({}) == ("", None, None)


def test_load_env_file_missing_is_noop(tmp_path):
    assert config.load_env_file(str(tmp_path / "nope.env")) == []


def test_load_env_file_parses_and_respects_explicit_export(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text(
        "# a comment\n"
        "[section-should-be-skipped]\n"
        'export LAKERA_API_KEY="abc=def"\n'   # export prefix + quotes + '=' in value
        "LAKERA_PROJECT_ID = pid \n"          # surrounding whitespace
        "PRESET=fromfile\n"
        "\n"
        "NO_EQUALS_LINE\n"
    )
    fake = {"PRESET": "exported"}             # already-exported var must win
    monkeypatch.setattr(config.os, "environ", fake)
    loaded = config.load_env_file(str(p))
    assert fake["LAKERA_API_KEY"] == "abc=def"      # quotes stripped, '=' preserved, no interpolation
    assert fake["LAKERA_PROJECT_ID"] == "pid"       # trimmed
    assert fake["PRESET"] == "exported"             # explicit env wins over file (setdefault)
    assert set(loaded) == {"LAKERA_API_KEY", "LAKERA_PROJECT_ID"}
def test_load_env_file_strips_utf8_bom(tmp_path, monkeypatch):
    # A Windows/Notepad-saved .env starts with a UTF-8 BOM; the first key must
    # not be corrupted into "﻿LAKERA_API_KEY".
    p = tmp_path / ".env"
    p.write_text("LAKERA_API_KEY=abc\nLAKERA_PROJECT_ID=pid\n", encoding="utf-8-sig")
    fake = {}
    monkeypatch.setattr(config.os, "environ", fake)
    loaded = config.load_env_file(str(p))
    assert fake.get("LAKERA_API_KEY") == "abc"       # BOM stripped, key intact
    assert "LAKERA_API_KEY" in loaded


def test_load_env_file_inline_comment_and_literal_hash(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text(
        "A=val  # trailing comment\n"   # unquoted inline comment -> stripped
        "B=frag#ment\n"                 # '#' with no leading space -> kept literally
        'C="quo # ted"  # note\n'       # '#' inside quotes kept; trailing comment dropped
    )
    fake = {}
    monkeypatch.setattr(config.os, "environ", fake)
    config.load_env_file(str(p))
    assert fake["A"] == "val"
    assert fake["B"] == "frag#ment"
    assert fake["C"] == "quo # ted"
