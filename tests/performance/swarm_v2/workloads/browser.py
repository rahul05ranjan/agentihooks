"""Browser heavy workload: headless Chromium with one isolated page per child, each rendering and scripting a large page."""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROWS = 4000
SCRIPT = """() => {
  const canvas = document.querySelector('canvas');
  const ctx = canvas.getContext('2d');
  let total = 0;
  for (let round = 0; round < 40; round++) {
    for (let i = 0; i < 2000; i++) {
      ctx.fillStyle = `hsl(${(i * 7 + round) % 360}, 60%, 50%)`;
      ctx.fillRect((i * 13) % 1200, (i * 17) % 800, 9, 9);
    }
    const rows = [...document.querySelectorAll('tr')];
    total += rows.filter((r, i) => (i + round) % 3 === 0).length;
    window.scrollTo(0, (round * 997) % document.body.scrollHeight);
  }
  return total;
}"""


def page(root: Path) -> Path:
    rows = "".join(f"<tr><td>{i}</td><td>row {i}</td><td>{i * 31 % 977}</td></tr>" for i in range(ROWS))
    path = root / "page.html"
    path.write_text(
        f"<!doctype html><html><body><canvas width=1200 height=800></canvas><table>{rows}</table></body></html>"
    )
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--children", type=int, required=True)
    args = parser.parse_args()
    url = page(Path.cwd()).as_uri()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        pages = [browser.new_context().new_page() for _ in range(max(args.children, 1))]
        for tab in pages:
            tab.goto(url)
        totals = [tab.evaluate(SCRIPT) for tab in pages for _ in range(2)]
        browser.close()
    return 0 if all(totals) else 1


if __name__ == "__main__":
    sys.exit(main())
