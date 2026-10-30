"""Generate a horizontal row of teammate chat panels for testing.

The pain point this solves: hand-computing twelve ROIs is absurd, and testing
multi-region capture needs a scene where each region is a different teammate's
chat box. This lays the panels out to fit a given screen and can write the
matching config for you.

    python tools/demo_scene.py --screen 1 --write-config
    python tools/demo_scene.py --screen 1 --no-window     # just print fractions

Then run the product normally:

    translator run --skip-setup --no-settings
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

TEAM = [
    ("PlayerOne", "#ff6b6b"),
    ("xX_Shadow_Xx", "#7ecbff"),
    ("TeamMate", "#9dff9d"),
    ("RushKing", "#ffd166"),
    ("Ninja", "#c792ff"),
    ("Sniper42", "#66ffd9"),
    ("Medic", "#ff9ecd"),
    ("Tank", "#a0d8ff"),
    ("Ghost", "#e0e0e0"),
    ("Blaze", "#ffa94d"),
    ("Viper", "#b8ff6b"),
    ("Echo", "#8fd3ff"),
]

MESSAGES = [
    "rush B site now",
    "gg wp",
    "enemy behind box, 2hp",
    "need cover reloading",
    "i have ult",
    "rotate to A",
    "nice shot",
    "oom wait 30s",
    "they are pushing mid",
    "watch the flank",
    "planting now",
    "defuse, cover me",
]

MARGIN = 10
GAP = 6
HEIGHT = 92
TOP = 380
TICK_MS = 1200  # one random panel changes at a time


def layout(screen_w: int, count: int = len(TEAM)) -> tuple[int, int]:
    """Panel width and the left edge of the row, sized to fit ``screen_w``."""
    width = (screen_w - 2 * MARGIN - (count - 1) * GAP) // count
    return width, MARGIN


def regions_for(screen_w: int, screen_h: int, count: int = len(TEAM)):
    width, left = layout(screen_w, count)
    out = []
    for i in range(count):
        x = left + i * (width + GAP)
        out.append(
            (
                round(x / screen_w, 4),
                round(TOP / screen_h, 4),
                round(width / screen_w, 4),
                round(HEIGHT / screen_h, 4),
            )
        )
    return out


def run_window(screen_w: int, origin: tuple[int, int] = (0, 0)) -> None:
    from PySide6.QtCore import QRect, Qt, QTimer
    from PySide6.QtGui import QColor, QFont, QPainter
    from PySide6.QtWidgets import QApplication, QWidget

    width, left = layout(screen_w)
    # Reuse the singleton: main() already created one to read the screen list,
    # and PySide refuses to create a second QApplication.
    app = QApplication.instance() or QApplication([])

    class Panel(QWidget):
        def __init__(self) -> None:
            super().__init__(
                None,
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool,
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setGeometry(QRect(origin[0], origin[1] + TOP, screen_w, HEIGHT))
            self.cursor = [random.randrange(len(MESSAGES)) for _ in TEAM]
            self.name_font = QFont("DejaVu Sans", 9, QFont.Weight.Bold)
            self.msg_font = QFont("DejaVu Sans", 8)
            timer = QTimer(self)
            timer.timeout.connect(self.tick)
            timer.start(TICK_MS)

        def tick(self) -> None:
            i = random.randrange(len(TEAM))
            self.cursor[i] = (self.cursor[i] + 1) % len(MESSAGES)
            self.update(QRect(left + i * (width + GAP), 0, width, HEIGHT))

        def paintEvent(self, _event) -> None:  # noqa: N802
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            for i, (name, colour) in enumerate(TEAM):
                x = left + i * (width + GAP)
                # Opaque panels: a translucent backdrop lets the desktop text
                # bleed through and OCR then reads nonsense.
                painter.fillRect(QRect(x, 0, width, HEIGHT), QColor(10, 12, 16))
                painter.setPen(QColor(70, 78, 92))
                painter.drawRect(QRect(x, 0, width - 1, HEIGHT - 1))
                painter.setFont(self.name_font)
                painter.setPen(QColor(colour))
                painter.drawText(x + 8, 20, name)
                painter.setFont(self.msg_font)
                painter.setPen(QColor(235, 235, 235))
                painter.drawText(
                    QRect(x + 8, 28, width - 16, HEIGHT - 34),
                    Qt.TextFlag.TextWordWrap,
                    MESSAGES[self.cursor[i]],
                )

    panel = Panel()
    panel.show()
    print(f"panels shown at origin={origin}, {len(TEAM)} boxes", flush=True)
    app.exec()


def main() -> int:
    parser = argparse.ArgumentParser(description="demo scene of N teammate chat panels")
    parser.add_argument("--screen", type=int, default=0, help="Qt screen index")
    parser.add_argument("--count", type=int, default=len(TEAM))
    parser.add_argument("--no-window", action="store_true", help="print the layout only")
    parser.add_argument("--write-config", action="store_true", help="save regions to config.toml")
    parser.add_argument("--config", default=None, help="config path for --write-config")
    args = parser.parse_args()

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    screens = app.screens()
    index = max(0, min(args.screen, len(screens) - 1))
    geometry = screens[index].geometry()
    origin = (geometry.x(), geometry.y())
    print(
        f"screen[{index}] {screens[index].name()} {geometry.width()}x{geometry.height()} "
        f"origin={origin}"
    )

    regions = regions_for(geometry.width(), geometry.height(), args.count)
    print(f"{len(regions)} regions:")
    for i, (x, y, w, h) in enumerate(regions, start=1):
        print(
            f"  {i:2}. {x:.4f} {y:.4f} {w:.4f} {h:.4f}   "
            f"-> {round(x * geometry.width())},{round(y * geometry.height())} "
            f"{round(w * geometry.width())}x{round(h * geometry.height())} px"
        )

    if args.write_config:
        from translator.core.config import AppConfig

        cfg = AppConfig.load(args.config)
        cfg.screen.monitor_origin = origin
        cfg.screen.regions = regions
        cfg.screen.region = None
        cfg.screen.monitor = 1
        path = cfg.save(args.config)
        print(f"config written: {path}")

    if not args.no_window:
        run_window(geometry.width(), origin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
