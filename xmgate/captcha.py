"""Builds "tap the two faction logos" challenges (design §7).

Every grid shows both faction logos plus decoys drawn at random from the rest
of xmgate/icons/. Drop a PNG into that folder to add a decoy; a transparent
background works best.

Tiles are rendered here as PNGs with random ids, so neither the answer nor the
original file reaches the client. Each icon is recoloured to one ink per grid
(so colour never gives the logos away), then rotated, scaled, moved and laid
over background noise.
"""

import base64
import io
import random
import secrets
import time
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat

ICON_DIR = Path(__file__).resolve().parent / "icons"
ANSWER_FILES = {
    "enl": "avatar-faction-enlightened.png",
    "res": "avatar-faction-resistance.png",
}

COLUMNS, ROWS = 3, 4
TILE_COUNT = COLUMNS * ROWS
TILE_PX = 192

# Waits before a new grid, by number of wrong answers in a row (design §7):
# the first three are free, then 5 s, 15 s and 30 s.
WAITS = (0, 0, 0, 5, 15, 30)
SUGGEST_MANUAL_AFTER = 5
MISS_WINDOW = 3600

# One light ink per grid on a dark tile, like the game's own UI.
INKS = ((236, 241, 238), (150, 232, 255), (255, 214, 140), (196, 255, 214), (230, 200, 255))
BACKGROUNDS = ((18, 28, 32), (24, 26, 36), (28, 24, 22), (16, 30, 26))


@lru_cache(maxsize=1)
def icon_masks() -> dict[str, Image.Image]:
    """Alpha mask of every icon, cropped to its content. Keys: 'enl', 'res', and decoy file stems."""
    masks = {}
    for path in sorted(ICON_DIR.glob("*.png")):
        img = Image.open(path).convert("RGBA")
        alpha = img.getchannel("A")
        # Light icons keep their shading, dark ones (black or grey on transparency) are
        # inverted, so inner detail survives the recolouring. Then stretch each icon to
        # full strength, so a coloured logo looks no fainter than a white item.
        lum = img.convert("L")
        if _mean(lum, alpha) < 110:
            lum = ImageChops.invert(lum)
        mask = ImageChops.multiply(alpha, lum)
        peak = mask.getextrema()[1]
        if peak:
            mask = mask.point(lambda v, peak=peak: min(255, v * 255 // peak))
        # Crop to the visible shape; faint glows would otherwise make the icon look small.
        box = mask.point(lambda v: 255 if v > 48 else 0).getbbox()
        if not box:
            continue
        key = next((k for k, f in ANSWER_FILES.items() if f == path.name), path.stem)
        masks[key] = mask.crop(box)
    missing = set(ANSWER_FILES) - set(masks)
    if missing:
        raise RuntimeError(f"faction logo missing from {ICON_DIR}: {', '.join(ANSWER_FILES[k] for k in missing)}")
    return masks


def _mean(lum: Image.Image, alpha: Image.Image) -> float:
    """Mean luminance of the visible pixels."""
    visible = alpha.point(lambda v: 255 if v > 32 else 0)
    return ImageStat.Stat(lum, mask=visible).mean[0] if visible.getbbox() else 255


def decoy_keys() -> list[str]:
    return [k for k in icon_masks() if k not in ANSWER_FILES]


def render_tile(key: str, ink: tuple, bg: tuple, rng: random.Random) -> bytes:
    size = TILE_PX
    tile = Image.new("RGB", (size, size), tuple(max(0, min(255, c + rng.randint(-6, 6))) for c in bg))
    draw = ImageDraw.Draw(tile, "RGBA")
    for _ in range(rng.randint(6, 11)):
        alpha = rng.randint(25, 60)
        if rng.random() < 0.5:
            x, y, r = rng.uniform(0, size), rng.uniform(0, size), rng.uniform(1.5, 4)
            draw.ellipse((x - r, y - r, x + r, y + r), fill=(*ink, alpha))
        else:
            draw.line(
                (rng.uniform(0, size), rng.uniform(0, size), rng.uniform(0, size), rng.uniform(0, size)),
                fill=(*ink, alpha),
                width=rng.randint(1, 2),
            )

    mask = icon_masks()[key]
    # Size by area, so tall thin icons (like the Enlightened logo) don't look smaller than square ones.
    target = size * rng.uniform(0.52, 0.64)
    scale = min(target / (mask.width * mask.height) ** 0.5, size * 0.8 / max(mask.size))
    mask = mask.resize((max(1, round(mask.width * scale)), max(1, round(mask.height * scale))), Image.LANCZOS)
    mask = mask.rotate(rng.uniform(-40, 40), resample=Image.BICUBIC, expand=True)
    x = (size - mask.width) // 2 + rng.randint(-10, 10)
    y = (size - mask.height) // 2 + rng.randint(-10, 10)
    tile.paste(Image.new("RGB", mask.size, ink), (x, y), mask)

    out = io.BytesIO()
    tile.save(out, format="PNG", optimize=True)
    return out.getvalue()


def new_challenge(rng: random.Random | None = None) -> tuple[list[dict], list[dict]]:
    """Returns (stored tiles with answers, public tiles for the client)."""
    rng = rng or random.SystemRandom()
    keys = list(ANSWER_FILES) + rng.sample(decoy_keys(), TILE_COUNT - len(ANSWER_FILES))
    rng.shuffle(keys)
    ink, bg = rng.choice(INKS), rng.choice(BACKGROUNDS)
    stored, public = [], []
    for key in keys:
        tile_id = secrets.token_urlsafe(9)
        png = base64.b64encode(render_tile(key, ink, bg, rng)).decode()
        stored.append({"id": tile_id, "key": key, "answer": key in ANSWER_FILES})
        public.append({"id": tile_id, "img": f"data:image/png;base64,{png}"})
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
