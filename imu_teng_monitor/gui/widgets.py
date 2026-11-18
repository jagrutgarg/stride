"""Small custom widgets: animated button, status dot, trigger lamp, dot + label, fade helpers.

Motion rules: it explains state (hover, connection activity, trigger fired, banners, first paint).
One easing curve, 120 to 360 ms, only colour / opacity change.
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import (QEasingCurve, QPointF, QPropertyAnimation, QRectF, QSize, Qt, QTimer,
                            QVariantAnimation)
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QWidget)

from gui import theme


def _ease() -> QEasingCurve:
    return QEasingCurve(QEasingCurve.OutExpo)


def mix(a: str, b: str, t: float) -> QColor:
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t), round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t))


# ---------------------------------------------------------------- fades
def fade_in(widget: QWidget, delay_ms: int = 0, duration_ms: int = 360):
    """Opacity 0 -> 1, then drop the effect so the widget paints normally again."""
    eff = QGraphicsOpacityEffect(widget)
    eff.setOpacity(0.0)
    widget.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(duration_ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(_ease())
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    QTimer.singleShot(delay_ms, anim.start)
    widget._fade_anim = anim          # keep a reference


def set_visible_animated(widget: QWidget, visible: bool, duration_ms: int = 200):
    """Show/hide with a short fade (hide completes after the fade)."""
    if visible == getattr(widget, "_want_visible", widget.isVisible()):
        return
    widget._want_visible = visible
    eff = widget.graphicsEffect()
    if not isinstance(eff, QGraphicsOpacityEffect):
        eff = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(duration_ms)
    anim.setEasingCurve(_ease())
    if visible:
        widget.setVisible(True)
        anim.setStartValue(eff.opacity() if eff.opacity() < 1 else 0.0)
        anim.setEndValue(1.0)
        anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    else:
        anim.setStartValue(eff.opacity())
        anim.setEndValue(0.0)
        anim.finished.connect(lambda: (widget.setVisible(False), widget.setGraphicsEffect(None)))
    anim.start()
    widget._vis_anim = anim


def set_visible_now(widget: QWidget, visible: bool):
    anim = getattr(widget, "_vis_anim", None)
    if anim is not None:
        anim.stop()
    widget.setGraphicsEffect(None)
    widget._want_visible = visible
    widget.setVisible(visible)


# -------------------------------------------------------------- button
class AnimatedButton(QPushButton):
    """Flat button with an eased hover/press colour change. variant: 'secondary' | 'primary'."""

    def __init__(self, text: str, variant: str = "secondary", parent=None):
        super().__init__(text, parent)
        self.variant = variant
        self._t = 0.0
        self._press = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(_ease())
        self._anim.valueChanged.connect(self._set_t)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(36)
        self.setFocusPolicy(Qt.StrongFocus)

    def _set_t(self, v):
        self._t = float(v)
        self.update()

    def _go(self, target):
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(target)
        self._anim.start()

    def enterEvent(self, e):
        self._go(1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._go(0.0)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        self._press = 1.0
        self.update()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._press = 0.0
        self.update()
        super().mouseReleaseEvent(e)

    def sizeHint(self):
        w = self.fontMetrics().horizontalAdvance(self.text()) + 40
        return QSize(max(w, 72), 36)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        on = self.isEnabled()
        if self.variant == "primary":
            base, hov, txt = theme.TEXT, "#ffffff", theme.BG                  # off white fill, dark text
            border = None
            if not on:
                base, hov, txt = "#2a2a2d", "#2a2a2d", theme.MUTED
        else:
            base, hov, txt = theme.RAISED, "#2e2e31", theme.TEXT
            border = QColor(237, 237, 237, 20)
            if self.isChecked():
                base, hov, txt = "#2e2e31", "#38383c", theme.ACCENT
            if not on:
                base, hov, txt = theme.CARD, theme.CARD, theme.MUTED
        col = mix(base, hov, self._t if on else 0.0)
        if self._press and on:
            col = col.darker(115)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(r, 8, 8)
        if border is not None:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(border, 1))
            p.drawRoundedRect(r, 8, 8)
        if self.hasFocus() and self.focusPolicy() != Qt.NoFocus and getattr(self, "_kbd_focus", False):
            p.setPen(QPen(QColor(theme.ACCENT), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(-1, -1, 1, 1), 9, 9)
        f = QFont(self.font())
        f.setWeight(QFont.Medium)
        p.setFont(f)
        p.setPen(QColor(txt))
        p.drawText(self.rect(), Qt.AlignCenter, self.text())

    def focusInEvent(self, e):
        self._kbd_focus = e.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason, Qt.ShortcutFocusReason)
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self._kbd_focus = False
        super().focusOutEvent(e)


# --------------------------------------------------------- status dot
class StatusDot(QWidget):
    """Painted dot. When `active` an eased ring expands from it once per second (live data arriving)."""

    def __init__(self, color: str = theme.MUTED, size: int = 16, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._color = QColor(color)
        self._active = False
        self._blink = False
        self._phase = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(1600)
        self._anim.setLoopCount(-1)
        self._anim.setEasingCurve(_ease())
        self._anim.valueChanged.connect(self._tick)

    def _tick(self, v):
        self._phase = float(v)
        self.update()

    def set_state(self, color: str, active: bool = False, blink: bool = False):
        self._color = QColor(color)
        self._blink = blink
        want = active or blink
        if want and self._anim.state() != QVariantAnimation.Running:
            self._anim.start()
        elif not want and self._anim.state() == QVariantAnimation.Running:
            self._anim.stop()
            self._phase = 0.0
        self._active = active
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        core = self.width() * 0.25
        if self._active:                                  # ring expands and fades
            ring = QColor(self._color)
            ring.setAlphaF(0.45 * (1 - self._phase))
            p.setPen(QPen(ring, 1.5))
            p.setBrush(Qt.NoBrush)
            rad = core + (self.width() / 2 - core) * self._phase
            p.drawEllipse(c, rad, rad)
        col = QColor(self._color)
        if self._blink:                                   # connecting: dot breathes between 35 % and 100 %
            col.setAlphaF(0.35 + 0.65 * (0.5 + 0.5 * math.cos(self._phase * 2 * math.pi)))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(c, core, core)


class DotLabel(QWidget):
    """A small static dot followed by text, for quiet status lines."""

    def __init__(self, text: str = "", color: str = theme.MUTED, text_color: str | None = None, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.dot = StatusDot(color, 12)
        self.label = QLabel(text)
        lay.addWidget(self.dot)
        lay.addWidget(self.label)
        self.set(text, color, text_color)

    def set(self, text: str, color: str, text_color: str | None = None):
        self.label.setText(text)
        self.dot.set_state(color)
        self.label.setStyleSheet(f"color:{text_color};" if text_color else "")


# --------------------------------------------------------- trigger lamp
class TriggerLamp(QWidget):
    """Signature element: the scope's trigger lamp. Idle ring = armed, solid = capturing, fades after a hit."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self._level = 0.0          # 0 = dark ring, 1 = lit
        self._capturing = False
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(900)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._set)

    def _set(self, v):
        self._level = float(v)
        self.update()

    def set_capturing(self, on: bool):
        if on == self._capturing:
            return
        self._capturing = on
        if on:
            self._anim.stop()
            self._level = 1.0
        else:
            self._level = 1.0
            self._anim.start()                      # fade out after the capture completes
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QPointF(8, 8)
        ring = mix(theme.MUTED, theme.ACCENT, self._level)
        p.setPen(QPen(ring, 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, 6, 6)
        if self._level > 0.02:
            fill = QColor(theme.ACCENT)
            fill.setAlphaF(self._level)
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawEllipse(c, 3, 3)


def spacer() -> QWidget:
    w = QWidget()
    w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return w
