"""
MPRIS2 D-Bus integration for TIDAL Bitperfect.

Exposes the player on the session bus so desktop environments, media keys,
KDE Connect, playerctl, and similar tools can see and control playback.

Requires the optional ``dbus-fast`` package.
"""

import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional

from PySide6 import QtCore

try:
    from dbus_fast.aio import MessageBus
    from dbus_fast.service import ServiceInterface, method, dbus_property, signal, PropertyAccess
    from dbus_fast import Variant, BusType
    DBUS_AVAILABLE = True
except Exception:
    DBUS_AVAILABLE = False

logger = logging.getLogger(__name__)

_BUS_NAME = "org.mpris.MediaPlayer2.tidal_bitperfect"
_OBJECT_PATH = "/org/mpris/MediaPlayer2"


# ---------------------------------------------------------------------------
# D-Bus interface implementations (only defined when dbus-fast is available)
# ---------------------------------------------------------------------------

if DBUS_AVAILABLE:

    class _MediaPlayer2(ServiceInterface):
        """``org.mpris.MediaPlayer2`` — application-level identity."""

        def __init__(self, service: "MprisService"):
            super().__init__("org.mpris.MediaPlayer2")
            self._svc = service

        # -- methods --

        @method()
        def Raise(self):
            self._svc.raise_requested.emit()

        @method()
        def Quit(self):
            self._svc.quit_requested.emit()

        # -- properties --

        @dbus_property(access=PropertyAccess.READ)
        def CanQuit(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanRaise(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def HasTrackList(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Identity(self) -> "s":
            return "TIDAL Bitperfect"

        @dbus_property(access=PropertyAccess.READ)
        def DesktopEntry(self) -> "s":
            return "tidal-bitperfect"

        @dbus_property(access=PropertyAccess.READ)
        def SupportedUriSchemes(self) -> "as":
            return ["tidal"]

        @dbus_property(access=PropertyAccess.READ)
        def SupportedMimeTypes(self) -> "as":
            return []

    class _MediaPlayer2Player(ServiceInterface):
        """``org.mpris.MediaPlayer2.Player`` — playback control & metadata."""

        def __init__(self, service: "MprisService"):
            super().__init__("org.mpris.MediaPlayer2.Player")
            self._svc = service

        # -- methods --

        @method()
        def Next(self):
            self._svc.next_requested.emit()

        @method()
        def Previous(self):
            self._svc.previous_requested.emit()

        @method()
        def Pause(self):
            self._svc.pause_requested.emit()

        @method()
        def PlayPause(self):
            self._svc.play_pause_requested.emit()

        @method()
        def Stop(self):
            self._svc.stop_requested.emit()

        @method()
        def Play(self):
            self._svc.play_requested.emit()

        @method()
        def Seek(self, offset: "x"):
            self._svc.seek_requested.emit(offset)

        @method()
        def SetPosition(self, track_id: "o", position: "x"):
            self._svc.set_position_requested.emit(position)

        @method()
        def OpenUri(self, uri: "s"):
            self._svc.open_uri_requested.emit(uri)

        # -- signals --

        @signal()
        def Seeked(self) -> "x":
            return self._svc._position_us

        # -- properties --

        @dbus_property(access=PropertyAccess.READ)
        def PlaybackStatus(self) -> "s":
            return self._svc._playback_status

        @dbus_property(access=PropertyAccess.READ)
        def LoopStatus(self) -> "s":
            return "None"

        @dbus_property(access=PropertyAccess.READ)
        def Rate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def Shuffle(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Metadata(self) -> "a{sv}":
            return self._svc._metadata

        @dbus_property(access=PropertyAccess.READWRITE)
        def Volume(self) -> "d":
            return self._svc._volume

        @Volume.setter
        def Volume(self, value: "d"):
            self._svc._volume = max(0.0, min(1.0, value))
            self._svc.volume_requested.emit(self._svc._volume)

        @dbus_property(access=PropertyAccess.READ)
        def Position(self) -> "x":
            return self._svc._position_us

        @dbus_property(access=PropertyAccess.READ)
        def MinimumRate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MaximumRate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def CanGoNext(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanGoPrevious(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def CanPlay(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanPause(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanSeek(self) -> "b":
            return self._svc._can_seek

        @dbus_property(access=PropertyAccess.READ)
        def CanControl(self) -> "b":
            return True


# ---------------------------------------------------------------------------
# Public Qt wrapper
# ---------------------------------------------------------------------------

class MprisService(QtCore.QObject):
    """
    MPRIS2 service exposed on the D-Bus session bus.

    The app calls ``update_track``, ``update_position``, ``set_playing``,
    ``stop``, and ``set_volume`` to push state.  Control requests from
    external clients arrive as Qt signals.
    """

    # Signals emitted when an external MPRIS client requests an action
    play_requested = QtCore.Signal()
    pause_requested = QtCore.Signal()
    play_pause_requested = QtCore.Signal()
    stop_requested = QtCore.Signal()
    next_requested = QtCore.Signal()
    previous_requested = QtCore.Signal()
    seek_requested = QtCore.Signal(int)          # offset in microseconds
    set_position_requested = QtCore.Signal(int)  # absolute position in µs
    volume_requested = QtCore.Signal(float)      # 0.0 – 1.0
    open_uri_requested = QtCore.Signal(str)
    raise_requested = QtCore.Signal()
    quit_requested = QtCore.Signal()

    # Status signals (mirror Discord pattern)
    status_message = QtCore.Signal(str)
    error_message = QtCore.Signal(str)

    def __init__(self, parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        if not DBUS_AVAILABLE:
            raise RuntimeError("dbus-fast is not installed")

        # Internal state (read by the D-Bus property implementations)
        self._playback_status: str = "Stopped"
        self._metadata: Dict[str, Variant] = {}
        self._position_us: int = 0
        self._volume: float = 1.0
        self._can_seek: bool = True

        # asyncio loop running in a daemon thread
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._bus: Optional[MessageBus] = None
        self._mp2: Optional[_MediaPlayer2] = None
        self._player: Optional[_MediaPlayer2Player] = None

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Start the D-Bus service in a background thread.  Returns True on success."""
        if self._thread is not None:
            return True
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()
        self._start_ok = False

        def _run():
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._async_start())
                self._start_ok = True
            except Exception as exc:
                logger.error("MPRIS D-Bus start failed: %s", exc)
                self._start_ok = False
            finally:
                ready.set()
            if self._start_ok:
                self._loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True, name="mpris-dbus")
        self._thread.start()
        ready.wait(timeout=5)

        if self._start_ok:
            self.status_message.emit("MPRIS D-Bus service started")
        else:
            self.error_message.emit("Failed to start MPRIS D-Bus service")
            self._thread = None
        return self._start_ok

    async def _async_start(self):
        self._bus = await MessageBus(bus_type=BusType.SESSION).connect()
        self._mp2 = _MediaPlayer2(self)
        self._player = _MediaPlayer2Player(self)
        self._bus.export(_OBJECT_PATH, self._mp2)
        self._bus.export(_OBJECT_PATH, self._player)
        await self._bus.request_name(_BUS_NAME)
        logger.info("MPRIS service registered as %s", _BUS_NAME)

    def shutdown(self):
        """Disconnect from the bus and stop the background loop."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._bus is not None:
            try:
                self._bus.disconnect()
            except Exception:
                pass
            self._bus = None
        self._mp2 = None
        self._player = None
        self._loop = None
        self.status_message.emit("MPRIS D-Bus service stopped")

    # ---- state updates (called by the app) --------------------------------

    def update_track(self, track: Optional[Dict[str, Any]],
                     quality_info: Optional[Dict[str, Any]] = None) -> None:
        """Push new track metadata (or ``None`` to clear)."""
        if track is None:
            self._metadata = {}
            self._playback_status = "Stopped"
            self._position_us = 0
        else:
            tid = str(track.get("id", "0"))
            meta: Dict[str, Variant] = {
                "mpris:trackid": Variant("o", f"/org/tidal/track/{tid}"),
                "xesam:title": Variant("s", track.get("title", "")),
                "xesam:artist": Variant("as", [track.get("artist", "")]),
                "xesam:album": Variant("s", track.get("album", "")),
            }
            cover_url = track.get("cover_url")
            if cover_url:
                meta["mpris:artUrl"] = Variant("s", cover_url)
            duration_us = int(track.get("duration", 0) * 1_000_000) if track.get("duration") else 0
            if duration_us:
                meta["mpris:length"] = Variant("x", duration_us)
            self._metadata = meta
            self._playback_status = "Playing"

        self._emit_properties_changed()

    def update_position(self, position_s: float, duration_s: float) -> None:
        """Update current playback position."""
        self._position_us = int(position_s * 1_000_000)
        # Also keep duration in sync
        if duration_s > 0 and "mpris:length" not in self._metadata:
            self._metadata["mpris:length"] = Variant("x", int(duration_s * 1_000_000))

    def set_playing(self, is_playing: bool) -> None:
        """Set play/pause state."""
        new_status = "Playing" if is_playing else "Paused"
        if new_status != self._playback_status:
            self._playback_status = new_status
            self._emit_properties_changed()

    def stop(self) -> None:
        """Indicate playback stopped."""
        self._playback_status = "Stopped"
        self._metadata = {}
        self._position_us = 0
        self._emit_properties_changed()

    def set_volume(self, fraction: float) -> None:
        """Push volume (0.0–1.0) from the app to MPRIS."""
        self._volume = max(0.0, min(1.0, fraction))

    # ---- internal helpers -------------------------------------------------

    def _emit_properties_changed(self) -> None:
        """Emit ``PropertiesChanged`` on the Player interface."""
        if self._player is None or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(
                self._player.emit_properties_changed,
                {
                    "PlaybackStatus": self._playback_status,
                    "Metadata": self._metadata,
                },
            )
        except Exception:
            pass
