"""Entry point for the `streamgrab` console command."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon, QFont

from streamgrab.ui.main_window import MainWindow

LOGO_PATH = Path(__file__).parent / "resources" / "logo.png"


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("StreamGrab")

    # Explicitly set a valid application-wide font with a real point size.
    # PyQt6's default QFont() reports pointSize() == -1 on some platforms
    # (confirmed: Windows commonly hits this), which triggers a harmless
    # but noisy "QFont::setPointSize: Point size <= 0" warning whenever
    # Qt's internal style code queries the default font. Setting a real
    # font here up front means Qt never falls back to that ambiguous
    # default, eliminating the warning at its source rather than just
    # hiding it.
    app.setFont(QFont("Segoe UI", 10))

    if LOGO_PATH.exists():
        app.setWindowIcon(QIcon(str(LOGO_PATH)))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
