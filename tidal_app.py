#!/usr/bin/env python3

import html
import sys
import os
import shutil
import urllib.request
import traceback
import socket
from bisect import bisect_right
from typing import Callable, Optional, List, Dict, Any

import alsaaudio
import tidalapi
from PySide6 import QtCore, QtGui, QtWidgets, QtNetwork

import tidal_core
from tidal_playback import AudioFormat, StreamInfo, CacheManager, PlaybackWorker, PrefetchWorker, DownloadWorker, tag_flac_path
from tidal_discord import DiscordRPC, DEFAULT_CLIENT_ID, PYPRESENCE_AVAILABLE
from tidal_mpris import MprisService, DBUS_AVAILABLE


SEARCH_LOADERS = {
    "track": tidal_core.search_tracks,
    "album": tidal_core.search_albums,
    "playlist": tidal_core.search_playlists,
    "artist": tidal_core.search_artists,
}

COLLECTION_LOADERS = {
    "track": tidal_core.list_favorite_tracks,
    "album": tidal_core.list_favorite_albums,
    "playlist": tidal_core.list_favorite_playlists,
    "artist": tidal_core.list_favorite_artists,
}

FAVORITE_SETTERS = {
    "track": tidal_core.set_track_favorite,
    "album": tidal_core.set_album_favorite,
    "playlist": tidal_core.set_playlist_favorite,
    "artist": tidal_core.set_artist_favorite,
}


class CoverImageWidget(QtWidgets.QWidget):
    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self._pixmap: Optional[QtGui.QPixmap] = None
        self._fallback: Optional[QtGui.QPixmap] = None
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding
        )

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(1, 1)

    def minimumSizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(1, 1)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        return max(1, w)

    def set_bytes(self, data: Optional[bytes]) -> None:
        if not data:
            self._pixmap = None
            self.update()
            return
        pix = QtGui.QPixmap()
        if not pix.loadFromData(data):
            self._pixmap = None
        else:
            self._pixmap = pix
        self.update()

    def set_fallback_pixmap(self, pixmap: Optional[QtGui.QPixmap]) -> None:
        self._fallback = pixmap
        self.update()

    def paintEvent(self, event) -> None:
        pixmap = self._pixmap
        if pixmap is None or pixmap.isNull():
            pixmap = self._fallback
        if pixmap is None or pixmap.isNull():
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        target = self.rect()
        side = min(target.width(), target.height())
        if side <= 0:
            return
        x0 = target.x() + (target.width() - side) // 2
        y0 = target.y() + (target.height() - side) // 2
        target = QtCore.QRect(x0, y0, side, side)
        scaled = pixmap.scaled(
            target.size(),
            QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        x = target.x() + (target.width() - scaled.width()) // 2
        y = target.y() + (target.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)


class MarqueeLabel(QtWidgets.QLabel):
    def __init__(self, text: str = "", parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(text, parent)
        self._offset = 0.0
        self._speed_px = 30.0
        self._gap_px = 40
        self._pause_s = 0.8
        self._pause_remaining = 0.0
        self._baseline_offset = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed
        )
        self.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.NoTextInteraction)
        self.setWordWrap(False)

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(1, self.fontMetrics().height())

    def minimumSizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(1, self.fontMetrics().height())

    def setText(self, text: str) -> None:
        super().setText(text)
        self._reset_scroll()

    def set_baseline_offset(self, px: int) -> None:
        self._baseline_offset = px
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reset_scroll()

    def _reset_scroll(self) -> None:
        self._offset = 0.0
        self._pause_remaining = self._pause_s
        if self._needs_scroll():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _needs_scroll(self) -> bool:
        fm = self.fontMetrics()
        return fm.horizontalAdvance(self.text()) > max(1, self.width())

    def _tick(self) -> None:
        if not self._needs_scroll():
            self._timer.stop()
            return
        if self._pause_remaining > 0:
            self._pause_remaining -= self._timer.interval() / 1000.0
            return
        fm = self.fontMetrics()
        text_w = fm.horizontalAdvance(self.text())
        step = self._speed_px * (self._timer.interval() / 1000.0)
        self._offset += step
        total = text_w + self._gap_px
        if self._offset >= total:
            self._offset = 0.0
            self._pause_remaining = self._pause_s
        self.update()

    def paintEvent(self, event) -> None:
        painter = QtGui.QPainter(self)
        painter.setFont(self.font())
        fm = self.fontMetrics()
        text = self.text()
        h = self.height()
        y = (h - fm.height()) // 2 + fm.ascent() + self._baseline_offset
        if not self._needs_scroll():
            elided = fm.elidedText(text, QtCore.Qt.TextElideMode.ElideRight, self.width())
            painter.drawText(0, y, elided)
            return
        text_w = fm.horizontalAdvance(text)
        x = int(-self._offset)
        painter.drawText(x, y, text)
        painter.drawText(x + text_w + self._gap_px, y, text)


def list_playback_devices() -> List[str]:
    devs = set(alsaaudio.pcms(alsaaudio.PCM_PLAYBACK))
    # Add hw/plughw variants for each ALSA card id when available.
    try:
        for entry in sorted(os.listdir("/proc/asound")):
            if not entry.startswith("card"):
                continue
            card_id_path = f"/proc/asound/{entry}/id"
            try:
                with open(card_id_path, "r", encoding="utf-8") as f:
                    card_id = f.read().strip()
                if card_id:
                    devs.add(f"hw:CARD={card_id},DEV=0")
                    devs.add(f"plughw:CARD={card_id},DEV=0")
                    devs.add(f"sysdefault:CARD={card_id}")
            except Exception:
                continue
    except Exception:
        pass
    devs.add("null")
    return sorted(devs)


class LoginWorker(QtCore.QThread):
    message = QtCore.Signal(str)
    login_link = QtCore.Signal(str, str, int)  # url, code, expires_s
    ready = QtCore.Signal(object)  # tidalapi.Session
    error = QtCore.Signal(str)

    def run(self) -> None:
        try:
            quality = tidal_core.pick_quality()
            config = tidalapi.Config(quality=quality) if quality is not None else tidalapi.Config()
            session = tidalapi.Session(config)
            if tidal_core.load_saved_oauth(session):
                self.ready.emit(session)
                return

            login, future = session.login_oauth()
            self.login_link.emit(
                login.verification_uri_complete, login.user_code, int(login.expires_in)
            )
            self.message.emit(
                "TIDAL login: open this link and authorize:\n"
                f"  {login.verification_uri_complete}\n"
                f"Code: {login.user_code} (expires in {int(login.expires_in)}s)"
            )

            future.result()
            if not session.check_login():
                raise RuntimeError("tidal login failed")
            tidal_core.save_oauth(session)
            self.ready.emit(session)
        except Exception as e:
            self.error.emit(tidal_core.safe_str(e))


class CallWorker(QtCore.QThread):
    ready = QtCore.Signal(object)
    error = QtCore.Signal(str)

    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self.ready.emit(self._fn())
        except Exception as e:
            self.error.emit(tidal_core.safe_str(e))


class KeyedCallWorker(QtCore.QThread):
    ready = QtCore.Signal(str, object)
    error = QtCore.Signal(str)

    def __init__(self, key: str, fn: Callable[[], Any]):
        super().__init__()
        self._key = key
        self._fn = fn

    def run(self) -> None:
        try:
            self.ready.emit(self._key, self._fn())
        except Exception as e:
            self.error.emit(tidal_core.safe_str(e))


class TracksWorker(QtCore.QThread):
    ready = QtCore.Signal(object)  # Dict result
    error = QtCore.Signal(str)

    def __init__(
        self,
        session: tidalapi.Session,
        mode: str,
        text: str,
        limit: int,
        search_type: str,
    ):
        super().__init__()
        self._session = session
        self._mode = mode
        self._text = text
        self._limit = limit
        self._search_type = search_type

    def run(self) -> None:
        try:
            if self._mode == "search":
                items = SEARCH_LOADERS[self._search_type](
                    self._session,
                    self._text,
                    limit=self._limit,
                )
                self.ready.emit({"type": self._search_type, "items": items})
                return
            if self._mode == "url":
                result = tidal_core.link_to_result(self._session, self._text)
                self.ready.emit(result)
                return
            raise ValueError(f"unknown mode: {self._mode}")
        except Exception as e:
            self.error.emit(tidal_core.safe_str(e))


class FavoriteToggleWorker(QtCore.QThread):
    ready = QtCore.Signal(str, str, bool)  # item_type, item_id, favorite
    error = QtCore.Signal(str)

    def __init__(self, session: tidalapi.Session, item_type: str, item_id: str, favorite: bool):
        super().__init__()
        self._session = session
        self._item_type = item_type
        self._item_id = item_id
        self._favorite = favorite

    def run(self) -> None:
        try:
            FAVORITE_SETTERS[self._item_type](self._session, self._item_id, self._favorite)
            self.ready.emit(self._item_type, self._item_id, self._favorite)
        except Exception as e:
            self.error.emit(tidal_core.safe_str(e))

def _download_cover(url: str) -> Optional[bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = resp.read()
        return data if data else None
    except Exception:
        return None


def _shrink_cover_bytes(data: bytes, max_px: int = 1280) -> bytes:
    if not data:
        return data
    img = QtGui.QImage()
    if not img.loadFromData(data):
        return data
    w = img.width()
    h = img.height()
    if w <= 0 or h <= 0:
        return data
    if max(w, h) <= max_px:
        return data
    scaled = img.scaled(
        max_px,
        max_px,
        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation,
    )
    buf = QtCore.QBuffer()
    buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    fmt = "JPEG"
    quality = 92
    ok = scaled.save(buf, fmt, quality)
    if not ok:
        return data
    out = bytes(buf.data())
    return out if out else data


def _fetch_cover_bytes(track) -> Optional[bytes]:
    album = getattr(track, "album", None)
    if album is None:
        return None
    for dim in ("origin",):
        try:
            url = album.image(dim)
        except Exception:
            continue
        data = _download_cover(url)
        if data:
            return data
    return None


class _CoverLoadStopped(Exception):
    pass


def _load_cover_data(
    session: tidalapi.Session,
    track_id: str,
    cover_url: Optional[str],
    *,
    log: Optional[Callable[[str], None]] = None,
    url_log: str,
    session_log: str,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Optional[bytes]:
    def ensure_running() -> None:
        if stop_check is not None and stop_check():
            raise _CoverLoadStopped()

    ensure_running()
    if cover_url:
        if log is not None:
            log(url_log.format(track_id=track_id))
        data = _download_cover(cover_url)
        ensure_running()
        return _shrink_cover_bytes(data) if data else None

    track = session.track(track_id)
    ensure_running()
    if log is not None:
        log(session_log.format(track_id=track_id))
    data = _fetch_cover_bytes(track) if track is not None else None
    ensure_running()
    return _shrink_cover_bytes(data) if data else None


class CoverWorker(QtCore.QThread):
    ready = QtCore.Signal(str, object)  # track_id, Optional[bytes]
    log = QtCore.Signal(str)

    def __init__(self, session: tidalapi.Session, track_id: str, cover_url: Optional[str]):
        super().__init__()
        self._session = session
        self._track_id = track_id
        self._cover_url = cover_url
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            data = _load_cover_data(
                self._session,
                self._track_id,
                self._cover_url,
                log=self.log.emit,
                url_log="cover: download url for track={track_id}",
                session_log="cover: fetch via session for track={track_id}",
                stop_check=lambda: self._stop,
            )
            self.ready.emit(self._track_id, data)
        except _CoverLoadStopped:
            return
        except Exception:
            if not self._stop:
                self.log.emit(f"cover: error {traceback.format_exc().strip()}")
                self.ready.emit(self._track_id, None)


class CoverPrefetchWorker(QtCore.QThread):
    ready = QtCore.Signal(str, object, object)  # track_id, cover_url, Optional[bytes]
    log = QtCore.Signal(str)

    def __init__(self, session: tidalapi.Session, items: List[tuple[str, Optional[str]]]):
        super().__init__()
        self._session = session
        self._items = items
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        local_cache: Dict[str, Optional[bytes]] = {}
        for track_id, cover_url in self._items:
            if self._stop:
                return
            try:
                if cover_url and cover_url in local_cache:
                    data = local_cache[cover_url]
                else:
                    data = _load_cover_data(
                        self._session,
                        track_id,
                        cover_url,
                        log=self.log.emit,
                        url_log="cover: prefetch url for track={track_id}",
                        session_log="cover: prefetch via session for track={track_id}",
                        stop_check=lambda: self._stop,
                    )
                    if cover_url:
                        local_cache[cover_url] = data
                self.ready.emit(track_id, cover_url, data)
            except _CoverLoadStopped:
                return
            except Exception:
                if not self._stop:
                    self.log.emit(f"cover: prefetch error {traceback.format_exc().strip()}")
                    self.ready.emit(track_id, None, None)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TIDAL Bitperfect (ALSA)")

        self._session: Optional[tidalapi.Session] = None
        self._search_tracks: List[Dict[str, Any]] = []
        self._url_tracks: List[Dict[str, Any]] = []
        self._track_map_all: Dict[str, Dict[str, Any]] = {}
        self._play_worker: Optional[PlaybackWorker] = None
        self._play_had_error = False
        self._stopped_by_user = False
        self._pending_play: Optional[tuple[str, str]] = None
        self._current_play: Optional[tuple[str, str]] = None  # (track_id, alsa_device)
        self._lyrics_worker: Optional[CallWorker] = None
        self._lyrics_request_id: Optional[str] = None
        self._lyrics_cache: Dict[str, Dict[str, Any]] = {}
        self._lyrics_body_text: str = ""
        self._lyrics_rtl = False
        self._lyrics_muted = False
        self._lyrics_timed_lines: List[Dict[str, Any]] = []
        self._lyrics_line_starts: List[float] = []
        self._lyrics_active_line: Optional[int] = None
        self._settings = QtCore.QSettings()
        cache_dir = os.path.expanduser("~/.cache/tidal-bitperfect")
        self._cache_dir = cache_dir
        self._cache = CacheManager(cache_dir, max_bytes=1024 * 1024 * 1024)
        self._cache_max_gb = 1
        self._cache_full_notified = False
        self._cache_disabled = False
        self._creds_disabled = False
        self._stream_info: Optional[StreamInfo] = None
        self._audio_fmt: Optional[AudioFormat] = None
        self._decode_path: Optional[str] = None
        self._disable_ffmpeg = False
        self._debug_enabled = False
        self._duration_s: float = 0.0
        self._pos_s: float = 0.0
        self._seeking = False
        self._pending_seek_target_s: Optional[float] = None
        self._cover_bytes: Optional[bytes] = None
        self._cover_worker: Optional[CoverWorker] = None
        self._cover_request_id: Optional[str] = None
        self._cover_request_url: Optional[str] = None
        self._prefetch_worker: Optional[CoverPrefetchWorker] = None
        self._cover_cache: Dict[str, bytes] = {}
        self._cover_url_cache: Dict[str, bytes] = {}
        self._cover_prefetch_max = 10
        self._last_tracks_mode: Optional[str] = None
        self._download_worker: Optional[DownloadWorker] = None
        self._radio_worker: Optional[CallWorker] = None
        self._artist_radio_worker: Optional[CallWorker] = None
        self._radio_mode: str = "play"
        self._home_worker: Optional[CallWorker] = None
        self._home_loading_placeholder: Optional[QtWidgets.QTreeWidgetItem] = None
        self._mix_tracks_workers: Dict[str, KeyedCallWorker] = {}
        self._mix_items: Dict[str, List[QtWidgets.QTreeWidgetItem]] = {}
        self._playlist_tracks_workers: Dict[str, KeyedCallWorker] = {}
        self._playlist_home_items: Dict[str, List[QtWidgets.QTreeWidgetItem]] = {}
        self._home_tracks: List[Dict[str, Any]] = []
        self._collection_worker: Optional[KeyedCallWorker] = None
        self._favorite_toggle_worker: Optional[FavoriteToggleWorker] = None
        self._favorite_tracks: List[Dict[str, Any]] = []
        self._favorite_ids: set[str] = set()
        self._favorite_album_ids: set[str] = set()
        self._favorite_playlist_ids: set[str] = set()
        self._favorite_artist_ids: set[str] = set()
        self._cache_tracks: List[Dict[str, Any]] = []
        self._download_tracks: List[Dict[str, Any]] = []
        self._artist_detail_workers: Dict[str, KeyedCallWorker] = {}
        self._now_playing_track: Optional[Dict[str, Any]] = None
        self._album_tracks_workers: Dict[str, KeyedCallWorker] = {}
        self._artist_items: Dict[str, List[QtWidgets.QTreeWidgetItem]] = {}
        self._album_items: Dict[str, List[QtWidgets.QTreeWidgetItem]] = {}
        self._orphaned_workers: List[QtCore.QThread] = []
        self._loading_items: List[QtWidgets.QTreeWidgetItem] = []
        self._loading_phase = 0
        self._pending_seek_timer = QtCore.QTimer(self)
        self._pending_seek_timer.setSingleShot(True)
        self._pending_seek_timer.timeout.connect(self._commit_pending_seek)
        self._loading_timer = QtCore.QTimer(self)
        self._loading_timer.setInterval(300)
        self._loading_timer.timeout.connect(self._tick_loading_labels)
        self._offline_mode = False
        self._discord_rpc: Optional[DiscordRPC] = None
        self._discord_enabled = False
        self._discord_available = PYPRESENCE_AVAILABLE
        self._discord_cb: Optional[QtWidgets.QCheckBox] = None
        self._discord_id_label: Optional[QtWidgets.QLabel] = None
        self._discord_id_edit: Optional[QtWidgets.QLineEdit] = None
        self._discord_help_label: Optional[QtWidgets.QLabel] = None
        self._mpris_service: Optional[MprisService] = None
        self._mpris_enabled = False
        self._mpris_available = DBUS_AVAILABLE
        self._mpris_cb: Optional[QtWidgets.QCheckBox] = None
        self._mpris_help_label: Optional[QtWidgets.QLabel] = None
        self._gapless_enabled = True
        self._gapless_cb: Optional[QtWidgets.QCheckBox] = None
        self._audio_prefetch_worker: Optional["PrefetchWorker"] = None
        self._prefetch_track_id: Optional[str] = None
        self._closing = False
        self.lyrics_title_label: Optional[QtWidgets.QLabel] = None
        self.lyrics_meta_label: Optional[QtWidgets.QLabel] = None
        self.lyrics_view: Optional[QtWidgets.QTextBrowser] = None

        self._build_ui()
        self._start_login()

    def _new_browse_tree(self) -> QtWidgets.QTreeWidget:
        tree = QtWidgets.QTreeWidget()
        tree.setHeaderHidden(True)
        tree.itemActivated.connect(self._on_tree_item_activated)
        tree.itemExpanded.connect(self._on_tree_item_expanded)
        tree.currentItemChanged.connect(self._on_selection_changed)
        tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(
            lambda pos, tree=tree: self._show_tree_context_menu(tree, pos)
        )
        return tree

    def _new_track_list(self) -> QtWidgets.QListWidget:
        widget = QtWidgets.QListWidget()
        widget.itemActivated.connect(self._play_selected)
        widget.currentItemChanged.connect(self._on_selection_changed)
        widget.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        widget.customContextMenuRequested.connect(
            lambda pos, widget=widget: self._show_track_context_menu(widget, pos)
        )
        return widget

    def _new_media_type_combo(self) -> QtWidgets.QComboBox:
        combo = QtWidgets.QComboBox()
        combo.setMinimumWidth(110)
        combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow
        )
        combo.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed
        )
        combo.addItems(["Tracks", "Albums", "Playlists", "Artists"])
        return combo

    def _new_stretch_status_label(self) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel("")
        label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed
        )
        return label

    def _set_now_playing_context_menu(self, widget: QtWidgets.QWidget) -> None:
        widget.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        widget.customContextMenuRequested.connect(self._show_now_playing_context_menu)

    def _lyrics_header(self, track_id: Optional[str]) -> tuple[str, str]:
        if not track_id:
            return "Lyrics", ""
        track = self._track_map_all.get(str(track_id)) or {}
        title = track.get("title") or f"Track {track_id}"
        artist = track.get("artist") or ""
        return str(title), str(artist)

    def _normalize_timed_lyrics(self, timed_lines: object) -> List[Dict[str, Any]]:
        if not isinstance(timed_lines, list):
            return []
        entries: List[Dict[str, Any]] = []
        for order, entry in enumerate(timed_lines):
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            try:
                start_s = max(0.0, float(entry.get("start_s")))
            except (TypeError, ValueError):
                continue
            end_s = None
            raw_end = entry.get("end_s")
            if raw_end is not None:
                try:
                    candidate = float(raw_end)
                except (TypeError, ValueError):
                    candidate = None
                if candidate is not None and candidate > start_s:
                    end_s = candidate
            entries.append(
                {
                    "start_s": start_s,
                    "end_s": end_s,
                    "text": text,
                    "_order": order,
                }
            )
        entries.sort(key=lambda entry: (entry["start_s"], entry["_order"]))
        normalized: List[Dict[str, Any]] = []
        for index, entry in enumerate(entries):
            end_s = entry["end_s"]
            if end_s is None and index + 1 < len(entries):
                next_start = float(entries[index + 1]["start_s"])
                if next_start > float(entry["start_s"]):
                    end_s = next_start
            normalized.append(
                {
                    "start_s": float(entry["start_s"]),
                    "end_s": float(end_s) if end_s is not None else None,
                    "text": str(entry["text"]),
                }
            )
        return normalized

    def _lyrics_anchor(self, index: int) -> str:
        return f"lyrics-line-{index}"

    def _lyrics_seek_href(self, start_s: float) -> str:
        return f"lyricseek:{int(round(max(0.0, start_s) * 1000.0))}"

    def _on_lyrics_anchor_clicked(self, url: QtCore.QUrl) -> None:
        if url.scheme() != "lyricseek":
            return
        raw_value = (url.path() or url.toString().split(":", 1)[-1]).strip().lstrip("/")
        try:
            target_ms = int(raw_value)
        except (TypeError, ValueError):
            return
        self._seek_to_position(float(target_ms) / 1000.0)
        QtCore.QTimer.singleShot(0, self._clear_lyrics_selection)

    def _lyrics_document_html(self) -> str:
        palette = self.palette()
        text_color = palette.color(QtGui.QPalette.ColorRole.Text)
        meta_color = f"rgba({text_color.red()}, {text_color.green()}, {text_color.blue()}, 180)"
        if self._lyrics_muted:
            body_color = meta_color
            inactive_color = meta_color
            active_color = meta_color
        else:
            body_color = text_color.name()
            active_color = text_color.name()
            if self._lyrics_active_line is None:
                inactive_color = body_color
            else:
                inactive_color = (
                    f"rgba({text_color.red()}, {text_color.green()}, {text_color.blue()}, 145)"
                )
        align = "right" if self._lyrics_rtl else "left"
        direction = "rtl" if self._lyrics_rtl else "ltr"

        if self._lyrics_timed_lines:
            parts: List[str] = []
            for index, line in enumerate(self._lyrics_timed_lines):
                classes = "line active" if index == self._lyrics_active_line else "line"
                line_html = html.escape(line["text"]).replace("\n", "<br>")
                if not line_html:
                    line_html = "&nbsp;"
                line_color = active_color if index == self._lyrics_active_line else inactive_color
                line_link = (
                    f'<a href="{self._lyrics_seek_href(float(line["start_s"]))}"'
                    f' class="line-link" style="color: {line_color};">'
                    f"{line_html}</a>"
                )
                parts.append(f'<a name="{self._lyrics_anchor(index)}"></a>')
                parts.append(f'<div class="{classes}">{line_link}</div>')
            body_html = "".join(parts)
        else:
            plain_html = html.escape(self._lyrics_body_text).replace("\n", "<br>")
            if not plain_html:
                plain_html = "&nbsp;"
            body_html = f'<div class="plain">{plain_html}</div>'

        return (
            "<html><head><style>"
            f"body {{ margin: 0; padding: 0; background: transparent; text-align: {align}; }}"
            f".plain {{ white-space: pre-wrap; color: {body_color}; }}"
            f".line {{ margin: 0 0 0.55em 0; white-space: pre-wrap; color: {inactive_color}; }}"
            f".line.active {{ color: {active_color}; font-weight: 600; }}"
            ".line-link { display: block; text-decoration: none; }"
            "</style></head>"
            f'<body dir="{direction}">{body_html}</body></html>'
        )

    def _render_plain_lyrics(self) -> None:
        if self.lyrics_view is None:
            return
        palette = self.palette()
        text_color = palette.color(QtGui.QPalette.ColorRole.Text)
        body_color = (
            f"rgba({text_color.red()}, {text_color.green()}, {text_color.blue()}, 170)"
            if self._lyrics_muted
            else text_color.name()
        )
        self.lyrics_view.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight
            if self._lyrics_rtl
            else QtCore.Qt.AlignmentFlag.AlignLeft
        )
        self.lyrics_view.setStyleSheet(
            "QTextBrowser#lyricsBody {"
            " border: none;"
            " background: transparent;"
            f" color: {body_color};"
            " selection-background-color: palette(highlight);"
            "}"
        )
        self.lyrics_view.setPlainText(self._lyrics_body_text)

    def _render_lyrics_document(self) -> None:
        if self.lyrics_view is None:
            return
        self.lyrics_view.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight
            if self._lyrics_rtl
            else QtCore.Qt.AlignmentFlag.AlignLeft
        )
        self.lyrics_view.setStyleSheet(
            "QTextBrowser#lyricsBody {"
            " border: none;"
            " background: transparent;"
            " selection-background-color: palette(highlight);"
            "}"
        )
        self.lyrics_view.setHtml(self._lyrics_document_html())

    def _render_lyrics_view(self, *, force_scroll: bool = False) -> None:
        if self.lyrics_view is None:
            return
        if self._lyrics_timed_lines:
            self._sync_lyrics_to_position(self._pos_s, force_render=True, force_scroll=force_scroll)
            return
        self._render_plain_lyrics()
        if force_scroll:
            QtCore.QTimer.singleShot(0, self._scroll_lyrics_to_top)

    def _lyrics_has_selection(self) -> bool:
        if self.lyrics_view is None:
            return False
        return self.lyrics_view.textCursor().hasSelection()

    def _clear_lyrics_selection(self) -> None:
        if self.lyrics_view is None:
            return
        cursor = self.lyrics_view.textCursor()
        if not cursor.hasSelection():
            return
        cursor.clearSelection()
        self.lyrics_view.setTextCursor(cursor)

    def _scroll_lyrics_to_top(self) -> None:
        if self.lyrics_view is None:
            return
        self.lyrics_view.moveCursor(QtGui.QTextCursor.MoveOperation.Start)
        bar = self.lyrics_view.verticalScrollBar()
        if bar is not None:
            bar.setValue(bar.minimum())

    def _scroll_lyrics_to_anchor(self, anchor: str) -> None:
        if self.lyrics_view is None:
            return
        self.lyrics_view.scrollToAnchor(anchor)
        bar = self.lyrics_view.verticalScrollBar()
        if bar is not None:
            bar.setValue(max(bar.minimum(), bar.value() - (bar.pageStep() // 3)))

    def _sync_lyrics_to_position(
        self,
        pos_s: float,
        *,
        force_render: bool = False,
        force_scroll: bool = False,
    ) -> None:
        if self.lyrics_view is None:
            return
        if not self._lyrics_timed_lines:
            if force_render:
                self._render_plain_lyrics()
            if force_scroll:
                QtCore.QTimer.singleShot(0, self._scroll_lyrics_to_top)
            return
        if not force_render and self._lyrics_has_selection():
            return
        index = bisect_right(self._lyrics_line_starts, float(pos_s)) - 1
        active_index = index if index >= 0 else None
        previous_index = self._lyrics_active_line
        if active_index != previous_index or force_render:
            self._lyrics_active_line = active_index
            self._render_lyrics_document()
        if active_index is None:
            if force_scroll and (force_render or previous_index is not None):
                QtCore.QTimer.singleShot(0, self._scroll_lyrics_to_top)
            return
        if active_index != previous_index or force_scroll:
            anchor = self._lyrics_anchor(active_index)
            QtCore.QTimer.singleShot(
                0,
                lambda anchor=anchor: self._scroll_lyrics_to_anchor(anchor),
            )

    def _set_lyrics_content(
        self,
        track_id: Optional[str],
        body: str,
        *,
        provider: Optional[str] = None,
        rtl: bool = False,
        muted: bool = False,
        timed_lines: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if (
            self.lyrics_view is None
            or self.lyrics_title_label is None
            or self.lyrics_meta_label is None
        ):
            return
        title, artist = self._lyrics_header(track_id)
        meta = artist
        palette = self.palette()
        text_color = palette.color(QtGui.QPalette.ColorRole.Text)
        meta_color = f"rgba({text_color.red()}, {text_color.green()}, {text_color.blue()}, 180)"

        self.lyrics_title_label.setText(title)
        self.lyrics_meta_label.setText(meta)
        self.lyrics_meta_label.setVisible(bool(meta))
        self.lyrics_meta_label.setStyleSheet(f"color: {meta_color};")
        self.lyrics_meta_label.setToolTip(f"Lyrics source: {provider}" if provider else "")

        self._lyrics_body_text = body
        self._lyrics_rtl = bool(rtl)
        self._lyrics_muted = bool(muted)
        self._lyrics_timed_lines = self._normalize_timed_lyrics(timed_lines)
        self._lyrics_line_starts = [line["start_s"] for line in self._lyrics_timed_lines]
        self._lyrics_active_line = None

        self.lyrics_view.setLayoutDirection(
            QtCore.Qt.LayoutDirection.RightToLeft
            if rtl
            else QtCore.Qt.LayoutDirection.LeftToRight
        )
        self._render_lyrics_view(force_scroll=True)

    def _fallback_cover_pixmap(self) -> Optional[QtGui.QPixmap]:
        icon_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "packaging",
            "linux",
            "tidal-bitperfect-transparent.svg",
        )
        if os.path.exists(icon_path):
            pixmap = QtGui.QPixmap(icon_path)
            if not pixmap.isNull():
                return pixmap
        icon = QtGui.QIcon.fromTheme("tidal-bitperfect")
        if icon.isNull():
            icon = QtGui.QIcon.fromTheme("audio-x-generic")
        if icon.isNull():
            return None
        pixmap = icon.pixmap(512, 512)
        return pixmap if not pixmap.isNull() else None

    def _build_device_row(self, layout: QtWidgets.QVBoxLayout) -> None:
        device_row = QtWidgets.QHBoxLayout()
        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.setEditable(True)
        # NOTE: Keep device selection persisted across runs, but avoid saving
        # programmatic changes during device list refresh (see _refresh_devices()).
        self.device_combo.currentTextChanged.connect(self._on_device_changed)
        self.device_combo.editTextChanged.connect(self._on_device_changed)
        self.refresh_devices_btn = QtWidgets.QPushButton("Refresh devices")
        self.refresh_devices_btn.clicked.connect(self._refresh_devices)
        device_row.addWidget(QtWidgets.QLabel("ALSA device:"))
        device_row.addWidget(self.device_combo, 1)
        device_row.addWidget(self.refresh_devices_btn)
        layout.addLayout(device_row)

    def _build_home_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Home"))
        top.addStretch(1)
        self.home_refresh_btn = QtWidgets.QPushButton("Refresh")
        self.home_refresh_btn.clicked.connect(self._load_home)
        top.addWidget(self.home_refresh_btn)
        layout.addLayout(top)
        self.home_list = self._new_browse_tree()
        layout.addWidget(self.home_list, 1)
        return tab

    def _build_search_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        top = QtWidgets.QHBoxLayout()
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText('Search, e.g. "aphex twin flim"')
        self.search_edit.returnPressed.connect(self._do_search)
        self.search_type = self._new_media_type_combo()
        self.search_limit = QtWidgets.QSpinBox()
        self.search_limit.setRange(1, 50)
        self.search_limit.setValue(10)
        self.search_limit.valueChanged.connect(self._on_search_limit_changed)
        self.search_btn = QtWidgets.QPushButton("Search")
        self.search_btn.clicked.connect(self._do_search)
        top.addWidget(self.search_edit, 1)
        top.addWidget(QtWidgets.QLabel("Type:"))
        top.addWidget(self.search_type)
        top.addWidget(QtWidgets.QLabel("Limit:"))
        top.addWidget(self.search_limit)
        top.addWidget(self.search_btn)
        layout.addLayout(top)
        self.search_list = self._new_browse_tree()
        layout.addWidget(self.search_list, 1)
        return tab

    def _build_url_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        top = QtWidgets.QHBoxLayout()
        self.url_edit = QtWidgets.QLineEdit()
        self.url_edit.setPlaceholderText("Paste a TIDAL track/album/playlist URL")
        self.url_edit.returnPressed.connect(self._do_url_load)
        self.url_load_btn = QtWidgets.QPushButton("Load")
        self.url_load_btn.clicked.connect(self._do_url_load)
        self.url_queue_btn = QtWidgets.QPushButton("Queue")
        self.url_queue_btn.clicked.connect(self._queue_url_tracks)
        top.addWidget(self.url_edit, 1)
        top.addWidget(self.url_load_btn)
        top.addWidget(self.url_queue_btn)
        layout.addLayout(top)
        self.url_list = self._new_browse_tree()
        layout.addWidget(self.url_list, 1)
        return tab

    def _build_collection_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        top = QtWidgets.QHBoxLayout()
        self.collection_type = self._new_media_type_combo()
        self.collection_type.currentTextChanged.connect(self._refresh_collection)
        self.fav_refresh_btn = QtWidgets.QPushButton("Refresh")
        self.fav_refresh_btn.clicked.connect(self._refresh_collection)
        top.addWidget(QtWidgets.QLabel("Collection"))
        top.addWidget(self.collection_type)
        top.addStretch(1)
        top.addWidget(self.fav_refresh_btn)
        layout.addLayout(top)
        self.fav_list = self._new_browse_tree()
        layout.addWidget(self.fav_list, 1)
        return tab

    def _build_cache_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)

        cache_group = QtWidgets.QGroupBox("Cache")
        cache_layout = QtWidgets.QVBoxLayout(cache_group)
        cache_top = QtWidgets.QHBoxLayout()
        self.cache_queue_btn = QtWidgets.QPushButton("Queue")
        self.cache_queue_btn.clicked.connect(self._queue_cache_tracks)
        self.cache_clear_btn = QtWidgets.QPushButton("Clear")
        self.cache_clear_btn.clicked.connect(self._clear_cache)
        self._cache_tab_status_label = self._new_stretch_status_label()
        cache_top.addWidget(self._cache_tab_status_label, 1)
        cache_top.addWidget(self.cache_queue_btn)
        cache_top.addWidget(self.cache_clear_btn)
        cache_layout.addLayout(cache_top)
        self.cache_list = self._new_track_list()
        cache_layout.addWidget(self.cache_list, 1)
        layout.addWidget(cache_group, 1)

        downloads_group = QtWidgets.QGroupBox("Downloads")
        downloads_layout = QtWidgets.QVBoxLayout(downloads_group)
        downloads_top = QtWidgets.QHBoxLayout()
        self._downloads_tab_status_label = self._new_stretch_status_label()
        self.downloads_queue_btn = QtWidgets.QPushButton("Queue")
        self.downloads_queue_btn.clicked.connect(self._queue_downloads_tracks)
        self.downloads_clear_btn = QtWidgets.QPushButton("Clear")
        self.downloads_clear_btn.clicked.connect(self._clear_downloads)
        self.downloads_open_btn = QtWidgets.QPushButton("Open folder")
        self.downloads_open_btn.clicked.connect(self._open_downloads_folder)
        downloads_top.addWidget(self.downloads_open_btn)
        downloads_top.addWidget(self._downloads_tab_status_label, 1)
        downloads_top.addWidget(self.downloads_queue_btn)
        downloads_top.addWidget(self.downloads_clear_btn)
        downloads_layout.addLayout(downloads_top)
        self.downloads_list = self._new_track_list()
        downloads_layout.addWidget(self.downloads_list, 1)
        layout.addWidget(downloads_group, 1)

        return tab

    def _build_lyrics_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 6, 0)
        layout.setSpacing(8)

        self.lyrics_title_label = QtWidgets.QLabel("Lyrics")
        self.lyrics_title_label.setWordWrap(True)
        title_font = self.lyrics_title_label.font()
        title_font.setPointSize(title_font.pointSize() + 3)
        title_font.setBold(True)
        self.lyrics_title_label.setFont(title_font)
        self.lyrics_title_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.lyrics_title_label)

        self.lyrics_meta_label = QtWidgets.QLabel("")
        self.lyrics_meta_label.setWordWrap(True)
        self.lyrics_meta_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.lyrics_meta_label)

        divider = QtWidgets.QFrame()
        divider.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        divider.setFrameShadow(QtWidgets.QFrame.Shadow.Plain)
        divider.setStyleSheet("color: rgba(255, 255, 255, 0.08);")
        layout.addWidget(divider)

        self.lyrics_view = QtWidgets.QTextBrowser()
        self.lyrics_view.setObjectName("lyricsBody")
        self.lyrics_view.setReadOnly(True)
        self.lyrics_view.setOpenLinks(False)
        self.lyrics_view.setOpenExternalLinks(False)
        self.lyrics_view.anchorClicked.connect(self._on_lyrics_anchor_clicked)
        self.lyrics_view.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.lyrics_view.setLineWrapMode(QtWidgets.QTextEdit.LineWrapMode.WidgetWidth)
        self.lyrics_view.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        body_font = self.lyrics_view.font()
        body_font.setPointSize(max(body_font.pointSize() + 1, 13))
        self.lyrics_view.setFont(body_font)
        option = self.lyrics_view.document().defaultTextOption()
        option.setWrapMode(QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.lyrics_view.document().setDefaultTextOption(option)
        self.lyrics_view.document().setDocumentMargin(4)
        self.lyrics_view.setStyleSheet(
            "QTextBrowser#lyricsBody { border: none; background: transparent; }"
        )
        layout.addWidget(self.lyrics_view, 1)
        self._set_lyrics_content(
            None,
            "Lyrics will appear for the currently playing track.",
            muted=True,
        )
        return panel

    def _build_tabs(self) -> None:
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_home_tab(), "Home")
        self.tabs.addTab(self._build_search_tab(), "Search")
        self.tabs.addTab(self._build_url_tab(), "URL")
        self.tabs.addTab(self._build_collection_tab(), "Collection")
        self.tabs.addTab(self._build_cache_tab(), "Cache")
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _build_left_panel(self, split: QtWidgets.QSplitter) -> None:
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs, 1)
        split.addWidget(panel)

    def _build_now_playing_panel(self, right_layout: QtWidgets.QVBoxLayout) -> None:
        now = QtWidgets.QFrame()
        self.now_panel = now
        now.setObjectName("nowPlaying")
        now.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        now.setFrameShadow(QtWidgets.QFrame.Shadow.Raised)
        now_layout = QtWidgets.QVBoxLayout(now)
        now_layout.setSpacing(8)

        self.cover_label = CoverImageWidget()
        fallback = self._fallback_cover_pixmap()
        if fallback is not None:
            self.cover_label.set_fallback_pixmap(fallback)
        self._set_now_playing_context_menu(self.cover_label)
        cover_row = QtWidgets.QHBoxLayout()
        cover_row.addWidget(self.cover_label, 1)
        now_layout.addLayout(cover_row)

        now_text = QtWidgets.QVBoxLayout()
        now_text.setSpacing(2)
        self.now_title = MarqueeLabel("Nothing playing")
        self._set_now_playing_context_menu(self.now_title)
        title_font = self.now_title.font()
        title_font.setPointSize(title_font.pointSize() + 2)
        title_font.setBold(True)
        self.now_title.setFont(title_font)
        title_h = self.now_title.fontMetrics().height()
        self.now_title.setMinimumHeight(title_h)
        self.now_title.setMaximumHeight(title_h)

        self.now_meta = MarqueeLabel("—")
        self._set_now_playing_context_menu(self.now_meta)
        # Qt font metrics can bias this label slightly high depending on font/hinting.
        self.now_meta.set_baseline_offset(2)
        meta_h = self.now_meta.fontMetrics().height()
        self.now_meta.setMinimumHeight(meta_h)
        self.now_meta.setMaximumHeight(meta_h)
        now_text.addWidget(self.now_title)
        now_text.addWidget(self.now_meta)
        now_layout.addLayout(now_text)

        now_meta_row = QtWidgets.QHBoxLayout()
        now_meta_left = QtWidgets.QVBoxLayout()
        self.quality_label = QtWidgets.QLabel("Quality: —")
        self.bitrate_label = QtWidgets.QLabel("Bitrate: —")
        self.bitperfect_label = QtWidgets.QLabel("Bit-perfect: —")
        for label in (self.quality_label, self.bitrate_label, self.bitperfect_label):
            self._set_now_playing_context_menu(label)
            label.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            now_meta_left.addWidget(label)
        now_meta_row.addLayout(now_meta_left)
        now_meta_row.addStretch(1)
        now_layout.addLayout(now_meta_row)

        self._set_now_playing_context_menu(now)
        right_layout.addWidget(now, 1)

    def _build_transport_panel(self, right_layout: QtWidgets.QVBoxLayout) -> None:
        controls_row = QtWidgets.QHBoxLayout()
        self.pause_btn = QtWidgets.QPushButton("Play")
        self.pause_btn.clicked.connect(self._toggle_play_pause)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop_playback)
        self.stop_btn.setEnabled(False)
        self.play_next_btn = QtWidgets.QPushButton("Skip")
        self.play_next_btn.clicked.connect(self._play_next_selected)
        controls_row.addWidget(self.pause_btn)
        controls_row.addWidget(self.stop_btn)
        controls_row.addWidget(self.play_next_btn)

        self.seek_time = QtWidgets.QLabel("0:00 / 0:00")
        self.seek_time.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        time_wrap = QtWidgets.QWidget()
        time_layout = QtWidgets.QHBoxLayout(time_wrap)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.addStretch(1)
        time_layout.addWidget(self.seek_time)
        time_layout.addStretch(1)
        controls_row.addWidget(time_wrap, 1)
        right_layout.addLayout(controls_row)

        self.seek_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.seek_slider.setEnabled(False)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self.seek_slider.sliderReleased.connect(self._on_seek_released)
        right_layout.addWidget(self.seek_slider)

        self.status_label = QtWidgets.QLabel("Status: starting…")
        right_layout.addWidget(self.status_label)

        diag_row = QtWidgets.QHBoxLayout()
        diag_row.setSpacing(6)
        self.queue_toggle = QtWidgets.QToolButton()
        self.queue_toggle.setText("Show queue")
        self.queue_toggle.setCheckable(True)
        self.queue_toggle.toggled.connect(self._toggle_queue)
        self.settings_btn = QtWidgets.QToolButton()
        self.settings_btn.setText("Settings")
        self.settings_btn.clicked.connect(self._open_settings_window)
        diag_row.addWidget(self.queue_toggle)
        diag_row.addWidget(self.settings_btn)

        self.volume_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.volume_slider.setMinimum(0)
        self.volume_slider.setMaximum(100)
        self.volume_slider.setValue(100)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        diag_row.addWidget(self.volume_slider, 1)

        self.volume_label = QtWidgets.QLabel("100%")
        self.volume_label.setFixedWidth(35)
        self.volume_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignCenter | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        diag_row.addWidget(self.volume_label)
        right_layout.addLayout(diag_row)

    def _build_right_panel(self, split: QtWidgets.QSplitter) -> None:
        panel = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(panel)
        right_layout.setContentsMargins(4, 0, 0, 0)
        split.addWidget(panel)
        details_tabs = QtWidgets.QTabWidget()
        details_tabs.setDocumentMode(True)
        now_tab = QtWidgets.QWidget()
        now_layout = QtWidgets.QVBoxLayout(now_tab)
        now_layout.setContentsMargins(0, 0, 0, 0)
        self._build_now_playing_panel(now_layout)
        details_tabs.addTab(now_tab, "Now Playing")
        details_tabs.addTab(self._build_lyrics_panel(), "Lyrics")
        right_layout.addWidget(details_tabs, 1)
        self._build_transport_panel(right_layout)

    def _init_tool_window_state(self) -> None:
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.log.customContextMenuRequested.connect(self._show_log_context_menu)
        self._log_window = None
        self._log_window_geometry: Optional[bytes] = None
        self._queue_window = None
        self._queue_list: Optional[QtWidgets.QListWidget] = None
        self._queue_items: List[str] = []
        self._queue_now_playing_id: Optional[str] = None
        self._queue_nudge_anim: Optional[QtCore.QPropertyAnimation] = None
        self._restore_debug_state = False
        self._restore_ffmpeg_disable_state = False
        self._settings_window = None
        self._settings_window_geometry: Optional[bytes] = None
        self._settings_debug_cb: Optional[QtWidgets.QCheckBox] = None
        self._settings_ffmpeg_cb: Optional[QtWidgets.QCheckBox] = None
        self._cache_status_label: Optional[QtWidgets.QLabel] = None
        self._cache_full_label: Optional[QtWidgets.QLabel] = None
        self._cache_size_spin: Optional[QtWidgets.QSpinBox] = None

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)

        self._build_device_row(layout)
        self._build_tabs()

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        split.setHandleWidth(0)
        layout.addWidget(split, 1)
        self._build_left_panel(split)
        self._build_right_panel(split)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        self._init_tool_window_state()
        self._install_shortcuts()
        self._refresh_devices()
        self._load_device_pref()
        self._set_enabled(False)

    def _install_shortcuts(self) -> None:
        # Keep shortcuts modifier-based so they don't interfere with typing in the search/url boxes.
        def allow_single_key_shortcuts() -> bool:
            w = QtWidgets.QApplication.focusWidget()
            if w is None:
                return True
            # Avoid stealing unmodified keys while the user is typing into an editor/spinbox.
            if isinstance(
                w,
                (
                    QtWidgets.QLineEdit,
                    QtWidgets.QTextEdit,
                    QtWidgets.QPlainTextEdit,
                    QtWidgets.QAbstractSpinBox,
                ),
            ):
                return False
            return True

        def guarded(handler, *, allow_while_typing: bool = True):
            def _wrapped() -> None:
                if not allow_while_typing and not allow_single_key_shortcuts():
                    return
                handler()

            return _wrapped

        def add_action(shortcuts: List[str], handler, *, allow_while_typing: bool = True) -> None:
            act = QtGui.QAction(self)
            act.setShortcuts([QtGui.QKeySequence(s) for s in shortcuts])
            act.triggered.connect(guarded(handler, allow_while_typing=allow_while_typing))
            self.addAction(act)

        add_action(["Ctrl+1"], lambda: self.tabs.setCurrentIndex(0))
        add_action(["Ctrl+2"], lambda: self.tabs.setCurrentIndex(1))
        add_action(["Ctrl+3"], lambda: self.tabs.setCurrentIndex(2))
        add_action(["Ctrl+4"], lambda: self.tabs.setCurrentIndex(3))
        add_action(["Ctrl+F"], self._focus_search)
        add_action(["Ctrl+L"], self._focus_url)
        add_action(["F5", "Ctrl+R"], self._refresh_devices)

        add_action(["Ctrl+Return", "Ctrl+Enter"], self._toggle_play_pause)
        add_action(["Ctrl+Shift+Return", "Ctrl+Shift+Enter"], self._play_next_selected)
        add_action(["Ctrl+Space"], self._toggle_play_pause)
        add_action(["Ctrl+."], self._stop_playback)

        add_action(["Ctrl+Left"], lambda: self._seek_delta_preview(-10.0))
        add_action(["Ctrl+Right"], lambda: self._seek_delta_preview(10.0))

        # Media-player style bindings (only when you're not typing in a text field).
        add_action(["J"], lambda: self._seek_delta_preview(-10.0), allow_while_typing=False)
        add_action(["L"], lambda: self._seek_delta_preview(10.0), allow_while_typing=False)
        add_action(["K"], self._toggle_play_pause, allow_while_typing=False)
        add_action(["Escape"], self._stop_playback, allow_while_typing=False)

    def _focus_search(self) -> None:
        self.tabs.setCurrentIndex(1)
        self.search_edit.setFocus(QtCore.Qt.FocusReason.ShortcutFocusReason)
        self.search_edit.selectAll()

    def _focus_url(self) -> None:
        self.tabs.setCurrentIndex(2)
        self.url_edit.setFocus(QtCore.Qt.FocusReason.ShortcutFocusReason)
        self.url_edit.selectAll()

    def _toggle_play_pause(self) -> None:
        # If nothing is playing, resume current track if available, otherwise play selected.
        if self._play_worker is None or not self._play_worker.isRunning():
            if self._current_play is not None:
                tid, dev = self._current_play
                self._start_playback(tid, dev)
            else:
                self._play_selected()
            return
        self._toggle_pause()

    def _set_enabled(self, enabled: bool) -> None:
        self.device_combo.setEnabled(enabled)
        self.refresh_devices_btn.setEnabled(enabled)
        self.home_refresh_btn.setEnabled(enabled)
        self.search_edit.setEnabled(enabled)
        self.search_limit.setEnabled(enabled)
        self.search_btn.setEnabled(enabled)
        self.url_edit.setEnabled(enabled)
        self.url_load_btn.setEnabled(enabled)
        self.url_queue_btn.setEnabled(enabled)
        self.play_next_btn.setEnabled(enabled)
        self.pause_btn.setEnabled(enabled)
        self.fav_refresh_btn.setEnabled(enabled)

    def _append_log(self, msg: str) -> None:
        self.log.appendPlainText(msg)

    def _show_log_context_menu(self, pos: QtCore.QPoint) -> None:
        menu = QtWidgets.QMenu(self.log)
        clear_action = QtGui.QAction("Clear log", self)
        clear_action.triggered.connect(self.log.clear)
        menu.addAction(clear_action)
        menu.exec(self.log.viewport().mapToGlobal(pos))

    def _save_window_geometry(
        self,
        window: Optional[QtWidgets.QWidget],
        attr_name: str,
        settings_key: str,
    ) -> None:
        if window is None:
            return
        geometry = window.saveGeometry()
        setattr(self, attr_name, geometry)
        self._settings.setValue(settings_key, geometry)
        self._settings.sync()

    def _restore_or_resize_window(
        self,
        window: QtWidgets.QWidget,
        geometry: Optional[QtCore.QByteArray],
        size: tuple[int, int],
    ) -> None:
        if geometry:
            window.restoreGeometry(geometry)
            return
        window.resize(*size)

    def _present_window(self, window: QtWidgets.QWidget) -> None:
        window.show()
        window.raise_()
        window.activateWindow()

    def _close_window_attr(self, attr_name: str) -> None:
        window = getattr(self, attr_name)
        if window is not None:
            window.close()

    def _on_log_window_finished(self, _result: int) -> None:
        self._save_window_geometry(self._log_window, "_log_window_geometry", "log_window_geometry")
        self._log_window = None
        if self._debug_enabled:
            self._debug_enabled = False
            if self._settings_debug_cb is not None:
                with QtCore.QSignalBlocker(self._settings_debug_cb):
                    self._settings_debug_cb.setChecked(False)
            self._settings.setValue("debug_enabled", False)
            self._settings.sync()

    def _open_log_window(self) -> None:
        if self._log_window is None:
            win = QtWidgets.QDialog(self)
            win.setWindowTitle("TIDAL Bitperfect — Log")
            layout = QtWidgets.QVBoxLayout(win)
            layout.addWidget(self.log)
            self._restore_or_resize_window(win, self._log_window_geometry, (700, 400))
            win.finished.connect(self._on_log_window_finished)
            self._log_window = win
        self._present_window(self._log_window)

    def _close_log_window(self) -> None:
        self._close_window_attr("_log_window")

    def _on_settings_window_finished(self, _result: int) -> None:
        self._save_window_geometry(
            self._settings_window,
            "_settings_window_geometry",
            "settings_window_geometry",
        )
        self._settings_window = None
        self._settings_debug_cb = None
        self._settings_ffmpeg_cb = None
        self._cache_status_label = None
        self._cache_full_label = None
        self._cache_size_spin = None

    def _new_settings_help_label(self, text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color: gray; font-size: 10px;")
        return label

    def _build_cache_settings_group(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("Cache")
        layout = QtWidgets.QVBoxLayout(group)

        cache_size_row = QtWidgets.QHBoxLayout()
        cache_size_row.addWidget(QtWidgets.QLabel("Max size (GB):"))
        cache_size_spin = QtWidgets.QSpinBox()
        cache_size_spin.setRange(0, 128)
        cache_size_spin.setValue(self._cache_max_gb)
        cache_size_spin.valueChanged.connect(self._on_cache_size_changed)
        cache_size_row.addWidget(cache_size_spin)
        cache_size_row.addStretch(1)
        layout.addLayout(cache_size_row)

        self._cache_status_label = QtWidgets.QLabel("")
        self._cache_full_label = QtWidgets.QLabel("Cache is full; caching is disabled.")
        self._cache_full_label.setVisible(False)
        clear_btn = QtWidgets.QPushButton("Clear cache")
        clear_btn.clicked.connect(self._clear_cache)

        layout.addWidget(self._cache_status_label)
        layout.addWidget(self._cache_full_label)
        layout.addWidget(clear_btn)

        self._cache_size_spin = cache_size_spin
        return group

    def _build_diagnostics_settings_group(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("Diagnostics")
        layout = QtWidgets.QGridLayout(group)

        debug_cb = QtWidgets.QCheckBox("Enable debug log")
        debug_cb.setChecked(self._debug_enabled)
        debug_cb.toggled.connect(self._on_debug_toggled)
        ffmpeg_cb = QtWidgets.QCheckBox("Disable ffmpeg")
        ffmpeg_cb.setChecked(self._disable_ffmpeg)
        ffmpeg_cb.toggled.connect(self._on_disable_ffmpeg_toggled)
        cache_cb = QtWidgets.QCheckBox("Disable cache")
        cache_cb.setChecked(self._cache_disabled)
        cache_cb.toggled.connect(self._on_cache_disabled_toggled)
        creds_cb = QtWidgets.QCheckBox("Disable credentials")
        creds_cb.setChecked(self._creds_disabled)
        creds_cb.toggled.connect(self._on_creds_disabled_toggled)

        layout.addWidget(debug_cb, 0, 0)
        layout.addWidget(ffmpeg_cb, 0, 1)
        layout.addWidget(cache_cb, 1, 0)
        layout.addWidget(creds_cb, 1, 1)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)

        self._settings_debug_cb = debug_cb
        self._settings_ffmpeg_cb = ffmpeg_cb
        return group

    def _build_discord_settings_group(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("Discord Rich Presence")
        layout = QtWidgets.QVBoxLayout(group)

        discord_cb = QtWidgets.QCheckBox("Enable Discord Rich Presence")
        discord_cb.setChecked(self._discord_enabled)
        discord_cb.toggled.connect(self._on_discord_toggled)
        layout.addWidget(discord_cb)
        self._discord_cb = discord_cb

        discord_id_row = QtWidgets.QHBoxLayout()
        discord_id_label = QtWidgets.QLabel("Discord Client ID:")
        discord_id_edit = QtWidgets.QLineEdit()
        discord_id_edit.setPlaceholderText("Leave blank to use the built-in Discord app ID")
        discord_id_edit.setText(self._settings.value("discord_client_id", "", type=str))
        discord_id_edit.textChanged.connect(self._on_discord_client_id_changed)
        discord_id_row.addWidget(discord_id_label)
        discord_id_row.addWidget(discord_id_edit, 1)
        layout.addLayout(discord_id_row)
        self._discord_id_label = discord_id_label
        self._discord_id_edit = discord_id_edit

        self._discord_help_label = self._new_settings_help_label(
            "Uses the built-in Discord app ID by default; you can override it here."
        )
        layout.addWidget(self._discord_help_label)
        self._apply_discord_settings_ui()
        return group

    def _build_mpris_settings_group(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("MPRIS D-Bus Integration")
        layout = QtWidgets.QVBoxLayout(group)

        mpris_cb = QtWidgets.QCheckBox(
            "Enable MPRIS (media keys, playerctl, KDE Connect)"
        )
        mpris_cb.setChecked(self._mpris_enabled)
        mpris_cb.toggled.connect(self._on_mpris_toggled)
        layout.addWidget(mpris_cb)
        self._mpris_cb = mpris_cb

        self._mpris_help_label = self._new_settings_help_label(
            "Exposes playback controls on D-Bus for desktop integration."
        )
        layout.addWidget(self._mpris_help_label)
        self._apply_mpris_settings_ui()
        return group

    def _build_playback_settings_group(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("Playback")
        layout = QtWidgets.QVBoxLayout(group)

        gapless_cb = QtWidgets.QCheckBox("Enable gapless playback")
        gapless_cb.setChecked(self._gapless_enabled)
        gapless_cb.toggled.connect(self._on_gapless_toggled)
        layout.addWidget(gapless_cb)
        self._gapless_cb = gapless_cb

        layout.addWidget(
            self._new_settings_help_label(
                "Eliminates silence between consecutive tracks. "
                "Pre-downloads the next track while the current one plays."
            )
        )
        return group

    def _open_settings_window(self) -> None:
        if self._settings_window is None:
            self._cache.refresh_usage()
            win = QtWidgets.QDialog(self)
            win.setWindowTitle("TIDAL Bitperfect — Settings")
            layout = QtWidgets.QVBoxLayout(win)
            layout.addWidget(self._build_cache_settings_group())
            layout.addWidget(self._build_playback_settings_group())
            layout.addWidget(self._build_discord_settings_group())
            layout.addWidget(self._build_mpris_settings_group())
            layout.addWidget(self._build_diagnostics_settings_group())

            self._restore_or_resize_window(win, self._settings_window_geometry, (420, 260))
            win.finished.connect(self._on_settings_window_finished)
            self._settings_window = win
            self._update_cache_status_ui()
        self._present_window(self._settings_window)

    def _on_queue_window_finished(self, _result: int) -> None:
        self._queue_window = None
        self._queue_list = None
        if self.queue_toggle.isChecked():
            with QtCore.QSignalBlocker(self.queue_toggle):
                self.queue_toggle.setChecked(False)
        self.queue_toggle.setText("Show queue")

    def _build_queue_window(self) -> QtWidgets.QDialog:
        win = QtWidgets.QDialog(None, QtCore.Qt.WindowType.Window)
        win.setWindowTitle("TIDAL Bitperfect — Queue")
        win.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._queue_list = QtWidgets.QListWidget()
        self._queue_list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self._queue_list.customContextMenuRequested.connect(self._show_queue_context_menu)
        self._queue_list.itemDoubleClicked.connect(self._on_queue_item_activated)
        layout = QtWidgets.QVBoxLayout(win)
        layout.addWidget(self._queue_list)
        main_geo = self.frameGeometry()
        width = 360
        height = max(200, main_geo.height())
        x = main_geo.x() + main_geo.width() + 10
        y = main_geo.y()
        win.resize(width, height)
        win.move(x, y)
        win.finished.connect(self._on_queue_window_finished)
        win.destroyed.connect(lambda _obj=None: self._on_queue_window_finished(0))
        return win

    def _open_queue_window(self) -> None:
        # Use a top-level window so we don't fight the WM, and recreate each time.
        win = self._build_queue_window()
        self._queue_window = win
        self._refresh_queue_view()
        self._present_window(self._queue_window)

    def _close_queue_window(self) -> None:
        self._close_window_attr("_queue_window")

    def _toggle_queue(self, checked: bool) -> None:
        if checked:
            self._open_queue_window()
        else:
            self._close_queue_window()
        self.queue_toggle.setText("Hide queue" if checked else "Show queue")

    def _nudge_button(self, btn: Optional[QtWidgets.QWidget], attr_name: str) -> None:
        if btn is None:
            return
        anim = getattr(self, attr_name, None)
        if anim is not None and anim.state() == QtCore.QAbstractAnimation.State.Running:
            return
        start = btn.pos()
        left = QtCore.QPoint(start.x() - 4, start.y())
        right = QtCore.QPoint(start.x() + 4, start.y())
        anim = QtCore.QPropertyAnimation(btn, b"pos", self)
        anim.setDuration(220)
        anim.setEasingCurve(QtCore.QEasingCurve.Type.InOutSine)
        anim.setKeyValueAt(0.0, start)
        anim.setKeyValueAt(0.33, left)
        anim.setKeyValueAt(0.66, right)
        anim.setKeyValueAt(1.0, start)
        anim.finished.connect(lambda: setattr(self, attr_name, None))
        setattr(self, attr_name, anim)
        anim.start()

    def _nudge_queue_button(self) -> None:
        self._nudge_button(self.queue_toggle, "_queue_nudge_anim")

    def _refresh_devices(self) -> None:
        # Preserve current selection on refresh, falling back to the saved preference.
        # Important: block signals while repopulating, otherwise QComboBox will emit
        # currentTextChanged when it auto-selects index 0, overwriting the stored pref.
        offline = self._is_offline()
        if offline and not self._offline_mode:
            self._enter_offline_mode("Offline detected; cache playback only.")
        elif self._offline_mode and not offline:
            self._start_login()
        if not self._offline_mode and self._session is not None and not offline:
            try:
                self._session.check_login()
            except Exception:
                pass
        current = (self.device_combo.currentText() or "").strip()
        preferred = (self._settings.value("alsa_device", "", type=str) or "").strip()
        devs_sorted = sorted(list_playback_devices())

        target = current or preferred
        with QtCore.QSignalBlocker(self.device_combo):
            self.device_combo.clear()

            # Ensure we can keep showing a custom/manual device string even if it
            # doesn't appear in the enumerated device list.
            extras = []
            for v in (target,):
                if v and v not in devs_sorted:
                    extras.append(v)
            self.device_combo.addItems(extras + devs_sorted)

            if target:
                self.device_combo.setCurrentText(target)
            elif devs_sorted:
                self.device_combo.setCurrentIndex(0)

    def _save_device_pref(self, text: str) -> None:
        t = (text or "").strip()
        if t:
            self._settings.setValue("alsa_device", t)
            self._settings.sync()

    def _on_device_changed(self, text: str) -> None:
        self._save_device_pref(text)
        self._update_bitperfect_label()

    def _on_debug_toggled(self, checked: bool) -> None:
        self._debug_enabled = checked
        if checked:
            self._open_log_window()
        else:
            self._close_log_window()
        self._settings.setValue("debug_enabled", checked)
        self._settings.sync()

    def _on_disable_ffmpeg_toggled(self, checked: bool) -> None:
        self._disable_ffmpeg = checked
        self._settings.setValue("disable_ffmpeg", checked)
        self._settings.sync()

    def _on_cache_disabled_toggled(self, checked: bool) -> None:
        self._cache_disabled = bool(checked)
        self._cache.set_disabled(self._cache_disabled)
        self._settings.setValue("cache_disabled", self._cache_disabled)
        self._settings.sync()
        self._update_cache_status_ui()

    def _on_creds_disabled_toggled(self, checked: bool) -> None:
        self._creds_disabled = bool(checked)
        tidal_core.CREDS_DISABLED = self._creds_disabled
        self._settings.setValue("creds_disabled", self._creds_disabled)
        self._settings.sync()

    def _on_gapless_toggled(self, checked: bool) -> None:
        self._gapless_enabled = bool(checked)
        self._settings.setValue("gapless_enabled", self._gapless_enabled)
        self._settings.sync()

    def _on_discord_toggled(self, checked: bool) -> None:
        if not self._discord_available:
            self._discord_enabled = False
            if self._discord_cb is not None:
                with QtCore.QSignalBlocker(self._discord_cb):
                    self._discord_cb.setChecked(False)
            return
        self._discord_enabled = bool(checked)
        self._settings.setValue("discord_enabled", self._discord_enabled)
        self._settings.sync()

        if self._discord_enabled:
            # Enable Discord RPC
            discord_client_id = self._settings.value("discord_client_id", "", type=str).strip()
            if not discord_client_id:
                discord_client_id = DEFAULT_CLIENT_ID
            if discord_client_id:
                if self._discord_rpc is None:
                    self._init_discord_rpc(discord_client_id)
                else:
                    # Reconnect if already initialized
                    self._discord_rpc.connect()
            else:
                self._append_log("Discord Rich Presence enabled but no client ID set")
        else:
            # Disable Discord RPC
            if self._discord_rpc:
                self._discord_rpc.disconnect()

    def _on_discord_client_id_changed(self, text: str) -> None:
        client_id = text.strip()
        self._settings.setValue("discord_client_id", client_id)
        self._settings.sync()

        # Reinitialize Discord RPC if enabled and ID changed
        if not self._discord_available:
            return
        if self._discord_enabled:
            if self._discord_rpc:
                self._discord_rpc.disconnect()
            resolved_id = client_id or DEFAULT_CLIENT_ID
            self._init_discord_rpc(resolved_id)

    def _apply_discord_settings_ui(self) -> None:
        if (
            self._discord_cb is None
            or self._discord_id_label is None
            or self._discord_id_edit is None
            or self._discord_help_label is None
        ):
            return
        self._discord_cb.setChecked(self._discord_enabled)
        self._discord_cb.setEnabled(self._discord_available)
        self._discord_id_label.setEnabled(self._discord_available)
        self._discord_id_edit.setEnabled(self._discord_available)
        if self._discord_available:
            self._discord_help_label.setText(
                "Uses the built-in Discord app ID by default; you can override it here."
            )
        else:
            self._discord_help_label.setText(
                "Install pypresence to enable Discord Rich Presence."
            )

    def _format_bytes(self, size: int) -> str:
        size = max(0, int(size))
        gb = 1024 * 1024 * 1024
        mb = 1024 * 1024
        if size >= gb:
            return f"{size / gb:.2f} GB"
        return f"{size / mb:.1f} MB"

    def _update_cache_status_ui(self) -> None:
        used = self._cache.used_bytes
        max_b = self._cache.max_bytes
        if max_b <= 0:
            msg = "Cache disabled (max size is 0 GB)."
        else:
            msg = f"Cache used: {self._format_bytes(used)} / {self._format_bytes(max_b)}"
        if self._cache_status_label is not None:
            self._cache_status_label.setText(msg)
        if self._cache_tab_status_label is not None:
            cover_count, _cover_bytes = self._cache.cover_stats()
            tab_msg = (
                f"Tracks: {len(self._cache_tracks)} | Covers: {cover_count}"
                f" | {self._format_bytes(used)} / {self._format_bytes(max_b)}"
            )
            if self._cache.full:
                tab_msg += " (full; caching disabled)"
            self._cache_tab_status_label.setText(tab_msg)
        if self._cache_full_label is not None:
            self._cache_full_label.setVisible(self._cache.full)
        if self._cache.full and not self._cache_full_notified:
            self._cache_full_notified = True
        if not self._cache.full:
            self._cache_full_notified = False

    def _on_cache_size_changed(self, value: int) -> None:
        self._cache_max_gb = max(0, int(value))
        self._cache.set_max_bytes(self._cache_max_gb * 1024 * 1024 * 1024)
        self._settings.setValue("cache_max_gb", self._cache_max_gb)
        self._settings.sync()
        self._update_cache_status_ui()

    def _clear_cache(self) -> None:
        audio_count, audio_bytes = self._cache.audio_stats()
        cover_count, cover_bytes = self._cache.cover_stats()
        total_bytes = audio_bytes + cover_bytes
        msg = (
            "Clear cached tracks, covers, or both?\n\n"
            f"Tracks: {audio_count} ({self._format_bytes(audio_bytes)})\n"
            f"Covers: {cover_count} ({self._format_bytes(cover_bytes)})\n"
            f"Total: {self._format_bytes(total_bytes)}"
        )
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Clear cache")
        box.setText(msg)
        tracks_btn = box.addButton("Tracks", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        covers_btn = box.addButton("Covers", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        both_btn = box.addButton("Both", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is None or clicked == box.button(QtWidgets.QMessageBox.StandardButton.Cancel):
            return
        if clicked == tracks_btn:
            self._cache.clear_audio()
        elif clicked == covers_btn:
            self._cache.clear_covers()
        elif clicked == both_btn:
            self._cache.clear()
        self._cache_tracks = []
        if hasattr(self, "cache_list"):
            self.cache_list.clear()
        self._refresh_cache_tab()
        self._update_cache_status_ui()

    def _clear_downloads(self) -> None:
        count = len(self._download_tracks)
        if count <= 0:
            return
        msg = f"This will delete ALL {count} manually downloaded songs."
        resp = QtWidgets.QMessageBox.warning(
            self,
            "Clear downloads",
            msg,
            QtWidgets.QMessageBox.StandardButton.Cancel
            | QtWidgets.QMessageBox.StandardButton.Yes,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        if resp != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._cache.clear_downloads()
        self._download_tracks = []
        if hasattr(self, "downloads_list"):
            self.downloads_list.clear()
        self._refresh_cache_tab()
        self._update_cache_status_ui()

    def _open_downloads_folder(self) -> None:
        downloads_dir = os.path.join(self._cache_dir, "downloads")
        try:
            os.makedirs(downloads_dir, exist_ok=True)
        except Exception as exc:
            self._append_log(f"downloads: failed to create folder: {exc}")
            return
        url = QtCore.QUrl.fromLocalFile(downloads_dir)
        if not QtGui.QDesktopServices.openUrl(url):
            self._append_log(f"downloads: failed to open folder: {downloads_dir}")

    def _on_cache_write(self) -> None:
        self._cache.refresh_usage()
        self._update_cache_status_ui()
        if self.tabs.currentIndex() == 4:
            self._refresh_cache_tab()

    def _on_search_limit_changed(self, value: int) -> None:
        self._settings.setValue("search_limit", int(value))
        self._settings.sync()

    def _load_device_pref(self) -> None:
        preferred = (self._settings.value("alsa_device", "", type=str) or "").strip()
        if preferred:
            with QtCore.QSignalBlocker(self.device_combo):
                # If the stored device isn't in the list (custom hw string),
                # allow it to be displayed/selected anyway.
                if self.device_combo.findText(preferred) < 0:
                    self.device_combo.insertItem(0, preferred)
                self.device_combo.setCurrentText(preferred)

        # Load saved volume (default to 100%)
        saved_volume = self._settings.value("volume", 100, type=int)
        saved_volume = max(0, min(100, saved_volume))  # Clamp to 0-100
        with QtCore.QSignalBlocker(self.volume_slider):
            self.volume_slider.setValue(saved_volume)
        self.volume_label.setText(f"{saved_volume}%")
        self._set_alsa_volume(saved_volume)
        self._log_window_geometry = self._settings.value(
            "log_window_geometry", None, type=QtCore.QByteArray
        )
        self._settings_window_geometry = self._settings.value(
            "settings_window_geometry", None, type=QtCore.QByteArray
        )
        self._restore_debug_state = bool(
            self._settings.value("debug_enabled", False, type=bool)
        )
        self._restore_ffmpeg_disable_state = bool(
            self._settings.value("disable_ffmpeg", False, type=bool)
        )
        self._debug_enabled = self._restore_debug_state
        if self._restore_debug_state:
            self._open_log_window()
        if self._restore_ffmpeg_disable_state:
            self._disable_ffmpeg = True
        self._cache_disabled = bool(self._settings.value("cache_disabled", False, type=bool))
        self._cache.set_disabled(self._cache_disabled)
        self._creds_disabled = bool(self._settings.value("creds_disabled", False, type=bool))
        tidal_core.CREDS_DISABLED = self._creds_disabled
        saved_cache_gb = self._settings.value("cache_max_gb", None)
        if saved_cache_gb is not None:
            try:
                self._cache_max_gb = max(0, int(saved_cache_gb))
            except Exception:
                self._cache_max_gb = 1
        self._cache.set_max_bytes(self._cache_max_gb * 1024 * 1024 * 1024)
        if self._cache_size_spin is not None:
            with QtCore.QSignalBlocker(self._cache_size_spin):
                self._cache_size_spin.setValue(self._cache_max_gb)
        self._update_cache_status_ui()
        saved_limit = self._settings.value("search_limit", None)
        if saved_limit is not None:
            try:
                limit_val = int(saved_limit)
            except Exception:
                limit_val = 10
            if 1 <= limit_val <= 50:
                with QtCore.QSignalBlocker(self.search_limit):
                    self.search_limit.setValue(limit_val)

        # Load Discord Rich Presence settings
        self._discord_enabled = bool(self._settings.value("discord_enabled", self._discord_available, type=bool))
        discord_client_id = self._settings.value("discord_client_id", "", type=str).strip()
        if not self._discord_available:
            self._discord_enabled = False
        self._apply_discord_settings_ui()
        if self._discord_enabled and self._discord_available:
            if not discord_client_id:
                discord_client_id = DEFAULT_CLIENT_ID
            if discord_client_id:
                self._init_discord_rpc(discord_client_id)

        # Load gapless playback setting
        self._gapless_enabled = bool(self._settings.value("gapless_enabled", True, type=bool))

        # Load MPRIS settings
        self._mpris_enabled = bool(self._settings.value("mpris_enabled", self._mpris_available, type=bool))
        if not self._mpris_available:
            self._mpris_enabled = False
        self._apply_mpris_settings_ui()
        if self._mpris_enabled and self._mpris_available:
            self._init_mpris()

    def _is_offline(self) -> bool:
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=0.5).close()
            return False
        except Exception:
            return True

    def _init_discord_rpc(self, client_id: str) -> None:
        """Initialize Discord Rich Presence."""
        try:
            self._discord_rpc = DiscordRPC(client_id, parent=self)
            self._discord_rpc.status_message.connect(self._append_log)
            self._discord_rpc.error_message.connect(self._append_log)
            if self._discord_rpc.connect():
                self._append_log("Discord Rich Presence enabled")
        except Exception as e:
            self._append_log(f"Failed to initialize Discord RPC: {e}")
            self._discord_rpc = None

    def _update_discord_track(self, track: Optional[Dict[str, Any]], quality_info: Optional[StreamInfo] = None) -> None:
        """Update Discord RPC with current track and quality info."""
        if not self._discord_rpc or not self._discord_enabled:
            return

        if track:
            quality_dict = None
            if quality_info:
                quality_dict = {
                    'audio_quality': quality_info.audio_quality,
                    'bit_depth': quality_info.bit_depth,
                    'sample_rate': quality_info.sample_rate,
                }
            self._discord_rpc.update_track(track, quality_dict)
        else:
            self._discord_rpc.stop()

    def _update_discord_position(self, position_s: float, duration_s: float) -> None:
        """Update Discord RPC with playback position."""
        if self._discord_rpc and self._discord_enabled:
            self._discord_rpc.update_position(position_s, duration_s)

    def _update_discord_playing(self, is_playing: bool) -> None:
        """Update Discord RPC play/pause state."""
        if self._discord_rpc and self._discord_enabled:
            self._discord_rpc.set_playing(is_playing)

    # ---- MPRIS D-Bus helpers ------------------------------------------------

    def _on_mpris_toggled(self, checked: bool) -> None:
        if not self._mpris_available:
            self._mpris_enabled = False
            if self._mpris_cb is not None:
                with QtCore.QSignalBlocker(self._mpris_cb):
                    self._mpris_cb.setChecked(False)
            return
        self._mpris_enabled = bool(checked)
        self._settings.setValue("mpris_enabled", self._mpris_enabled)

        if self._mpris_enabled:
            if self._mpris_service is None:
                self._init_mpris()
        else:
            if self._mpris_service is not None:
                self._mpris_service.shutdown()
                self._mpris_service = None

    def _apply_mpris_settings_ui(self) -> None:
        if self._mpris_cb is None or self._mpris_help_label is None:
            return
        self._mpris_cb.setChecked(self._mpris_enabled)
        self._mpris_cb.setEnabled(self._mpris_available)
        if self._mpris_available:
            self._mpris_help_label.setText(
                "Exposes playback controls on D-Bus for desktop integration."
            )
        else:
            self._mpris_help_label.setText(
                "Install dbus-fast to enable MPRIS D-Bus integration."
            )

    def _init_mpris(self) -> None:
        """Initialize and start the MPRIS D-Bus service."""
        try:
            self._mpris_service = MprisService(parent=self)
            self._mpris_service.status_message.connect(self._append_log)
            self._mpris_service.error_message.connect(self._append_log)
            # Wire control signals from MPRIS → app
            self._mpris_service.play_requested.connect(self._mpris_on_play)
            self._mpris_service.pause_requested.connect(self._mpris_on_pause)
            self._mpris_service.play_pause_requested.connect(self._toggle_pause)
            self._mpris_service.stop_requested.connect(self._stop_playback)
            self._mpris_service.next_requested.connect(self._mpris_on_next)
            self._mpris_service.seek_requested.connect(self._mpris_on_seek)
            self._mpris_service.set_position_requested.connect(self._mpris_on_set_position)
            self._mpris_service.volume_requested.connect(self._mpris_on_volume)
            self._mpris_service.raise_requested.connect(self._mpris_on_raise)
            self._mpris_service.quit_requested.connect(self.close)
            if self._mpris_service.start():
                self._append_log("MPRIS D-Bus service enabled")
            else:
                self._mpris_service = None
        except Exception as e:
            self._append_log(f"Failed to initialize MPRIS: {e}")
            self._mpris_service = None

    def _mpris_on_play(self) -> None:
        if self._play_worker is not None and self._play_worker.isRunning():
            if self.pause_btn.text() != "Pause":
                self._toggle_pause()
        elif self._now_playing_track:
            tid = self._now_playing_track.get("id")
            if tid:
                self._play_track_id(str(tid))

    def _mpris_on_pause(self) -> None:
        if self._play_worker is not None and self._play_worker.isRunning():
            if self.pause_btn.text() == "Pause":
                self._toggle_pause()

    def _mpris_on_next(self) -> None:
        if self._queue_items:
            next_tid = self._queue_items.pop(0)
            self._refresh_queue_view()
            self._play_track_id(str(next_tid))
        elif self._play_worker is not None and self._play_worker.isRunning():
            self._stop_playback()

    def _mpris_on_seek(self, offset_us: int) -> None:
        if self._play_worker is not None and self._play_worker.isRunning():
            delta_s = offset_us / 1_000_000.0
            target = max(0.0, self._pos_s + delta_s)
            if self._duration_s > 0:
                target = min(target, self._duration_s)
            self._play_worker.seek_to(target)

    def _mpris_on_set_position(self, position_us: int) -> None:
        if self._play_worker is not None and self._play_worker.isRunning():
            target_s = position_us / 1_000_000.0
            target_s = max(0.0, min(target_s, self._duration_s)) if self._duration_s > 0 else max(0.0, target_s)
            self._play_worker.seek_to(target_s)

    def _mpris_on_volume(self, fraction: float) -> None:
        percent = int(round(fraction * 100))
        percent = max(0, min(100, percent))
        self.volume_slider.setValue(percent)

    def _mpris_on_raise(self) -> None:
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _update_mpris_track(self, track: Optional[Dict[str, Any]], quality_info: Optional[StreamInfo] = None) -> None:
        """Update MPRIS with current track and quality info."""
        if not self._mpris_service or not self._mpris_enabled:
            return
        if track:
            mpris_track = dict(track)
            mpris_track["duration"] = self._duration_s
            quality_dict = None
            if quality_info:
                quality_dict = {
                    'audio_quality': quality_info.audio_quality,
                    'bit_depth': quality_info.bit_depth,
                    'sample_rate': quality_info.sample_rate,
                }
            self._mpris_service.update_track(mpris_track, quality_dict)
        else:
            self._mpris_service.stop()

    def _update_mpris_position(self, position_s: float, duration_s: float) -> None:
        if self._mpris_service and self._mpris_enabled:
            self._mpris_service.update_position(position_s, duration_s)

    def _update_mpris_playing(self, is_playing: bool) -> None:
        if self._mpris_service and self._mpris_enabled:
            self._mpris_service.set_playing(is_playing)

    def _enter_offline_mode(self, reason: Optional[str] = None) -> None:
        self._offline_mode = True
        self._session = None
        self.status_label.setText("Status: offline mode (cache only)")
        if reason:
            self._append_log(reason)
        # Disable network-driven tabs, keep cache + device controls available.
        self.tabs.setTabEnabled(0, False)
        self.tabs.setTabEnabled(1, False)
        self.tabs.setTabEnabled(2, False)
        self.tabs.setTabEnabled(3, False)
        self.tabs.setTabEnabled(4, True)
        self.tabs.setCurrentIndex(4)
        self.device_combo.setEnabled(True)
        self.refresh_devices_btn.setEnabled(True)
        self.play_next_btn.setEnabled(True)
        self.pause_btn.setEnabled(True)
        self.url_queue_btn.setEnabled(False)
        self.search_btn.setEnabled(False)
        self.search_edit.setEnabled(False)
        self.search_limit.setEnabled(False)
        self.url_edit.setEnabled(False)
        self.url_load_btn.setEnabled(False)
        self.fav_refresh_btn.setEnabled(False)
        self._refresh_cache_tab()

    def _start_login(self) -> None:
        if self._is_offline():
            self._enter_offline_mode("Offline detected; cache playback only.")
            return
        self.status_label.setText("Status: login required…")
        self._append_log("Logging in to TIDAL…")
        self._login = LoginWorker()
        self._login.message.connect(self._append_log)
        self._login.login_link.connect(self._on_login_link)
        self._login.ready.connect(self._on_login_ready)
        self._login.error.connect(self._on_error)
        self._login.start()

    def _on_login_link(self, url: str, code: str, expires_s: int) -> None:
        self.status_label.setText("Status: waiting for login…")
        text = f"Open this link to log in:\n{url}\n\nCode: {code}\nExpires in: {expires_s}s"
        self._append_log(text)
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("TIDAL Login")
        box.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        box.setText(text)
        box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
        box.exec()

    def _on_login_ready(self, session: tidalapi.Session) -> None:
        self._session = session
        self._offline_mode = False
        self.tabs.setTabEnabled(0, True)
        self.tabs.setTabEnabled(1, True)
        self.tabs.setTabEnabled(2, True)
        self.tabs.setTabEnabled(3, True)
        self.tabs.setTabEnabled(4, True)
        self.status_label.setText("Status: ready")
        self._set_enabled(True)
        self._load_home()

    def _on_error(self, msg: str) -> None:
        if self._is_offline():
            self._enter_offline_mode(msg)
            return
        self.status_label.setText("Status: error")
        self._append_log(msg)
        QtWidgets.QMessageBox.critical(self, "Error", msg)

    def _populate_tracks(self, tracks: List[Dict[str, Any]], mode: str) -> None:
        if mode == "search":
            self._search_tracks = tracks
        elif mode == "url":
            self._url_tracks = tracks
        elif mode == "cache":
            self._cache_tracks = tracks
        elif mode == "downloads":
            self._download_tracks = tracks
        else:
            self._favorite_tracks = tracks
        for t in tracks:
            tid = t.get("id")
            if tid is not None:
                self._track_map_all[str(tid)] = t
        if mode == "search":
            active = self.search_list
        elif mode == "url":
            active = self.url_list
        elif mode == "cache":
            active = self.cache_list
        elif mode == "downloads":
            active = self.downloads_list
        else:
            active = self.fav_list
        active.clear()
        for t in tracks:
            item = QtWidgets.QListWidgetItem(tidal_core.format_track_line(t))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, t.get("id"))
            active.addItem(item)
        if active.count() > 0:
            active.setCurrentRow(0)
            active.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
        self._start_cover_prefetch()
        self._update_open_album_btn()

    def _do_search(self) -> None:
        if self._session is None:
            return
        q = self.search_edit.text().strip()
        if not q:
            return
        stype = self.search_type.currentText().strip().lower()
        if stype.endswith("s"):
            stype = stype[:-1]
        self.status_label.setText("Status: searching…")
        self._append_log(f"Search: {q}")
        self._last_tracks_mode = "search"
        self._tracks_worker = TracksWorker(
            self._session, "search", q, self.search_limit.value(), stype
        )
        self._tracks_worker.ready.connect(self._on_tracks_ready)
        self._tracks_worker.error.connect(self._on_error)
        self._tracks_worker.start()

    def _do_url_load(self) -> None:
        if self._session is None:
            return
        u = self.url_edit.text().strip()
        if not u:
            return
        self.status_label.setText("Status: loading URL…")
        self._append_log(f"URL: {u}")
        self._last_tracks_mode = "url"
        self._tracks_worker = TracksWorker(self._session, "url", u, 0, "track")
        self._tracks_worker.ready.connect(self._on_tracks_ready)
        self._tracks_worker.error.connect(self._on_error)
        self._tracks_worker.start()

    def _queue_url_tracks(self) -> None:
        if not self._url_tracks:
            return
        self._queue_named_tracks(self._url_tracks, "url list")

    def _on_tracks_ready(self, result: object) -> None:
        self.status_label.setText("Status: ready")
        if not isinstance(result, dict):
            return
        mode = self._last_tracks_mode or "search"
        if mode == "search":
            self._render_tree_results(self.search_list, result)
            return
        if mode == "url":
            self._render_tree_results(self.url_list, result)
            return
        items = result.get("items", [])
        if isinstance(items, list):
            self._populate_tracks(items, mode)

    def _register_loading_item(self, item: Optional[QtWidgets.QTreeWidgetItem]) -> None:
        if item is None:
            return
        if item not in self._loading_items:
            self._loading_items.append(item)
        if not self._loading_timer.isActive():
            self._loading_timer.start()

    def _unregister_loading_item(self, item: Optional[QtWidgets.QTreeWidgetItem]) -> None:
        if item is None:
            return
        try:
            self._loading_items.remove(item)
        except ValueError:
            return
        if not self._loading_items:
            self._loading_timer.stop()

    def _tick_loading_labels(self) -> None:
        if not self._loading_items:
            self._loading_timer.stop()
            return
        phases = ["Loading", "Loading.", "Loading..", "Loading..."]
        self._loading_phase = (self._loading_phase + 1) % len(phases)
        label = phases[self._loading_phase]
        alive: List[QtWidgets.QTreeWidgetItem] = []
        for item in self._loading_items:
            try:
                if item is None or item.treeWidget() is None:
                    continue
                item.setText(0, label)
                alive.append(item)
            except RuntimeError:
                continue  # C++ object already deleted
        self._loading_items = alive
        if not self._loading_items:
            self._loading_timer.stop()

    def _add_track_item(
        self,
        parent: QtWidgets.QTreeWidgetItem,
        track: Dict[str, Any],
        flat_tracks: Optional[List[Dict[str, Any]]] = None,
    ) -> QtWidgets.QTreeWidgetItem:
        item = QtWidgets.QTreeWidgetItem(parent, [tidal_core.format_track_line(track)])
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, "track")
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, track)
        tid = track.get("id")
        if tid is not None:
            self._track_map_all[str(tid)] = track
        if flat_tracks is not None:
            flat_tracks.append(track)
        return item

    def _on_tree_item_expanded(self, item: QtWidgets.QTreeWidgetItem) -> None:
        kind = self._tree_item_kind(item)
        if kind == "artist":
            self._ensure_artist_loaded(item)
        elif kind == "album":
            self._ensure_album_loaded(item)
        elif kind == "mix":
            self._ensure_mix_loaded(item)
        elif kind == "playlist":
            self._ensure_playlist_loaded(item)

    def _start_keyed_track_load(
        self,
        item: QtWidgets.QTreeWidgetItem,
        item_id: object,
        workers: Dict[str, KeyedCallWorker],
        items_dict: Dict[str, List[QtWidgets.QTreeWidgetItem]],
        loader: Callable[[tidalapi.Session, str], List[Dict[str, Any]]],
        ready: Callable[[str, object], None],
    ) -> None:
        if self._session is None or not item_id:
            return
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 3, "loading")
        if item.childCount():
            placeholder = item.child(0)
            placeholder.setText(0, "Loading")
            self._register_loading_item(placeholder)
        item_key = str(item_id)
        worker = workers.get(item_key)
        if worker is None:
            session = self._session
            worker = KeyedCallWorker(
                item_key,
                lambda session=session, item_key=item_key: loader(session, item_key),
            )
            worker.ready.connect(ready)
            worker.error.connect(self._on_error)
            worker.finished.connect(lambda item_key=item_key: workers.pop(item_key, None))
            workers[item_key] = worker
            worker.start()
        items = items_dict.setdefault(item_key, [])
        if item not in items:
            items.append(item)

    def _ensure_artist_loaded(self, item: QtWidgets.QTreeWidgetItem) -> None:
        if self._session is None or self._offline_mode:
            return
        state = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 2)
        if state in ("loading", "loaded"):
            return
        payload = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1) or {}
        artist_id = payload.get("id")
        if not artist_id:
            return
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 2, "loading")
        if item.childCount():
            placeholder = item.child(0)
            placeholder.setText(0, "Loading")
            self._register_loading_item(placeholder)
        artist_key = str(artist_id)
        worker = self._artist_detail_workers.get(artist_key)
        if worker is None:
            session = self._session
            assert session is not None
            worker = KeyedCallWorker(
                artist_key,
                lambda session=session, artist_key=artist_key: tidal_core.artist_details(
                    session, artist_key
                ),
            )
            worker.ready.connect(self._on_artist_details_ready)
            worker.error.connect(self._on_error)
            worker.finished.connect(
                lambda artist_key=artist_key: self._artist_detail_workers.pop(artist_key, None)
            )
            self._artist_detail_workers[artist_key] = worker
            worker.start()
        items = self._artist_items.setdefault(artist_key, [])
        if item not in items:
            items.append(item)

    def _ensure_album_loaded(self, item: QtWidgets.QTreeWidgetItem) -> None:
        if self._session is None or self._offline_mode:
            return
        state = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 3)
        if state in ("loading", "loaded"):
            return
        payload = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1) or {}
        if payload.get("tracks"):
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 3, "loaded")
            return
        album_id = payload.get("id") or payload.get("album_id")
        self._start_keyed_track_load(
            item,
            album_id,
            self._album_tracks_workers,
            self._album_items,
            tidal_core.album_tracks,
            self._on_album_tracks_ready,
        )

    def _on_artist_details_ready(self, artist_id: str, artist: Dict[str, Any]) -> None:
        items = self._artist_items.get(str(artist_id), [])
        alive = []
        for item in items:
            if item is None or item.treeWidget() is None:
                continue
            self._populate_artist_item(item, artist)
            alive.append(item)
        if alive:
            self._artist_items[str(artist_id)] = alive

    def _apply_loaded_tracks(
        self,
        items_dict: Dict[str, List[QtWidgets.QTreeWidgetItem]],
        item_id: str,
        tracks: List[Dict[str, Any]],
    ) -> None:
        items = items_dict.get(str(item_id), [])
        alive = []
        for item in items:
            if item is None or item.treeWidget() is None:
                continue
            if item.childCount():
                self._unregister_loading_item(item.child(0))
            item.takeChildren()
            payload = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1) or {}
            payload["tracks"] = tracks
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, payload)
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 3, "loaded")
            if tracks:
                for t in tracks:
                    if isinstance(t, dict):
                        self._add_track_item(item, t)
            else:
                empty = QtWidgets.QTreeWidgetItem(item, ["No tracks found"])
                empty.setData(0, QtCore.Qt.ItemDataRole.UserRole, "empty")
            alive.append(item)
        if alive:
            items_dict[str(item_id)] = alive
        self._start_cover_prefetch()

    def _on_album_tracks_ready(self, album_id: str, tracks: List[Dict[str, Any]]) -> None:
        self._apply_loaded_tracks(self._album_items, album_id, tracks)

    def _populate_artist_item(self, item: QtWidgets.QTreeWidgetItem, artist: Dict[str, Any]) -> None:
        if item.childCount():
            self._unregister_loading_item(item.child(0))
        item.takeChildren()
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, artist)
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 2, "loaded")
        tracks = artist.get("tracks", []) or []
        albums = artist.get("albums", []) or []
        ep_singles = artist.get("ep_singles", []) or []
        if tracks:
            group = QtWidgets.QTreeWidgetItem(item, ["Top tracks"])
            group.setData(0, QtCore.Qt.ItemDataRole.UserRole, "top_tracks_group")
            group.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, tracks)
            for t in tracks:
                if isinstance(t, dict):
                    self._add_track_item(group, t)
        for group_label, group_albums, fmt_fn in [
            ("Albums", albums, tidal_core.format_album_line),
            ("EP & Singles", ep_singles, tidal_core.format_ep_line),
        ]:
            if not group_albums:
                continue
            group = QtWidgets.QTreeWidgetItem(item, [group_label])
            group.setData(0, QtCore.Qt.ItemDataRole.UserRole, "group")
            for alb in group_albums:
                if not isinstance(alb, dict):
                    continue
                album_item = QtWidgets.QTreeWidgetItem(
                    group, [fmt_fn(alb)]
                )
                album_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, "album")
                album_item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, alb)
                album_item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 3, "pending")
                placeholder = QtWidgets.QTreeWidgetItem(album_item, ["Expand to load tracks"])
                placeholder.setData(0, QtCore.Qt.ItemDataRole.UserRole, "album_placeholder")
                album_id = alb.get("id") or alb.get("album_id")
                if album_id:
                    items = self._album_items.setdefault(str(album_id), [])
                    if album_item not in items:
                        items.append(album_item)
        if not tracks and not albums and not ep_singles:
            empty = QtWidgets.QTreeWidgetItem(item, ["No tracks or albums found"])
            empty.setData(0, QtCore.Qt.ItemDataRole.UserRole, "empty")
        self._start_cover_prefetch()

    def _render_tree_results(self, tree: QtWidgets.QTreeWidget, result: Dict[str, Any]) -> None:
        tree.clear()
        rtype = result.get("type")
        items = result.get("items", [])
        if not isinstance(items, list):
            items = []
        flat_tracks: List[Dict[str, Any]] = []

        if rtype == "track":
            for t in items:
                self._add_track_item(tree, t, flat_tracks)
        elif rtype in ("album", "playlist"):
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                if rtype == "album":
                    header = tidal_core.format_album_line(entry)
                else:
                    header = tidal_core.format_playlist_line(entry)
                parent = QtWidgets.QTreeWidgetItem(tree, [header])
                parent.setData(0, QtCore.Qt.ItemDataRole.UserRole, rtype)
                parent.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, entry)
                for t in entry.get("tracks", []) or []:
                    if isinstance(t, dict):
                        self._add_track_item(parent, t, flat_tracks)
        elif rtype == "artist":
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                header = tidal_core.format_artist_line(entry)
                parent = QtWidgets.QTreeWidgetItem(tree, [header])
                parent.setData(0, QtCore.Qt.ItemDataRole.UserRole, "artist")
                parent.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, entry)
                parent.setData(0, QtCore.Qt.ItemDataRole.UserRole + 2, "pending")
                placeholder = QtWidgets.QTreeWidgetItem(parent, ["Expand to load artist"])
                placeholder.setData(0, QtCore.Qt.ItemDataRole.UserRole, "artist_placeholder")
                artist_id = entry.get("id")
                if artist_id:
                    self._artist_items.setdefault(str(artist_id), []).append(parent)
        else:
            return

        if tree is self.search_list:
            self._search_tracks = flat_tracks
        elif tree is self.url_list:
            self._url_tracks = flat_tracks
            if rtype in ("album", "playlist"):
                tree.expandAll()
        self._start_cover_prefetch()

    def _cache_active_list(self) -> QtWidgets.QListWidget:
        if self.downloads_list.hasFocus():
            return self.downloads_list
        if self.cache_list.hasFocus():
            return self.cache_list
        if self.downloads_list.currentItem() is not None:
            return self.downloads_list
        return self.cache_list

    def _tree_item_kind(self, item: Optional[QtWidgets.QTreeWidgetItem]) -> Optional[str]:
        if item is None:
            return None
        return item.data(0, QtCore.Qt.ItemDataRole.UserRole)

    def _tree_item_payload(self, item: Optional[QtWidgets.QTreeWidgetItem]) -> Optional[Dict[str, Any]]:
        if item is None:
            return None
        payload = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1)
        return payload

    def _current_browse_tree(self) -> Optional[QtWidgets.QTreeWidget]:
        idx = self.tabs.currentIndex()
        if idx == 0:
            return self.home_list
        if idx == 1:
            return self.search_list
        if idx == 2:
            return self.url_list
        if idx == 3:
            return self.fav_list
        return None

    def _selected_track_id(self) -> Optional[str]:
        tree = self._current_browse_tree()
        if tree is not None:
            item = tree.currentItem()
            if self._tree_item_kind(item) != "track":
                return None
            payload = self._tree_item_payload(item) or {}
            tid = payload.get("id")
            return str(tid) if tid is not None else None
        item = self._cache_active_list().currentItem()
        if item is None:
            return None
        tid = item.data(QtCore.Qt.ItemDataRole.UserRole)
        return str(tid) if tid is not None else None

    def _selected_album_cover(self) -> tuple[Optional[str], Optional[str]]:
        tree = self._current_browse_tree()
        if tree is None:
            return None, None
        item = tree.currentItem()
        if self._tree_item_kind(item) != "album":
            return None, None
        payload = self._tree_item_payload(item) or {}
        cover_url = payload.get("cover_url")
        album_id = payload.get("id") or payload.get("album_id")
        if album_id is not None:
            return f"album:{album_id}", cover_url
        if cover_url:
            return f"album:{cover_url}", cover_url
        return None, None

    def _selected_track(self) -> Optional[Dict[str, Any]]:
        tid = self._selected_track_id()
        if tid is None:
            return None
        return self._track_map_all.get(str(tid))

    def _is_cached_track(self, track_id: str) -> bool:
        return bool(self._cache.get_cached_audio_by_track_id(track_id))

    def _update_open_album_btn(self) -> None:
        pass

    def _collection_type_key(self) -> str:
        text = self.collection_type.currentText().strip().lower()
        if text.endswith("s"):
            text = text[:-1]
        return text or "track"

    def _favorite_id_set(self, item_type: str) -> set[str]:
        if item_type == "album":
            return self._favorite_album_ids
        if item_type == "playlist":
            return self._favorite_playlist_ids
        if item_type == "artist":
            return self._favorite_artist_ids
        return self._favorite_ids

    def _is_favorite_item(self, item_type: str, item_id: str) -> bool:
        return bool(item_id) and item_id in self._favorite_id_set(item_type)

    def _set_favorite_state(self, item_type: str, item_id: str, favorite: bool) -> None:
        item_id = str(item_id)
        favorite_ids = self._favorite_id_set(item_type)
        if favorite:
            favorite_ids.add(item_id)
            return
        favorite_ids.discard(item_id)

    def _apply_collection_items(self, item_type: str, items: List[Dict[str, Any]]) -> None:
        favorite_ids = self._favorite_id_set(item_type)
        favorite_ids.clear()
        favorite_ids.update(str(item.get("id")) for item in items if item.get("id") is not None)
        if item_type == "track":
            self._favorite_tracks = items
        self._render_tree_results(self.fav_list, {"type": item_type, "items": items})

    def _toggle_item_favorite(self, item_type: str, item_id: str, favorite: bool) -> None:
        if self._session is None or not item_id:
            return
        if self._favorite_toggle_worker is not None and self._favorite_toggle_worker.isRunning():
            return
        self._append_log(f"favorite: {item_type} {item_id} -> {'add' if favorite else 'remove'}")
        worker = FavoriteToggleWorker(self._session, item_type, item_id, favorite)
        worker.ready.connect(self._on_favorite_toggled)
        worker.error.connect(self._on_favorite_toggle_error)
        self._favorite_toggle_worker = worker
        worker.start()

    def _tidal_url(self, kind: str, item_id: Optional[object]) -> Optional[str]:
        if item_id is None:
            return None
        item_id_str = str(item_id).strip()
        if not item_id_str:
            return None
        return f"https://tidal.com/{kind}/{item_id_str}"

    def _open_tidal_item(self, kind: str, item_id: Optional[object]) -> None:
        url = self._tidal_url(kind, item_id)
        if not url:
            return
        self.tabs.setCurrentIndex(2)
        self.url_edit.setText(url)
        self._do_url_load()

    def _refresh_collection(self) -> None:
        if self._session is None:
            return
        if self._collection_worker is not None and self._collection_worker.isRunning():
            return
        self.status_label.setText("Status: loading collection…")
        item_type = self._collection_type_key()
        session = self._session
        worker = KeyedCallWorker(
            item_type,
            lambda session=session, item_type=item_type: COLLECTION_LOADERS[item_type](
                session,
                limit=200,
                offset=0,
            ),
        )
        worker.ready.connect(self._on_collection_ready)
        worker.error.connect(self._on_collection_error)
        self._collection_worker = worker
        worker.start()

    def _on_collection_ready(self, item_type: str, items: List[Dict[str, Any]]) -> None:
        self.status_label.setText("Status: ready")
        self._collection_worker = None
        self._apply_collection_items(item_type, items)

    def _on_collection_error(self, msg: str) -> None:
        self.status_label.setText("Status: error")
        self._collection_worker = None
        QtWidgets.QMessageBox.critical(self, "Collection error", msg)

    def _refresh_cache_tab(self) -> None:
        cache_entries = self._cache.list_cached_audio()
        cache_tracks = []
        for info in cache_entries:
            tid = info.get("id")
            if tid is None:
                continue
            title = info.get("title") or f"Track {tid}"
            artist = info.get("artist") or "Unknown artist"
            album = info.get("album")
            track = {
                "id": tid,
                "title": title,
                "artist": artist,
                "album": album,
                "album_id": info.get("album_id"),
                "cover_url": info.get("cover_url"),
            }
            cache_tracks.append(track)
        self._populate_tracks(cache_tracks, "cache")

        download_entries = self._cache.list_downloads()
        download_tracks = []
        downloads_bytes = 0
        for info in download_entries:
            tid = info.get("id")
            if tid is None:
                continue
            size = info.get("size")
            if isinstance(size, (int, float)):
                downloads_bytes += int(size)
            title = info.get("title") or f"Track {tid}"
            artist = info.get("artist") or "Unknown artist"
            album = info.get("album")
            track = {
                "id": tid,
                "title": title,
                "artist": artist,
                "album": album,
                "album_id": info.get("album_id"),
                "cover_url": info.get("cover_url"),
            }
            download_tracks.append(track)
        self._populate_tracks(download_tracks, "downloads")
        if self._cache_tab_status_label is not None:
            used = self._cache.used_bytes
            max_b = self._cache.max_bytes
            cover_count, _cover_bytes = self._cache.cover_stats()
            msg = (
                f"Tracks: {len(cache_tracks)} | Covers: {cover_count}"
                f" | {self._format_bytes(used)} / {self._format_bytes(max_b)}"
            )
            if self._cache.full:
                msg += " (full; caching disabled)"
            self._cache_tab_status_label.setText(msg)
        if self._downloads_tab_status_label is not None:
            self._downloads_tab_status_label.setText(
                f"Tracks: {len(download_tracks)} | {self._format_bytes(downloads_bytes)}"
            )
        self._update_cache_status_ui()

    def _queue_cache_tracks(self) -> None:
        self._queue_named_tracks(self._cache_tracks, "cache list")

    def _queue_downloads_tracks(self) -> None:
        self._queue_named_tracks(self._download_tracks, "downloads list")

    def _on_favorite_toggled(self, item_type: str, item_id: str, favorite: bool) -> None:
        self._favorite_toggle_worker = None
        self._set_favorite_state(item_type, item_id, favorite)
        if self.tabs.currentIndex() == 3:
            self._refresh_collection()

    def _on_favorite_toggle_error(self, msg: str) -> None:
        self._favorite_toggle_worker = None
        self._append_log(f"favorite: error {msg}")
        QtWidgets.QMessageBox.critical(self, "Favorite error", msg)

    def _queue_track_line(self, track_id: str) -> str:
        track = self._track_map_all.get(str(track_id))
        if track:
            return tidal_core.format_track_line(track)
        return f"Track {track_id}"

    def _refresh_queue_view(self) -> None:
        if self._queue_list is None:
            return
        self._queue_list.clear()
        if self._queue_now_playing_id:
            item = QtWidgets.QListWidgetItem(
                "Now: " + self._queue_track_line(self._queue_now_playing_id)
            )
            item.setData(QtCore.Qt.ItemDataRole.UserRole, self._queue_now_playing_id)
            item.setData(QtCore.Qt.ItemDataRole.UserRole + 1, "now")
            self._queue_list.addItem(item)
        for idx, tid in enumerate(self._queue_items):
            prefix = "Next: " if idx == 0 else ""
            item = QtWidgets.QListWidgetItem(prefix + self._queue_track_line(tid))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, tid)
            item.setData(QtCore.Qt.ItemDataRole.UserRole + 1, "queue")
            item.setData(QtCore.Qt.ItemDataRole.UserRole + 2, idx)
            self._queue_list.addItem(item)

    def _set_now_playing_queue(self, track_id: str) -> None:
        self._queue_now_playing_id = track_id
        if track_id in self._queue_items:
            idx = self._queue_items.index(track_id)
            self._queue_items.pop(idx)
        self._append_log(f"queue: now playing {track_id}")
        self._refresh_queue_view()

    def _queue_add_next(self, track_id: str) -> None:
        if not track_id:
            return
        self._queue_items.insert(0, track_id)
        self._append_log(f"queue: add next {track_id}")
        self._refresh_queue_view()
        self._nudge_queue_button()
        # New track at front — cancel stale prefetch and start new one
        self._cancel_prefetch()
        self._maybe_prefetch_next()

    def _queue_append(self, track_id: str) -> None:
        if not track_id:
            return
        self._queue_items.append(track_id)
        self._append_log(f"queue: append {track_id}")
        self._refresh_queue_view()
        self._nudge_queue_button()
        self._maybe_prefetch_next()

    def _queue_clear(self) -> None:
        self._queue_items = []
        self._cancel_prefetch()
        self._append_log("queue: clear")
        self._refresh_queue_view()

    def _queue_replace(self, items: List[str]) -> None:
        self._queue_items = list(items)
        self._cancel_prefetch()
        self._append_log(f"queue: replace count={len(self._queue_items)}")
        self._refresh_queue_view()
        self._nudge_queue_button()
        self._maybe_prefetch_next()

    def _queue_play_next(self) -> None:
        if not self._queue_items:
            return
        next_tid = self._queue_items.pop(0)
        self._append_log(f"queue: play next {next_tid}")
        self._refresh_queue_view()
        self._play_track_id(str(next_tid))

    def _queue_track_ids(self, tids: List[str], autoplay: bool) -> None:
        self._queue_track_batch(tids, autoplay=autoplay, label="list")

    def _queue_named_tracks(self, tracks: List[Dict[str, Any]], label: str) -> None:
        self._queue_track_batch(self._track_ids(tracks), autoplay=True, label=label)

    def _queue_track_batch(self, tids: List[str], *, autoplay: bool, label: str) -> None:
        tids = [t for t in tids if t]
        if not tids:
            return
        if autoplay and (self._play_worker is None or not self._play_worker.isRunning()):
            first, rest = tids[0], tids[1:]
            self._queue_items.extend(rest)
            self._append_log(f"queue: append {label} count={len(rest)} (autoplay first)")
            self._refresh_queue_view()
            self._nudge_queue_button()
            self._play_track_id(first)
            return
        self._queue_items.extend(tids)
        self._append_log(f"queue: append {label} count={len(tids)}")
        self._refresh_queue_view()
        self._nudge_queue_button()
        self._maybe_prefetch_next()

    def _play_track_id(self, track_id: str) -> None:
        if self._session is None and not self._is_cached_track(track_id):
            return
        dev = self.device_combo.currentText().strip()
        if not dev:
            return
        self._append_log(f"play: track_id={track_id} device={dev}")
        if self._pending_play == (track_id, dev):
            return
        if self._play_worker is not None and self._play_worker.isRunning():
            if self._current_play == (track_id, dev):
                return
            self._pending_play = (track_id, dev)
            self.status_label.setText("Status: switching track…")
            self.stop_btn.setEnabled(False)
            self._play_worker.stop()
            return
        self._start_playback(track_id, dev)

    def _on_queue_item_activated(self, item: QtWidgets.QListWidgetItem) -> None:
        if item is None:
            return
        kind = item.data(QtCore.Qt.ItemDataRole.UserRole + 1)
        if kind != "queue":
            return
        tid = item.data(QtCore.Qt.ItemDataRole.UserRole)
        idx = item.data(QtCore.Qt.ItemDataRole.UserRole + 2)
        if tid is None or idx is None:
            return
        try:
            idx = int(idx)
        except Exception:
            return
        if 0 <= idx < len(self._queue_items):
            self._queue_items = self._queue_items[idx + 1 :]
        self._append_log(f"queue: jump to {tid} idx={idx}")
        self._refresh_queue_view()
        self._play_track_id(str(tid))

    def _show_queue_context_menu(self, pos: QtCore.QPoint) -> None:
        if self._queue_list is None:
            return
        item = self._queue_list.itemAt(pos)
        track = self._track_for_item(item)
        menu = QtWidgets.QMenu(self._queue_list)

        if track is None:
            clear_action = QtGui.QAction("Clear queue", self)
            clear_action.setEnabled(bool(self._queue_items))
            clear_action.triggered.connect(self._queue_clear)

            play_next_action = QtGui.QAction("Play next", self)
            play_next_action.setEnabled(bool(self._queue_items))
            play_next_action.triggered.connect(self._queue_play_next)

            menu.addAction(play_next_action)
            menu.addSeparator()
            menu.addAction(clear_action)
        else:
            remove_action = QtGui.QAction("Remove from queue", self)
            kind = item.data(QtCore.Qt.ItemDataRole.UserRole + 1) if item else None
            remove_action.setEnabled(bool(track and track.get("id") and kind == "queue"))
            clear_action = QtGui.QAction("Clear queue", self)
            clear_action.setEnabled(bool(self._queue_items))
            clear_action.triggered.connect(self._queue_clear)

            def do_remove() -> None:
                tid = track.get("id") if track else None
                if tid is None:
                    return
                if item is None:
                    return
                idx = item.data(QtCore.Qt.ItemDataRole.UserRole + 2)
                try:
                    idx = int(idx)
                except Exception:
                    return
                if 0 <= idx < len(self._queue_items):
                    self._queue_items.pop(idx)
                self._refresh_queue_view()
                self._reconcile_prefetch_target()

            remove_action.triggered.connect(do_remove)
            menu.addAction(remove_action)
            menu.addAction(clear_action)
            menu.addSeparator()
            self._populate_track_menu(menu, track, item)

        view = self._queue_list.viewport()
        if view is not None and view.rect().contains(pos):
            global_pos = view.mapToGlobal(pos)
        else:
            global_pos = self._queue_list.mapToGlobal(pos)
        menu.exec(global_pos)

    def _track_for_item(self, item: Optional[QtWidgets.QListWidgetItem]) -> Optional[Dict[str, Any]]:
        if item is None:
            return None
        tid = item.data(QtCore.Qt.ItemDataRole.UserRole)
        return self._track_map_all.get(str(tid)) if tid is not None else None

    def _copy_to_clipboard(self, text: str) -> None:
        QtWidgets.QApplication.clipboard().setText(text)

    def _track_ids(self, tracks: Any) -> List[str]:
        if not isinstance(tracks, list):
            return []
        return [str(track.get("id")) for track in tracks if isinstance(track, dict) and track.get("id")]

    def _populate_track_menu(
        self,
        menu: QtWidgets.QMenu,
        track: Optional[Dict[str, Any]],
        item: Optional[QtWidgets.QListWidgetItem],
        *,
        widget: Optional[QtWidgets.QListWidget] = None,
        allow_download: bool = False,
    ) -> None:
        # Keep this in one place so it's easy to expand with new actions later.
        play_action = QtGui.QAction("Play", self)
        play_next_action = QtGui.QAction("Play next", self)
        play_radio_action = QtGui.QAction("Play radio", self)
        queue_radio_action = QtGui.QAction("Queue radio", self)
        favorite_action = QtGui.QAction("Favorite", self)
        append_action = QtGui.QAction("Append to queue", self)
        copy_track = QtGui.QAction("Copy track link", self)
        open_album = QtGui.QAction("Open album", self)
        open_artist = QtGui.QAction("Open artist", self)
        download_track = QtGui.QAction("Download track", self)
        has_track = bool(track and track.get("id"))
        copy_track.setEnabled(has_track)
        play_action.setEnabled(has_track)
        play_next_action.setEnabled(has_track)
        play_radio_action.setEnabled(has_track)
        queue_radio_action.setEnabled(has_track)
        append_action.setEnabled(has_track)
        has_album = bool(track and track.get("album_id"))
        open_album.setEnabled(has_album)
        has_artist = bool(track and track.get("artist_id"))
        open_artist.setEnabled(has_artist)
        download_track.setEnabled(has_track and allow_download)
        favorite_action.setEnabled(has_track)
        storage = self._track_storage_status(str(track.get("id")) if track else "")
        if storage:
            download_track.setText("Delete track")
            download_track.setEnabled(has_track)
        elif self._session is None:
            download_track.setEnabled(False)
        if has_track and self._is_favorite_item("track", str(track.get("id"))):
            favorite_action.setText("Unfavorite")

        def do_play() -> None:
            if item is None:
                return
            if widget is not None:
                widget.setCurrentItem(item)
                self._play_selected()
                return
            tid = track.get("id") if track else None
            if tid is None:
                return
            self._play_track_id(str(tid))

        def do_play_next() -> None:
            tid = track.get("id") if track else None
            if tid is None:
                return
            self._queue_add_next(str(tid))

        def do_play_radio_next() -> None:
            tid = track.get("id") if track else None
            if tid is None:
                return
            self._play_radio_next(str(tid))

        def do_queue_radio() -> None:
            tid = track.get("id") if track else None
            if tid is None:
                return
            self._queue_radio_append(str(tid))

        def do_append() -> None:
            tid = track.get("id") if track else None
            if tid is None:
                return
            self._queue_append(str(tid))

        def do_favorite() -> None:
            tid = track.get("id") if track else None
            if tid is None:
                return
            tid_str = str(tid)
            self._toggle_item_favorite("track", tid_str, not self._is_favorite_item("track", tid_str))

        def do_copy_track() -> None:
            tid = track.get("id") if track else None
            url = self._tidal_url("track", tid)
            if not url:
                return
            self._copy_to_clipboard(url)

        def do_open_album() -> None:
            album_id = track.get("album_id") if track else None
            self._open_tidal_item("album", album_id)

        def do_open_artist() -> None:
            artist_id = track.get("artist_id") if track else None
            self._open_tidal_item("artist", artist_id)

        def do_download() -> None:
            tid = track.get("id") if track else None
            if tid is None:
                return
            tid_str = str(tid)
            if self._track_storage_status(tid_str):
                self._delete_download_track(tid_str)
            else:
                self._download_track(tid_str)

        play_action.triggered.connect(do_play)
        play_next_action.triggered.connect(do_play_next)
        play_radio_action.triggered.connect(do_play_radio_next)
        queue_radio_action.triggered.connect(do_queue_radio)
        append_action.triggered.connect(do_append)
        favorite_action.triggered.connect(do_favorite)
        copy_track.triggered.connect(do_copy_track)
        open_album.triggered.connect(do_open_album)
        open_artist.triggered.connect(do_open_artist)
        download_track.triggered.connect(do_download)
        menu.addAction(play_action)
        menu.addAction(play_next_action)
        menu.addAction(append_action)
        menu.addSeparator()
        menu.addAction(play_radio_action)
        menu.addAction(queue_radio_action)
        menu.addSeparator()
        menu.addAction(favorite_action)
        menu.addAction(copy_track)
        menu.addAction(open_album)
        menu.addAction(open_artist)
        if allow_download:
            menu.addSeparator()
            menu.addAction(download_track)

    def _show_track_context_menu(self, widget: QtWidgets.QListWidget, pos: QtCore.QPoint) -> None:
        item = widget.itemAt(pos)
        track = self._track_for_item(item)
        menu = QtWidgets.QMenu(self)
        self._populate_track_menu(menu, track, item, widget=widget, allow_download=True)
        menu.exec(widget.mapToGlobal(pos))

    def _track_storage_status(self, track_id: str) -> Optional[str]:
        if self._cache.has_download(track_id):
            return "download"
        return None

    def _download_track(self, track_id: str) -> None:
        if self._session is None:
            return
        track = self._track_map_all.get(str(track_id))
        if track is None:
            return

        cover_bytes = self._cover_cache.get(str(track_id))
        if cover_bytes is None:
            cover_url = track.get("cover_url")
            if cover_url and cover_url in self._cover_url_cache:
                cover_bytes = self._cover_url_cache[cover_url]

        promoted = self._cache.promote_cache_to_download(str(track_id), track)
        if promoted:
            tag_flac_path(promoted, track, cover_bytes)
            self.status_label.setText("Status: download saved")
            self._refresh_cache_tab()
            self._update_cache_status_ui()
            return

        if self._download_worker is not None and self._download_worker.isRunning():
            QtWidgets.QMessageBox.warning(
                self, "Download in progress", "Another download is already running."
            )
            return

        worker = DownloadWorker(self._session, str(track_id), self._cache, track, cover_bytes)
        worker.status.connect(lambda s: self.status_label.setText(f"Status: {s}"))
        worker.log.connect(self._append_log)
        worker.error.connect(self._on_download_error)
        worker.finished.connect(self._on_download_finished)
        self._download_worker = worker
        worker.start()

    def _delete_download_track(self, track_id: str) -> None:
        removed = self._cache.delete_download(track_id)
        if removed:
            self._cache.refresh_usage()
            self._refresh_cache_tab()
            self._update_cache_status_ui()

    def _on_download_error(self, msg: str) -> None:
        self.status_label.setText("Status: error")
        QtWidgets.QMessageBox.critical(self, "Download error", msg)
        self._download_worker = None

    def _on_download_finished(self, path: str) -> None:
        self.status_label.setText("Status: download saved")
        self._download_worker = None
        self._refresh_cache_tab()
        self._update_cache_status_ui()

    def _start_radio_request(
        self,
        worker_attr: str,
        mode: str,
        log_message: str,
        fn: Callable[[], List[Dict[str, Any]]],
    ) -> None:
        worker = getattr(self, worker_attr)
        if worker is not None and worker.isRunning():
            return
        self.status_label.setText("Status: loading radio…")
        self._append_log(log_message)
        self._radio_mode = mode
        worker = CallWorker(fn)
        worker.ready.connect(self._on_radio_ready)
        worker.error.connect(self._on_radio_error)
        setattr(self, worker_attr, worker)
        worker.start()

    def _play_radio_next(self, track_id: str) -> None:
        if self._session is None:
            return
        session = self._session
        self._start_radio_request(
            "_radio_worker",
            "play",
            f"radio: request track_id={track_id}",
            lambda session=session, track_id=track_id: tidal_core.track_radio(
                session,
                track_id,
                limit=30,
            ),
        )

    def _queue_radio_append(self, track_id: str) -> None:
        if self._session is None:
            return
        session = self._session
        self._start_radio_request(
            "_radio_worker",
            "queue",
            f"radio: queue request track_id={track_id}",
            lambda session=session, track_id=track_id: tidal_core.track_radio(
                session,
                track_id,
                limit=30,
            ),
        )

    def _play_artist_radio(self, artist_id: str) -> None:
        if self._session is None:
            return
        session = self._session
        self._start_radio_request(
            "_artist_radio_worker",
            "play",
            f"radio: request artist_id={artist_id}",
            lambda session=session, artist_id=artist_id: tidal_core.artist_radio(
                session,
                artist_id,
                limit=30,
            ),
        )

    def _queue_artist_radio(self, artist_id: str) -> None:
        if self._session is None:
            return
        session = self._session
        self._start_radio_request(
            "_artist_radio_worker",
            "queue",
            f"radio: queue request artist_id={artist_id}",
            lambda session=session, artist_id=artist_id: tidal_core.artist_radio(
                session,
                artist_id,
                limit=30,
            ),
        )

    def _on_radio_ready(self, tracks: List[Dict[str, Any]]) -> None:
        self.status_label.setText("Status: ready")
        self._radio_worker = None
        self._artist_radio_worker = None
        if not tracks:
            self._append_log("radio: empty result")
            return
        for t in tracks:
            tid = t.get("id")
            if tid is not None:
                self._track_map_all[str(tid)] = t
        ids = [str(t["id"]) for t in tracks if t.get("id") is not None]
        if not ids:
            self._append_log("radio: no valid track ids")
            return
        current_id = self._current_play[0] if self._current_play is not None else None
        if current_id:
            ids = [t for t in ids if t != str(current_id)]
        if not ids:
            self._append_log("radio: no tracks after filtering current")
            return

        if self._radio_mode == "queue":
            if self._play_worker is not None and self._play_worker.isRunning():
                self._queue_items.extend(ids)
                self._append_log(f"radio: queued count={len(ids)}")
                self._refresh_queue_view()
                self._nudge_queue_button()
                return
            first, rest = ids[0], ids[1:]
            self._queue_items.extend(rest)
            self._append_log(f"radio: queued count={len(rest)} (autoplay first)")
            self._refresh_queue_view()
            self._nudge_queue_button()
            self._play_track_id(first)
            return

        if self._play_worker is not None and self._play_worker.isRunning():
            self._queue_replace(ids)
            return
        first, rest = ids[0], ids[1:]
        self._queue_replace(rest)
        self._play_track_id(first)

    def _on_radio_error(self, msg: str) -> None:
        self.status_label.setText("Status: error")
        self._radio_worker = None
        self._artist_radio_worker = None
        QtWidgets.QMessageBox.critical(self, "Radio error", msg)

    # ── Home tab ──────────────────────────────────────────────────────────────

    def _load_home(self) -> None:
        if self._session is None:
            return
        if self._home_worker is not None and self._home_worker.isRunning():
            return
        self.home_list.clear()
        self._home_tracks = []
        placeholder = QtWidgets.QTreeWidgetItem(self.home_list, ["Loading"])
        placeholder.setData(0, QtCore.Qt.ItemDataRole.UserRole, "loading_placeholder")
        self._home_loading_placeholder = placeholder
        self._register_loading_item(placeholder)
        self.status_label.setText("Status: loading home feed…")
        session = self._session
        worker = CallWorker(lambda session=session: tidal_core.home_page(session))
        worker.ready.connect(self._on_home_ready)
        worker.error.connect(self._on_home_error)
        worker.finished.connect(lambda: setattr(self, "_home_worker", None))
        self._home_worker = worker
        worker.start()

    def _on_home_ready(self, sections: List[Dict[str, Any]]) -> None:
        self._home_worker = None
        self.status_label.setText("Status: ready")
        self._unregister_loading_item(self._home_loading_placeholder)
        self._home_loading_placeholder = None
        self.home_list.clear()
        self._home_tracks = []
        if not sections:
            empty = QtWidgets.QTreeWidgetItem(self.home_list, ["Nothing to show"])
            empty.setData(0, QtCore.Qt.ItemDataRole.UserRole, "empty")
            return
        for section in sections:
            title = section.get("title") or "—"
            items = section.get("items") or []
            group = QtWidgets.QTreeWidgetItem(self.home_list, [title])
            group.setData(0, QtCore.Qt.ItemDataRole.UserRole, "group")
            for entry in items:
                kind = entry.get("type")
                data = entry.get("data") or {}
                if kind == "track":
                    self._add_track_item(group, data, self._home_tracks)
                elif kind in ("album", "playlist", "mix"):
                    self._add_home_container_item(group, kind, data)
            group.setExpanded(True)
        self._start_cover_prefetch()

    def _add_home_container_item(
        self, parent: QtWidgets.QTreeWidgetItem, kind: str, data: Dict[str, Any]
    ) -> None:
        if kind == "album":
            label = tidal_core.format_album_line(data)
            item_id = data.get("id") or data.get("album_id")
            state_role = QtCore.Qt.ItemDataRole.UserRole + 3
            items_dict = self._album_items
            placeholder_kind = "album_placeholder"
        elif kind == "mix":
            label = tidal_core.format_mix_line(data)
            item_id = data.get("id")
            state_role = QtCore.Qt.ItemDataRole.UserRole + 3
            items_dict = self._mix_items
            placeholder_kind = "mix_placeholder"
        else:  # playlist
            label = f"Playlist — {data.get('title', '?')}"
            item_id = data.get("id")
            state_role = QtCore.Qt.ItemDataRole.UserRole + 3
            items_dict = self._playlist_home_items
            placeholder_kind = "playlist_placeholder"

        container = QtWidgets.QTreeWidgetItem(parent, [label])
        container.setData(0, QtCore.Qt.ItemDataRole.UserRole, kind)
        container.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, data)
        container.setData(0, state_role, "pending")
        ph = QtWidgets.QTreeWidgetItem(container, ["Expand to load tracks"])
        ph.setData(0, QtCore.Qt.ItemDataRole.UserRole, placeholder_kind)
        if item_id:
            lst = items_dict.setdefault(str(item_id), [])
            if container not in lst:
                lst.append(container)

    def _on_home_error(self, msg: str) -> None:
        self._home_worker = None
        self.status_label.setText("Status: error")
        self._unregister_loading_item(self._home_loading_placeholder)
        self._home_loading_placeholder = None
        self.home_list.clear()
        err = QtWidgets.QTreeWidgetItem(self.home_list, [f"Failed to load home feed: {msg}"])
        err.setData(0, QtCore.Qt.ItemDataRole.UserRole, "empty")

    def _ensure_mix_loaded(self, item: QtWidgets.QTreeWidgetItem) -> None:
        if self._session is None or self._offline_mode:
            return
        state = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 3)
        if state in ("loading", "loaded"):
            return
        payload = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1) or {}
        mix_id = payload.get("id")
        self._start_keyed_track_load(
            item,
            mix_id,
            self._mix_tracks_workers,
            self._mix_items,
            tidal_core.mix_tracks,
            self._on_mix_tracks_ready,
        )

    def _on_mix_tracks_ready(self, mix_id: str, tracks: List[Dict[str, Any]]) -> None:
        self._apply_loaded_tracks(self._mix_items, mix_id, tracks)

    def _ensure_playlist_loaded(self, item: QtWidgets.QTreeWidgetItem) -> None:
        if self._session is None or self._offline_mode:
            return
        state = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 3)
        if state in ("loading", "loaded"):
            return
        payload = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1) or {}
        if payload.get("tracks"):
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 3, "loaded")
            return
        playlist_id = payload.get("id")
        self._start_keyed_track_load(
            item,
            playlist_id,
            self._playlist_tracks_workers,
            self._playlist_home_items,
            tidal_core.playlist_tracks,
            self._on_playlist_tracks_ready,
        )

    def _on_playlist_tracks_ready(self, playlist_id: str, tracks: List[Dict[str, Any]]) -> None:
        self._apply_loaded_tracks(self._playlist_home_items, playlist_id, tracks)

    def _populate_mix_menu(self, menu: QtWidgets.QMenu, mix: Dict[str, Any]) -> None:
        play_action = QtGui.QAction("Play mix", self)
        queue_action = QtGui.QAction("Queue mix", self)
        track_ids = self._track_ids(mix.get("tracks"))
        has_tracks = bool(track_ids)
        play_action.setEnabled(has_tracks)
        queue_action.setEnabled(has_tracks)
        if not has_tracks:
            hint = QtGui.QAction("Expand to load tracks first", self)
            hint.setEnabled(False)
            menu.addAction(hint)

        def do_play() -> None:
            self._queue_track_ids(track_ids, autoplay=True)

        def do_queue() -> None:
            self._queue_track_ids(track_ids, autoplay=False)

        play_action.triggered.connect(do_play)
        queue_action.triggered.connect(do_queue)
        menu.addAction(play_action)
        menu.addAction(queue_action)

    def _populate_library_menu(self, menu: QtWidgets.QMenu, kind: str, payload: Dict[str, Any]) -> None:
        item_id = payload.get("album_id") or payload.get("id") if kind == "album" else payload.get("id")
        item_id_str = str(item_id) if item_id is not None else ""
        track_ids = self._track_ids(payload.get("tracks"))

        play_action = QtGui.QAction(f"Play {kind}", self)
        queue_action = QtGui.QAction(f"Queue {kind}", self)
        favorite_action = QtGui.QAction("Favorite", self)
        copy_action = QtGui.QAction(f"Copy {kind} link", self)
        open_action = QtGui.QAction(f"Open {kind}", self)

        has_tracks = bool(item_id_str and track_ids)
        play_action.setEnabled(has_tracks)
        queue_action.setEnabled(has_tracks)
        favorite_action.setEnabled(bool(item_id_str))
        copy_action.setEnabled(bool(item_id_str))
        open_action.setEnabled(bool(item_id_str))
        if self._is_favorite_item(kind, item_id_str):
            favorite_action.setText("Unfavorite")

        def do_play() -> None:
            self._queue_track_ids(track_ids, autoplay=True)

        def do_queue() -> None:
            self._queue_track_ids(track_ids, autoplay=False)

        def do_favorite() -> None:
            self._toggle_item_favorite(kind, item_id_str, not self._is_favorite_item(kind, item_id_str))

        def do_copy() -> None:
            url = self._tidal_url(kind, item_id_str)
            if not url:
                return
            self._copy_to_clipboard(url)

        def do_open() -> None:
            self._open_tidal_item(kind, item_id_str)

        play_action.triggered.connect(do_play)
        queue_action.triggered.connect(do_queue)
        favorite_action.triggered.connect(do_favorite)
        copy_action.triggered.connect(do_copy)
        open_action.triggered.connect(do_open)

        menu.addAction(play_action)
        menu.addAction(queue_action)

        if kind == "artist":
            play_radio_action = QtGui.QAction("Play radio", self)
            queue_radio_action = QtGui.QAction("Queue radio", self)
            play_radio_action.setEnabled(bool(item_id_str))
            queue_radio_action.setEnabled(bool(item_id_str))
            play_radio_action.triggered.connect(lambda: self._play_artist_radio(item_id_str))
            queue_radio_action.triggered.connect(lambda: self._queue_artist_radio(item_id_str))
            menu.addSeparator()
            menu.addAction(play_radio_action)
            menu.addAction(queue_radio_action)

        menu.addSeparator()
        menu.addAction(favorite_action)
        menu.addSeparator()
        menu.addAction(copy_action)
        menu.addAction(open_action)

    # ── end Home tab ──────────────────────────────────────────────────────────

    def _on_selection_changed(self, _current, _previous) -> None:
        self._load_cover_for_selected()
        self._update_open_album_btn()

    def _on_tree_item_activated(self, item: QtWidgets.QTreeWidgetItem, _column: int) -> None:
        kind = self._tree_item_kind(item)
        if kind in ("album", "playlist", "artist"):
            # Native QTreeWidget double-click already toggles expand/collapse,
            # so don't toggle again here.
            return
        if kind == "track":
            self._play_selected()

    def _show_tree_context_menu(self, tree: QtWidgets.QTreeWidget, pos: QtCore.QPoint) -> None:
        item = tree.itemAt(pos)
        kind = self._tree_item_kind(item)
        payload = self._tree_item_payload(item)
        menu = QtWidgets.QMenu(self)
        if kind == "top_tracks_group" and payload:
            play_action = QtGui.QAction("Play top tracks", self)
            queue_action = QtGui.QAction("Queue top tracks", self)
            tracks = payload if isinstance(payload, list) else []
            tids = [str(t.get("id")) for t in tracks if isinstance(t, dict) and t.get("id")]
            play_action.setEnabled(bool(tids))
            queue_action.setEnabled(bool(tids))

            def do_play() -> None:
                if not tids:
                    return
                self._queue_replace(tids[1:])
                self._play_track_id(tids[0])

            def do_queue() -> None:
                self._queue_track_ids(tids, autoplay=False)

            play_action.triggered.connect(do_play)
            queue_action.triggered.connect(do_queue)
            menu.addAction(play_action)
            menu.addAction(queue_action)
            menu.exec(tree.viewport().mapToGlobal(pos))
            return
        elif kind in ("album", "playlist", "artist") and payload:
            self._populate_library_menu(menu, kind, payload)
        elif kind == "mix" and payload:
            self._populate_mix_menu(menu, payload)
        elif kind == "track" and payload:
            self._populate_track_menu(menu, payload, None, widget=None, allow_download=True)
        else:
            return
        menu.exec(tree.viewport().mapToGlobal(pos))

    def _on_tab_changed(self, _index: int) -> None:
        self._load_cover_for_selected()
        self._update_open_album_btn()
        if self.tabs.currentIndex() == 3 and not self._favorite_tracks:
            self._refresh_collection()
        if self.tabs.currentIndex() == 4:
            self._refresh_cache_tab()

    def _cover_url_for_track_id(self, track_id: str) -> Optional[str]:
        track = self._track_map_all.get(track_id)
        if not track:
            return None
        return track.get("cover_url")

    def _active_tracks(self) -> List[Dict[str, Any]]:
        idx = self.tabs.currentIndex()
        if idx == 0:
            return self._home_tracks
        if idx == 1:
            return self._search_tracks
        if idx == 2:
            return self._url_tracks
        if idx == 4:
            active = self._cache_active_list()
            return self._download_tracks if active is self.downloads_list else self._cache_tracks
        return self._favorite_tracks

    def _set_cover_request(self, request_id: str, cover_url: Optional[str]) -> None:
        self._cover_request_id = request_id
        self._cover_request_url = cover_url

    def _cache_cover_data(
        self,
        request_id: str,
        cover_url: Optional[str],
        data: Optional[bytes],
        *,
        persist: bool = True,
    ) -> None:
        if not data:
            return
        self._cover_cache[request_id] = data
        if cover_url:
            self._cover_url_cache[cover_url] = data
            if persist:
                self._cache.store_cover_bytes(cover_url, data)
                self._update_cache_status_ui()

    def _cached_cover_data(self, request_id: str, cover_url: Optional[str]) -> Optional[bytes]:
        cached = self._cover_cache.get(request_id)
        if cached is not None:
            self._append_log(f"cover: cache hit request={request_id}")
            return cached
        if cover_url and cover_url in self._cover_url_cache:
            self._append_log(f"cover: url cache hit request={request_id}")
            data = self._cover_url_cache[cover_url]
            self._cover_cache[request_id] = data
            return data
        if cover_url:
            disk = self._cache.get_cover_bytes(cover_url)
            if disk:
                self._append_log(f"cover: disk cache hit request={request_id}")
                self._cache_cover_data(request_id, cover_url, disk, persist=False)
                return disk
        return None

    def _start_cover_request(
        self,
        request_id: str,
        cover_url: Optional[str],
        *,
        force: bool,
    ) -> None:
        cached = self._cached_cover_data(request_id, cover_url)
        if cached is not None:
            self._set_cover_request(request_id, cover_url)
            self._set_cover_bytes(cached)
            return
        if self._session is None:
            return
        if not force and self._cover_request_id == request_id and self._cover_bytes is not None:
            return
        self._set_cover_request(request_id, cover_url)
        if self._cover_worker is not None and self._cover_worker.isRunning():
            self._cover_worker.stop()
        self._set_cover_bytes(None)
        worker = CoverWorker(self._session, request_id, cover_url)
        worker.ready.connect(self._on_cover_loaded)
        worker.log.connect(self._append_log)
        worker.finished.connect(lambda: self._on_cover_worker_finished(worker))
        self._cover_worker = worker
        worker.start()

    def _load_cover_for_selected(self) -> None:
        if self._session is None and self.tabs.currentIndex() != 4:
            return
        tid = self._selected_track_id()
        if tid is not None:
            if self._play_worker is not None and self._play_worker.isRunning():
                if self._current_play is not None and tid != self._current_play[0]:
                    return
            self._start_cover_request(tid, self._cover_url_for_track_id(tid), force=False)
            return
        if self._play_worker is not None and self._play_worker.isRunning():
            return
        request_id, cover_url = self._selected_album_cover()
        if not request_id or not cover_url:
            return
        self._start_cover_request(request_id, cover_url, force=False)

    def _play_next_selected(self) -> None:
        self._queue_play_next()

    def _play_selected(self) -> None:
        tid = self._selected_track_id()
        if tid is None:
            return
        self._play_track_id(tid)

    def _start_playback(self, tid: str, dev: str) -> None:
        self._cancel_pending_seek()
        self._play_had_error = False
        self._stopped_by_user = False
        self._stream_info = None
        self._audio_fmt = None
        self._decode_path = None
        self.quality_label.setText("Quality: —")
        self.bitrate_label.setText("Bitrate: —")
        self.bitperfect_label.setText("Bit-perfect: —")
        self.pause_btn.setText("Pause")
        self._duration_s = 0.0
        self._pos_s = 0.0
        self._seeking = False
        self.seek_slider.setEnabled(False)
        self.seek_slider.setRange(0, 0)
        self.seek_time.setText("0:00 / 0:00")
        self._set_now_playing(self._track_map_all.get(str(tid)))
        self._set_now_playing_queue(str(tid))
        self._load_lyrics_for_track(str(tid))

        # Update Discord RPC / MPRIS immediately when track starts (quality info will be added later)
        if self._now_playing_track:
            self._update_discord_track(self._now_playing_track, None)
            self._update_mpris_track(self._now_playing_track, None)

        self.stop_btn.setEnabled(True)
        self.pause_btn.setEnabled(True)
        self.status_label.setText("Status: starting playback…")
        self._current_play = (tid, dev)
        self._cancel_prefetch()
        self._play_worker = PlaybackWorker(
            self._session,
            tid,
            dev,
            disable_ffmpeg=self._disable_ffmpeg,
            cache_manager=self._cache,
            track_meta=self._track_map_all.get(str(tid)),
            gapless=self._gapless_enabled,
        )
        self._play_worker.status.connect(lambda s: self.status_label.setText(f"Status: {s}"))
        self._play_worker.log.connect(self._append_log)
        self._play_worker.error.connect(self._on_playback_error)
        self._play_worker.fmt_ready.connect(self._on_fmt_ready)
        self._play_worker.stream_info.connect(self._on_stream_info)
        self._play_worker.decode_path.connect(self._on_decode_path)
        self._play_worker.position.connect(self._on_position)
        self._play_worker.finished_ok.connect(self._on_playback_done)
        self._play_worker.finished.connect(self._on_playback_thread_finished)
        self._play_worker.cache_write.connect(self._on_cache_write)
        self._play_worker.track_advanced.connect(self._on_track_advanced)
        self._start_cover_request(tid, self._cover_url_for_track_id(tid), force=True)
        self._play_worker.start()
        self._maybe_prefetch_next()

    def _on_stream_info(self, info: StreamInfo) -> None:
        self._stream_info = info
        parts = []
        if info.audio_quality:
            parts.append(f"{info.audio_quality}")
        if info.bit_depth and info.sample_rate:
            parts.append(f"{info.bit_depth}-bit/{info.sample_rate} Hz")
        self.quality_label.setText("Quality: " + (" ".join(parts) if parts else "—"))
        self._update_bitperfect_label()
        self._update_bitrate_label()
        # Update Discord RPC / MPRIS with quality info
        if self._now_playing_track:
            self._update_discord_track(self._now_playing_track, info)
            self._update_mpris_track(self._now_playing_track, info)

    def _on_fmt_ready(self, fmt: AudioFormat) -> None:
        self._audio_fmt = fmt
        self._update_bitperfect_label()
        self._update_bitrate_label()
        self._update_cache_status_ui()

    def _on_decode_path(self, path: str) -> None:
        self._decode_path = path
        self._update_bitperfect_label()

    def _update_bitrate_label(self) -> None:
        if self._audio_fmt is None:
            self.bitrate_label.setText("Bitrate: —")
            return
        out = self._audio_fmt
        out_kbps = (out.channels * out.rate * out.bits) / 1000.0
        s = f"Bitrate: output PCM {out_kbps:.0f} kbps"
        if self._stream_info is not None and self._stream_info.sample_rate and self._stream_info.bit_depth:
            si = self._stream_info
            stream_kbps = (out.channels * si.sample_rate * si.bit_depth) / 1000.0
            s = f"Bitrate: stream ~{stream_kbps:.0f} kbps | output PCM {out_kbps:.0f} kbps"
        self.bitrate_label.setText(s)

    def _update_bitperfect_label(self) -> None:
        dev = self.device_combo.currentText().strip()
        is_bitperfect_active = False

        if not dev:
            self.bitperfect_label.setText("Bit-perfect: —")
        elif not dev.startswith("hw:"):
            self.bitperfect_label.setText("Bit-perfect: unlikely (not hw:)")
        elif self._stream_info is None or self._audio_fmt is None:
            self.bitperfect_label.setText("Bit-perfect: unknown (stream/format pending)")
        else:
            decode_note = ""
            if self._decode_path:
                decode_note = f" | {self._decode_path}"
            si = self._stream_info
            af = self._audio_fmt
            is_match = bool(si.sample_rate and si.bit_depth and af.rate == si.sample_rate and af.bits == si.bit_depth)
            is_bitperfect = bool(is_match)
            if is_bitperfect:
                self.bitperfect_label.setText("Bit-perfect: yes" + decode_note)
                is_bitperfect_active = True
            elif si.sample_rate and af.rate != si.sample_rate:
                self.bitperfect_label.setText(
                    f"Bit-perfect: no ({af.rate}Hz != {si.sample_rate}Hz){decode_note}"
                )
            elif si.bit_depth and af.bits != si.bit_depth:
                if si.bit_depth == 24 and af.bits == 32:
                    self.bitperfect_label.setText(
                        f"Bit-perfect: padded (24/32 PCM){decode_note}"
                    )
                else:
                    self.bitperfect_label.setText(
                        f"Bit-perfect: no ({af.bits}-bit != {si.bit_depth}-bit){decode_note}"
                    )
            else:
                self.bitperfect_label.setText("Bit-perfect: likely" + decode_note)
                is_bitperfect_active = True

        # Enable/disable volume slider based on bitperfect mode
        self.volume_slider.setEnabled(not is_bitperfect_active)
        self.volume_label.setEnabled(not is_bitperfect_active)

    def _set_cover_bytes(self, data: Optional[bytes]) -> None:
        self._cover_bytes = data
        self.cover_label.set_bytes(data)

    def _on_cover_loaded(self, track_id: str, data: Optional[bytes]) -> None:
        if track_id != self._cover_request_id:
            return
        cover_url = self._cover_url_for_track_id(track_id)
        if cover_url is None and track_id == self._cover_request_id:
            cover_url = self._cover_request_url
        self._cache_cover_data(track_id, cover_url, data)
        self._set_cover_bytes(data)

    def _on_cover_worker_finished(self, worker: CoverWorker) -> None:
        if self._cover_worker is worker:
            self._cover_worker = None

    def _start_cover_prefetch(self) -> None:
        if self._session is None:
            return
        tracks = self._active_tracks()
        if not tracks:
            return
        limit = self._cover_prefetch_max
        if self.tabs.currentIndex() == 1:
            limit = min(limit, int(self.search_limit.value()))
        items: List[tuple[str, Optional[str]]] = []
        for t in tracks[:limit]:
            tid = t.get("id")
            if tid is None:
                continue
            tid_str = str(tid)
            if tid_str in self._cover_cache:
                continue
            cover_url = t.get("cover_url")
            if cover_url and cover_url in self._cover_url_cache:
                data = self._cover_url_cache[cover_url]
                self._cache_cover_data(tid_str, cover_url, data, persist=False)
                if tid_str == self._cover_request_id:
                    self._set_cover_bytes(data)
                continue
            if cover_url:
                disk = self._cache.get_cover_bytes(cover_url)
                if disk:
                    self._cache_cover_data(tid_str, cover_url, disk, persist=False)
                    if tid_str == self._cover_request_id:
                        self._set_cover_bytes(disk)
                    continue
            items.append((tid_str, cover_url))

        if not items:
            return
        if self._prefetch_worker is not None and self._prefetch_worker.isRunning():
            self._prefetch_worker.stop()
        worker = CoverPrefetchWorker(self._session, items)
        worker.ready.connect(self._on_cover_prefetched)
        worker.log.connect(self._append_log)
        worker.finished.connect(lambda: self._on_prefetch_worker_finished(worker))
        self._prefetch_worker = worker
        worker.start()

    def _on_cover_prefetched(self, track_id: str, cover_url: Optional[str], data: Optional[bytes]) -> None:
        if not data:
            return
        self._cache_cover_data(track_id, cover_url, data)
        if track_id == self._cover_request_id:
            self._set_cover_bytes(data)

    def _on_prefetch_worker_finished(self, worker: CoverPrefetchWorker) -> None:
        if self._prefetch_worker is worker:
            self._prefetch_worker = None

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)

    def _set_now_playing(self, track: Optional[Dict[str, Any]]) -> None:
        if not track:
            self._now_playing_track = None
            self.now_title.setText("Nothing playing")
            self.now_meta.setText("—")
            return
        self._now_playing_track = track
        title = track.get("title") or "Unknown title"
        artist = track.get("artist") or "Unknown artist"
        album = track.get("album") or ""
        self.now_title.setText(title)
        self.now_meta.setText(f"{artist} - {album}" if album else artist)

    def _load_lyrics_for_track(self, track_id: Optional[str]) -> None:
        tid = str(track_id) if track_id is not None else None
        self._lyrics_request_id = tid
        if not tid:
            self._set_lyrics_content(
                None,
                "Lyrics will appear for the currently playing track.",
                muted=True,
            )
            return
        cached = self._lyrics_cache.get(tid)
        if cached is not None:
            self._apply_lyrics_payload(tid, cached)
            return
        if self._session is None or self._offline_mode:
            self._set_lyrics_content(tid, "Lyrics unavailable while offline.", muted=True)
            return
        self._set_lyrics_content(tid, "Loading lyrics…", muted=True)
        if self._lyrics_worker is not None and self._lyrics_worker.isRunning():
            return
        self._start_lyrics_request(tid)

    def _start_lyrics_request(self, track_id: str) -> None:
        if self._session is None or self._offline_mode:
            return
        session = self._session
        self._append_log(f"lyrics: request track_id={track_id}")
        worker = CallWorker(
            lambda session=session, track_id=track_id: tidal_core.track_lyrics(session, track_id)
        )
        worker.ready.connect(self._on_lyrics_ready)
        worker.error.connect(self._on_lyrics_error)
        worker.finished.connect(lambda: self._on_lyrics_worker_finished(worker))
        self._lyrics_worker = worker
        worker.start()

    def _apply_lyrics_payload(self, track_id: str, payload: Dict[str, Any]) -> None:
        provider = payload.get("provider")
        rtl = bool(payload.get("right_to_left"))
        text = (payload.get("text") or "").strip()
        timed_lines = payload.get("timed_lines")
        normalized_timed_lines = self._normalize_timed_lyrics(timed_lines)
        if payload.get("error"):
            self._set_lyrics_content(
                track_id,
                "Lyrics unavailable right now.",
                provider=provider,
                rtl=rtl,
                muted=True,
            )
            return
        if not text and not normalized_timed_lines:
            self._set_lyrics_content(
                track_id,
                "No lyrics published for this track.",
                provider=provider,
                rtl=rtl,
                muted=True,
            )
            return
        if not text:
            text = "\n".join(
                str(line["text"]).strip()
                for line in normalized_timed_lines
            ).strip()
        self._set_lyrics_content(
            track_id,
            text,
            provider=provider,
            rtl=rtl,
            timed_lines=normalized_timed_lines,
        )

    def _on_lyrics_ready(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        track_id = payload.get("track_id")
        if track_id is None:
            return
        tid = str(track_id)
        if payload.get("error"):
            self._append_log(f"lyrics: track_id={tid} error={payload['error']}")
        self._lyrics_cache[tid] = payload
        if tid == self._lyrics_request_id:
            self._apply_lyrics_payload(tid, payload)

    def _on_lyrics_error(self, msg: str) -> None:
        track_id = self._lyrics_request_id
        if not track_id:
            return
        self._append_log(f"lyrics: track_id={track_id} error={msg}")
        payload = {
            "track_id": track_id,
            "provider": None,
            "right_to_left": False,
            "text": "",
            "timed_lines": [],
            "error": msg,
        }
        self._lyrics_cache[track_id] = payload
        self._apply_lyrics_payload(track_id, payload)

    def _on_lyrics_worker_finished(self, worker: CallWorker) -> None:
        if self._lyrics_worker is not worker:
            return
        self._lyrics_worker = None
        track_id = self._lyrics_request_id
        if (
            track_id
            and track_id not in self._lyrics_cache
            and self._session is not None
            and not self._offline_mode
        ):
            self._start_lyrics_request(track_id)

    def _show_now_playing_context_menu(self, pos: QtCore.QPoint) -> None:
        track = self._now_playing_track
        if not track or not track.get("id"):
            return
        dummy_item = QtWidgets.QListWidgetItem()
        dummy_item.setData(QtCore.Qt.ItemDataRole.UserRole, track.get("id"))
        menu = QtWidgets.QMenu(self)
        self._populate_track_menu(menu, track, dummy_item, allow_download=True)
        sender = self.sender()
        if isinstance(sender, QtWidgets.QWidget):
            global_pos = sender.mapToGlobal(pos)
        elif hasattr(self, "now_panel"):
            global_pos = self.now_panel.mapToGlobal(pos)
        else:
            global_pos = QtGui.QCursor.pos()
        menu.exec(global_pos)

    # ------------------------------------------------------------------
    # Gapless prefetch orchestration
    # ------------------------------------------------------------------

    def _maybe_prefetch_next(self) -> None:
        """Start prefetching the next queued track for gapless playback."""
        if not self._gapless_enabled:
            return
        if not self._queue_items or self._session is None:
            return
        next_tid = str(self._queue_items[0])
        # Already prefetching / prefetched for this track
        if self._prefetch_track_id == next_tid:
            return
        if self._audio_prefetch_worker is not None and self._audio_prefetch_worker.isRunning():
            # Queue head changed while an old target is still prefetching.
            # Cancel and restart so gapless always targets the current next track.
            self._append_log(
                f"prefetch: restart requested (current={self._prefetch_track_id}, next={next_tid})"
            )
            self._cancel_prefetch()
        self._prefetch_track_id = next_tid
        worker = PrefetchWorker(
            session=self._session,
            track_id=next_tid,
            cache_manager=self._cache,
            disable_ffmpeg=self._disable_ffmpeg,
            track_meta=self._track_map_all.get(next_tid),
        )
        worker.log.connect(self._append_log)
        worker.ready.connect(
            lambda track_id, cached_path, w=worker: self._on_prefetch_ready(w, track_id, cached_path)
        )
        worker.failed.connect(lambda track_id, w=worker: self._on_prefetch_failed(w, track_id))
        self._audio_prefetch_worker = worker
        worker.start()
        self._append_log(f"prefetch: started for track {next_tid}")

    def _on_prefetch_ready(
        self, worker: PrefetchWorker, track_id: str, cached_path: str
    ) -> None:
        """Deliver prefetch result to the running PlaybackWorker."""
        if worker is not self._audio_prefetch_worker:
            self._append_log(f"prefetch: stale ready ignored for track {track_id}")
            return
        next_tid = str(self._queue_items[0]) if self._queue_items else None
        if next_tid is not None and str(track_id) != next_tid:
            self._append_log(
                f"prefetch: ignored mismatched target (ready={track_id}, expected={next_tid})"
            )
            self._prefetch_track_id = None
            return
        if self._play_worker is not None and self._play_worker.isRunning():
            self._play_worker._next_track_path = cached_path
            self._play_worker._next_track_id = track_id
            self._append_log(f"prefetch: delivered {track_id} -> worker")
        else:
            self._append_log(f"prefetch: ready but no active worker ({track_id})")

    def _on_prefetch_failed(self, worker: PrefetchWorker, track_id: str) -> None:
        if worker is not self._audio_prefetch_worker:
            self._append_log(f"prefetch: stale failure ignored for track {track_id}")
            return
        self._append_log(f"prefetch: failed for track {track_id}")
        self._prefetch_track_id = None

    def _reconcile_prefetch_target(self) -> None:
        if not self._gapless_enabled or self._session is None:
            self._cancel_prefetch()
            return
        if not self._queue_items:
            self._cancel_prefetch()
            return
        expected = str(self._queue_items[0])
        if self._prefetch_track_id != expected:
            self._cancel_prefetch()
        self._maybe_prefetch_next()

    def _cancel_prefetch(self) -> None:
        """Stop any running prefetch worker and clear state."""
        if self._audio_prefetch_worker is not None:
            self._audio_prefetch_worker.stop()
            if self._audio_prefetch_worker.isRunning():
                if not self._audio_prefetch_worker.wait(1000):
                    self._abandon_worker(self._audio_prefetch_worker)
            self._audio_prefetch_worker = None
        self._prefetch_track_id = None

    def _on_track_advanced(self, track_id: str) -> None:
        """Handle gapless transition to a new track — update UI, queue, metadata."""
        self._append_log(f"gapless: advanced to track {track_id}")
        # Pop from queue (verify it matches)
        if self._queue_items and str(self._queue_items[0]) == str(track_id):
            self._queue_items.pop(0)
        # Update current play tracking
        dev = self.device_combo.currentText().strip()
        self._current_play = (track_id, dev)
        self._seeking = False
        self._pos_s = 0.0
        next_track = self._track_map_all.get(str(track_id)) or {}
        self._duration_s = float(next_track.get("duration") or 0.0)
        if self._duration_s > 0:
            self.seek_slider.setEnabled(True)
            self.seek_slider.setRange(0, int(self._duration_s * 1000))
            with QtCore.QSignalBlocker(self.seek_slider):
                self.seek_slider.setValue(0)
        else:
            self.seek_slider.setEnabled(False)
            self.seek_slider.setRange(0, 0)
        self.seek_time.setText(
            f"{self._format_time(self._pos_s)} / {self._format_time(self._duration_s)}"
        )
        # Update now-playing UI
        self._set_now_playing(next_track)
        self._set_now_playing_queue(str(track_id))
        self._load_lyrics_for_track(str(track_id))
        self._start_cover_request(track_id, self._cover_url_for_track_id(track_id), force=True)
        # Update Discord RPC and MPRIS
        if self._now_playing_track:
            self._update_discord_track(self._now_playing_track, None)
            self._update_mpris_track(self._now_playing_track, None)
        self._refresh_queue_view()
        # Prefetch the next track in the chain
        self._prefetch_track_id = None  # Reset so we can prefetch the new next
        self._maybe_prefetch_next()

    def _stop_playback(self) -> None:
        self._cancel_pending_seek()
        self._cancel_prefetch()
        if self._play_worker is None:
            return
        self.status_label.setText("Status: stopping…")
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("Play")
        self.seek_slider.setEnabled(False)
        self._stopped_by_user = True
        self._pending_play = None
        # Clear Discord RPC / MPRIS on stop
        self._update_discord_track(None)
        self._update_mpris_track(None)
        self._play_worker.stop()
        self._play_worker.wait(500)
        if self._play_worker is not None and self._play_worker.isRunning():
            self._append_log("playback: forced stop")
        if self._play_worker is not None and self._play_worker.isRunning():
            self._append_log("playback: forced cleanup")
            try:
                self._play_worker.finished.disconnect(self._on_playback_thread_finished)
            except Exception:
                pass
            self._play_had_error = True
            self._current_play = None
            self._abandon_worker(self._play_worker)
            self._play_worker = None
            self._queue_now_playing_id = None
            self.status_label.setText("Status: ready")
            self.stop_btn.setEnabled(False)
            self.pause_btn.setEnabled(True)
            self.pause_btn.setText("Play")
            self.seek_slider.setEnabled(False)
            self._refresh_queue_view()

    def _on_playback_done(self) -> None:
        self.status_label.setText("Status: ready")
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("Play")
        self.seek_slider.setEnabled(False)

    def _on_playback_error(self, msg: str) -> None:
        self._play_had_error = True
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("Play")
        self.seek_slider.setEnabled(False)
        self.status_label.setText("Status: error")
        self._append_log(msg)
        if self._closing:
            return
        QtWidgets.QMessageBox.critical(self, "Playback error", msg)

    def _on_playback_thread_finished(self) -> None:
        self._cancel_pending_seek()
        self._cancel_prefetch()
        self._play_worker = None
        if self._closing:
            return
        if self._play_had_error:
            self._current_play = None
        self._update_cache_status_ui()
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("Play")
        self.seek_slider.setEnabled(False)
        pending = self._pending_play
        self._pending_play = None
        if pending is not None and self._session is not None:
            tid, dev = pending
            self._start_playback(tid, dev)
            return
        if self._stopped_by_user:
            # Clear Discord RPC / MPRIS when playback is stopped by user
            self._update_discord_track(None)
            self._update_mpris_track(None)
            return
        if not self._play_had_error and self._queue_items and self._session is not None:
            next_tid = self._queue_items.pop(0)
            dev = self.device_combo.currentText().strip()
            if dev:
                self._start_playback(next_tid, dev)
                return
        # Clear Discord RPC / MPRIS when queue finishes or error occurs
        if self._play_had_error or not self._queue_items:
            self._update_discord_track(None)
            self._update_mpris_track(None)
        self._queue_now_playing_id = None
        self._refresh_queue_view()

    def _abandon_worker(self, worker: Optional[QtCore.QThread]) -> None:
        if worker is None:
            return
        if worker not in self._orphaned_workers:
            self._orphaned_workers.append(worker)
        try:
            worker.finished.connect(lambda: self._orphaned_workers.remove(worker))
        except Exception:
            pass

    def _toggle_pause(self) -> None:
        if self._play_worker is None or not self._play_worker.isRunning():
            return
        self._play_worker.toggle_pause()
        # Optimistic UI update; worker status signal will correct it if needed.
        is_pausing = self.pause_btn.text() == "Pause"
        self.pause_btn.setText("Resume" if is_pausing else "Pause")
        # Update Discord RPC / MPRIS play/pause state
        self._update_discord_playing(not is_pausing)
        self._update_mpris_playing(not is_pausing)

    def _format_time(self, s: float) -> str:
        s = max(0, int(s))
        m, sec = divmod(s, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m}:{sec:02d}"

    def _on_position(self, pos_s: float, duration_s: float) -> None:
        self._duration_s = float(duration_s)
        if not self._seeking:
            self._pos_s = float(pos_s)
            if self._duration_s > 0:
                self.seek_slider.setEnabled(True)
                self.seek_slider.setRange(0, int(self._duration_s * 1000))
                self.seek_slider.setValue(int(max(0.0, min(self._duration_s, self._pos_s)) * 1000))
        self.seek_time.setText(
            f"{self._format_time(self._pos_s)} / {self._format_time(self._duration_s)}"
        )
        self._sync_lyrics_to_position(self._pos_s)
        # Update Discord RPC / MPRIS position
        self._update_discord_position(pos_s, duration_s)
        self._update_mpris_position(pos_s, duration_s)

    def _on_seek_pressed(self) -> None:
        self._seeking = True

    def _seek_to_position(self, target_s: float, *, force_scroll: bool = True) -> bool:
        self._cancel_pending_seek()
        if self._play_worker is None or not self._play_worker.isRunning():
            self._seeking = False
            return False
        target = max(0.0, float(target_s))
        if self._duration_s > 0:
            target = min(target, self._duration_s)
            self.seek_slider.setEnabled(True)
            self.seek_slider.setRange(0, int(self._duration_s * 1000))
            with QtCore.QSignalBlocker(self.seek_slider):
                self.seek_slider.setValue(int(target * 1000))
        self._seeking = True
        self._pos_s = target
        self.seek_time.setText(
            f"{self._format_time(self._pos_s)} / {self._format_time(self._duration_s)}"
        )
        self._sync_lyrics_to_position(self._pos_s, force_scroll=force_scroll)
        self._play_worker.seek_to(target)
        self._seeking = False
        return True

    def _on_seek_released(self) -> None:
        if self._duration_s <= 0:
            self._cancel_pending_seek()
            self._seeking = False
            return
        target_s = float(self.seek_slider.value()) / 1000.0
        self._seek_to_position(target_s, force_scroll=True)

    def _on_volume_changed(self, value: int) -> None:
        """Handle volume slider changes and update ALSA mixer."""
        self.volume_label.setText(f"{value}%")
        self._set_alsa_volume(value)
        # Save to settings
        self._settings.setValue("volume", value)
        # Sync volume to MPRIS
        if self._mpris_service and self._mpris_enabled:
            self._mpris_service.set_volume(value / 100.0)

    def _set_alsa_volume(self, percent: int) -> None:
        """Set audio volume to the given percentage (0-100)."""
        import alsaaudio
        import subprocess

        device = self.device_combo.currentText().strip()
        if not device:
            return

        # For "default" or plughw/plug devices, try PulseAudio/PipeWire first
        if not device.startswith("hw:"):
            try:
                # Use pactl to control PulseAudio/PipeWire volume
                subprocess.run(
                    ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"],
                    check=True,
                    capture_output=True,
                    timeout=1.0
                )
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
                # pactl not available or failed, fall through to ALSA mixer
                pass

        # For hw: devices or if pactl failed, try ALSA mixer control
        try:
            card_indices = []
            if device.startswith("hw:"):
                card_part = device[3:].split(",")[0]
                try:
                    card_indices = [int(card_part)]
                except ValueError:
                    card_indices = list(range(len(alsaaudio.cards())))
            else:
                card_indices = list(range(len(alsaaudio.cards())))

            # Try to find and set Master mixer first, then PCM as fallback
            for mixer_name in ["Master", "PCM"]:
                for card_num in card_indices:
                    try:
                        mixer = alsaaudio.Mixer(mixer_name, cardindex=card_num)
                        mixer.setvolume(percent)
                        return
                    except alsaaudio.ALSAAudioError:
                        continue

        except Exception:
            # Silently ignore mixer errors - not all devices support software volume
            pass

    def _cancel_pending_seek(self) -> None:
        self._pending_seek_timer.stop()
        self._pending_seek_target_s = None

    def _seek_delta_preview(self, delta_s: float) -> None:
        if self._play_worker is None or not self._play_worker.isRunning():
            return
        if self._duration_s <= 0:
            return
        base = float(self._pending_seek_target_s) if self._pending_seek_target_s is not None else self._pos_s
        target_s = max(0.0, min(self._duration_s, base + float(delta_s)))
        self._queue_seek_preview(target_s)

    def _queue_seek_preview(self, target_s: float) -> None:
        # Show the target immediately, but only send a seek after a short pause
        # so repeated key presses coalesce into one request.
        self._pending_seek_target_s = float(target_s)
        self._seeking = True
        self._pos_s = float(target_s)
        if self._duration_s > 0:
            self.seek_slider.setEnabled(True)
            self.seek_slider.setRange(0, int(self._duration_s * 1000))
            with QtCore.QSignalBlocker(self.seek_slider):
                self.seek_slider.setValue(int(max(0.0, min(self._duration_s, self._pos_s)) * 1000))
        self.seek_time.setText(
            f"{self._format_time(self._pos_s)} / {self._format_time(self._duration_s)}"
        )
        self._sync_lyrics_to_position(self._pos_s, force_scroll=True)
        self._pending_seek_timer.start(500)

    def _commit_pending_seek(self) -> None:
        if self._play_worker is None or not self._play_worker.isRunning():
            self._seeking = False
            self._pending_seek_target_s = None
            return
        if self._pending_seek_target_s is None:
            self._seeking = False
            return
        target = float(self._pending_seek_target_s)
        self._pending_seek_target_s = None
        self._play_worker.seek_to(target)
        self._seeking = False

    def _shutdown_worker(
        self,
        name: str,
        worker: Optional[QtCore.QThread],
        *,
        stop_first: bool = False,
        timeout_ms: int = 2000,
    ) -> None:
        if worker is None:
            return
        try:
            if stop_first:
                stop_fn = getattr(worker, "stop", None)
                if callable(stop_fn):
                    stop_fn()
        except Exception:
            pass
        try:
            if worker.isRunning():
                if not worker.wait(timeout_ms):
                    self._append_log(f"shutdown: {name} still running after {timeout_ms}ms; terminating")
                    try:
                        worker.terminate()
                    except Exception:
                        pass
                    if not worker.wait(1000):
                        self._append_log(f"shutdown: {name} did not terminate cleanly")
        except Exception:
            pass

    def closeEvent(self, event) -> None:
        if self._closing:
            super().closeEvent(event)
            return
        self._closing = True
        try:
            self._append_log("shutdown: begin")
            self._settings.sync()
            self._cancel_pending_seek()
            self._pending_play = None
            self._stopped_by_user = True
            self._shutdown_worker("playback", self._play_worker, stop_first=True, timeout_ms=2000)
            for worker in list(self._orphaned_workers):
                self._shutdown_worker("orphaned-worker", worker, stop_first=True, timeout_ms=2000)
            self._orphaned_workers.clear()
            self._shutdown_worker("radio", self._radio_worker, timeout_ms=2000)
            self._shutdown_worker("artist-radio", self._artist_radio_worker, timeout_ms=2000)
            self._shutdown_worker("home", self._home_worker, timeout_ms=2000)
            for worker in list(self._mix_tracks_workers.values()):
                self._shutdown_worker("mix-tracks", worker, timeout_ms=2000)
            self._mix_tracks_workers.clear()
            for worker in list(self._playlist_tracks_workers.values()):
                self._shutdown_worker("playlist-tracks", worker, timeout_ms=2000)
            self._playlist_tracks_workers.clear()
            self._shutdown_worker("collection", self._collection_worker, timeout_ms=2000)
            self._shutdown_worker("favorite-toggle", self._favorite_toggle_worker, timeout_ms=2000)
            for worker in list(self._artist_detail_workers.values()):
                self._shutdown_worker("artist-details", worker, timeout_ms=2000)
            self._artist_detail_workers.clear()
            for worker in list(self._album_tracks_workers.values()):
                self._shutdown_worker("album-tracks", worker, timeout_ms=2000)
            self._album_tracks_workers.clear()
            if self._queue_window is not None:
                self._queue_window.close()
            if self._settings_window is not None:
                self._settings_window.close()
            self._shutdown_worker("download", self._download_worker, stop_first=True, timeout_ms=2000)
            self._shutdown_worker("lyrics", self._lyrics_worker, timeout_ms=1000)
            self._shutdown_worker("cover", self._cover_worker, stop_first=True, timeout_ms=1000)
            self._shutdown_worker("cover-prefetch", self._prefetch_worker, stop_first=True, timeout_ms=1000)
            self._shutdown_worker("audio-prefetch", self._audio_prefetch_worker, stop_first=True, timeout_ms=1000)
            if hasattr(self, "_tracks_worker"):
                self._shutdown_worker("tracks", self._tracks_worker, timeout_ms=1000)
            if hasattr(self, "_login"):
                self._shutdown_worker("login", self._login, timeout_ms=1000)
            # Disconnect Discord RPC on close
            if self._discord_rpc is not None:
                self._discord_rpc.disconnect()
            # Shutdown MPRIS D-Bus service on close
            if self._mpris_service is not None:
                self._mpris_service.shutdown()
            self._append_log("shutdown: complete")
        finally:
            super().closeEvent(event)
            QtCore.QTimer.singleShot(0, QtCore.QCoreApplication.quit)


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    QtCore.QCoreApplication.setOrganizationName("tidal-bitperfect")
    QtCore.QCoreApplication.setOrganizationDomain("local")
    QtCore.QCoreApplication.setApplicationName("tidal-bitperfect")
    QtGui.QGuiApplication.setApplicationDisplayName("TIDAL Bitperfect")
    # On Wayland, this influences xdg-shell app_id, which controls window grouping/isolation.
    QtGui.QGuiApplication.setDesktopFileName("tidal-bitperfect")

    instance_server_name = "tidal-bitperfect.instance.v1"

    def activate_existing_instance() -> bool:
        sock = QtNetwork.QLocalSocket()
        sock.connectToServer(instance_server_name, QtCore.QIODevice.OpenModeFlag.WriteOnly)
        if not sock.waitForConnected(250):
            return False
        sock.write(b"activate\n")
        sock.flush()
        sock.waitForBytesWritten(250)
        sock.disconnectFromServer()
        return True

    if activate_existing_instance():
        return 0

    server = QtNetwork.QLocalServer(app)
    QtNetwork.QLocalServer.removeServer(instance_server_name)
    if not server.listen(instance_server_name):
        # If we cannot create a listener, continue without single-instance behavior.
        server = None

    # Nicer default icon/title when launched from a desktop entry.
    app.setApplicationDisplayName("TIDAL Bitperfect")
    win = MainWindow()
    win.resize(900, 650)
    win.show()

    def _raise_main_window() -> None:
        if win.isMinimized():
            win.showNormal()
        elif not win.isVisible():
            win.show()
        win.raise_()
        win.activateWindow()

    if server is not None:
        def _on_new_connection() -> None:
            while server.hasPendingConnections():
                client = server.nextPendingConnection()
                if client is None:
                    break

                def _on_ready_read(sock: QtNetwork.QLocalSocket = client) -> None:
                    try:
                        payload = bytes(sock.readAll()).decode("utf-8", errors="ignore")
                    except Exception:
                        payload = ""
                    if "activate" in payload:
                        win._append_log("app: received second-launch request, focusing existing window")
                        _raise_main_window()

                client.readyRead.connect(_on_ready_read)
                client.disconnected.connect(client.deleteLater)

        server.newConnection.connect(_on_new_connection)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
