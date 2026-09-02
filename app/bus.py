"""In-process pub/sub for streaming agent progress to every connected browser.

Keeps a small replay ring so a visitor who arrives mid-run still sees the
conversation so far. Both buffers are size-capped: memory cannot grow.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections import deque

REPLAY_MAX = 60
QUEUE_MAX = 200
# Per-message ceiling for the replay copy: the model's deliberations run to
# thousands of characters and a late visitor does not need every word.
REPLAY_TEXT_MAX = 2000
# Screenshots are far heavier than text, and the replay ring is held in memory
# and re-sent to every new visitor - so only the newest few are kept.
IMAGE_REPLAY_MAX = 2

_subscribers: set = set()
_replay: deque = deque(maxlen=REPLAY_MAX)
_ids = itertools.count(1)


def _envelope(event):
    event = dict(event)
    event.setdefault("t", round(time.time(), 3))
    event["id"] = next(_ids)
    return event


def _remember(env):
    """Keep the replay buffer holding whole messages, not stream fragments.

    A streamed message arrives as an empty `message` followed by many `delta`s.
    Replaying that scaffolding alone would hand a late visitor a row of empty
    bubbles, so deltas are folded back into the message they belong to.
    """
    etype = env.get("type")
    mid = env.get("mid")

    if etype == "delta":
        if not mid:
            return
        for entry in reversed(_replay):
            if entry.get("mid") == mid and entry.get("type") == "message":
                if len(entry.get("text", "")) < REPLAY_TEXT_MAX:
                    entry["text"] = (entry.get("text", "") + env.get("text", ""))[
                        :REPLAY_TEXT_MAX
                    ]
                return
        return

    if etype == "image":
        _replay.append(dict(env))
        stale = [e for e in _replay if e.get("type") == "image"][:-IMAGE_REPLAY_MAX]
        for old_img in stale:
            try:
                _replay.remove(old_img)
            except ValueError:
                pass
        return

    # "reload" is deliberately absent: it is a live signal, not history. The
    # server already renders the current stylesheet version into the HTML for
    # anyone arriving later, so replaying old reloads only made fresh page loads
    # inject stylesheets again - including onto ?safe=1, which must stay bare.
    if etype in ("prompt", "message", "tool", "status", "error"):
        _replay.append(dict(env))


def publish(event):
    """Fan an event out to all subscribers. Safe to call from the event loop."""
    env = _envelope(event)
    _remember(env)
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(env)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.discard(q)
    return env


def replay():
    return list(_replay)


def clear_replay():
    _replay.clear()


class Subscription:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)

    def __enter__(self):
        _subscribers.add(self.queue)
        return self.queue

    def __exit__(self, *exc):
        _subscribers.discard(self.queue)
        return False


def sse(event):
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def subscriber_count():
    return len(_subscribers)
