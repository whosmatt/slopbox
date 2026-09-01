"""Queue and context-carry-over tests. No browser, no model, no secrets.

Regression cover for a bug where a prompt submitted during an agent's final
step was accepted as steering, never consumed, and silently dropped - while the
visitor was told it had been added to the running job. And for the follow-on
problem: the next run started with an empty context, so "try again" referred to
something the model had no memory of.

Run with: python -m tests.test_steering
"""
import json
import sys

from app.agent import Run, prepare_tool_calls
from app.worker import Orchestrator


class StubRun:
    """Stands in for a Run at a chosen point in its life."""

    def __init__(self, accepting):
        self.accepting_steering = accepting
        self.steering = []

    def add_steering(self, prompt):
        self.steering.append(prompt)


def check(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (("  " + detail) if detail else ""))
    return bool(condition)


def main():
    results = []

    print("outcome() describes the previous attempt:")
    r = Run("make it pink")
    r.published, r.version = True, 7
    results.append(check("published run mentions the version", "version 7" in r.outcome()))

    r = Run("make it pink")
    r.last_failure = ["[desktop @ 0.0s] the prompt input is not rendered"]
    out = r.outcome()
    results.append(check("rejected run says it never went live", "REJECTED" in out and "never went live" in out))
    results.append(check("rejected run passes on the reason", "not rendered" in out))

    r = Run("make it pink")
    r.exhausted = True
    results.append(check("exhausted run says nothing was published", "ran out of steps" in r.outcome()))

    print()
    print("a follow-up run can see what came before:")
    prior = r.outcome()
    follow = Run("try again", prior=prior)
    texts = [m["content"] for m in follow.messages]
    results.append(check("prior note is present", any("Context from the attempt" in t for t in texts)))
    results.append(check("note precedes the new request",
                         texts.index(prior) < texts.index("Visitor request: try again")))
    results.append(check("system prompt still first", follow.messages[0]["role"] == "system"))
    results.append(check("no prior note when there is none",
                         len(Run("fresh").messages) == 2))

    print()
    print("late prompts are queued instead of vanishing:")
    o = Orchestrator()
    o.active = StubRun(accepting=True)
    ok, msg = o.submit("steer me")
    results.append(check("mid-run prompt steers the active run",
                         ok and o.active.steering == ["steer me"] and not o.pending, msg))

    o = Orchestrator()
    o.active = StubRun(accepting=False)
    ok, msg = o.submit("try again")
    results.append(check("prompt on the final step is queued, not steered",
                         ok and not o.active.steering and list(o.pending) == ["try again"], msg))

    o = Orchestrator()
    o.active = StubRun(accepting=True)
    for i in range(5):
        o.submit("p%d" % i)
    results.append(check("steering is capped, the rest queue",
                         len(o.active.steering) == 3 and len(o.pending) == 2,
                         "steering=%d queued=%d" % (len(o.active.steering), len(o.pending))))

    o = Orchestrator()
    results.append(check("empty prompt refused", o.submit("   ")[0] is False))

    print()
    print("malformed tool arguments never enter the transcript:")
    # A cut-off write_css leaves this behind. Storing it verbatim made every
    # later request in the run fail with a server-side 400, because the server
    # parses tool-call arguments when applying its chat template.
    truncated = [{"id": "c1", "name": "write_css", "arguments": '{"css": "body{color:red'}]
    outgoing, prepared = prepare_tool_calls(truncated, truncated=True)
    results.append(check("bad arguments replaced with {}",
                         outgoing[0]["function"]["arguments"] == "{}"))
    results.append(check("every stored argument string parses",
                         all(json.loads(o["function"]["arguments"]) == {} for o in outgoing)))
    results.append(check("truncation is explained to the model",
                         "CUT OFF" in prepared[0]["error"] and "compact" in prepared[0]["error"]))
    results.append(check("the call is not dispatched", prepared[0]["args"] == {}))

    outgoing, prepared = prepare_tool_calls(
        [{"id": "c2", "name": "write_css", "arguments": '{"css": "body{color:red}"}'}])
    results.append(check("valid arguments pass through untouched",
                         outgoing[0]["function"]["arguments"] == '{"css": "body{color:red}"}'
                         and prepared[0]["error"] is None
                         and prepared[0]["args"]["css"] == "body{color:red}"))

    outgoing, prepared = prepare_tool_calls(
        [{"id": "c3", "name": "write_css", "arguments": '"just a string"'}])
    results.append(check("non-object arguments rejected",
                         prepared[0]["error"] is not None
                         and outgoing[0]["function"]["arguments"] == "{}"))

    outgoing, prepared = prepare_tool_calls(
        [{"id": "c4", "name": "publish", "arguments": ""}])
    results.append(check("empty arguments treated as {}",
                         prepared[0]["error"] is None and prepared[0]["args"] == {}))

    print()
    failed = results.count(False)
    if failed:
        print("%d/%d checks failed" % (failed, len(results)))
        return 1
    print("all %d queue/context checks passed" % len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
