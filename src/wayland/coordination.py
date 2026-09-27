"""Bounded hub-and-spoke analytical requests; no peer routing or execution tools."""

import time
import uuid

from .agents import COORDINATOR_ID, SPECIALISTS


async def coordinate(runtime, text, context, config, cancel, synthesize=None):
    run_id = str(uuid.uuid4())
    reports = {}
    with runtime.lock, runtime.store() as store:
        threads = store.get("ui:threads")
        plan = [{"step": "Collect " + role.name + " report", "status": "pending"} for role in SPECIALISTS]
        plan.append({"step": "Synthesize evidence and disagreements", "status": "pending"})
        # Preserve additional prompts queued while the current run starts.
        queued_steps = [
            s for s in threads[COORDINATOR_ID]["plan"] if s["step"] == "Incorporate the additional prompt"
        ]
        threads[COORDINATOR_ID]["plan"] = plan + queued_steps
        store.set("ui:threads", threads)
        store.event(
            "coordination.started",
            {"coordinator": COORDINATOR_ID, "specialists": [r.agent_id for r in SPECIALISTS]},
            run_id,
        )

    for index, role in enumerate(SPECIALISTS):
        if cancel.is_set():
            return ""
        with runtime.lock, runtime.store() as store:
            threads = store.get("ui:threads")
            child = threads.get(role.agent_id)
            unavailable = (
                not child
                or child.get("bucket") != "threads"
                or role.agent_id in runtime.active
                or role.agent_id in runtime.delegated
            )
            if unavailable:
                reports[role.name] = {
                    "status": "unavailable",
                    "report": "Missing, archived or busy. No report was obtained.",
                }
                threads[COORDINATOR_ID]["plan"][index]["status"] = "completed"
                store.set("ui:threads", threads)
                store.event("coordination.unavailable", {"role": role.agent_id}, run_id)
                continue
            runtime.delegated[role.agent_id] = cancel
            child["status"] = {"type": "active"}
            child["startedAt"] = time.time()
            child["plan"] = [{"step": "Report to Coordinator", "status": "inProgress"}]
            child["items"].append(runtime.message("agentMessage", "**Coordinator assignment**\n\n" + text))
            threads[COORDINATOR_ID]["plan"][index]["status"] = "inProgress"
            store.set("ui:threads", threads)
            store.event(
                "coordination.request", {"from": COORDINATOR_ID, "to": role.agent_id, "task": text}, run_id
            )
        failed = True
        answer = "Report interrupted before completion."
        reports[role.name] = {"status": "unavailable", "report": answer}
        try:
            # Construct a new envelope; earlier reports and conversations cannot leak to the next role.
            child_context = {
                k: context.get(k) for k in ("market", "setup", "operating_state", "state_reasons")
            }
            child_context.update(specialist_task=True, coordination_id=run_id)
            answer = await runtime.model_reply(role.agent_id, text, child_context, config, cancel)
            failed = cancel.is_set()
            if cancel.is_set():
                answer = "Coordinator analysis interrupted; no completed report."
                failed = True
            reports[role.name] = {"status": "unavailable" if failed else "completed", "report": answer}
        except Exception as error:  # noqa: BLE001 -- never relay raw provider error payloads
            answer = "Report unavailable (" + type(error).__name__ + ")."
            reports[role.name] = {"status": "unavailable", "report": answer}
            failed = True
        finally:
            with runtime.lock, runtime.store() as store:
                threads = store.get("ui:threads")
                child = threads[role.agent_id]
                child["items"].append(
                    runtime.message("agentMessage", "**Report to Coordinator**\n\n" + answer)
                )
                child["status"] = {"type": "waiting" if failed else "idle"}
                child["plan"][0]["status"] = "pending" if failed else "completed"
                child["durationMs"] = int((time.time() - child.pop("startedAt", time.time())) * 1000)
                threads[COORDINATOR_ID]["plan"][index]["status"] = (
                    "pending" if cancel.is_set() else "completed"
                )
                store.set("ui:threads", threads)
                store.event(
                    "coordination.report",
                    {"from": role.agent_id, "to": COORDINATOR_ID, **reports[role.name]},
                    run_id,
                )
                runtime.delegated.pop(role.agent_id, None)
    if cancel.is_set():
        return ""
    with runtime.lock, runtime.store() as store:
        threads = store.get("ui:threads")
        threads[COORDINATOR_ID]["plan"][len(SPECIALISTS)]["status"] = "inProgress"
        store.set("ui:threads", threads)
    final_context = dict(context, analyst_reports=reports, coordination_id=run_id)
    result = (
        (await synthesize(reports))
        if synthesize
        else await runtime.model_reply(COORDINATOR_ID, text, final_context, config, cancel)
    )
    with runtime.store() as store:
        store.event(
            "coordination.synthesis",
            {
                "coordinator": COORDINATOR_ID,
                "reports": reports,
                "answer": result.model_dump(mode="json") if hasattr(result, "model_dump") else result,
                "interrupted": cancel.is_set(),
            },
            run_id,
        )
    return result
