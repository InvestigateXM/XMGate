"""Builds "tap the two faction logos" challenges (design §7).

Tiles are rendered here as self-contained SVGs with random ids, so the answer
never reaches the client. The glyphs are simplified placeholders drawn for this
prototype, not Niantic artwork. The full design rasterises tiles to PNG.
"""

import random
import secrets
import time

COLUMNS, ROWS = 3, 4
TILE_COUNT = COLUMNS * ROWS
ANSWERS = ("enl", "res")

# Waits before a new grid, by number of wrong answers in a row (design §7):
# the first three are free, then 5 s, 15 s and 30 s.
WAITS = (0, 0, 0, 5, 15, 30)
SUGGEST_MANUAL_AFTER = 5
MISS_WINDOW = 3600

_S = 'fill="none" stroke-linecap="round" stroke-linejoin="round"'

GLYPHS = {
    "enl": ("Enlightened", f'<circle cx="50" cy="50" r="38" {_S}/><path d="M50 16 C70 34 70 66 50 84 C30 66 30 34 50 16Z" {_S}/>'),
    "res": ("Resistance", f'<path d="M50 12 L84 32 L84 68 L50 88 L16 68 L16 32Z" {_S}/><path d="M50 30 L50 70 M34 44 L50 30 L66 44" {_S}/>'),
    "reso": ("Resonator", f'<path d="M50 14 L68 50 L50 86 L32 50Z" {_S}/><path d="M32 50 H68" {_S}/>'),
    "xmp": ("XMP burster", f'<circle cx="50" cy="50" r="12" {_S}/><path d="M50 14 V30 M50 70 V86 M14 50 H30 M70 50 H86" {_S}/>'),
    "us": ("Ultra strike", f'<path d="M50 14 L50 86 M30 34 L50 14 L70 34" {_S}/><circle cx="50" cy="62" r="8" {_S}/>'),
    "cube": ("Power cube", f'<rect x="24" y="24" width="52" height="52" rx="4" {_S}/><path d="M24 50 H76 M50 24 V76" {_S}/>'),
    "key": ("Portal key", f'<circle cx="36" cy="50" r="16" {_S}/><path d="M52 50 H84 M74 50 V62 M84 50 V60" {_S}/>'),
    "shield": ("Portal shield", f'<path d="M50 14 L80 26 V50 C80 70 64 82 50 88 C36 82 20 70 20 50 V26Z" {_S}/>'),
    "hs": ("Heat sink", f'<path d="M26 30 H74 M26 44 H74 M26 58 H74 M26 72 H74" {_S}/>'),
    "mh": ("Multi-hack", f'<path d="M22 70 L40 30 L50 55 L60 30 L78 70" {_S}/>'),
    "turret": ("Turret", f'<circle cx="50" cy="56" r="20" {_S}/><path d="M50 36 V14" {_S}/>'),
    "amp": ("Link amp", f'<path d="M20 50 H80" {_S}/><circle cx="20" cy="50" r="8" {_S}/><circle cx="80" cy="50" r="8" {_S}/><path d="M40 36 L60 64" {_S}/>'),
    "caps": ("Capsule", f'<rect x="30" y="16" width="40" height="68" rx="20" {_S}/><path d="M30 50 H70" {_S}/>'),
    "hyper": ("Hypercube", f'<rect x="18" y="18" width="44" height="44" {_S}/><rect x="38" y="38" width="44" height="44" {_S}/>'),
    "fa": ("Force amp", f'<path d="M50 14 L80 50 L50 86 L20 50Z" {_S}/><path d="M50 32 L50 68 M36 50 H64" {_S}/>'),
    "beacon": ("Beacon", f'<path d="M50 86 V40" {_S}/><path d="M30 40 L50 14 L70 40Z" {_S}/><path d="M36 60 H64" {_S}/>'),
    "frack": ("Fracker", f'<path d="M20 80 L50 20 L80 80Z" {_S}/><path d="M35 60 H65 M42 46 H58" {_S}/>'),
}
DECOYS = [k for k in GLYPHS if k not in ANSWERS]

# One ink per grid, so colour never tells the logos apart from the items.
INKS = ("#2F7D6D", "#3B5BA9", "#8A5A2B", "#6C4AA0", "#9A3F57", "#3F6F8F")


def render_tile(key: str, ink: str, rng: random.Random) -> str:
    rot = rng.uniform(-40, 40)
    scale = rng.uniform(0.72, 0.95)
    dx, dy = rng.uniform(-6, 6), rng.uniform(-6, 6)
    width = rng.uniform(4.5, 6.5)
    noise = []
    for _ in range(rng.randint(5, 9)):
        if rng.random() < 0.5:
            noise.append(
                f'<circle cx="{rng.uniform(4, 96):.1f}" cy="{rng.uniform(4, 96):.1f}" r="{rng.uniform(1, 3):.1f}" '
                f'fill="{ink}" opacity="{rng.uniform(.12, .3):.2f}"/>'
            )
        else:
            noise.append(
                f'<path d="M{rng.uniform(0, 100):.1f} {rng.uniform(0, 100):.1f} L{rng.uniform(0, 100):.1f} '
                f'{rng.uniform(0, 100):.1f}" stroke="{ink}" stroke-width="{rng.uniform(1, 2):.1f}" '
                f'opacity="{rng.uniform(.1, .22):.2f}"/>'
            )
    glyph = GLYPHS[key][1]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        + "".join(noise)
        + f'<g transform="translate({50 + dx:.1f} {50 + dy:.1f}) rotate({rot:.1f}) scale({scale:.2f}) translate(-50 -50)" '
        f'stroke="{ink}" stroke-width="{width:.1f}">{glyph}</g></svg>'
    )


def new_challenge(rng: random.Random | None = None) -> tuple[list[dict], list[dict]]:
    """Returns (stored tiles with answers, public tiles for the client)."""
    rng = rng or random.SystemRandom()
    keys = list(ANSWERS) + rng.sample(DECOYS, TILE_COUNT - len(ANSWERS))
    rng.shuffle(keys)
    ink = rng.choice(INKS)
    stored, public = [], []
    for key in keys:
        tile_id = secrets.token_urlsafe(9)
        stored.append({"id": tile_id, "key": key, "answer": key in ANSWERS})
        public.append({"id": tile_id, "svg": render_tile(key, ink, rng)})
    return stored, public


def is_correct(stored: list[dict], picked: list[str]) -> bool:
    answers = {t["id"] for t in stored if t["answer"]}
    return len(picked) == len(answers) and set(picked) == answers


def wait_seconds(misses: list[float], now: float | None = None) -> int:
    """How long the user must still wait before the next grid. `misses` is newest first."""
    if not misses:
        return 0
    needed = WAITS[min(len(misses), len(WAITS) - 1)]
    elapsed = (now or time.time()) - misses[0]
    return max(0, int(needed - elapsed + 0.999))
