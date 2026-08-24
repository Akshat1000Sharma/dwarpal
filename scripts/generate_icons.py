"""Regenerate the site icon set from the Dwarpal logo.

The logo is a three-part lockup: an arch, a shield inside it, and the wordmark below. Each output
uses the largest part that is still readable at its size. The wordmark is illegible below roughly
64px and the arch's circuit detail turns to mud below 48px, so the favicons carry the shield alone.

Run after changing frontend/public/logo/logo.png. Needs Pillow, which is not a project dependency
because nothing at run time reads an image:

    python -m pip install pillow
    python scripts/generate_icons.py
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent / "frontend"
SOURCE = ROOT / "public" / "logo" / "logo.png"

# Sampled from the logo's own outline, and close to the dashboard's dark surface.
BRAND = (7, 17, 33, 255)

SHIELD_BOX = (410, 320, 840, 880)
ARCH_BOX = (0, 118, 1254, 898)
LOCKUP_BOX = (0, 118, 1254, 1092)


def part(box: tuple[int, int, int, int]) -> Image.Image:
    art = Image.open(SOURCE).convert("RGBA").crop(box)
    bbox = art.getbbox()
    return art.crop(bbox) if bbox else art


def tile(art: Image.Image, size: int, inset: float) -> Image.Image:
    """Fit the art in a square, leaving inset as a fraction of the edge free on every side."""
    canvas = Image.new("RGBA", (size, size), BRAND)
    scaled = art.copy()
    usable = int(size * (1 - 2 * inset))
    scaled.thumbnail((usable, usable), Image.LANCZOS)
    canvas.alpha_composite(scaled, ((size - scaled.width) // 2, (size - scaled.height) // 2))
    return canvas


shield, arch, lockup = part(SHIELD_BOX), part(ARCH_BOX), part(LOCKUP_BOX)
written: list[tuple[Path, str]] = []

# Rendered once per size rather than letting the encoder downscale a single large frame, so each
# entry is resampled from the full-resolution art.
ico = ROOT / "app" / "favicon.ico"
frames = [tile(shield, s, 0.03) for s in (16, 32, 48, 64)]
frames[-1].save(ico, format="ICO", sizes=[f.size for f in frames], append_images=frames[:-1])
written.append((ico, "16/32/48/64"))

png = ROOT / "public" / "favicon.png"
tile(shield, 32, 0.03).save(png, format="PNG", optimize=True)
written.append((png, "32x32"))

# Opaque with a margin, so a launcher's mask cannot clip the arch.
for size in (192, 512):
    p = ROOT / "public" / f"icon-{size}x{size}.png"
    tile(arch, size, 0.10).save(p, format="PNG", optimize=True)
    written.append((p, f"{size}x{size}"))

# Apple composites on black and applies its own rounded mask, so this is opaque and full bleed.
apple = ROOT / "public" / "apple-touch-icon.png"
tile(arch, 180, 0.08).save(apple, format="PNG", optimize=True)
written.append((apple, "180x180"))

# The social card is the only place with room for the wordmark, which is the point at that size.
card = Image.new("RGBA", (1200, 630), BRAND)
art = lockup.copy()
art.thumbnail((int(1200 * 0.46), int(630 * 0.80)), Image.LANCZOS)
card.alpha_composite(art, ((1200 - art.width) // 2, (630 - art.height) // 2))
og = ROOT / "public" / "og-image.png"
card.convert("RGB").save(og, format="PNG", optimize=True)
written.append((og, "1200x630"))

for path, dims in written:
    print(f"  {str(path.relative_to(ROOT)):34} {dims:22} {path.stat().st_size / 1024:7.1f} KB")
