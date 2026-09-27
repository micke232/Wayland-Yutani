import asyncio
import threading

from wayland.agents import COORDINATOR_ID, REGISTRY, SPECIALISTS
from wayland.config import app_root
from wayland.coordination import coordinate
from wayland.operator import OperatorRuntime
from wayland.tui.ui import HELP


def test_default_roles_documented_and_renames_preserved(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    assert all(role.name in HELP and role.purpose in HELP for role in REGISTRY)
    runtime.dispatch("rename", threadId=COORDINATOR_ID, name="My Coordinator")
    restarted = OperatorRuntime(runtime.root)
    assert restarted.dispatch("snapshot")["threads"][COORDINATOR_ID]["name"] == "My Coordinator"


def test_hub_and_spoke_never_shares_peer_context(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    calls = []

    async def reply(tid, text, context, config, cancel):
        calls.append((tid, dict(context)))
        if tid != COORDINATOR_ID:
            assert "analyst_reports" not in context
            assert context["specialist_task"] is True
            return tid + " independent evidence"
        assert len(context["analyst_reports"]) == 4
        return "Combined answer"

    runtime.model_reply = reply
    result = asyncio.run(
        coordinate(
            runtime, "Review ORCL", {"market": {"symbol": "ORCL"}}, runtime.settings(), threading.Event()
        )
    )
    assert result == "Combined answer"
    assert [tid for tid, _ in calls] == [r.agent_id for r in SPECIALISTS] + [COORDINATOR_ID]
    assert not runtime.delegated
    with runtime.store() as store:
        routes = [e for e in store.recent(100) if e["kind"] == "coordination.request"]
        assert len(routes) == 4
        assert all(COORDINATOR_ID in e["payload"] for e in routes)
        for role in SPECIALISTS:
            thread = store.get("ui:threads")[role.agent_id]
            assert thread["status"]["type"] == "idle"
            assert "Report to Coordinator" in thread["items"][-1]["text"]


def test_archived_and_failed_specialists_are_explicitly_unavailable(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    runtime.dispatch("archive", threadId=SPECIALISTS[0].agent_id)
    calls = []

    async def reply(tid, text, context, config, cancel):
        calls.append(tid)
        if tid == SPECIALISTS[1].agent_id:
            raise TimeoutError("must not expose raw credentials")
        if tid == COORDINATOR_ID:
            assert context["analyst_reports"][SPECIALISTS[0].name]["status"] == "unavailable"
            assert context["analyst_reports"][SPECIALISTS[1].name]["status"] == "unavailable"
        return "Report"

    runtime.model_reply = reply
    asyncio.run(coordinate(runtime, "Review", {}, runtime.settings(), threading.Event()))
    assert SPECIALISTS[0].agent_id not in calls
    assert not runtime.delegated
    with runtime.store() as store:
        assert "must not expose raw credentials" not in str(store.recent(100))


def test_interrupt_stops_further_delegation_and_synthesis(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    cancel = threading.Event()
    calls = []

    async def reply(tid, text, context, config, token):
        calls.append(tid)
        cancel.set()
        return ""

    runtime.model_reply = reply
    assert asyncio.run(coordinate(runtime, "Review", {}, runtime.settings(), cancel)) == ""
    assert calls == [SPECIALISTS[0].agent_id]
    assert not runtime.delegated
    with runtime.store() as store:
        assert store.get("ui:threads")[SPECIALISTS[0].agent_id]["status"]["type"] == "waiting"
