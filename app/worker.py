"""Serial prompt queue.

One agent at a time - the site has a single stylesheet, so concurrent runs would
just fight each other. A prompt that arrives mid-run becomes steering for the
active agent (up to a small cap); beyond that it waits in a bounded queue.
"""
from __future__ import annotations

import asyncio
from collections import deque

from . import bus, store
from .agent import Run

MAX_QUEUE = 8
MAX_STEERING = 3


class Orchestrator:
    def __init__(self):
        self.pending: deque = deque()
        self.active = None
        # Note from the previous run, handed to the next one so a follow-up like
        # "try again" is not addressed to a model with no memory of it.
        self.prior = None
        self._task = None
        self._wake = asyncio.Event()

    # -- state for the UI -------------------------------------------------
    def status(self):
        return {
            "busy": self.active is not None,
            "queued": len(self.pending),
            "version": store.read_meta().get("version", 0),
        }

    def submit(self, prompt):
        """Returns (accepted, message)."""
        prompt = prompt.strip()
        if not prompt:
            return False, "Say something first."

        # Only steer a run that still has an iteration left to read it in.
        # Otherwise the prompt would be accepted, never consumed, and dropped
        # when the run ended - while the visitor was told it had been added.
        if (
            self.active is not None
            and self.active.accepting_steering
            and len(self.active.steering) < MAX_STEERING
        ):
            self.active.add_steering(prompt)
            bus.publish({"type": "prompt", "text": prompt, "steering": True})
            return True, "Added to the running agent's list."

        if len(self.pending) >= MAX_QUEUE:
            return False, "The queue is full - try again in a minute."

        self.pending.append(prompt)
        bus.publish({"type": "prompt", "text": prompt, "steering": False})
        self._wake.set()
        return True, "Queued."

    # -- worker -----------------------------------------------------------
    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self):
        while True:
            if not self.pending:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=30)
                except asyncio.TimeoutError:
                    continue
                continue

            prompt = self.pending.popleft()
            run = Run(prompt, prior=self.prior)
            self.active = run
            try:
                summary = await run.execute()
                self.prior = run.outcome()
                # Anything steered in too late to be consumed becomes its own
                # job rather than disappearing.
                if run.steering:
                    for late in reversed(run.steering):
                        self.pending.appendleft(late)
                    run.steering.clear()
                    self._wake.set()
                if summary:
                    bus.publish({"type": "message", "role": "system", "text": summary})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                store.clear_candidate()
                bus.publish(
                    {
                        "type": "error",
                        "text": "The agent hit an error: %s: %s"
                        % (type(exc).__name__, str(exc)[:200]),
                    }
                )
                bus.publish({"type": "status", "text": "idle", "busy": False})
            finally:
                self.active = None


ORCHESTRATOR = Orchestrator()
