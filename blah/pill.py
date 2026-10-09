"""The floating pill at the bottom of the screen.

It appears only while Blah is listening or working, never takes focus, and lets
clicks pass straight through to whatever is underneath.
"""

import math

import objc
from AppKit import (
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakeRect,
    NSMouseInRect,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)

BARS = 11
HEIGHT = 34
BAR_WIDTH = 3.0
BAR_GAP = 3.5
BOTTOM_MARGIN = 28
ACCENT = (0.40, 0.47, 1.0)     # Blah blue, lightened to read on the dark pill
RECORD_RED = (1.0, 0.33, 0.30)


def _rgb(rgb, alpha=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


class PillView(NSView):

    def initWithFrame_(self, frame):
        self = objc.super(PillView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.mode = "hidden"          # recording | handsfree | processing | notice
        self.levels = [0.0] * BARS
        self.phase = 0.0
        self.message = ""
        return self

    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        bounds = self.bounds()
        width, height = bounds.size.width, bounds.size.height
        radius = height / 2

        body = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0.5, 0.5, width - 1, height - 1), radius - 0.5, radius - 0.5)
        NSColor.colorWithCalibratedWhite_alpha_(0.07, 0.94).setFill()
        body.fill()
        NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.14).setStroke()
        body.setLineWidth_(1.0)
        body.stroke()

        if self.mode in ("recording", "handsfree"):
            self._draw_record_marker(height)
            self._draw_bars(width, height, start_x=30)
        elif self.mode == "processing":
            self._draw_thinking(width, height)
        elif self.mode == "notice":
            self._draw_message(width, height)

    @objc.python_method
    def _draw_record_marker(self, height):
        cy = height / 2
        pulse = 0.75 + 0.25 * math.sin(self.phase * 4)
        _rgb(RECORD_RED, pulse).setFill()
        if self.mode == "handsfree":
            # A stop square says "tap again to finish".
            square = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(12, cy - 4.5, 9, 9), 2, 2)
            square.fill()
        else:
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(12.5, cy - 4, 8, 8)).fill()

    @objc.python_method
    def _draw_bars(self, width, height, start_x):
        available = height - 14
        cy = height / 2
        span = BARS * BAR_WIDTH + (BARS - 1) * BAR_GAP
        x = start_x + (width - start_x - 14 - span) / 2
        NSColor.whiteColor().setFill()
        for i, level in enumerate(self.levels):
            # Taller in the middle, so quiet speech still reads as a voice shape.
            envelope = 0.55 + 0.45 * math.sin(math.pi * (i + 0.5) / BARS)
            h = max(BAR_WIDTH, available * min(1.0, level) * envelope)
            bar = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x, cy - h / 2, BAR_WIDTH, h), BAR_WIDTH / 2, BAR_WIDTH / 2)
            bar.fill()
            x += BAR_WIDTH + BAR_GAP

    @objc.python_method
    def _draw_thinking(self, width, height):
        cy = height / 2
        for i in range(3):
            t = self.phase * 5 - i * 0.7
            lift = max(0.0, math.sin(t)) * 4
            alpha = 0.45 + 0.55 * max(0.0, math.sin(t))
            _rgb(ACCENT, alpha).setFill()
            cx = width / 2 + (i - 1) * 11
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - 3, cy - 3 + lift, 6, 6)).fill()

    @objc.python_method
    def _draw_message(self, width, height):
        text = _message_string(self.message)
        size = text.size()
        text.drawAtPoint_(((width - size.width) / 2, (height - size.height) / 2))


def _message_string(message):
    attributes = {
        NSFontAttributeName: NSFont.systemFontOfSize_weight_(12.5, 0.23),
        NSForegroundColorAttributeName: NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.92),
    }
    return NSAttributedString.alloc().initWithString_attributes_(message, attributes)


class Pill:
    """Owns the panel; app.py calls update() about 30 times a second."""

    def __init__(self):
        frame = NSMakeRect(0, 0, 132, HEIGHT)
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        self.panel.setLevel_(NSStatusWindowLevel)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(True)
        self.panel.setIgnoresMouseEvents_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary)
        self.view = PillView.alloc().initWithFrame_(frame)
        self.panel.setContentView_(self.view)
        self.visible = False
        self._width = 132

    def _place(self, width):
        mouse = NSEvent.mouseLocation()
        screen = NSScreen.mainScreen()
        for candidate in NSScreen.screens():
            if NSMouseInRect(mouse, candidate.frame(), False):
                screen = candidate
                break
        area = screen.visibleFrame()
        x = area.origin.x + (area.size.width - width) / 2
        y = area.origin.y + BOTTOM_MARGIN
        self.panel.setFrame_display_(NSMakeRect(round(x), round(y), width, HEIGHT), True)

    def update(self, mode, levels=None, message="", dt=1 / 30):
        if mode == "hidden":
            if self.visible:
                self.panel.orderOut_(None)
                self.visible = False
            self.view.mode = "hidden"
            return

        if mode == "notice":
            width = max(132, int(_message_string(message).size().width) + 36)
        elif mode == "processing":
            width = 84
        else:
            width = 132

        if not self.visible or width != self._width or mode != self.view.mode:
            self._width = width
            self._place(width)

        view = self.view
        view.mode = mode
        view.message = message
        view.phase += dt
        if levels is not None:
            view.levels = list(levels)
        view.setNeedsDisplay_(True)

        if not self.visible:
            self.panel.orderFrontRegardless()
            self.visible = True
