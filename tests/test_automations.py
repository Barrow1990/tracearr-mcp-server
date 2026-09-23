"""Automation (rules) tools: availability check, login handling, and each tool's behaviour.

Approval logic is checked by handing the tools an outcome; the end-to-end prompt flow
through a real MCP client is in test_approval_flow.py.
"""

from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server

DEFINITION = {
    "name": "No 4K transcodes",
    "kind": "policy",
    "severity": "warning",
    "triggers": [{"id": "t1", "type": "stream.started", "enabled": True}],
    "conditions": {"groups": []},
    "actions": {"actions": [{"id": "a1", "type": "terminate"}]},
}


def accepted(approve: bool = True):
    return SimpleNamespace(action="accept", data=SimpleNamespace(approve=approve))


DECLINED = SimpleNamespace(action="decline", data=None)


# --- availability check ------------------------------------------------------


def test_check_needs_credentials(fake_tracearr, with_auth, monkeypatch):
    monkeypatch.setattr(server, "TRACEARR_PASSWORD", None)
    state = server.check_automations_access()
    assert state.status == "disabled" and "TRACEARR_USERNAME" in state.reason
    assert fake_tracearr.logins == 0


def test_check_needs_mcp_auth_token(fake_tracearr, no_auth):
    state = server.check_automations_access()
    assert state.status == "disabled" and "MCP_AUTH_TOKEN" in state.reason
    assert fake_tracearr.logins == 0


def test_check_passes_for_an_owner(fake_tracearr, with_auth):
    state = server.check_automations_access()
    assert (state.status, state.account) == ("ready", "mcp-owner")


def test_check_rejects_a_non_owner(fake_tracearr, with_auth):
    fake_tracearr.role = "viewer"
    state = server.check_automations_access()
    assert state.status == "disabled" and "owner" in state.reason


def test_check_bad_password_is_permanent_but_rate_limit_and_5xx_are_retried(fake_tracearr, with_auth):
    fake_tracearr.login_status = 401
    assert server.check_automations_access().status == "disabled"
    fake_tracearr.login_status = 429
    assert server.check_automations_access().status == "pending"
    fake_tracearr.login_status = 503
    assert server.check_automations_access().status == "pending"


def test_check_unsupported_tracearr(fake_tracearr, with_auth):
    fake_tracearr.list_status = 404
    assert server.check_automations_access().status == "disabled"


def test_status_tool_reports_the_state(monkeypatch):
    monkeypatch.setattr(server, "automations_state", server.AutomationsState("pending", "Tracearr down"))
    assert server.automations_status() == {
        "enabled": False,
        "status": "pending",
        "reason": "Tracearr down",
        "account": None,
    }


# --- login handling ----------------------------------------------------------


def test_expired_session_logs_in_again_once_and_retries(fake_tracearr, automations_ready):
    server.list_automations()
    assert fake_tracearr.logins == 1
    fake_tracearr.expire_session()
    server.list_automations()
    assert fake_tracearr.logins == 2


def test_a_session_that_is_never_accepted_gives_up_after_one_relogin(fake_tracearr, automations_ready):
    fake_tracearr.always_unauthorized = True
    with pytest.raises(ToolError, match="still rejects"):
        server.list_automations()
    assert fake_tracearr.logins == 2


def test_no_static_api_key_is_sent_to_the_internal_api(fake_tracearr, automations_ready):
    server.list_automations()
    assert fake_tracearr.requests[0][1] == "/auth/sign-in/username"
    assert fake_tracearr.requests[0][2] == {"username": "mcp-owner", "password": "test-password"}


# --- read tools --------------------------------------------------------------


def test_list_passes_filters_and_clamps_page_size(fake_tracearr, automations_ready, monkeypatch):
    seen = {}
    real = server._session_request

    def spy(method, path, **kwargs):
        seen.update(path=path, params=kwargs.get("params"))
        return real(method, path, **kwargs)

    monkeypatch.setattr(server, "_session_request", spy)
    server.list_automations(kind="policy", enabled=True, search="4k", page_size=5000)
    assert seen["path"] == "/automations"
    assert seen["params"] == {"kind": "policy", "enabled": "true", "search": "4k", "pageSize": 100}


def test_get_runs_evaluations_export_hit_the_right_paths(fake_tracearr, automations_ready):
    auto = fake_tracearr.add_automation()
    server.get_automation(auto["id"])
    server.automation_runs(auto["id"], outcome="error")
    server.automation_evaluations(auto["id"])
    server.export_automation(auto["id"])
    paths = [p for (_, p, _) in fake_tracearr.requests]
    for suffix in ("", "/runs", "/evaluations", "/export"):
        assert f"/automations/{auto['id']}{suffix}" in paths


def test_unknown_automation_is_a_readable_error(fake_tracearr, automations_ready):
    with pytest.raises(ToolError, match="Not found"):
        server.get_automation("00000000-0000-0000-0000-999999999999")


def test_dry_run_returns_summary_and_saves_nothing(fake_tracearr, automations_ready):
    result = server.dry_run_automation(DEFINITION)
    assert result["summary"]["samples_checked"] == 3 and result["summary"]["would_run"] == 2
    assert fake_tracearr.calls("POST", "/automations") == []


# --- create ------------------------------------------------------------------


def test_create_always_saves_inactive_even_if_asked_otherwise(fake_tracearr, automations_ready):
    result = server.create_automation({**DEFINITION, "isActive": True})
    assert result["created"] is True and result["active"] is False
    assert fake_tracearr.calls("POST", "/automations")[0]["isActive"] is False
    assert fake_tracearr.automations[0]["isActive"] is False
    assert result["dry_run"]["would_run"] == 2


def test_create_dry_runs_first_and_stops_on_an_invalid_definition(fake_tracearr, automations_ready):
    fake_tracearr.dry_run_status = 400
    with pytest.raises(ToolError, match="At least one enabled trigger"):
        server.create_automation(DEFINITION)
    assert fake_tracearr.calls("POST", "/automations") == []


# --- update / enable / delete: approval logic --------------------------------


def test_update_of_inactive_automation_needs_no_prompt(fake_tracearr, automations_ready):
    auto = fake_tracearr.add_automation()
    assert server._approve_update(auto["id"], {"name": "x"}) is server._NO_APPROVAL_NEEDED


def test_update_of_active_automation_prompts(fake_tracearr, automations_ready):
    auto = fake_tracearr.add_automation(isActive=True, name="Live one")
    elicit = server._approve_update(auto["id"], {"name": "Renamed"})
    assert "Live one" in elicit.message and "Renamed" in elicit.message and "ACTIVE" in elicit.message


def test_update_applies_only_when_approved(fake_tracearr, automations_ready):
    auto = fake_tracearr.add_automation(isActive=True, name="Live one")
    for outcome in (DECLINED, accepted(False), None):
        result = server.update_automation(auto["id"], {"name": "NO"}, approval=outcome)
        assert result["updated"] is False
    assert fake_tracearr.automations[0]["name"] == "Live one"
    result = server.update_automation(auto["id"], {"name": "Yes"}, approval=accepted())
    assert result["updated"] is True and fake_tracearr.automations[0]["name"] == "Yes"


def test_update_refuses_to_toggle_isActive(fake_tracearr, automations_ready):
    auto = fake_tracearr.add_automation()
    with pytest.raises(ToolError, match="set_automation_active"):
        server.update_automation(auto["id"], {"isActive": True}, approval=accepted())


def test_enabling_prompts_and_warns_about_policies(fake_tracearr, automations_ready):
    auto = fake_tracearr.add_automation(kind="policy")
    elicit = server._approve_active(auto["id"], True)
    assert "POLICY" in elicit.message and "terminate" in elicit.message


def test_disabling_or_already_enabled_needs_no_prompt(fake_tracearr, automations_ready):
    on = fake_tracearr.add_automation(isActive=True)
    assert server._approve_active(on["id"], False) is server._NO_APPROVAL_NEEDED
    assert server._approve_active(on["id"], True) is server._NO_APPROVAL_NEEDED


def test_enable_applies_only_when_approved(fake_tracearr, automations_ready):
    auto = fake_tracearr.add_automation()
    assert server.set_automation_active(auto["id"], True, approval=DECLINED)["updated"] is False
    assert fake_tracearr.automations[0]["isActive"] is False
    assert server.set_automation_active(auto["id"], True, approval=accepted())["updated"] is True
    assert fake_tracearr.automations[0]["isActive"] is True
    assert fake_tracearr.calls("PATCH", f"/automations/{auto['id']}") == [{"isActive": True}]


def test_delete_always_prompts_and_only_deletes_when_approved(fake_tracearr, automations_ready):
    auto = fake_tracearr.add_automation()
    assert "cannot be undone" in server._approve_delete(auto["id"]).message
    assert server.delete_automation(auto["id"], approval=DECLINED)["deleted"] is False
    assert len(fake_tracearr.automations) == 1
    assert server.delete_automation(auto["id"], approval=accepted())["deleted"] is True
    assert fake_tracearr.automations == []


# --- tool registration -------------------------------------------------------


def test_only_the_status_tool_is_listed_until_the_check_passes(monkeypatch):
    import asyncio

    monkeypatch.setattr(server, "_registered_tools", set())
    server.sync_tools()
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "automations_status" in names and "create_automation" not in names


def test_automation_tools_are_listed_once_ready_and_removed_again(automations_ready, monkeypatch):
    import asyncio

    monkeypatch.setattr(server, "_registered_tools", set())
    server.sync_tools()
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {t.__name__ for t in server.AUTOMATION_TOOLS} <= names
    monkeypatch.setattr(server, "automations_state", server.AutomationsState("disabled", "gone"))
    server.sync_tools()
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "create_automation" not in names
