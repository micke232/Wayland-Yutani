"""Specialist reporting followed by a strict typed proposal; called by market events."""

import asyncio
import os
import threading
import time

from ..agents import COORDINATOR_ID
from ..coordination import coordinate
from ..models import TradeProposal
from .openai import OpenAIProvider


class CoordinatorProvider(OpenAIProvider):
    def __init__(self, settings, store, runtime):
        super().__init__(settings, store)
        self.runtime = runtime

    async def analyze(self, context: dict) -> TradeProposal | None:
        runtime = self.runtime
        cancel = threading.Event()
        with runtime.lock, runtime.store() as store:
            threads = store.get("ui:threads")
            thread = threads.get(COORDINATOR_ID)
            if not thread or thread.get("bucket") != "threads" or COORDINATOR_ID in runtime.active:
                store.event("analysis.wait", {"reason": "coordinator_unavailable"})
                return None
            if not self.settings.openai_model or not os.environ.get("OPENAI_API_KEY"):
                store.set("analysis_healthy", False)
                store.event("analysis.wait", {"reason": "runtime_api_not_configured"})
                return None
            runtime.active[COORDINATOR_ID] = cancel
            runtime.autonomous_roles.add(COORDINATOR_ID)
            thread["status"] = {"type": "active"}
            thread["startedAt"] = time.time()
            thread["items"].append(
                runtime.message(
                    "agentMessage",
                    "**Autonomous analysis**\n\nA market setup triggered specialist review. No manual prompt or order approval is required.",
                )
            )
            store.set("ui:threads", threads)

        async def synthesize(reports):
            if cancel.is_set():
                return None
            supplied = dict(context, setup={**context["setup"], "analyst_reports": reports})
            task = asyncio.create_task(super(CoordinatorProvider, self).analyze(supplied))
            try:
                while not task.done():
                    if cancel.is_set():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        return None
                    await asyncio.sleep(0.1)
                return await task
            finally:
                if not task.done():
                    task.cancel()

        proposal = None
        try:
            proposal = await coordinate(
                runtime,
                "Review the detected market setup using only supplied evidence. Report uncertainty and missing data.",
                context,
                self.settings,
                cancel,
                synthesize,
            )
            return None if cancel.is_set() else proposal
        finally:
            with runtime.lock, runtime.store() as store:
                threads = store.get("ui:threads")
                thread = threads[COORDINATOR_ID]
                complete = isinstance(proposal, TradeProposal) and not cancel.is_set()
                text = (
                    (
                        "Proposal: "
                        + proposal.action
                        + " · "
                        + proposal.thesis
                        + "\nAwaiting deterministic validation; this is not an execution confirmation."
                    )
                    if isinstance(proposal, TradeProposal) and not cancel.is_set()
                    else "Autonomous analysis produced no usable proposal. No new order is authorized."
                )
                thread["items"].append(runtime.message("agentMessage", text))
                thread["status"] = {"type": "idle" if complete else "waiting"}
                for step in thread["plan"]:
                    if step["status"] == "inProgress":
                        step["status"] = "completed" if complete else "pending"
                thread["durationMs"] = int((time.time() - thread.pop("startedAt", time.time())) * 1000)
                runtime.active.pop(COORDINATOR_ID, None)
                runtime.autonomous_roles.discard(COORDINATOR_ID)
                queued = thread.get("queued", [])
                if queued:
                    next_text = queued.pop(0)
                    next_cancel = threading.Event()
                    runtime.active[COORDINATOR_ID] = next_cancel
                    thread["status"] = {"type": "active"}
                    thread["startedAt"] = time.time()
                    thread["plan"].append({"step": "Answer queued operator message", "status": "inProgress"})
                    store.set("ui:threads", threads)
                    next_context = runtime.snapshot(store)["trading"]
                    threading.Thread(
                        target=runtime.analyze,
                        args=(COORDINATOR_ID, next_text, next_context, next_cancel),
                        daemon=True,
                    ).start()
                else:
                    store.set("ui:threads", threads)
