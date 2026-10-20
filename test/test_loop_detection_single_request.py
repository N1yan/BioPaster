"""One request assesses both repetition and continuity with the prior loop."""
import json

import pytest

from biopaster.agent.conversation import Conversation
from biopaster.agent.loop_detection import CHECK_PROMPT, LoopDetector
from biopaster.providers.base import ChatResponse


class Judge:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.inputs = []

    def chat_stream_response(self, messages, **kwargs):
        assert kwargs["system"] == CHECK_PROMPT
        assert kwargs["tools"] is None
        assert kwargs["thinking"] == {"type": "disabled"}
        payload = json.loads(messages[0]["content"])
        assert isinstance(payload, dict)
        self.inputs.append(payload)
        decision = next(self.decisions)
        if isinstance(decision, BaseException):
            raise decision
        if isinstance(decision, str):
            return ChatResponse(decision, "test", {"output_tokens": 7}, "end_turn")
        stuck, same = decision
        turns = [t["turn"] for t in payload["recent_attempts"]]
        return ChatResponse(json.dumps({
            "reason": "Observed repeated strategy" if stuck else "Verified progress",
            "turns": turns, "is_stuck": stuck, "same_as_previous": same,
        }), "test", {"input_tokens": 11, "output_tokens": 7}, "end_turn")


def advance(detector, count=5, repeated=False):
    for _ in range(count):
        detector.begin_turn("Attempt")
        detector.record_result("Read", {"path": "missing" if repeated else str(detector.turn_count)},
                               "Missing file", True)


def test_three_same_loops_use_three_requests_and_stop():
    detector = LoopDetector(status="running")
    judge = Judge([(True, False), (True, True), (True, True)])
    for expected in (1, 2, 3):
        advance(detector)
        reason, usage, due = detector.check(judge)
        assert due and reason and usage["output_tokens"] == 7
        assert len(judge.inputs) == expected
        assert detector.consecutive_count == expected
        assert (detector.confirm(reason) is None) == (expected == 3)
    assert judge.inputs[0]["previous_loop"] is None
    assert [x["turn"] for x in judge.inputs[1]["previous_loop"]["turns"]] == [1, 2, 3, 4, 5]
    assert [x["turn"] for x in judge.inputs[1]["recent_attempts"]] == [6, 7, 8, 9, 10]
    assert detector.status == "stuck"


def test_different_loop_and_no_loop_reset_count():
    detector = LoopDetector(status="running")
    judge = Judge([(True, False), (True, True), (True, False), (False, False), (True, False)])
    for expected in (1, 2, 1, 0, 1):
        advance(detector)
        reason, _, _ = detector.check(judge)
        assert detector.consecutive_count == expected
        if reason:
            assert detector.confirm(reason) is not None
        else:
            assert detector.previous_loop is None
    assert len(judge.inputs) == 5
    assert judge.inputs[-1]["previous_loop"] is None


@pytest.mark.parametrize("failure", [
    TimeoutError("unavailable"),
    "invalid json",
    '{"reason":"bad","is_stuck":true,"turns":[6,10],"same_as_previous":"true"}',
    '{"reason":"old evidence","is_stuck":true,"turns":[1,5],"same_as_previous":true}',
])
def test_failed_judgment_preserves_tracking(failure):
    detector = LoopDetector(status="running")
    judge = Judge([(True, False), failure, (True, True)])
    advance(detector)
    reason, _, _ = detector.check(judge)
    detector.confirm(reason)
    previous = detector.previous_loop
    advance(detector)
    reason, _, _ = detector.check(judge)
    assert reason is None
    assert detector.previous_loop == previous and detector.consecutive_count == 1
    advance(detector)
    assert detector.check(judge)[0]
    assert detector.consecutive_count == 2 and len(judge.inputs) == 3


def test_exact_repeat_uses_one_request_even_with_no_previous_record():
    detector = LoopDetector(status="running")
    judge = Judge([(True, False), (True, True), (True, True)])
    for count in (1, 2, 3):
        # Three identical failures can occur in a single multi-tool turn.
        detector.begin_turn("Retry")
        for _ in range(3):
            detector.record_result("Read", {"path": "missing"}, "Missing file", True)
        reason, _, due = detector.check(judge)
        assert not due and detector.consecutive_count == count
        assert judge.inputs[-1]["exact_repeat"] is True
        assert (detector.confirm(reason) is None) == (count == 3)
    assert len(judge.inputs) == 3


def test_restore_preserves_count_and_new_task_resets():
    conversation = Conversation()
    detector = conversation.loop_detector
    detector.begin_task()
    judge = Judge([(True, False), (True, True), (True, True)])
    for _ in range(2):
        advance(detector)
        reason, _, _ = detector.check(judge)
        detector.confirm(reason)
    restored = Conversation.from_dict(json.loads(json.dumps(conversation.to_dict())))
    restored.loop_detector.begin_task()
    advance(restored.loop_detector)
    reason, _, _ = restored.loop_detector.check(judge)
    assert restored.loop_detector.confirm(reason) is None
    restored.loop_detector.begin_task()
    assert restored.loop_detector.previous_loop is None
    assert restored.loop_detector.consecutive_count == restored.loop_detector.turn_count == 0
    legacy = LoopDetector().to_dict()
    legacy.pop("previous_loop")
    legacy.pop("consecutive_count")
    assert LoopDetector.from_dict(legacy).previous_loop is None


def test_periodic_schedule_is_unchanged():
    detector = LoopDetector(status="running")
    judge = Judge([(False, False), (False, False)])
    for turn in range(1, 11):
        advance(detector, 1)
        _, _, due = detector.check(judge)
        assert due == (turn in (5, 10))
    assert len(judge.inputs) == 2
