import random

from xmgate import captcha


def test_grid_has_two_answers_and_ten_distinct_decoys():
    stored, public = captcha.new_challenge(random.Random(1))
    assert len(stored) == len(public) == 12
    assert sorted(t["key"] for t in stored if t["answer"]) == ["enl", "res"]
    assert len({t["key"] for t in stored}) == 12
    assert [t["id"] for t in stored] == [t["id"] for t in public]


def test_every_icon_except_the_logos_is_a_decoy():
    files = {p.stem for p in captcha.ICON_DIR.glob("*.png")}
    logos = {name.removesuffix(".png") for name in captcha.ANSWER_FILES.values()}
    assert set(captcha.decoy_keys()) == files - logos
    assert len(captcha.decoy_keys()) >= captcha.TILE_COUNT - 2


def test_decoys_change_between_grids():
    rng = random.Random(7)
    grids = [frozenset(t["key"] for t in captcha.new_challenge(rng)[0]) for _ in range(5)]
    assert all({"enl", "res"} <= g for g in grids)
    assert len(set(grids)) == 5


def test_tiles_are_png_and_reveal_nothing():
    stored, public = captcha.new_challenge(random.Random(2))
    for tile in public:
        assert set(tile) == {"id", "img"}
        assert tile["img"].startswith("data:image/png;base64,")
    # the same icon renders differently each time
    rng = random.Random(4)
    ink, bg = captcha.INKS[0], captcha.BACKGROUNDS[0]
    assert captcha.render_tile("enl", ink, bg, rng) != captcha.render_tile("enl", ink, bg, rng)


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
