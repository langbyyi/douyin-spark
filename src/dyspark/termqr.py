"""Draw a QR code in the terminal so a phone camera can open the printed URL."""

from __future__ import annotations

import segno

QUIET_ZONE = 2  # modules of light border around the symbol


def _can_encode(chars: str, encoding: str) -> bool:
    try:
        chars.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def light_grid(text: str) -> list[list[bool]]:
    """The QR symbol for ``text`` as rows of "light module?" flags, quiet zone included."""
    symbol = segno.make(text, error="m", micro=False)
    rows = [[not bool(module) for module in row] for row in symbol.matrix]
    width = len(rows[0]) + 2 * QUIET_ZONE
    border = [[True] * width for _ in range(QUIET_ZONE)]
    padded = [[True] * QUIET_ZONE + row + [True] * QUIET_ZONE for row in rows]
    return border + padded + [list(row) for row in border]


def render(text: str, encoding: str = "utf-8") -> str | None:
    """Block-character QR code, or ``None`` if ``encoding`` cannot carry the blocks.

    Light modules are drawn as blocks, which reads correctly on the usual dark
    terminal background. Half blocks pack two module rows per text line.
    """
    grid = light_grid(text)
    if _can_encode("▀▄█", encoding):
        if len(grid) % 2:
            grid.append([True] * len(grid[0]))
        glyphs = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}
        lines = [
            "".join(glyphs[top, bottom] for top, bottom in zip(upper, lower, strict=True))
            for upper, lower in zip(grid[::2], grid[1::2], strict=True)
        ]
    elif _can_encode("█", encoding):
        lines = ["".join("██" if light else "  " for light in row) for row in grid]
    else:
        return None
    return "\n".join(lines)
