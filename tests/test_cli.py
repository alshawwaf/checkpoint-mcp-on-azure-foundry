"""CLI surface: global options must work both before and after the subcommand
(argparse SUPPRESS trick, mirrored from the AWS CLI), and the last-resort
credential guard must turn expired-session failures into the friendly re-auth
message -- never a traceback -- while real bugs still raise.
"""

import pytest
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError

import chkpmcpaz.cli as cli
from chkpmcpaz import __version__
from chkpmcpaz.azutil import AzCliError
from chkpmcpaz.cli import _build_parser, _is_credential_error


def _run_main(argv):
    """Exit code whether main() returns it or argparse raises SystemExit."""
    try:
        return cli.main(argv)
    except SystemExit as e:
        return e.code or 0


# --- global flag positioning ----------------------------------------------------

def test_globals_after_subcommand():
    args = _build_parser().parse_args(["deploy", "--prefix", "demo2",
                                       "--location", "swedencentral"])
    assert args.prefix == "demo2"
    assert args.location == "swedencentral"


def test_globals_before_subcommand_survive_subparser():
    args = _build_parser().parse_args(["--prefix", "demo2", "status"])
    assert args.prefix == "demo2"


def test_subcommand_position_wins_when_both_given():
    args = _build_parser().parse_args(["--prefix", "a", "deploy", "--prefix", "b"])
    assert args.prefix == "b"


def test_globals_absent_when_never_passed():
    args = _build_parser().parse_args(["destroy", "--yes"])
    assert not hasattr(args, "prefix")
    assert not hasattr(args, "location")
    assert not hasattr(args, "subscription")


# --- command surface ------------------------------------------------------------

def test_chat_task_optional_defaults_and_flags():
    p = _build_parser()
    assert p.parse_args(["chat"]).task is None       # bare chat -> examples, exit 2
    args = p.parse_args(["chat", "how many hosts?"])
    assert args.task == "how many hosts?"
    # runtime defaults to None at parse time; _resolve_runtime picks
    # hosted/local from the deployed stack (see test_resolve_runtime_*)
    assert args.runtime is None
    # default actor is chkp-analyst -- bound either at parse time or by the
    # command handler ('args.actor or DEFAULT_ACTOR'); both satisfy 6.1
    assert args.actor in (None, "chkp-analyst")
    assert p.parse_args(["chat", "q", "--actor", "alice"]).actor == "alice"
    args = p.parse_args(["chat", "q", "--runtime", "hosted", "--guardrail",
                         "--session", "s1", "--model", "claude-haiku-4-5"])
    assert args.runtime == "hosted" and args.guardrail is True
    assert args.session == "s1" and args.model == "claude-haiku-4-5"


def test_resolve_runtime_prefers_the_deployed_hosted_agent():
    """`chat` with no --runtime asks YOUR deployed agent (AWS-parity): hosted
    when deploy persisted CHKP_AGENT_HOSTED and the stack outputs exist."""
    from chkpmcpaz.cli import _resolve_runtime

    hosted_env = {"CHKP_AGENT_HOSTED": "true",
                  "FOUNDRY_PROJECT_ENDPOINT": "https://x/api/projects/p"}
    assert _resolve_runtime(None, hosted_env) == "hosted"
    # explicit flag always wins, both directions
    assert _resolve_runtime("local", hosted_env) == "local"
    assert _resolve_runtime("hosted", {}) == "hosted"
    # no marker (never deployed, or deploy --no-agent set it false) -> local
    assert _resolve_runtime(None, {}) == "local"
    assert _resolve_runtime(None, {"CHKP_AGENT_HOSTED": "false",
                                   "FOUNDRY_PROJECT_ENDPOINT": "https://x"}) == "local"
    # marker without outputs (stack destroyed / azd down) -> local
    assert _resolve_runtime(None, {"CHKP_AGENT_HOSTED": "true"}) == "local"


def test_deploy_flags():
    p = _build_parser()
    assert p.parse_args(["deploy"]).creds is None
    assert p.parse_args(["deploy", "--creds"]).creds == "chkp-credentials.env"
    assert p.parse_args(["deploy", "--creds", "my.env"]).creds == "my.env"
    assert p.parse_args(["deploy"]).no_agent is False
    assert p.parse_args(["deploy", "--no-agent"]).no_agent is True


def test_creds_and_guardrail_subcommands_parse():
    p = _build_parser()
    for action in ("template", "apply"):
        assert p.parse_args(["creds", action]).action == action
    # default file is chkp-credentials.env -- bound either at parse time or
    # by the command handler falling back to the module default
    import chkpmcpaz.creds as creds_mod
    assert p.parse_args(["creds", "template"]).file in (None, "chkp-credentials.env")
    assert getattr(creds_mod, "DEFAULT_CREDS_FILE", "chkp-credentials.env") == \
        "chkp-credentials.env"
    assert p.parse_args(["creds", "apply", "--file", "my.env"]).file == "my.env"
    assert p.parse_args(["guardrail", "test"]).action == "test"


def test_doctor_accepts_provider_and_model_flags():
    # Provider-aware preflight: `doctor` must accept the same --provider/--model
    # as deploy/chat so it can be pointed at the gpt path explicitly (they used
    # to be argparse 'unrecognized arguments' errors).
    p = _build_parser()
    assert p.parse_args(["doctor"]).provider == "auto"
    assert p.parse_args(["doctor"]).model is None
    assert p.parse_args(["doctor", "--model", "gpt-5-mini"]).model == "gpt-5-mini"
    assert p.parse_args(["doctor", "--provider", "azure-openai"]).provider == "azure-openai"


def test_bare_invocation_prints_full_help_exit_0(capsys):
    assert _run_main([]) == 0
    out = capsys.readouterr().out
    for cmd in ("deploy", "destroy", "chat", "status", "doctor",
                "refresh", "creds", "guardrail"):
        assert cmd in out


def test_version_flag(capsys):
    assert _run_main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_chat_without_task_exits_2_with_examples(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("CHKP_UI", "plain")
    monkeypatch.setenv("CHKP_LOG_DIR", str(tmp_path))
    assert _run_main(["chat"]) == 2
    out = capsys.readouterr().out
    assert "chat" in out    # example-question catalog references the command


# --- the credential guard (AWS 02b8c28 parity) ------------------------------------

def test_credential_error_shapes_are_caught():
    assert _is_credential_error(CredentialUnavailableError("no token")) is True
    assert _is_credential_error(ClientAuthenticationError("auth failed")) is True
    # az/azd subprocess failure whose stderr says the session expired
    assert _is_credential_error(AzCliError(
        "az", 1,
        "AADSTS700082: The refresh token has expired. Please run 'az login'")) is True
    # ...but az failures with unrelated stderr must NOT be swallowed
    assert _is_credential_error(AzCliError(
        "az", 1, "(ResourceGroupNotFound) Resource group 'rg-x' could not be found")) is False
    assert _is_credential_error(ValueError("boom")) is False
    assert _is_credential_error(RuntimeError("real bug")) is False


def test_keyboard_interrupt_exits_130_with_reassurance(monkeypatch, capsys):
    def _boom(argv=None):
        raise KeyboardInterrupt()
    monkeypatch.setattr(cli, "_main", _boom)
    assert _run_main(["status"]) == 130
    out = capsys.readouterr().out
    assert "Interrupted" in out and "idempotent" in out


def test_expired_session_prints_reauth_message_no_traceback(monkeypatch, capsys):
    long_msg = "token expired " + "z" * 500
    def _boom(argv=None):
        raise CredentialUnavailableError(long_msg)
    monkeypatch.setattr(cli, "_main", _boom)
    assert _run_main(["status"]) == 1
    out = capsys.readouterr().out
    assert "Your Azure session has expired" in out
    assert "az login" in out and "azd auth login" in out
    assert "idempotent" in out
    assert "CredentialUnavailableError" in out       # the truncated exception tag
    assert "z" * 300 not in out                      # message truncated to 160 chars
    assert "Traceback" not in out


def test_real_bugs_are_never_swallowed(monkeypatch):
    def _boom(argv=None):
        raise ValueError("real bug")
    monkeypatch.setattr(cli, "_main", _boom)
    with pytest.raises(ValueError):
        cli.main(["status"])
