"""The Blah window: a native window hosting the HTML interface in ui/index.html.

The page talks to Python through one WebKit message handler named "blah":
    page -> Python   window.webkit.messageHandlers.blah.postMessage(JSON string)
    Python -> page   window.blah.receive({...})
"""

import base64
import json
from pathlib import Path

import objc
from AppKit import (
    NSApp,
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSBackingStoreBuffered,
    NSColor,
    NSMakeRect,
    NSMakeSize,
    NSViewHeightSizable,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWindowTitleHidden,
)
from Foundation import NSObject
from WebKit import WKWebView, WKWebViewConfiguration

from .core import log

UI_FILE = Path(__file__).parent / "ui" / "index.html"

try:
    _HANDLER_PROTOCOLS = [objc.protocolNamed("WKScriptMessageHandler")]
except objc.ProtocolError:  # very old PyObjC: conformance isn't checked at runtime anyway
    _HANDLER_PROTOCOLS = []


def _page_html():
    """The page, with the logo inlined (the page is loaded from a string, so it can't fetch files)."""
    html = UI_FILE.read_text(encoding="utf-8")
    logo = UI_FILE.parent / "logo.png"
    if logo.exists():
        data = base64.b64encode(logo.read_bytes()).decode("ascii")
        html = html.replace("{{LOGO}}", "data:image/png;base64," + data)
    return html


class BridgeHandler(NSObject, protocols=_HANDLER_PROTOCOLS):

    def initWithCallback_(self, callback):
        self = objc.super(BridgeHandler, self).init()
        if self is None:
            return None
        self.callback = callback
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            frame = message.frameInfo()
            if frame is not None and not frame.isMainFrame():
                return                      # only Blah's own page may talk to Blah
        except Exception:  # noqa: BLE001
            return
        try:
            request = json.loads(str(message.body()))
        except (TypeError, ValueError):
            return
        self.callback(request)


def _window_background():
    """Matches the page background so the title bar strip blends in, in light and dark mode."""
    def provider(appearance):
        dark = appearance.bestMatchFromAppearancesWithNames_(
            [NSAppearanceNameAqua, NSAppearanceNameDarkAqua]) == NSAppearanceNameDarkAqua
        if dark:
            return NSColor.colorWithSRGBRed_green_blue_alpha_(0x16 / 255, 0x18 / 255, 0x1B / 255, 1)
        return NSColor.colorWithSRGBRed_green_blue_alpha_(0xF4 / 255, 0xF5 / 255, 0xF2 / 255, 1)
    try:
        return NSColor.colorWithName_dynamicProvider_("BlahBackground", provider)
    except Exception:  # noqa: BLE001
        return NSColor.windowBackgroundColor()


class Hub:

    def __init__(self, on_request):
        """on_request(request_dict) -> result; called on the main thread."""
        self._on_request = on_request
        self.window = None
        self.web = None
        self.ready = False

    def _build(self):
        config = WKWebViewConfiguration.alloc().init()
        self._handler = BridgeHandler.alloc().initWithCallback_(self._handle)
        config.userContentController().addScriptMessageHandler_name_(self._handler, "blah")

        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 940, 640), style, NSBackingStoreBuffered, False)
        window.setTitle_("Blah")
        window.setTitlebarAppearsTransparent_(True)
        window.setTitleVisibility_(NSWindowTitleHidden)
        window.setBackgroundColor_(_window_background())
        window.setReleasedWhenClosed_(False)
        window.setMinSize_(NSMakeSize(720, 500))
        window.center()
        window.setFrameAutosaveName_("BlahHub")

        web = WKWebView.alloc().initWithFrame_configuration_(window.contentView().bounds(), config)
        web.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        try:
            web.setValue_forKey_(False, "drawsBackground")  # no white flash in dark mode
        except Exception:  # noqa: BLE001
            pass
        # Nothing can be dropped onto the window, so it never leaves Blah's own page
        # (the page itself contains no links, and its security policy blocks all loading).
        web.unregisterDraggedTypes()
        window.contentView().addSubview_(web)
        web.loadHTMLString_baseURL_(_page_html(), None)

        self.window, self.web = window, web

    def show(self, page=None):
        if self.window is None:
            self._build()
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        if page and self.ready:
            self.send("navigate", {"page": page})

    def is_visible(self):
        return bool(self.window is not None and self.window.isVisible())

    def send(self, kind, data):
        if not self.ready or self.web is None:
            return
        payload = json.dumps({"type": kind, "data": data}, ensure_ascii=False)
        self.web.evaluateJavaScript_completionHandler_(
            f"window.blah && window.blah.receive({payload})", None)

    def _handle(self, request):
        request_id = request.get("id")
        try:
            result = self._on_request(request)
            reply = {"id": request_id, "ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001
            log(f"Window request {request.get('cmd')!r} failed: {exc}")
            reply = {"id": request_id, "ok": False, "error": str(exc)}
        if request.get("cmd") == "ready":
            self.ready = True
        if request_id is not None and self.web is not None:
            payload = json.dumps(reply, ensure_ascii=False)
            self.web.evaluateJavaScript_completionHandler_(
                f"window.blah && window.blah.reply({payload})", None)
