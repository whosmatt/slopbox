"""Local fonts and decorative assets the agent may reference.

Both are served from our own origin, so `font-src 'self'` and `img-src 'self'`
already cover them and the CSP needs no loosening. Nothing here reaches the
network: the fonts ship inside the base image, and the patterns are files in
this repository.

The font files are the ones Debian already installs for headless Chromium.
Serving them matters for honesty as much as for looks - a system font renders in
the validator's browser but falls back to something else in a visitor's, so the
model's screenshot would otherwise show a typeface nobody else sees.
"""
from __future__ import annotations

from pathlib import Path

from .config import STATIC_DIR

FONT_ROOT = Path("/usr/share/fonts/truetype/liberation")

# Liberation only: OFL 1.1, metric-compatible with the usual proprietary
# faces, and ~100-150KB per file. GNU FreeFont is also present in the image but
# FreeSerif alone is 1.9MB, which is not worth it for a fun site.
FONTS = {
    "Slop Sans": {
        "400": "LiberationSans-Regular.ttf",
        "700": "LiberationSans-Bold.ttf",
        "400i": "LiberationSans-Italic.ttf",
        "700i": "LiberationSans-BoldItalic.ttf",
    },
    "Slop Serif": {
        "400": "LiberationSerif-Regular.ttf",
        "700": "LiberationSerif-Bold.ttf",
        "400i": "LiberationSerif-Italic.ttf",
        "700i": "LiberationSerif-BoldItalic.ttf",
    },
    "Slop Mono": {
        "400": "LiberationMono-Regular.ttf",
        "700": "LiberationMono-Bold.ttf",
        "400i": "LiberationMono-Italic.ttf",
        "700i": "LiberationMono-BoldItalic.ttf",
    },
    "Slop Narrow": {
        "400": "LiberationSansNarrow-Regular.ttf",
        "700": "LiberationSansNarrow-Bold.ttf",
        "400i": "LiberationSansNarrow-Italic.ttf",
        "700i": "LiberationSansNarrow-BoldItalic.ttf",
    },
}

# filename -> absolute path. A fixed dict, so /fonts/<name> can never be walked
# out of the font directory however the name is spelled.
FONT_FILES = {
    filename: FONT_ROOT / filename
    for faces in FONTS.values()
    for filename in faces.values()
}

ASSET_DIR = STATIC_DIR / "assets"


def asset_names():
    """Pattern files that exist on disk, sorted for a stable prompt."""
    try:
        return sorted(f.name for f in ASSET_DIR.glob("*.svg"))
    except FileNotFoundError:
        return []


def asset_path(name):
    """Resolve an asset name, refusing anything that escapes the directory."""
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    candidate = ASSET_DIR / name
    try:
        candidate.relative_to(ASSET_DIR.resolve())
    except ValueError:
        try:
            candidate.resolve().relative_to(ASSET_DIR.resolve())
        except ValueError:
            return None
    return candidate if candidate.is_file() else None


def font_face_css():
    """The @font-face block the agent can rely on being present.

    Written by us, not the agent, so the families work without it having to get
    @font-face syntax right - it only needs `font-family: "Slop Serif"`.
    """
    blocks = []
    for family, faces in FONTS.items():
        for key, filename in faces.items():
            if not (FONT_ROOT / filename).exists():
                continue
            weight = key.rstrip("i")
            style = "italic" if key.endswith("i") else "normal"
            blocks.append(
                "@font-face{font-family:%r;font-weight:%s;font-style:%s;"
                "font-display:swap;src:url('/fonts/%s') format('truetype')}"
                % (family, weight, style, filename)
            )
    return "\n".join(blocks).replace("'", '"') + "\n"


def available_fonts():
    return [f for f in FONTS if all((FONT_ROOT / n).exists() for n in FONTS[f].values())]


def prompt_section(enable_fonts, enable_assets):
    """What to tell the model about resources it may reference."""
    lines = []
    fonts = available_fonts() if enable_fonts else []
    if fonts:
        lines.append(
            "LOCAL FONTS (already declared for you - just use the family name; "
            "each has regular, bold, italic and bold-italic): "
            + ", ".join('"%s"' % f for f in fonts)
            + ". These are real files served from this site, so the visitor sees "
            "exactly what your screenshot shows. Any other family name falls back "
            "to whatever the visitor happens to have."
        )
    names = asset_names() if enable_assets else []
    if names:
        lines.append(
            "LOCAL PATTERNS, usable as url(/assets/NAME) in background-image and "
            "designed to tile: " + ", ".join(names) + ". They are single-colour "
            "black shapes on transparency, so tint them by layering a colour "
            "underneath rather than expecting them to recolour themselves."
        )
    return "\n".join(lines)
