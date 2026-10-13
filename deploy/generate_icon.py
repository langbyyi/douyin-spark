"""Render the project SVG to PNG and multi-resolution ICO using existing Playwright."""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "src" / "dyspark" / "gui_static"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=("chrome", "msedge", "chromium"))
    args = parser.parse_args()
    svg = (STATIC / "icon.svg").read_text(encoding="utf-8")
    images = {}
    with sync_playwright() as playwright:
        channel = args.channel
        if channel is None and not Path(playwright.chromium.executable_path).is_file():
            channel = "chrome"
        with playwright.chromium.launch(
            channel=None if channel == "chromium" else channel, headless=True
        ) as browser:
            page = browser.new_page(device_scale_factor=1)
            page.route("**/*", lambda route: route.abort())
            page.set_content(
                "<style>html,body{margin:0;background:transparent}"
                "svg{display:block;width:100vw;height:100vh}</style>" + svg
            )
            for size in (*ICO_SIZES, 180, 512):
                page.set_viewport_size({"width": size, "height": size})
                images[size] = page.screenshot(omit_background=True)

    (STATIC / "icon.png").write_bytes(images[512])
    (STATIC / "apple-touch-icon.png").write_bytes(images[180])
    # ICO supports PNG frames; zero in a dimension byte denotes 256 pixels.
    offset = 6 + 16 * len(ICO_SIZES)
    entries = []
    for size in ICO_SIZES:
        frame = images[size]
        entries.append(
            struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(frame), offset)
        )
        offset += len(frame)
    (STATIC / "favicon.ico").write_bytes(
        struct.pack("<HHH", 0, 1, len(ICO_SIZES))
        + b"".join(entries)
        + b"".join(images[size] for size in ICO_SIZES)
    )
    print("Generated icon.png (512), apple-touch-icon.png (180), favicon.ico (16–256).")


if __name__ == "__main__":
    main()
