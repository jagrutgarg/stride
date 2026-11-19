"""Design tokens and stylesheet.

Direction: a bench instrument. Neutral graphite surfaces (apps stay grey), one accent taken from the
oscilloscope's CH1 trace colour, Geist for text and Geist Mono for figures. Spacing sits on a 4 px grid,
radii are 8 (controls) / 12 (cards). Motion explains state changes only and uses one easing curve.
(Tokens follow the unslop skill, CC BY 4.0, Damir Sokolovsky.)
"""
import os

from PySide6.QtGui import QFontDatabase

# ---- neutral ramp (graphite, dark) ----
DEEP = "#0f0f10"
BG = "#141415"
CARD = "#19191b"
RAISED = "#232325"
BORDER = "rgba(237,237,237,0.08)"          # hairline: primary text at 8 %
BORDER_HEX = "#252527"                     # same hairline flattened on CARD, for painted widgets
TEXT = "#ededed"
TEXT2 = "#a1a1a6"
MUTED = "#85858b"

# ---- accent: the CH1 trace colour on the scope ----
ACCENT = "#e6b450"
ACCENT_INK = "#1a1405"                     # text on the accent (>= 4.5:1)

# ---- semantic, toned down ----
GREEN = "#4cc38a"
RED = "#e5484d"
YELLOW = ACCENT                            # caution uses the accent, there is no second warm colour
ORANGE = ACCENT

# ---- series colours: muted, X leads, Z is neutral ----
SERIES = ["#6fa3e0", "#7ccba0", "#c8c8cc"]
TENG_COLOR = ACCENT

SANS = "Geist"
MONO = "Geist Mono"
EASE = "OutExpo"                           # Qt's closest to cubic-bezier(0.22, 1, 0.36, 1)

_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")


def load_fonts():
    """Register the bundled Geist files; fall back to system fonts if they are missing."""
    global SANS, MONO
    ok = False
    if os.path.isdir(_FONT_DIR):
        for f in sorted(os.listdir(_FONT_DIR)):
            if f.lower().endswith(".ttf"):
                ok |= QFontDatabase.addApplicationFont(os.path.join(_FONT_DIR, f)) >= 0
    if not ok:
        SANS, MONO = "Segoe UI", "Consolas"
    return ok


def stylesheet() -> str:
    return f"""
* {{ font-family: '{SANS}', 'Segoe UI'; font-size: 13px; color: {TEXT}; }}
QMainWindow, QWidget#root {{ background: {BG}; }}
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}
QLabel {{ background: transparent; border: none; }}
QLabel#muted {{ color: {TEXT2}; }}
QLabel#caption {{ color: {MUTED}; font-size: 12px; }}
QLabel#cardTitle {{ color: {TEXT2}; font-size: 13px; font-weight: 500; }}
QLabel#value {{ font-family: '{MONO}', Consolas; font-size: 15px; }}
QLabel#wordmark {{ font-size: 16px; font-weight: 500; }}
QLineEdit, QComboBox, QDoubleSpinBox {{
    background: {BG}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px 12px; min-height: 28px;
    selection-background-color: {ACCENT}; selection-color: {ACCENT_INK};
}}
QLineEdit:hover, QComboBox:hover, QDoubleSpinBox:hover {{ border-color: rgba(237,237,237,0.18); }}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {MUTED}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {RAISED}; border: 1px solid {BORDER}; selection-background-color: #2e2e31; outline: 0;
}}
QTextEdit, QPlainTextEdit {{
    background: {BG}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px 8px;
    font-family: '{MONO}', Consolas; font-size: 12px;
}}
QTabWidget::pane {{ border: none; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 8px 12px; border: none;
    border-bottom: 2px solid transparent; margin-right: 4px; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover:!selected {{ color: {TEXT2}; }}
QToolTip {{ background: {RAISED}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 8px; }}
QSplitter::handle {{ background: {BG}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #2c2c2f; border-radius: 4px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #3a3a3e; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #2c2c2f; border-radius: 4px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QMessageBox, QDialog, QProgressDialog {{ background: {CARD}; }}
QProgressBar {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 4px; text-align: center; height: 8px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QTableWidget {{
    background: {BG}; border: 1px solid {BORDER}; border-radius: 8px; gridline-color: transparent;
    font-family: '{MONO}', Consolas; font-size: 12px; outline: 0;
}}
QTableWidget::item {{ padding: 0 8px; border-bottom: 1px solid {BORDER}; }}
QHeaderView::section {{
    background: {BG}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER};
    padding: 4px 8px; font-family: '{SANS}'; font-size: 12px; font-weight: 400;
}}
"""
