import random

from xmgate import captcha


def test_grid_has_two_answers_and_ten_distinct_decoys():
    stored, public = captcha.new_challenge(random.Random(1))
    assert len(stored) == len(public) == 12
    assert sorted(t["key"] for t in stored if t["answer"]) == ["enl", "res"]
    assert len({t["key"] for t in stored}) == 12
    assert [t["id"] for t in stored] == [t["id"] for t in public]


def test_is_correct_needs_exactly_both_logos():
    stored, _ = captcha.new_challenge()
    answers = [t["id"] for t in stored if t["answer"]]
    decoy = next(t["id"] for t in stored if not t["answer"])
    assert captcha.is_correct(stored, answers[::-1])
    assert not captcha.is_correct(stored, answers[:1])
    assert not captcha.is_correct(stored, [answers[0], decoy])
    assert not captcha.is_correct(stored, answers + [decoy])


def test_waits_start_after_third_miss():
    now = 1000.0
    assert captcha.wait_seconds([], now) == 0
    assert captcha.wait_seconds([now] * 2, now) == 0
    assert captcha.wait_seconds([now] * 3, now) == 5
    assert captcha.wait_seconds([now] * 4, now) == 15
    assert captcha.wait_seconds([now] * 9, now) == 30
    assert captcha.wait_seconds([now - 10] * 3, now) == 0
