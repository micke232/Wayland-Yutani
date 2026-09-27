import asyncio
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from wayland.agents import SPECIALISTS
from wayland.audit import AuditStore
from wayland.delegation import TASK_KEYS, AssignmentPlan
from wayland.models import utcnow
from wayland.orchestration import (
    AgentOrchestrator,
    RunJournal,
    RunStatus,
    SpecialistProvider,
    SpecialistRunner,
    validated_reports,
)
from wayland.orchestration_demo import FixtureSpecialistProvider


def assignments():
    return AssignmentPlan(
        objective="Fresh ORCL review",
        language="Swedish",
        **{TASK_KEYS[r.agent_id]: r.purpose for r in SPECIALISTS},
    )


class Provider(FixtureSpecialistProvider):
    def __init__(self, *, failure=None, timeout=None, stale=None, barrier=False):
        self.requests = []
        self.active = self.maximum = 0
        self.failure, self.timeout, self.stale = failure, timeout, stale
        self.barrier = barrier
        self.ready = asyncio.Event()
        self.cancelled = []

    async def analyze(self, request):
        self.requests.append(copy.deepcopy(request))
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        if self.active == 4:
            self.ready.set()
        try:
            if self.barrier:
                await self.ready.wait()
            if request.role == self.failure:
                raise RuntimeError("sensitive provider error")
            if request.role == self.timeout:
                await asyncio.sleep(60)
            await asyncio.sleep(0.01)
            result = await super().analyze(request)
            if request.role == self.stale:
                return result.model_copy(
                    update={"output": {**result.output, "coordinator_run_id": "old-run"}}
                )
            return result
        except asyncio.CancelledError:
            self.cancelled.append(request.role)
            raise
        finally:
            self.active -= 1


def orchestrator(tmp_path, provider, timeout=1, parallel=4):
    journal = RunJournal(tmp_path / "audit.sqlite")
    return AgentOrchestrator(journal, SpecialistRunner(provider, journal, timeout), parallel)


def test_four_unique_parallel_requests_isolated_contexts_and_validated_results(tmp_path):
    async def run():
        provider = Provider(barrier=True)
        app = orchestrator(tmp_path, provider)
        context = {
            "market": {
                "snapshot_id": "fresh",
                "symbol": "ORCL",
                "price": "100",
                "options": [{"con_id": 123}],
            },
            "setup": {"event": "reversal", "analyst_reports": {"old": "DO NOT SEND"}},
            "news": [{"headline": "Supplied news", "timestamp": utcnow().isoformat()}],
            "analyst_reports": {"Market": "OLD REPORT"},
            "history": ["PRIVATE CHAT"],
            "account": "PRIVATE ACCOUNT",
        }
        results = await app.collect(assignments(), context, threading.Event())
        assert provider.maximum == 4
        assert len(provider.requests) == 4
        assert len({r.specialist_run_id for r in provider.requests}) == 4
        assert len({r.request_id for r in provider.requests}) == 4
        assert len({r.coordinator_run_id for r in provider.requests}) == 1
        assert all(r.status == RunStatus.COMPLETED for r in results)
        for req in provider.requests:
            text = req.model_dump_json()
            assert "OLD REPORT" not in text and "DO NOT SEND" not in text
            assert "PRIVATE CHAT" not in text and "PRIVATE ACCOUNT" not in text
            assert "analyst_reports" not in req.context
            assert ("news" in req.context) == (req.role == "wayland:news-analyst")
            assert ("options" in req.context["market"]) == (req.role == "wayland:options-analyst")
        provider.requests[0].context["market"]["symbol"] = "CHANGED"
        assert provider.requests[1].context["market"]["symbol"] == "ORCL"
        assert context["market"]["symbol"] == "ORCL"
        reports = validated_reports(results, results[0].request.coordinator_run_id)
        assert len(reports) == 4
        assert all(r["result"] and r["finished_at"] for r in reports.values())
        assert all("context" not in r and "history" not in r for r in reports.values())

    asyncio.run(run())


@pytest.mark.parametrize(
    "kind,status",
    [("failure", RunStatus.FAILED), ("timeout", RunStatus.TIMED_OUT), ("stale", RunStatus.FAILED)],
)
def test_failure_timeout_and_stale_report_do_not_fabricate_or_stop_peers(tmp_path, kind, status):
    async def run():
        role = SPECIALISTS[2].agent_id
        provider = Provider(**{kind: role})
        app = orchestrator(tmp_path, provider, timeout=0.1)
        results = await app.collect(assignments(), {}, threading.Event())
        bad = next(r for r in results if r.request.role == role)
        assert bad.status == status and bad.result is None
        assert sum(r.status == RunStatus.COMPLETED for r in results) == 3
        if kind == "timeout":
            assert role in provider.cancelled
        assert "sensitive provider error" not in json.dumps(app.journal.read())
        reports = validated_reports(results, bad.request.coordinator_run_id)
        assert reports[SPECIALISTS[2].name]["result"] is None

    asyncio.run(run())


def test_new_request_never_reuses_old_reports_and_audit_reconstructs_every_run(tmp_path):
    async def run():
        provider = Provider()
        app = orchestrator(tmp_path, provider)
        first = await app.collect(assignments(), {}, threading.Event())
        second = await app.collect(assignments(), {}, threading.Event())
        assert len(provider.requests) == 8
        first_id, second_id = first[0].request.coordinator_run_id, second[0].request.coordinator_run_id
        assert first_id != second_id
        assert not {r.request.request_id for r in first} & {r.request.request_id for r in second}
        with pytest.raises(ValueError, match="stale"):
            validated_reports(first, second_id)
        store = AuditStore(app.journal.path)
        try:
            # Reconstruct from events alone, without the materialized state documents.
            rows = store.db.execute(
                "SELECT kind,payload FROM events WHERE correlation=? ORDER BY sequence", (second_id,)
            ).fetchall()
            recovered = {}
            for row in rows:
                payload = json.loads(row["payload"])
                if row["kind"].startswith("orchestration.specialist."):
                    recovered[payload["request"]["role"]] = payload
            assert len(recovered) == 4
            assert all(r["status"] == "COMPLETED" for r in recovered.values())
            for record in recovered.values():
                request = record["request"]
                assert request["context_reference"]
                assert record["started_at"] and record["finished_at"]
                assert record["provider"] == "fixture" and record["reported_model"] == "fixture-model"
                assert record["result"]["request_id"] == request["request_id"]
                assert store.get(request["context_reference"]) == request["context"]
        finally:
            store.close()

    asyncio.run(run())


def test_cancellation_cleans_up_all_parallel_requests(tmp_path):
    async def run():
        class Slow(Provider):
            async def analyze(self, request):
                self.requests.append(request)
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    self.cancelled.append(request.role)
                    raise

        provider, cancel = Slow(), threading.Event()
        app = orchestrator(tmp_path, provider)
        task = asyncio.create_task(app.collect(assignments(), {}, cancel))
        while len(provider.requests) < 4:
            await asyncio.sleep(0)
        cancel.set()
        result = await asyncio.wait_for(task, 1)
        assert len(provider.cancelled) == 4
        assert all(r.status == RunStatus.CANCELLED for r in result)

    asyncio.run(run())


def test_structured_provider_receives_no_history_tools_or_broker_objects(tmp_path):
    from wayland.config import Settings

    async def run():
        seen = []

        async def transport(root, settings, instructions, payload, schema):
            seen.append((instructions, payload, schema))
            assert "history" not in payload and "tools" not in payload
            assert "PRIVATE" not in json.dumps(payload)
            assert set(schema["properties"]) == {
                "coordinator_run_id",
                "specialist_run_id",
                "request_id",
                "role",
                "summary",
                "findings",
                "missing_data",
            }
            report = {
                **{k: payload[k] for k in ("coordinator_run_id", "specialist_run_id", "request_id", "role")},
                "summary": "Missing data",
                "findings": [],
                "missing_data": ["No quotes"],
            }
            return SimpleNamespace(output=report, model="actual-model", id="codex-exec")

        provider = SpecialistProvider(tmp_path, Settings(), transport)
        results = await orchestrator(tmp_path, provider).collect(
            assignments(),
            {"broker": "PRIVATE", "execution_engine": "PRIVATE", "history": "PRIVATE"},
            threading.Event(),
        )
        assert len(seen) == 4 and all(r.reported_model == "actual-model" for r in results)
        assert all(r.provider_response_id is None for r in results)
        import ast
        from pathlib import Path

        source = Path(__file__).parents[2] / "src/wayland/orchestration.py"
        imports = [
            n.module or "" for n in ast.walk(ast.parse(source.read_text())) if isinstance(n, ast.ImportFrom)
        ]
        assert not any("broker" in name or "execution" in name or "tyrell" in name for name in imports)

    asyncio.run(run())


def test_archived_role_is_unavailable_without_a_model_request(tmp_path):
    async def run():
        provider = Provider()
        app = orchestrator(tmp_path, provider)
        results = await app.collect(
            assignments(), {}, threading.Event(), on_start=lambda r: r.role != SPECIALISTS[0].agent_id
        )
        assert len(provider.requests) == 3
        assert results[0].status == RunStatus.UNAVAILABLE and results[0].started_at is None
        assert results[0].result is None

    asyncio.run(run())


def test_restart_marks_inflight_work_interrupted_without_replay(tmp_path):
    async def run():
        app = orchestrator(tmp_path, Provider())
        results = await app.collect(assignments(), {}, threading.Event())
        old = results[0].model_copy(update={"status": RunStatus.RUNNING, "finished_at": None, "result": None})
        app.journal.specialist(old)
        app.journal.recover()
        recovered = app.journal.read()
        assert recovered["status"] == "INTERRUPTED"
        assert recovered["specialists"][old.request.role]["status"] == "INTERRUPTED"
        assert recovered["specialists"][old.request.role]["result"] is None

    asyncio.run(run())


def test_cli_fixture_tree_and_json_run_are_inspectable_without_broker(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")}
    root = str(tmp_path / "standalone")
    demo = subprocess.run(
        [sys.executable, "-m", "wayland", "--root", root, "orchestration-demo"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Coordinator  run:" in demo.stdout
    assert demo.stdout.count("COMPLETED") == 5
    view = subprocess.run(
        [sys.executable, "-m", "wayland", "--root", root, "runs", "--demo", "--json"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    record = json.loads(view.stdout)
    assert record["trigger"] == "fixture"
    assert record["provider"] == "fixture"
    assert len(record["specialists"]) == 4
    assert not (Path(root) / "state/wayland.sqlite").exists()


def test_coordinator_planner_rewrites_scoped_tasks_and_rejects_copy_paste(tmp_path, monkeypatch):
    from wayland.config import app_root
    from wayland.delegation import plan_assignments
    from wayland.operator import OperatorRuntime

    runtime = OperatorRuntime(app_root(tmp_path))
    task_text = "Start a new full ORCL analysis, call all four specialists and summarize their execution."
    response = SimpleNamespace(output=assignments().model_dump(), id="fixture", model="fixture-model")

    async def parse(*args):
        assert args[3]["user_request"] == task_text
        return response

    monkeypatch.setattr("wayland.providers.cli_transport.analyze", parse)

    async def run():
        plan = await plan_assignments(
            runtime, task_text, {}, runtime.settings(), threading.Event(), "coordinator-test"
        )
        assert len({getattr(plan, key) for key in TASK_KEYS.values()}) == 4
        response.output["market_task"] = task_text
        with pytest.raises(ValueError, match="copied"):
            await plan_assignments(
                runtime, task_text, {}, runtime.settings(), threading.Event(), "coordinator-test-2"
            )

    asyncio.run(run())


@pytest.mark.parametrize("field", ["request_id", "specialist_run_id", "role", "tool_calls"])
def test_wrong_request_role_or_tool_result_never_reaches_coordinator(tmp_path, field):
    async def run():
        class Wrong(Provider):
            async def analyze(self, request):
                reply = await super().analyze(request)
                if request.role == SPECIALISTS[0].agent_id:
                    value = SPECIALISTS[1].agent_id if field == "role" else "stale-or-forbidden"
                    return reply.model_copy(update={"output": {**reply.output, field: value}})
                return reply

        app = orchestrator(tmp_path, Wrong())
        results = await app.collect(assignments(), {}, threading.Event())
        assert results[0].status == RunStatus.FAILED and results[0].result is None
        assert sum(r.status == RunStatus.COMPLETED for r in results) == 3

    asyncio.run(run())
