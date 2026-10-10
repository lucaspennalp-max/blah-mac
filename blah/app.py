"""Blah: hold a key, talk, and clean text appears wherever your cursor is.

This module runs the app: the menu-bar icon, the hotkey, the microphone, and the
glue between the engine (core.py), the pill (pill.py) and the window (hub.py).
Everything that touches AppKit runs on the main thread; slow work (Whisper,
cleanup) runs on a worker thread and reports back through plain attributes that
a 30 fps timer reads.
"""

import collections
import math
import fcntl
import os
import platform
import subprocess
import sys
import threading
import time

import numpy as np
import objc
import Quartz
import sounddevice as sd
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSStatusBar,
    NSTimer,
    NSVariableStatusItemLength,
    NSWorkspace,
)
from Foundation import NSURL, NSObject

from . import __version__, core
from .core import (
    CONFIG_PATH, HOTKEYS, KEYCODE_ESCAPE, LANGUAGES, LOCK_PATH, MIN_AUDIO_SECONDS,
    SAMPLE_RATE, SHOW_REQUEST_PATH, SILENCE_RMS, TAP_SECONDS, Cleaner, Transcriber, log,
)
from .hub import Hub
from .pill import BARS, Pill

TICK = 1 / 30
STATUS_SYMBOLS = {
    "idle": "waveform",
    "loading": "hourglass",
    "recording": "record.circle",
    "processing": "ellipsis.circle",
    "error": "exclamationmark.triangle",
}


BUILTIN_HINTS = ("macbook", "built-in", "builtin", "imac", "internal")


def refresh_audio_devices():
    """PortAudio only reads the device list once; re-read it so new or removed mics show up."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception as exc:  # noqa: BLE001
        log(f"Couldn't refresh audio devices: {exc}")


def input_devices():
    """[(index, name, default_samplerate)] for every device that can record."""
    devices = []
    try:
        for index, info in enumerate(sd.query_devices()):
            if info.get("max_input_channels", 0) > 0:
                devices.append((index, info["name"], int(info.get("default_samplerate") or SAMPLE_RATE)))
    except Exception as exc:  # noqa: BLE001
        log(f"Couldn't list microphones: {exc}")
    return devices


def default_input_index():
    try:
        index = sd.default.device[0]
        return index if index is not None and index >= 0 else sd.query_devices(kind="input")["index"]
    except Exception:  # noqa: BLE001
        return None


def to_16k(audio, rate):
    """Resample mono float audio to 16 kHz for Whisper."""
    if rate == SAMPLE_RATE or len(audio) == 0:
        return audio.astype(np.float32)
    if rate % SAMPLE_RATE == 0:                      # 48 kHz, 32 kHz…: average groups (also filters)
        step = rate // SAMPLE_RATE
        usable = len(audio) - len(audio) % step
        return audio[:usable].reshape(-1, step).mean(axis=1).astype(np.float32)
    n_out = int(len(audio) * SAMPLE_RATE / rate)
    return np.interp(np.arange(n_out) * (rate / SAMPLE_RATE), np.arange(len(audio)), audio).astype(np.float32)


class BlahController(NSObject):

    # ------------------------------------------------------------------ setup

    def initWithBackground_(self, background):
        self = objc.super(BlahController, self).init()
        if self is None:
            return None
        self.start_hidden = background
        self.cfg = core.load_config()
        self.transcriber = Transcriber(self.cfg)
        self.cleaner = Cleaner(self.cfg)
        self.model_ready = threading.Event()

        self.state = "idle"             # idle | recording | processing
        self.status_text = "Loading the speech model"
        self.fatal = None
        self.notice = ""
        self.notice_until = 0.0
        self.error_until = 0.0
        self.last_text = ""

        self._stream = None
        self._frames = []
        self._level = 0.0
        self._bars = collections.deque([0.0] * BARS, maxlen=BARS)
        self._hotkey_down = False
        self._pressed_at = 0.0
        self._hands_free = False
        self._ignore_release = False
        self._tap = None
        self._tap_source = None
        self._tap_trusted = False
        self._tap_retry_at = 0.0
        self._next_permission_check = 0.0
        self._events = collections.deque()   # (kind, data) for the window, filled from any thread
        self._last_show_check = 0.0
        self._apply_hotkey()
        return self

    def applicationDidFinishLaunching_(self, _notification):
        self._set_dock_icon()
        self._install_main_menu()
        self._install_status_item()
        self.pill = Pill()
        self.hub = Hub(self.handle_request)
        if core.microphone_status() == "unknown":
            core.request_microphone()           # macOS shows its microphone prompt now, not mid-dictation
        if core.input_monitoring_granted() is False:
            core.request_input_monitoring()
        self._install_event_tap()
        threading.Thread(target=self._load_model, daemon=True).start()

        self._timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            TICK, self, "tick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self._timer, NSRunLoopCommonModes)

        if not self.start_hidden:
            self.hub.show()

    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _visible):
        self.hub.show()
        return True

    def applicationShouldTerminateAfterLastWindowClosed_(self, _app):
        return False

    @objc.python_method
    def _apply_hotkey(self):
        keycode, mask, label, glyph = HOTKEYS[self.cfg["hotkey"]]
        self._hot_keycode, self._hot_mask, self._hot_label = keycode, mask, label

    @objc.python_method
    def _set_dock_icon(self):
        """Show the Blah logo in the Dock and ⌘Tab (otherwise macOS shows Python's icon)."""
        here = os.path.dirname(__file__)
        candidates = [os.path.join(core.app_bundle_path(), "Contents/Resources/Blah.icns"),
                      os.path.join(here, "ui", "logo.png")]
        for path in candidates:
            if path and os.path.exists(path):
                image = NSImage.alloc().initWithContentsOfFile_(path)
                if image is not None:
                    NSApp.setApplicationIconImage_(image)
                    return

    @objc.python_method
    def _install_main_menu(self):
        """Gives the window working ⌘C / ⌘V / ⌘A / ⌘W even though Blah has no Dock icon."""
        main = NSMenu.alloc().init()
        app_item = NSMenuItem.alloc().init()
        main.addItem_(app_item)
        app_menu = NSMenu.alloc().init()
        open_item = app_menu.addItemWithTitle_action_keyEquivalent_("Open Blah", "openHub:", "o")
        open_item.setTarget_(self)
        app_menu.addItem_(NSMenuItem.separatorItem())
        app_menu.addItemWithTitle_action_keyEquivalent_("Hide Blah", "hide:", "h")
        app_menu.addItemWithTitle_action_keyEquivalent_("Close Window", "performClose:", "w")
        app_menu.addItemWithTitle_action_keyEquivalent_("Quit Blah", "terminate:", "q")
        app_item.setSubmenu_(app_menu)

        edit_item = NSMenuItem.alloc().init()
        main.addItem_(edit_item)
        edit = NSMenu.alloc().initWithTitle_("Edit")
        for title, action, key in (("Undo", "undo:", "z"), ("Redo", "redo:", "Z"),
                                   ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                                   ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")):
            edit.addItemWithTitle_action_keyEquivalent_(title, action, key)
        edit_item.setSubmenu_(edit)
        NSApp.setMainMenu_(main)

    @objc.python_method
    def _load_mouths(self):
        """The menu-bar mouth frames (template images, so macOS tints them for the menu bar)."""
        folder = os.path.join(os.path.dirname(__file__), "ui", "menubar")
        mouths = []
        for i in range(4):
            path = os.path.join(folder, f"mouth-{i}.png")
            retina = os.path.join(folder, f"mouth-{i}@2x.png")
            if not os.path.exists(path):
                return []
            image = NSImage.alloc().initWithContentsOfFile_(path)
            if os.path.exists(retina):
                rep = NSImage.alloc().initWithContentsOfFile_(retina).representations()[0]
                rep.setSize_((18, 18))
                image.addRepresentation_(rep)
            image.setSize_((18, 18))
            image.setTemplate_(True)
            image.setAccessibilityDescription_("Blah")
            mouths.append(image)
        return mouths

    @objc.python_method
    def _install_status_item(self):
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self._symbols = {}
        for state, name in STATUS_SYMBOLS.items():
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "Blah")
            if image is not None:
                image.setTemplate_(True)
            self._symbols[state] = image
        self._mouths = self._load_mouths()
        self._shown_symbol = None
        self._mouth_level = 0.0
        self.status_item.button().setToolTip_("Blah")

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self.menu_status = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(self.status_text, None, "")
        self.menu_status.setEnabled_(False)
        menu.addItem_(self.menu_status)
        menu.addItem_(NSMenuItem.separatorItem())
        for title, action, key in (("Open Blah", "openHub:", "o"),
                                   ("Paste last dictation again", "pasteLast:", ""),
                                   ("Copy last dictation", "copyLast:", "")):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
            item.setTarget_(self)
            menu.addItem_(item)
        self.menu_cleanup = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Clean up text", "toggleCleanup:", "")
        self.menu_cleanup.setTarget_(self)
        menu.addItem_(self.menu_cleanup)
        menu.addItem_(NSMenuItem.separatorItem())
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit Blah", "terminate:", "q")
        menu.addItem_(quit_item)
        self.status_item.setMenu_(menu)
        self._refresh_menu()

    @objc.python_method
    def _refresh_menu(self):
        self.menu_cleanup.setState_(1 if self.cfg.get("cleanup") else 0)

    # ------------------------------------------------------------------ hotkey

    @objc.python_method
    def _install_event_tap(self):
        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
                | Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown))

        def callback(proxy, event_type, event, refcon):
            return self._on_event(event_type, event)

        self._remove_event_tap()
        self._tap_callback = callback  # keep a reference so it isn't garbage collected
        trusted = core.input_monitoring_granted()
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,  # Blah only observes keys, never blocks them
            mask, callback, None)
        if tap is None:
            self._tap = None
            self._tap_retry_at = time.monotonic() + 2
            return False
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetMain(), source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        self._tap, self._tap_source = tap, source
        # Without Input Monitoring, macOS still creates the listener but only passes it keys
        # while Blah is the front app. Remember that, so it's rebuilt once permission arrives.
        self._tap_trusted = trusted is not False
        log("Hotkey listener ready." if self._tap_trusted
            else "Hotkey listener limited to Blah's own window until Input Monitoring is allowed.")
        return True

    @objc.python_method
    def _remove_event_tap(self):
        tap, source = getattr(self, "_tap", None), getattr(self, "_tap_source", None)
        if tap is not None:
            Quartz.CGEventTapEnable(tap, False)
            if source is not None:
                Quartz.CFRunLoopRemoveSource(Quartz.CFRunLoopGetMain(), source, Quartz.kCFRunLoopCommonModes)
            Quartz.CFMachPortInvalidate(tap)
        self._tap, self._tap_source = None, None

    @objc.python_method
    def _on_event(self, event_type, event):
        try:
            if event_type in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
                Quartz.CGEventTapEnable(self._tap, True)
                return event
            keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)

            if event_type == Quartz.kCGEventFlagsChanged:
                if keycode == self._hot_keycode:
                    is_down = bool(Quartz.CGEventGetFlags(event) & self._hot_mask)
                    stamp = Quartz.CGEventGetTimestamp(event) / 1e9   # when it happened, in seconds
                    if is_down and not self._hotkey_down:
                        self._hotkey_down = True
                        self._event_pressed_at = stamp
                        self._hotkey_pressed()
                    elif not is_down and self._hotkey_down:
                        self._hotkey_down = False
                        self._event_held_for = stamp - getattr(self, "_event_pressed_at", stamp)
                        self._hotkey_released()
                elif self._hotkey_down and self.state == "recording" and not self._hands_free:
                    self._cancel("")  # another modifier joined: it's a shortcut, not dictation

            elif event_type == Quartz.kCGEventKeyDown and self.state == "recording":
                if keycode == KEYCODE_ESCAPE:
                    self._cancel("Cancelled")
                elif self._hotkey_down and not self._hands_free:
                    self._cancel("")  # e.g. Option+E for an accent: let the person type
        except Exception as exc:  # noqa: BLE001
            log(f"Key handler error: {exc}")
        return event

    @objc.python_method
    def _hotkey_pressed(self):
        if self.state == "recording":
            if self._hands_free:            # second tap finishes hands-free mode
                self._ignore_release = True
                self._finish()
            return
        if self.state == "processing" or self.fatal:
            return
        self._ignore_release = False
        self._pressed_at = time.monotonic()
        self._start_recording()

    @objc.python_method
    def _hotkey_released(self):
        if self._ignore_release:
            self._ignore_release = False
            return
        if self.state != "recording" or self._hands_free:
            return
        held = getattr(self, "_event_held_for", None)
        if held is None or held <= 0:
            held = time.monotonic() - self._pressed_at
        if held < TAP_SECONDS:
            self._hands_free = True
            self._set_status(f"Hands-free: tap {self._hot_label} to finish")
            return
        self._finish()

    # ------------------------------------------------------------------ audio

    @objc.python_method
    def _audio_callback(self, indata, frames, time_info, status):
        self._frames.append(indata.copy())
        rms = float(np.sqrt(np.mean(indata[:, 0] ** 2)))
        self._level = max(self._level * 0.6, rms)

    @objc.python_method
    def _start_recording(self):
        core.play_sound(self.cfg, "Tink")
        self._frames = []
        self._level = 0.0
        devices = input_devices()
        chosen = self.cfg.get("input_device")
        index = next((i for i, name, _ in devices if name == chosen), None) if chosen else None
        if index is None:
            index = default_input_index()
        name, rate = next(((n, r) for i, n, r in devices if i == index), ("default", SAMPLE_RATE))
        self._rec_device, self._rec_rate, self._rec_devices = name, rate, devices
        try:
            self._stream = sd.InputStream(device=index, samplerate=rate, channels=1, dtype="float32",
                                          blocksize=max(256, rate // 32), callback=self._audio_callback)
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            self._stream = None
            self._flag_error(f"Couldn't open the microphone ({name}). Check Settings.", detail=exc)
            return
        self.state = "recording"
        self._set_status("Listening")

    @objc.python_method
    def _stop_stream(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        frames, self._frames = self._frames, []
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return to_16k(np.concatenate(frames)[:, 0], getattr(self, "_rec_rate", SAMPLE_RATE))

    @objc.python_method
    def _cancel(self, message):
        self._stop_stream()
        self._hands_free = False
        self.state = "idle"
        self._set_status("Ready")
        if message:
            self._notify(message)
        core.play_sound(self.cfg, "Funk")

    @objc.python_method
    def _finish(self):
        audio = self._stop_stream()
        self._hands_free = False
        self.state = "processing"
        core.play_sound(self.cfg, "Pop")
        target_app = core.frontmost_app_name()
        threading.Thread(target=self._process, args=(audio, target_app), daemon=True).start()

    @objc.python_method
    def _process(self, audio, app_name):
        try:
            seconds = len(audio) / SAMPLE_RATE
            if seconds >= MIN_AUDIO_SECONDS and not np.any(audio):
                # Perfect digital silence: a blocked or silent microphone, never a quiet room.
                status = core.microphone_status()
                device = getattr(self, "_rec_device", "default")
                inputs = [n for _, n, _ in getattr(self, "_rec_devices", [])]
                log(f"Microphone gave only silence (device: {device}; macOS permission: {status}; "
                    f"microphones found: {', '.join(inputs) or 'none'}).")
                builtin = next((n for n in inputs if any(h in n.lower() for h in BUILTIN_HINTS)), None)
                if status == "granted" and builtin and builtin != device:
                    # Recording from a silent device (a virtual one from a meeting app, or one
                    # that disconnected). Use the Mac's own microphone from now on.
                    self.cfg["input_device"] = builtin
                    core.save_config(self.cfg)
                    self._events.append(("config", core.public_config(self.cfg)))
                    self._flag_error(f"{device} was silent. Switched to {builtin}, try again.")
                    self._refresh_devices_soon = True
                elif status != "denied" and status != "granted" and core.reset_microphone_permission():
                    self._flag_error("Fixing microphone access. Blah will restart and ask for it.")
                    self._restart_soon = True
                else:
                    self._flag_error(f"No sound from {device}. Pick another microphone in Settings.")
                    self._refresh_devices_soon = True
                self._events.append(("permissions", self._permissions()))
                return
            if seconds < MIN_AUDIO_SECONDS or float(np.sqrt(np.mean(audio ** 2))) < SILENCE_RMS:
                self._notify("Didn't catch that")
                return

            if not self.model_ready.is_set():
                self._set_status("Finishing loading the speech model")
            self.model_ready.wait()
            if self.transcriber.error:
                self._flag_error("The speech model didn't load. See Settings.")
                return

            self._set_status("Transcribing")
            started = time.monotonic()
            raw = self.transcriber.transcribe(audio)
            t_transcribed = time.monotonic()
            if not raw:
                self._notify("Didn't catch that")
                return

            text, cleaned, shortcuts = raw, False, []
            snippets = self.cfg.get("snippets") or []
            whole = core.match_whole_snippet(snippets, raw)
            if whole is not None:
                # The whole dictation is a shortcut phrase: type its saved text exactly.
                text, shortcuts = whole["text"], [whole["trigger"]]
            else:
                if self.cleaner.enabled():
                    self._set_status("Cleaning up")
                    try:
                        text, cleaned = self.cleaner.clean(raw, app_name), True
                    except Exception as exc:  # noqa: BLE001
                        log(f"Cleanup failed, pasting the raw transcript: {exc}")
                        self._notify("Cleanup failed, pasted raw text")
                text, shortcuts = core.expand_snippets(snippets, text)

            self.last_text = text
            self._last_dictation = time.monotonic()
            elapsed = time.monotonic() - started
            if core.accessibility_granted() is False:
                # macOS won't let Blah paste yet: keep the words on the clipboard instead.
                core.set_clipboard_text(text)
                self._set_status("Copied. Allow Accessibility so Blah can paste for you.")
                self._notify("Copied, press ⌘V to paste", seconds=3)
                self._events.append(("permissions", self._permissions()))
            else:
                core.paste_text(text, restore_clipboard=bool(self.cfg.get("restore_clipboard", True)))
                self._set_status(f"Pasted {len(text.split())} words in {elapsed:.1f}s")
            log(f"Dictation: {seconds:.1f}s of audio, speech-to-text {t_transcribed - started:.2f}s, "
                f"cleanup {'%.2fs' % (elapsed - (t_transcribed - started)) if cleaned else 'off'}, "
                f"total {elapsed:.2f}s ({self.transcriber.description})")

            entry = {"ts": time.time(), "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "app": app_name, "seconds": round(seconds, 1), "raw": raw,
                     "text": text, "cleaned": cleaned, "shortcuts": shortcuts}
            if self.cfg.get("save_history"):
                entry = core.append_history(entry)
            else:
                entry["id"] = "unsaved-" + str(time.time())
            self._events.append(("dictation", entry))
        except Exception as exc:  # noqa: BLE001
            self._flag_error("Something went wrong. Try again.", detail=exc)
        finally:
            self.state = "idle"

    @objc.python_method
    def _load_model(self):
        try:
            self.transcriber.load(progress=lambda f: self._set_status(
                f"Downloading the speech model… {int(f * 100)}%"))
            self._set_status("Ready")
        except Exception as exc:  # noqa: BLE001
            self.transcriber.error = str(exc)
            self.fatal = "The speech model didn't load"
            log(f"Speech model failed to load: {exc}")
        finally:
            self.model_ready.set()
            self._events.append(("engine", self._engine_info()))
            if (not self.transcriber.error and self.transcriber.engine == "mlx"
                    and self.cfg.get("mlx_model") != self.transcriber.repo):
                self._switch_model(self.cfg["mlx_model"])

    # ------------------------------------------------------------------ status

    @objc.python_method
    def _set_status(self, text):
        self.status_text = text
        self._events.append(("status", self._status_info()))

    @objc.python_method
    def _notify(self, message, seconds=1.8):
        self.notice = message
        self.notice_until = time.monotonic() + seconds

    @objc.python_method
    def _flag_error(self, message, detail=None):
        log(f"{message} {detail or ''}")
        self._set_status(message)
        self.error_until = time.monotonic() + 4
        self._notify(message, seconds=3)
        core.play_sound(self.cfg, "Basso")

    @objc.python_method
    def _status_info(self):
        return {"state": self.state, "text": self.fatal or self.status_text,
                "loading": not self.model_ready.is_set(), "fatal": self.fatal,
                "switching": getattr(self, "switching", None)}

    @objc.python_method
    def _engine_info(self):
        return {"ready": self.model_ready.is_set() and not self.transcriber.error,
                "model": self.transcriber.description, "error": self.transcriber.error,
                "repo": self.transcriber.repo, "engine": self.transcriber.engine,
                "machine": platform.machine()}

    # ------------------------------------------------------------------ main-thread tick

    def tick_(self, _timer):
        now = time.monotonic()

        # Waveform: push the latest microphone level into the scrolling bars.
        if self.state == "recording":
            self._bars.append(min(1.0, (self._level * 22) ** 0.7))
            self._level *= 0.82
            self.pill.update("handsfree" if self._hands_free else "recording", self._bars, dt=TICK)
        elif self.state == "processing":
            self.pill.update("processing", dt=TICK)
        elif now < self.notice_until and self.notice:
            self.pill.update("notice", message=self.notice, dt=TICK)
        else:
            self.pill.update("hidden")
            if self._bars[-1]:
                self._bars.extend([0.0] * BARS)

        # Menu-bar icon: a small mouth that's closed while Blah waits, moves with your voice
        # while it listens, and dims while it loads or works.
        button = self.status_item.button()
        if self.fatal or now < self.error_until:
            symbol, alpha = "error", 1.0
        elif self.state == "recording" and self._mouths:
            self._mouth_level = max(self._mouth_level * 0.75, self._bars[-1] if self._bars else 0.0)
            frame = 1 + min(2, int(self._mouth_level * 3.2))     # frames 1..3, louder = wider
            symbol, alpha = f"mouth-{frame}", 1.0
        elif self.state == "processing":
            symbol, alpha = ("mouth-1" if self._mouths else "processing"), 0.55 + 0.45 * abs(math.sin(now * 3))
        elif not self.model_ready.is_set():
            symbol, alpha = ("mouth-0" if self._mouths else "loading"), 0.45
        else:
            symbol, alpha = ("mouth-0" if self._mouths else "idle"), 1.0
        if symbol != self._shown_symbol:
            if symbol.startswith("mouth-"):
                image = self._mouths[int(symbol[-1])]
            else:
                image = self._symbols.get(symbol)
            if image is not None:
                button.setImage_(image)
                button.setTitle_("")
            else:
                button.setTitle_("Blah")
            self._shown_symbol = symbol
        if abs(button.alphaValue() - alpha) > 0.02:
            button.setAlphaValue_(alpha)
        text = self.fatal or self.status_text
        if self.menu_status.title() != text:
            self.menu_status.setTitle_(text)

        # Forward queued events to the window.
        while self._events:
            kind, data = self._events.popleft()
            self.hub.send(kind, data)

        # Retry the hotkey listener until it exists, and rebuild it once Input Monitoring is
        # granted so it hears the shortcut in every app, not just Blah's window.
        if self._tap is None and now >= self._tap_retry_at:
            self._install_event_tap()
        if now >= self._next_permission_check:
            self._next_permission_check = now + 2
            if not self._tap_trusted and core.input_monitoring_granted():
                self._install_event_tap()
            permissions = self._permissions()
            if permissions != getattr(self, "_last_permissions", None):
                self._last_permissions = permissions
                self.hub.send("permissions", permissions)

        if self.state == "idle" and (getattr(self, "_refresh_devices_soon", False)
                                     or now - getattr(self, "_devices_refreshed", 0.0) > 30):
            self._refresh_devices_soon = False
            self._devices_refreshed = now
            refresh_audio_devices()

        # After a few idle minutes macOS moves the model out of fast memory, which made the next
        # dictation take 8 s instead of 2 s. While you're actively using Blah, keep it warm.
        last_used = getattr(self, "_last_dictation", None)
        if (last_used is not None and self.state == "idle" and self.model_ready.is_set()
                and not getattr(self, "switching", None) and now - last_used < 3600 and now - getattr(self, "_last_warm", 0.0) > 240):
            self._last_warm = now
            threading.Thread(target=self.transcriber.keep_warm, daemon=True).start()

        if getattr(self, "_restart_soon", False) and self.state == "idle":
            self._restart_soon = False
            self._restart()
            return

        # Opening Blah.app while it's already running shows the window.
        if now - self._last_show_check > 1.0:
            self._last_show_check = now
            if SHOW_REQUEST_PATH.exists():
                SHOW_REQUEST_PATH.unlink(missing_ok=True)
                self.hub.show()

    # ------------------------------------------------------------------ menu actions

    def openHub_(self, _sender):
        self.hub.show()

    def copyLast_(self, _sender):
        if self.last_text:
            core.set_clipboard_text(self.last_text)

    def pasteLast_(self, _sender):
        if self.last_text:
            text = self.last_text
            threading.Thread(target=lambda: core.paste_text(text, self.cfg.get("restore_clipboard", True)),
                             daemon=True).start()

    def toggleCleanup_(self, _sender):
        self._update_settings({"cleanup": not self.cfg.get("cleanup")})
        self.hub.send("config", core.public_config(self.cfg))

    # ------------------------------------------------------------------ window requests

    @objc.python_method
    def _input_status(self):
        granted = core.input_monitoring_granted()
        if granted is None:                       # very old macOS: no way to ask, trust the listener
            return "granted" if self._tap is not None else "missing"
        return "granted" if granted else "missing"

    @objc.python_method
    def _permissions(self):
        return {
            "microphone": core.microphone_status(),
            "input": self._input_status(),
            "accessibility": {True: "granted", False: "missing", None: "unknown"}[core.accessibility_granted()],
        }

    @objc.python_method
    def _snapshot(self):
        history = core.load_history()
        return {
            "config": core.public_config(self.cfg),
            "hotkeys": [{"id": k, "label": v[2], "glyph": v[3]} for k, v in HOTKEYS.items()],
            "microphones": [name for _, name, _ in input_devices()],
            "speech_models": [{"id": repo, "label": label, "note": note}
                              for repo, label, note in core.SPEECH_MODELS.values()],
            "languages": [{"id": code, "label": label} for code, label in LANGUAGES],
            "history": history,
            "stats": core.history_stats(history),
            "status": self._status_info(),
            "engine": self._engine_info(),
            "permissions": self._permissions(),
            "launch_at_login": core.launch_at_login_enabled(),
            "can_launch_at_login": bool(core.app_bundle_path()),
            "version": __version__,
        }

    @objc.python_method
    def _model_label(self, repo):
        return next((label for r, label, _ in core.SPEECH_MODELS.values() if r == repo), "new")

    @objc.python_method
    def _switch_model(self, repo):
        """Download and switch to another speech model while the current one keeps working."""
        if self.transcriber.engine != "mlx":
            return                         # Intel Macs use a different engine; speed options don't apply
        label = self._model_label(repo)
        self.switching = label

        def current():
            return self.transcriber._wanted == repo     # a newer choice replaces this one

        def progress(fraction):
            if current():
                self._set_status(f"Downloading the {label} model… {int(fraction * 100)}%")

        def run():
            try:
                self.transcriber.switch_to(repo, progress=progress)
                if current() and self.transcriber.repo == repo:
                    self.switching = None
                    self.transcriber.error = None       # a working model fixes an earlier failed load
                    self.fatal = None
                    self._set_status(f"Ready · {label}")
                    self._notify(f"Switched to {label}")
            except Exception as exc:  # noqa: BLE001
                log(f"Couldn't switch to {repo}: {exc}")
                if current():
                    self.switching = None
                    working = self.transcriber.repo
                    if working:                             # stay on the model that works
                        self.cfg["mlx_model"] = working
                        core.save_config(self.cfg)
                        self._events.append(("config", core.public_config(self.cfg)))
                    self._flag_error(f"Couldn't download the {label} model. Check your internet connection.")
            finally:
                self._events.append(("engine", self._engine_info()))
                self._events.append(("status", self._status_info()))

        threading.Thread(target=run, daemon=True).start()

    @objc.python_method
    def _update_settings(self, changes):
        for key, value in changes.items():
            check = core.EDITABLE.get(key)
            if check is None or not check(value):
                raise ValueError(f"Invalid setting: {key}")
        model_changed = "mlx_model" in changes and changes["mlx_model"] != self.cfg.get("mlx_model")
        self.cfg.update(changes)
        core.save_config(self.cfg)
        self.cleaner.reset()
        if model_changed and self.model_ready.is_set():
            self._switch_model(changes["mlx_model"])
        self._apply_hotkey()
        self._refresh_menu()

    @objc.python_method
    def handle_request(self, request):
        cmd = request.get("cmd")
        args = request.get("args") or {}

        if cmd in ("ready", "refresh"):
            return self._snapshot()
        if cmd == "set":
            self._update_settings(args)
            return core.public_config(self.cfg)
        if cmd == "setApiKey":
            key = str(args.get("key", "")).strip()
            if key and not key.startswith("sk-"):
                raise ValueError("That doesn't look like an Anthropic API key. Keys start with sk-.")
            self.cfg["anthropic_api_key"] = key
            core.save_config(self.cfg)
            self.cleaner.reset()
            return core.public_config(self.cfg)
        if cmd == "copy":
            core.set_clipboard_text(str(args.get("text", "")))
            return True
        if cmd == "deleteEntry":
            core.delete_history(str(args["id"]))
            return True
        if cmd == "clearHistory":
            core.delete_history(None)
            return True
        if cmd == "requestPermission":
            which = args.get("which")
            if which == "microphone" and core.microphone_status() == "unknown":
                core.request_microphone()
            elif which == "input":
                core.request_input_monitoring()
                self._open_url(core.PERMISSION_PANES["input"])
            elif which == "accessibility":
                core.request_accessibility()
                self._open_url(core.PERMISSION_PANES["accessibility"])
            else:
                self._open_url(core.PERMISSION_PANES.get(which, core.PERMISSION_PANES["microphone"]))
            return True
        if cmd == "permissions":
            if self._tap is None or (not self._tap_trusted and core.input_monitoring_granted()):
                self._install_event_tap()
            return self._permissions()
        if cmd == "setLaunchAtLogin":
            if not core.set_launch_at_login(bool(args.get("on"))):
                raise ValueError("Launch at login works once Blah is installed with install.sh.")
            return core.launch_at_login_enabled()
        if cmd == "openSettingsFile":
            subprocess.Popen(["open", "-t", str(CONFIG_PATH)])
            return True
        if cmd == "restart":
            self._restart()
            return True
        raise ValueError(f"Unknown command: {cmd}")

    @objc.python_method
    def _open_url(self, url):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))

    @objc.python_method
    def _restart(self):
        bundle = core.app_bundle_path()
        if bundle:
            subprocess.Popen(["/bin/sh", "-c", 'sleep 1; open -a "$1"', "sh", bundle])
        else:
            subprocess.Popen([sys.executable, "-m", "blah"], cwd=os.path.dirname(os.path.dirname(__file__)),
                             start_new_session=True)
        NSApp.terminate_(None)


def _acquire_single_instance_lock():
    core.DATA_DIR.mkdir(mode=0o700, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return handle


def main():
    if platform.system() != "Darwin":
        sys.exit("Blah runs on macOS only.")

    background = "--background" in sys.argv
    lock = _acquire_single_instance_lock()
    if lock is None:
        # Already running: ask that copy to show its window, then quit.
        if not background:
            SHOW_REQUEST_PATH.touch()
        return

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    controller = BlahController.alloc().initWithBackground_(background)
    app.setDelegate_(controller)
    app.run()
    del lock
