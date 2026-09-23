"""End-to-end tests of the human-approval flow through the real MCP SDK.

test_automations.py checks the approval *logic* by handing the tools an outcome. These
go through an actual MCP client talking to `server.mcp`, whose `elicitation_callback`
plays the human, so they prove the SDK really injects the answer into the tool (and
really refuses when the client can't show a prompt) rather than assuming it.
"""

import asyncio

import pytest
from mcp.client import Client
from mcp_types import ElicitResult

import server


class Human:
    """Answers approval prompts; records what was asked."""

    def __init__(self, action: str = "accept", approve: bool = True):
        self.action = action
        self.approve = approve
        self.prompts: list[str] = []

    async def __call__(self, context, params) -> ElicitResult:
        self.prompts.append(params.message)
        if self.action == "accept":
            return ElicitResult(action="accept", content={"approve": self.approve})
        return ElicitResult(action=self.action)


def call_tool(tool: str, arguments: dict, human: Human | None):
    """Call a tool as an MCP client would; returns the result, or the exception that refused it."""

    async def run():
        kwargs = {"elicitation_callback": human} if human else {}
        async with Client(server.mcp, **kwargs) as client:
            return await client.call_tool(tool, arguments)

    try:
        return asyncio.run(run())
    except BaseException as error:  # the SDK's task group wraps the real error
        leaf = error
        while getattr(leaf, "exceptions", None):
            leaf = leaf.exceptions[0]
        return leaf


@pytest.fixture
def live(fake_tracearr, automations_ready, monkeypatch):
    """A Tracearr with an active and an inactive automation, and the automation tools listed."""
    monkeypatch.setattr(server, "_registered_tools", set())
    fake_tracearr.add_automation(name="Live one", isActive=True)
    fake_tracearr.add_automation(name="Quiet one", isActive=False)
    server.sync_tools()
    return fake_tracearr


def ids(fake) -> list[str]:
    return [a["id"] for a in fake.automations]


def names(fake) -> list[str]:
    return [a["name"] for a in fake.automations]


def test_update_of_active_automation_prompts_then_applies_when_approved(live):
    human = Human()
    result = call_tool("update_automation", {"automation_id": ids(live)[0], "changes": {"name": "Live renamed"}}, human)

    assert not isinstance(result, BaseException) and not result.is_error
    assert len(human.prompts) == 1
    assert "Live one" in human.prompts[0] and "Live renamed" in human.prompts[0]
    assert names(live) == ["Live renamed", "Quiet one"]


@pytest.mark.parametrize(
    "human",
    [Human("decline"), Human("cancel"), Human("accept", approve=False)],
    ids=["declined", "cancelled", "accepted-but-unticked"],
)
def test_update_of_active_automation_changes_nothing_unless_approved(live, human):
    result = call_tool("update_automation", {"automation_id": ids(live)[0], "changes": {"name": "NO"}}, human)

    assert not isinstance(result, BaseException)
    assert len(human.prompts) == 1
    assert names(live) == ["Live one", "Quiet one"]
    assert live.calls("PATCH", f"/automations/{ids(live)[0]}") == []


def test_update_of_inactive_automation_does_not_prompt(live):
    human = Human()
    call_tool("update_automation", {"automation_id": ids(live)[1], "changes": {"name": "Quiet renamed"}}, human)

    assert human.prompts == []
    assert names(live) == ["Live one", "Quiet renamed"]


def test_enabling_prompts_and_applies_only_when_approved(live):
    target = ids(live)[1]
    declined = Human("decline")
    call_tool("set_automation_active", {"automation_id": target, "active": True}, declined)
    assert len(declined.prompts) == 1 and live.automations[1]["isActive"] is False

    approved = Human()
    call_tool("set_automation_active", {"automation_id": target, "active": True}, approved)
    assert live.automations[1]["isActive"] is True


def test_disabling_does_not_prompt(live):
    human = Human()
    call_tool("set_automation_active", {"automation_id": ids(live)[0], "active": False}, human)

    assert human.prompts == []
    assert live.automations[0]["isActive"] is False


def test_delete_always_prompts_and_only_deletes_when_approved(live):
    target = ids(live)[1]
    declined = Human("decline")
    call_tool("delete_automation", {"automation_id": target}, declined)
    assert len(declined.prompts) == 1 and len(live.automations) == 2

    approved = Human()
    call_tool("delete_automation", {"automation_id": target}, approved)
    assert len(approved.prompts) == 1 and names(live) == ["Live one"]


def test_a_client_that_cannot_prompt_is_refused_and_nothing_changes(live):
    result = call_tool("delete_automation", {"automation_id": ids(live)[1]}, None)

    assert len(live.automations) == 2
    assert live.calls("DELETE", f"/automations/{ids(live)[1]}") == []
    # either the SDK refuses outright, or the tool reports the refusal; never a deletion
    assert isinstance(result, BaseException) or result.is_error or "declined" in str(result.content)


def test_create_never_prompts_and_is_saved_inactive(live):
    human = Human()
    definition = {
        "name": "New one",
        "kind": "policy",
        "severity": "warning",
        "triggers": [{"id": "t", "type": "stream.started", "enabled": True}],
        "conditions": {"groups": []},
        "actions": {"actions": [{"id": "a", "type": "terminate"}]},
    }
    call_tool("create_automation", {"definition": definition}, human)

    assert human.prompts == []
    assert live.automations[-1]["name"] == "New one" and live.automations[-1]["isActive"] is False
