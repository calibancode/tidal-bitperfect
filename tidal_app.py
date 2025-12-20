#!/usr/bin/env python3

import sys
import time
import subprocess
import os
import tempfile
import queue
import signal
import shutil
import urllib.request
import re
import traceback
import hashlib
import json
import socket
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

import alsaaudio
import tidalapi
try:
    import soundfile as sf
except Exception:  # optional dependency
    sf = None
try:
    from mutagen.flac import FLAC, Picture
except Exception:  # optional dependency
    FLAC = None
    Picture = None
from PySide6 import QtCore, QtGui, QtWidgets

import tidal_core


@dataclass
class AudioFormat:
    channels: int
    rate: int
    bits: int

    @property
    def pcm_bitrate_kbps(self) -> float:
        return (self.channels * self.rate * self.bits) / 1000.0


@dataclass
class StreamInfo:
    track_max_quality: Optional[str]
    audio_quality: Optional[str]
    bit_depth: Optional[int]
    sample_rate: Optional[int]


class CacheManager:
    def __init__(self, base_dir: str, max_bytes: int):
        self._base_dir = base_dir
        self._audio_dir = os.path.join(base_dir, "audio")
        self._downloads_dir = os.path.join(base_dir, "downloads")
        self._cover_dir = os.path.join(base_dir, "covers")
        self._index_path = os.path.join(base_dir, "index.json")
        self._max_bytes = max(0, int(max_bytes))
        self._used_bytes = 0
        self._full = False
        self._disabled = False
        self._index: Dict[str, Dict[str, Any]] = {"audio": {}, "covers": {}, "downloads": {}}
        self._ensure_dirs()
        self._load_index()
        self._recalculate_usage()

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def used_bytes(self) -> int:
        return self._used_bytes

    @property
    def full(self) -> bool:
        return self._full

    @property
    def disabled(self) -> bool:
        return self._disabled

    def set_max_bytes(self, max_bytes: int) -> None:
        self._max_bytes = max(0, int(max_bytes))
        self._recalculate_usage()

    def set_disabled(self, disabled: bool) -> None:
        self._disabled = bool(disabled)

    def _ensure_dirs(self) -> None:
        os.makedirs(self._audio_dir, exist_ok=True)
        os.makedirs(self._downloads_dir, exist_ok=True)
        os.makedirs(self._cover_dir, exist_ok=True)

    def _load_index(self) -> None:
        if not os.path.exists(self._index_path):
            return
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._index = data
                if "audio" not in self._index or not isinstance(self._index.get("audio"), dict):
                    self._index["audio"] = {}
                if "covers" not in self._index or not isinstance(self._index.get("covers"), dict):
                    self._index["covers"] = {}
                if "downloads" not in self._index or not isinstance(self._index.get("downloads"), dict):
                    self._index["downloads"] = {}
        except Exception:
            self._index = {"audio": {}, "covers": {}, "downloads": {}}

    def _save_index(self) -> None:
        tmp_name = None
        try:
            tmp = tempfile.NamedTemporaryFile(
                prefix="tidal_index_", delete=False, dir=self._base_dir
            )
            tmp_name = tmp.name
            try:
                with open(tmp.name, "w", encoding="utf-8") as f:
                    json.dump(self._index, f, indent=2)
            finally:
                try:
                    tmp.close()
                except Exception:
                    pass
            try:
                os.replace(tmp.name, self._index_path)
            except OSError:
                shutil.move(tmp.name, self._index_path)
        except Exception:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except Exception:
                    pass

    def _recalculate_usage(self) -> None:
        total = 0
        for root in (self._audio_dir, self._cover_dir):
            try:
                for base, _dirs, files in os.walk(root):
                    for name in files:
                        path = os.path.join(base, name)
                        try:
                            total += os.path.getsize(path)
                        except Exception:
                            continue
            except Exception:
                continue
        self._used_bytes = total
        self._full = self._max_bytes == 0 or self._used_bytes >= self._max_bytes

    def refresh_usage(self) -> None:
        self._recalculate_usage()

    def _hash_key(self, key: str) -> str:
        return hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()

    def _audio_path(self, track_id: str, url: str) -> str:
        safe_id = re.sub(r"[^0-9A-Za-z_-]+", "_", track_id) or self._hash_key(url)
        return os.path.join(self._audio_dir, f"{safe_id}.flac")

    def _download_path(self, track_id: str) -> str:
        safe_id = re.sub(r"[^0-9A-Za-z_-]+", "_", track_id) or self._hash_key(track_id)
        return os.path.join(self._downloads_dir, f"{safe_id}.flac")

    def _cover_path(self, cover_url: str) -> str:
        return os.path.join(self._cover_dir, f"{self._hash_key(cover_url)}.img")

    def _used_cover_hashes(self) -> set[str]:
        used: set[str] = set()
        audio = self._index.get("audio", {})
        downloads = self._index.get("downloads", {})
        for bucket in (audio, downloads):
            if not isinstance(bucket, dict):
                continue
            for info in bucket.values():
                if not isinstance(info, dict):
                    continue
                cover_url = info.get("cover_url")
                if cover_url:
                    used.add(self._hash_key(str(cover_url)))
        return used

    def _evict_unused_covers(self, needed_bytes: int) -> int:
        if needed_bytes <= 0:
            return 0
        used_hashes = self._used_cover_hashes()
        candidates: List[tuple[float, int, str]] = []
        try:
            for name in os.listdir(self._cover_dir):
                if not name.endswith(".img"):
                    continue
                cover_hash = os.path.splitext(name)[0]
                if cover_hash in used_hashes:
                    continue
                path = os.path.join(self._cover_dir, name)
                try:
                    st = os.stat(path)
                    candidates.append((st.st_mtime, int(st.st_size), path))
                except Exception:
                    continue
        except Exception:
            return 0

        freed = 0
        for _mtime, size, path in sorted(candidates, key=lambda x: x[0]):
            try:
                os.unlink(path)
                freed += int(size)
            except Exception:
                continue
            if freed >= needed_bytes:
                break
        if freed:
            self._used_bytes = max(0, self._used_bytes - freed)
            self._full = self._max_bytes == 0 or self._used_bytes >= self._max_bytes
        return freed

    def get_cached_audio(self, track_id: str, url: str) -> Optional[str]:
        if not track_id:
            return None
        if self._disabled:
            return None
        path = self._audio_path(track_id, url)
        if os.path.exists(path):
            try:
                os.utime(path, None)
            except Exception:
                pass
            return path
        return None

    def get_cached_audio_by_track_id(self, track_id: str) -> Optional[str]:
        if not track_id:
            return None
        if not self._disabled:
            path = self._audio_path(track_id, "")
            if os.path.exists(path):
                try:
                    os.utime(path, None)
                except Exception:
                    pass
                return path
        if not self._disabled:
            path = self._download_path(track_id)
            if os.path.exists(path):
                try:
                    os.utime(path, None)
                except Exception:
                    pass
                return path
        return None

    def list_cached_audio(self) -> List[Dict[str, Any]]:
        entries = []
        audio = self._index.get("audio", {})
        if not isinstance(audio, dict):
            return entries
        new_entries = False
        if not audio:
            try:
                for name in os.listdir(self._audio_dir):
                    if not name.lower().endswith(".flac"):
                        continue
                    tid = os.path.splitext(name)[0]
                    path = os.path.join(self._audio_dir, name)
                    audio[tid] = {"path": path}
                    new_entries = True
            except Exception:
                pass
        stale = []
        for tid, info in audio.items():
            if not isinstance(info, dict):
                continue
            path = info.get("path")
            if not path or not os.path.exists(path):
                stale.append(tid)
                continue
            try:
                st = os.stat(path)
                info["mtime"] = st.st_mtime
                info["size"] = st.st_size
            except Exception:
                continue
            entry = dict(info)
            entry["id"] = tid
            entries.append(entry)
        for tid in stale:
            audio.pop(tid, None)
        if stale or new_entries:
            self._save_index()
        entries.sort(key=lambda e: e.get("mtime", 0), reverse=True)
        return entries

    def list_downloads(self) -> List[Dict[str, Any]]:
        entries = []
        downloads = self._index.get("downloads", {})
        if not isinstance(downloads, dict):
            return entries
        new_entries = False
        if not downloads:
            try:
                for name in os.listdir(self._downloads_dir):
                    if not name.lower().endswith(".flac"):
                        continue
                    tid = os.path.splitext(name)[0]
                    path = os.path.join(self._downloads_dir, name)
                    downloads[tid] = {"path": path}
                    new_entries = True
            except Exception:
                pass
        stale = []
        for tid, info in downloads.items():
            if not isinstance(info, dict):
                continue
            path = info.get("path")
            if not path or not os.path.exists(path):
                stale.append(tid)
                continue
            try:
                st = os.stat(path)
                info["mtime"] = st.st_mtime
                info["size"] = st.st_size
            except Exception:
                continue
            entry = dict(info)
            entry["id"] = tid
            entries.append(entry)
        for tid in stale:
            downloads.pop(tid, None)
        if stale or new_entries:
            self._save_index()
        entries.sort(key=lambda e: e.get("mtime", 0), reverse=True)
        return entries

    def get_cover_bytes(self, cover_url: str) -> Optional[bytes]:
        if not cover_url:
            return None
        if self._disabled:
            return None
        path = self._cover_path(cover_url)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                data = f.read()
            try:
                os.utime(path, None)
            except Exception:
                pass
            return data if data else None
        except Exception:
            return None

    def store_audio(
        self,
        temp_path: str,
        track_id: str,
        url: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        if not track_id:
            return None
        if self._disabled:
            return None
        try:
            size = os.path.getsize(temp_path)
        except Exception:
            return None
        dest = self._audio_path(track_id, url)
        if os.path.exists(dest):
            try:
                os.unlink(temp_path)
            except Exception:
                pass
            self._update_audio_index(track_id, dest, meta)
            return dest
        if self._max_bytes == 0 or (self._used_bytes + size) > self._max_bytes:
            if self._max_bytes == 0:
                self._full = True
                return None
            needed = (self._used_bytes + size) - self._max_bytes
            self._evict_unused_covers(needed)
            if (self._used_bytes + size) > self._max_bytes:
                self._full = True
                return None
        try:
            try:
                os.replace(temp_path, dest)
            except OSError:
                shutil.move(temp_path, dest)
            try:
                size = os.path.getsize(dest)
            except Exception:
                pass
            self._used_bytes += size
            self._full = self._used_bytes >= self._max_bytes
            self._update_audio_index(track_id, dest, meta)
            return dest
        except Exception:
            return None

    def store_download(
        self,
        temp_path: str,
        track_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        if not track_id:
            return None
        dest = self._download_path(track_id)
        if os.path.exists(dest):
            try:
                os.unlink(temp_path)
            except Exception:
                pass
            self._update_download_index(track_id, dest, meta)
            return dest
        try:
            try:
                os.replace(temp_path, dest)
            except OSError:
                shutil.move(temp_path, dest)
            self._update_download_index(track_id, dest, meta)
            return dest
        except Exception:
            return None

    def promote_cache_to_download(
        self, track_id: str, meta: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        if not track_id:
            return None
        src = self._audio_path(track_id, "")
        if not os.path.exists(src):
            return None
        dest = self._download_path(track_id)
        if os.path.exists(dest):
            try:
                os.unlink(src)
            except Exception:
                pass
            self._update_download_index(track_id, dest, meta)
            return dest
        try:
            try:
                os.replace(src, dest)
            except OSError:
                shutil.move(src, dest)
            try:
                size = os.path.getsize(dest)
            except Exception:
                size = 0
            if size:
                self._used_bytes = max(0, self._used_bytes - size)
            audio = self._index.get("audio", {})
            if isinstance(audio, dict):
                audio.pop(str(track_id), None)
            self._update_download_index(track_id, dest, meta)
            self._full = self._max_bytes == 0 or self._used_bytes >= self._max_bytes
            self._save_index()
            return dest
        except Exception:
            return None

    def store_cover_bytes(self, cover_url: str, data: bytes) -> bool:
        if not cover_url or not data:
            return False
        if self._disabled:
            return False
        path = self._cover_path(cover_url)
        if os.path.exists(path):
            return True
        size = len(data)
        if self._max_bytes == 0 or (self._used_bytes + size) > self._max_bytes:
            if self._max_bytes == 0:
                self._full = True
                return False
            needed = (self._used_bytes + size) - self._max_bytes
            self._evict_unused_covers(needed)
            if (self._used_bytes + size) > self._max_bytes:
                self._full = True
                return False
        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(prefix="tidal_cover_", delete=False)
            tmp.write(data)
            tmp.flush()
            tmp.close()
            try:
                os.replace(tmp.name, path)
            except OSError:
                shutil.move(tmp.name, path)
            try:
                size = os.path.getsize(path)
            except Exception:
                pass
            self._used_bytes += size
            self._full = self._used_bytes >= self._max_bytes
            return True
        except Exception:
            if tmp is not None:
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass
            return False

    def _update_audio_index(
        self, track_id: str, path: str, meta: Optional[Dict[str, Any]]
    ) -> None:
        entry: Dict[str, Any] = {"path": path}
        try:
            st = os.stat(path)
            entry["mtime"] = st.st_mtime
            entry["size"] = st.st_size
        except Exception:
            pass
        if meta:
            entry["title"] = meta.get("title")
            entry["artist"] = meta.get("artist")
            entry["album"] = meta.get("album")
            entry["album_id"] = meta.get("album_id")
            entry["cover_url"] = meta.get("cover_url")
        audio = self._index.setdefault("audio", {})
        audio[str(track_id)] = entry
        self._save_index()

    def _update_download_index(
        self, track_id: str, path: str, meta: Optional[Dict[str, Any]]
    ) -> None:
        entry: Dict[str, Any] = {"path": path}
        try:
            st = os.stat(path)
            entry["mtime"] = st.st_mtime
            entry["size"] = st.st_size
        except Exception:
            pass
        if meta:
            entry["title"] = meta.get("title")
            entry["artist"] = meta.get("artist")
            entry["album"] = meta.get("album")
            entry["album_id"] = meta.get("album_id")
            entry["cover_url"] = meta.get("cover_url")
        downloads = self._index.setdefault("downloads", {})
        downloads[str(track_id)] = entry
        self._save_index()

    def has_download(self, track_id: str) -> bool:
        downloads = self._index.get("downloads", {})
        if isinstance(downloads, dict) and str(track_id) in downloads:
            return True
        path = self._download_path(track_id)
        return os.path.exists(path)

    def has_cached_audio(self, track_id: str) -> bool:
        audio = self._index.get("audio", {})
        if isinstance(audio, dict) and str(track_id) in audio:
            return True
        path = self._audio_path(track_id, "")
        return os.path.exists(path)

    def delete_track(self, track_id: str) -> bool:
        removed = False
        audio = self._index.get("audio", {})
        if isinstance(audio, dict) and str(track_id) in audio:
            info = audio.get(str(track_id)) or {}
            path = info.get("path")
            if path and os.path.exists(path):
                try:
                    size = os.path.getsize(path)
                except Exception:
                    size = 0
                try:
                    os.unlink(path)
                    removed = True
                    if size:
                        self._used_bytes = max(0, self._used_bytes - size)
                except Exception:
                    pass
            audio.pop(str(track_id), None)
        downloads = self._index.get("downloads", {})
        if isinstance(downloads, dict) and str(track_id) in downloads:
            info = downloads.get(str(track_id)) or {}
            path = info.get("path")
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                    removed = True
                except Exception:
                    pass
            downloads.pop(str(track_id), None)
        if removed:
            self._full = self._max_bytes == 0 or self._used_bytes >= self._max_bytes
            self._save_index()
        return removed

    def delete_download(self, track_id: str) -> bool:
        removed = False
        downloads = self._index.get("downloads", {})
        if isinstance(downloads, dict) and str(track_id) in downloads:
            info = downloads.get(str(track_id)) or {}
            path = info.get("path")
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                    removed = True
                except Exception:
                    pass
            downloads.pop(str(track_id), None)
        if removed:
            self._save_index()
        return removed

    def clear(self) -> None:
        for root in (self._audio_dir, self._cover_dir):
            try:
                for base, _dirs, files in os.walk(root):
                    for name in files:
                        try:
                            os.unlink(os.path.join(base, name))
                        except Exception:
                            continue
            except Exception:
                continue
        self._used_bytes = 0
        self._full = False
        downloads = self._index.get("downloads", {})
        self._index = {"audio": {}, "covers": {}, "downloads": downloads if isinstance(downloads, dict) else {}}
        self._save_index()

    def clear_audio(self) -> None:
        try:
            for base, _dirs, files in os.walk(self._audio_dir):
                for name in files:
                    try:
                        os.unlink(os.path.join(base, name))
                    except Exception:
                        continue
        except Exception:
            pass
        self._index["audio"] = {}
        self._recalculate_usage()
        self._save_index()

    def clear_covers(self) -> None:
        try:
            for base, _dirs, files in os.walk(self._cover_dir):
                for name in files:
                    try:
                        os.unlink(os.path.join(base, name))
                    except Exception:
                        continue
        except Exception:
            pass
        self._index["covers"] = {}
        self._recalculate_usage()
        self._save_index()

    def clear_downloads(self) -> int:
        removed = 0
        try:
            for base, _dirs, files in os.walk(self._downloads_dir):
                for name in files:
                    if not name.lower().endswith(".flac"):
                        continue
                    try:
                        os.unlink(os.path.join(base, name))
                        removed += 1
                    except Exception:
                        continue
        except Exception:
            pass
        self._index["downloads"] = {}
        self._save_index()
        return removed

    def cover_stats(self) -> tuple[int, int]:
        count = 0
        total = 0
        try:
            for name in os.listdir(self._cover_dir):
                if not name.endswith(".img"):
                    continue
                path = os.path.join(self._cover_dir, name)
                try:
                    total += os.path.getsize(path)
                    count += 1
                except Exception:
                    continue
        except Exception:
            pass
        return count, total

    def audio_stats(self) -> tuple[int, int]:
        count = 0
        total = 0
        try:
            for name in os.listdir(self._audio_dir):
                if not name.lower().endswith(".flac"):
                    continue
                path = os.path.join(self._audio_dir, name)
                try:
                    total += os.path.getsize(path)
                    count += 1
                except Exception:
                    continue
        except Exception:
            pass
        return count, total


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


def open_alsa(device: str, fmt: AudioFormat) -> alsaaudio.PCM:
    if fmt.bits == 16:
        alsa_fmt = alsaaudio.PCM_FORMAT_S16_LE
    elif fmt.bits == 24:
        alsa_fmt = alsaaudio.PCM_FORMAT_S24_3LE
    elif fmt.bits == 32:
        alsa_fmt = alsaaudio.PCM_FORMAT_S32_LE
    else:
        raise RuntimeError(f"unsupported bits per sample: {fmt.bits}")

    return alsaaudio.PCM(
        type=alsaaudio.PCM_PLAYBACK,
        mode=alsaaudio.PCM_NORMAL,
        device=device,
        channels=fmt.channels,
        rate=fmt.rate,
        format=alsa_fmt,
        periodsize=4096,
    )


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
                if self._search_type == "album":
                    items = tidal_core.search_albums(self._session, self._text, limit=self._limit)
                    self.ready.emit({"type": "album", "items": items})
                    return
                if self._search_type == "playlist":
                    items = tidal_core.search_playlists(self._session, self._text, limit=self._limit)
                    self.ready.emit({"type": "playlist", "items": items})
                    return
                if self._search_type == "artist":
                    items = tidal_core.search_artists(self._session, self._text, limit=self._limit)
                    self.ready.emit({"type": "artist", "items": items})
                    return
                items = tidal_core.search_tracks(self._session, self._text, limit=self._limit)
                self.ready.emit({"type": "track", "items": items})
                return
            if self._mode == "url":
                result = tidal_core.link_to_result(self._session, self._text)
                self.ready.emit(result)
                return
            raise ValueError(f"unknown mode: {self._mode}")
        except Exception as e:
            self.error.emit(tidal_core.safe_str(e))


class ArtistDetailsWorker(QtCore.QThread):
    ready = QtCore.Signal(str, dict)  # artist_id, artist dict
    error = QtCore.Signal(str)

    def __init__(self, session: tidalapi.Session, artist_id: str):
        super().__init__()
        self._session = session
        self._artist_id = artist_id

    def run(self) -> None:
        try:
            data = tidal_core.artist_details(self._session, self._artist_id)
            self.ready.emit(self._artist_id, data)
        except Exception as e:
            self.error.emit(tidal_core.safe_str(e))


class AlbumTracksWorker(QtCore.QThread):
    ready = QtCore.Signal(str, list)  # album_id, tracks
    error = QtCore.Signal(str)

    def __init__(self, session: tidalapi.Session, album_id: str):
        super().__init__()
        self._session = session
        self._album_id = album_id

    def run(self) -> None:
        try:
            tracks = tidal_core.album_tracks(self._session, self._album_id)
            self.ready.emit(self._album_id, tracks)
        except Exception as e:
            self.error.emit(tidal_core.safe_str(e))


class RadioWorker(QtCore.QThread):
    ready = QtCore.Signal(list)  # List[Dict]
    error = QtCore.Signal(str)

    def __init__(self, session: tidalapi.Session, track_id: str, limit: int):
        super().__init__()
        self._session = session
        self._track_id = track_id
        self._limit = limit

    def run(self) -> None:
        try:
            tracks = tidal_core.track_radio(self._session, self._track_id, limit=self._limit)
            self.ready.emit(tracks)
        except Exception as e:
            self.error.emit(tidal_core.safe_str(e))


class CollectionWorker(QtCore.QThread):
    ready = QtCore.Signal(str, list)  # type, List[Dict]
    error = QtCore.Signal(str)

    def __init__(self, session: tidalapi.Session, item_type: str, limit: int = 100, offset: int = 0):
        super().__init__()
        self._session = session
        self._limit = limit
        self._offset = offset
        self._item_type = item_type

    def run(self) -> None:
        try:
            if self._item_type == "album":
                items = tidal_core.list_favorite_albums(
                    self._session, limit=self._limit, offset=self._offset
                )
                self.ready.emit("album", items)
                return
            if self._item_type == "playlist":
                items = tidal_core.list_favorite_playlists(
                    self._session, limit=self._limit, offset=self._offset
                )
                self.ready.emit("playlist", items)
                return
            if self._item_type == "artist":
                items = tidal_core.list_favorite_artists(
                    self._session, limit=self._limit, offset=self._offset
                )
                self.ready.emit("artist", items)
                return
            tracks = tidal_core.list_favorite_tracks(
                self._session, limit=self._limit, offset=self._offset
            )
            self.ready.emit("track", tracks)
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
            if self._item_type == "album":
                tidal_core.set_album_favorite(self._session, self._item_id, self._favorite)
            elif self._item_type == "playlist":
                tidal_core.set_playlist_favorite(self._session, self._item_id, self._favorite)
            elif self._item_type == "artist":
                tidal_core.set_artist_favorite(self._session, self._item_id, self._favorite)
            else:
                tidal_core.set_track_favorite(self._session, self._item_id, self._favorite)
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
        if self._stop:
            return
        try:
            if self._cover_url:
                self.log.emit(f"cover: download url for track={self._track_id}")
                data = _download_cover(self._cover_url)
                if data:
                    data = _shrink_cover_bytes(data)
                if self._stop:
                    return
                self.ready.emit(self._track_id, data)
                return
            track = self._session.track(self._track_id)
            if self._stop:
                return
            self.log.emit(f"cover: fetch via session for track={self._track_id}")
            data = _fetch_cover_bytes(track) if track is not None else None
            if data:
                data = _shrink_cover_bytes(data)
            if self._stop:
                return
            self.ready.emit(self._track_id, data)
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
            if cover_url:
                self.log.emit(f"cover: prefetch url for track={track_id}")
                if cover_url in local_cache:
                    data = local_cache[cover_url]
                else:
                    data = _download_cover(cover_url)
                    if data:
                        data = _shrink_cover_bytes(data)
                    local_cache[cover_url] = data
                if self._stop:
                    return
                self.ready.emit(track_id, cover_url, data)
                continue
            try:
                track = self._session.track(track_id)
                if self._stop:
                    return
                self.log.emit(f"cover: prefetch via session for track={track_id}")
                data = _fetch_cover_bytes(track) if track is not None else None
                if data:
                    data = _shrink_cover_bytes(data)
                if self._stop:
                    return
                self.ready.emit(track_id, None, data)
            except Exception:
                if not self._stop:
                    self.log.emit(f"cover: prefetch error {traceback.format_exc().strip()}")
                    self.ready.emit(track_id, None, None)


class PlaybackWorker(QtCore.QThread):
    status = QtCore.Signal(str)
    log = QtCore.Signal(str)
    error = QtCore.Signal(str)
    fmt_ready = QtCore.Signal(object)  # AudioFormat
    stream_info = QtCore.Signal(object)  # StreamInfo
    position = QtCore.Signal(float, float)  # pos_s, duration_s (approx)
    decode_path = QtCore.Signal(str)  # "libsndfile" or "ffmpeg"
    finished_ok = QtCore.Signal()
    cache_write = QtCore.Signal()

    def __init__(
        self,
        session: Optional[tidalapi.Session],
        track_id: str,
        device: str,
        disable_ffmpeg: bool,
        cache_manager: Optional[CacheManager],
        track_meta: Optional[Dict[str, Any]] = None,
    ):
        super().__init__()
        self._session = session
        self._track_id = track_id
        self._device = device
        self._disable_ffmpeg = disable_ffmpeg
        self._cache = cache_manager
        self._track_meta = track_meta
        self._stop = False
        self._proc: Optional[subprocess.Popen] = None
        self._cmdq: "queue.Queue[tuple[str, float]]" = queue.Queue()
        self._paused = False

    def stop(self) -> None:
        self._stop = True
        self._cmdq.put(("stop", 0.0))
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def toggle_pause(self) -> None:
        self._cmdq.put(("pause_toggle", 0.0))

    def seek(self, delta_s: float) -> None:
        self._cmdq.put(("seek", float(delta_s)))

    def seek_to(self, pos_s: float) -> None:
        self._cmdq.put(("seek_to", float(pos_s)))

    def _ffmpeg_fail(self, why: str, url: str) -> str:
        err = ""
        rc = None
        try:
            if self._proc is not None:
                rc = self._proc.poll()
                try:
                    _out, _err = self._proc.communicate(timeout=1)
                except Exception:
                    _out, _err = (b"", b"")
                err = (_err or b"").decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        msg = f"{why}"
        msg += f"\nstream url: {url}"
        msg += f"\nffmpeg rc: {rc}"
        if err:
            msg += f"\nffmpeg stderr:\n{err}"
        return msg

    def _dbg(self, msg: str) -> None:
        self.log.emit(msg)

    def _dbg_exc(self, context: str) -> None:
        self._dbg(f"{context}: {traceback.format_exc().strip()}")

    def _download_to_temp(self, url: str) -> Optional[str]:
        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(prefix="tidal_", suffix=".flac", delete=False)
            start = time.time()
            total = 0
            with urllib.request.urlopen(url, timeout=15) as resp:
                while True:
                    if self._stop:
                        raise RuntimeError("download stopped")
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    tmp.write(chunk)
            tmp.flush()
            elapsed = max(0.0, time.time() - start)
            if total > 0:
                mb = total / (1024.0 * 1024.0)
                rate = mb / elapsed if elapsed > 0 else 0.0
                self._dbg(f"FLAC download: {mb:.1f} MB in {elapsed:.2f}s ({rate:.2f} MB/s)")
            return tmp.name
        except Exception:
            self._dbg_exc("flac download failed")
            if tmp is not None:
                try:
                    tmp.close()
                except Exception:
                    self._dbg_exc("flac download close failed")
                try:
                    os.unlink(tmp.name)
                except Exception:
                    self._dbg_exc("flac download cleanup failed")
            return None
        finally:
            if tmp is not None:
                try:
                    tmp.close()
                except Exception:
                    self._dbg_exc("flac download close failed")

    def _open_flac(self, url: str) -> Optional[tuple["sf.SoundFile", str, int, str, bool]]:
        if sf is None:
            return None
        self._dbg("trying in-process FLAC decode")
        cached_path = None
        if self._cache is not None:
            cached_path = self._cache.get_cached_audio(self._track_id, url)
        if cached_path:
            tmp_path = cached_path
            self._dbg("using cached FLAC")
        else:
            tmp_path = self._download_to_temp(url)
            if not tmp_path:
                self._dbg("FLAC download failed; falling back to ffmpeg")
                return None
            if self._cache is not None:
                stored = self._cache.store_audio(tmp_path, self._track_id, url, self._track_meta)
                if stored:
                    tmp_path = stored
                    cached_path = stored
                    self.cache_write.emit()
        try:
            f = sf.SoundFile(tmp_path, "r")
        except Exception:
            self._dbg_exc("flac open failed")
            try:
                os.unlink(tmp_path)
            except Exception:
                self._dbg_exc("flac temp cleanup failed")
            self._dbg("FLAC open failed; falling back to ffmpeg")
            return None
        if getattr(f, "format", "").upper() != "FLAC":
            try:
                f.close()
            except Exception:
                pass
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            self._dbg("not a FLAC stream; falling back to ffmpeg")
            return None
        subtype = getattr(f, "subtype", "")
        bits = 0
        dtype = ""
        if subtype == "PCM_16":
            bits = 16
            dtype = "int16"
        elif subtype in ("PCM_24", "PCM_32"):
            bits = 32
            dtype = "int32"
        else:
            try:
                f.close()
            except Exception:
                pass
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            self._dbg(f"unsupported FLAC subtype {subtype!r}; falling back to ffmpeg")
            return None
        self._dbg(f"FLAC format: {f.channels}ch @ {f.samplerate}Hz {subtype}")
        should_delete = cached_path is None
        return f, tmp_path, bits, dtype, should_delete

    def _open_flac_cached(self, path: str) -> Optional[tuple["sf.SoundFile", str, int, str, bool]]:
        if sf is None:
            return None
        try:
            f = sf.SoundFile(path, "r")
        except Exception:
            self._dbg_exc("cached flac open failed")
            return None
        if getattr(f, "format", "").upper() != "FLAC":
            try:
                f.close()
            except Exception:
                pass
            self._dbg("cached file is not FLAC")
            return None
        subtype = getattr(f, "subtype", "")
        bits = 0
        dtype = ""
        if subtype == "PCM_16":
            bits = 16
            dtype = "int16"
        elif subtype in ("PCM_24", "PCM_32"):
            bits = 32
            dtype = "int32"
        else:
            try:
                f.close()
            except Exception:
                pass
            self._dbg(f"unsupported cached FLAC subtype {subtype!r}")
            return None
        self._dbg(f"cached FLAC format: {f.channels}ch @ {f.samplerate}Hz {subtype}")
        return f, path, bits, dtype, False

    def _play_flac_opened(
        self,
        opened: tuple["sf.SoundFile", str, int, str, bool],
        duration_s: float,
    ) -> bool:
        f, tmp_path, bits, dtype, should_delete = opened
        pcm = None
        try:
            self.decode_path.emit("libsndfile")
            ch = int(f.channels)
            rate = int(f.samplerate)
            bytes_per_sample = bits // 8
            frame_size = ch * bytes_per_sample
            fmt = AudioFormat(channels=ch, rate=rate, bits=bits)
            self.fmt_ready.emit(fmt)
            self._dbg("in-process FLAC playback active")
            if duration_s <= 0 and getattr(f, "frames", 0):
                duration_s = float(f.frames) / float(rate)
                if duration_s > 0:
                    self.position.emit(0.0, duration_s)
            self.status.emit("Opening ALSA device…")
            pcm = open_alsa(self._device, fmt)
            self._dbg(f"alsa device={self._device} bits={fmt.bits} rate={fmt.rate} ch={fmt.channels}")
            self.status.emit("Playing")

            bytes_written = 0
            bytes_per_second = float(rate) * float(frame_size) if rate and frame_size else 0.0
            start_offset_s = 0.0
            last_pos_emit = 0.0
            chunk_frames = 4096

            while not self._stop:
                try:
                    while True:
                        cmd, arg = self._cmdq.get_nowait()
                        if cmd == "stop":
                            self._stop = True
                            break
                        if cmd == "pause_toggle":
                            self._paused = not self._paused
                            self._dbg(f"pause_toggle -> {self._paused}")
                            self._apply_flac_pause_state(pcm)
                            self.status.emit("Paused" if self._paused else "Playing")
                        if cmd == "seek":
                            if bytes_per_second <= 0:
                                continue
                            current_pos_s = bytes_written / bytes_per_second
                            new_offset = max(0.0, start_offset_s + current_pos_s + arg)
                            if duration_s > 0:
                                new_offset = min(duration_s, new_offset)
                            start_offset_s = new_offset
                            bytes_written = 0
                            self.status.emit("Seeking…")
                            self._dbg(f"seek delta={arg:.3f}s -> offset={start_offset_s:.3f}s")
                            pcm = self._restart_flac_playback(
                                f, pcm, fmt, start_offset_s, rate, duration_s
                            )
                        if cmd == "seek_to":
                            if bytes_per_second <= 0:
                                continue
                            new_offset = max(0.0, float(arg))
                            if duration_s > 0:
                                new_offset = min(duration_s, new_offset)
                            start_offset_s = new_offset
                            bytes_written = 0
                            self.status.emit("Seeking…")
                            self._dbg(f"seek_to target={start_offset_s:.3f}s")
                            pcm = self._restart_flac_playback(
                                f, pcm, fmt, start_offset_s, rate, duration_s
                            )
                except queue.Empty:
                    pass

                if self._paused:
                    time.sleep(0.05)
                    continue

                data = f.buffer_read(chunk_frames, dtype=dtype)
                if not data:
                    break
                if frame_size > 0:
                    whole = (len(data) // frame_size) * frame_size
                    if whole:
                        pcm.write(data[:whole])
                        bytes_written += whole
                        if duration_s > 0 and bytes_per_second > 0:
                            now = time.time()
                            if now - last_pos_emit >= 0.25:
                                self.position.emit(
                                    start_offset_s + (bytes_written / bytes_per_second),
                                    duration_s,
                                )
                                last_pos_emit = now
            return True
        finally:
            try:
                f.close()
            except Exception:
                pass
            try:
                if pcm is not None:
                    pcm.close()
            except Exception:
                pass
            if should_delete:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _play_flac(self, url: str, duration_s: float) -> bool:
        opened = self._open_flac(url)
        if opened is None:
            return False
        return self._play_flac_opened(opened, duration_s)

    def _select_stream(
        self, original_quality: Optional[str]
    ) -> tuple[object, object, Optional[str], StreamInfo, float, bool, object]:
        url = None
        stream = None
        track = None
        last_err: Optional[Exception] = None

        candidates = []
        for q in tidal_core.quality_preference() or [original_quality]:
            try:
                if q is not None:
                    self._session.config.quality = q
                track = self._session.track(self._track_id)
                try:
                    stream = track.get_stream()
                except Exception:
                    stream = None
                try:
                    url = tidal_core.get_stream_url(track)
                except Exception:
                    url = None

                sinfo = StreamInfo(
                    track_max_quality=None,
                    audio_quality=getattr(stream, "audio_quality", None),
                    bit_depth=getattr(stream, "bit_depth", None),
                    sample_rate=getattr(stream, "sample_rate", None),
                )

                candidates.append((q, track, stream, url, sinfo))
            except Exception as e:
                last_err = e
                continue

        if not candidates:
            if last_err is not None:
                raise last_err
            raise RuntimeError("could not load stream candidates")

        ffmpeg_available = shutil.which("ffmpeg") is not None
        if self._disable_ffmpeg:
            ffmpeg_available = False
        if not ffmpeg_available:
            direct = [c for c in candidates if c[3] is not None]
            if direct:
                candidates = direct
                self._dbg("ffmpeg not found; using direct stream only")
            else:
                raise RuntimeError(
                    "ffmpeg not found and no direct stream available (DASH/manifest only)"
                )

        def score(item) -> tuple:
            _q, _t, _s, _u, info = item
            return (
                tidal_core.quality_rank(info.audio_quality),
                int(info.bit_depth or 0),
                int(info.sample_rate or 0),
            )

        chosen_q, track, stream, url, sinfo = sorted(candidates, key=score, reverse=True)[0]
        duration_s = float(getattr(track, "duration", 0) or 0)
        track_max = getattr(track, "audio_quality", None)
        tags = getattr(track, "media_metadata_tags", None) or {}
        if isinstance(tags, dict):
            for k, v in tags.items():
                if not v:
                    continue
                kk = str(k).upper()
                if "HIRES_LOSSLESS" in kk or "HI_RES_LOSSLESS" in kk or kk == "HIRES":
                    track_max = "HI_RES_LOSSLESS"
                    break
        sinfo.track_max_quality = track_max
        self._dbg(f"track id={getattr(track,'id',None)} title={getattr(track,'title',None)!r}")
        self._dbg(f"track max audio_quality={getattr(track,'audio_quality',None)}")
        self._dbg(f"chosen session quality={chosen_q}")
        self._dbg(
            f"stream audio_quality={sinfo.audio_quality} bit_depth={sinfo.bit_depth} sample_rate={sinfo.sample_rate}"
        )

        return track, stream, url, sinfo, duration_s, ffmpeg_available, chosen_q

    def _resolve_input(
        self, stream: object, url: Optional[str]
    ) -> tuple[Optional[str], Optional[str]]:
        url, manifest_bytes, manifest_mime = tidal_core.resolve_stream_input(stream, url)
        mpd_path = None
        if manifest_bytes and manifest_mime and "dash" in str(manifest_mime).lower():
            tmp = tempfile.NamedTemporaryFile(prefix="tidal_", suffix=".mpd", delete=False)
            tmp.write(manifest_bytes)
            tmp.flush()
            tmp.close()
            mpd_path = tmp.name
            self._dbg(f"using DASH MPD input: {mpd_path}")
        else:
            if url is not None:
                self._dbg("using direct URL input")

        if url is None and mpd_path is None:
            raise RuntimeError("no playable URL or manifest was available for this track")
        return url, mpd_path

    def _choose_codec(self, sinfo: StreamInfo) -> str:
        codec = "pcm_s16le"
        if sinfo.bit_depth == 24:
            codec = "pcm_s32le"
        elif sinfo.bit_depth == 32:
            codec = "pcm_s32le"
        return codec

    def _start_ffmpeg(
        self, inp: str, codec_name: str, start_s: float, mpd_path: Optional[str]
    ) -> subprocess.Popen:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if start_s and start_s > 0:
            cmd += ["-ss", f"{start_s:.3f}"]
        if mpd_path is not None:
            cmd += ["-protocol_whitelist", "file,https,tls,tcp,crypto"]
        cmd += [
            "-i",
            inp,
            "-c:a",
            codec_name,
            "-f",
            "wav",
            "pipe:1",
        ]
        self._dbg(f"ffmpeg: {' '.join(cmd)}")
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _parse_wav(self, stdout, url: Optional[str]) -> tuple[int, int, int, int]:
        try:
            return tidal_core.parse_wav_header(stdout)
        except Exception as e:
            raise RuntimeError(self._ffmpeg_fail(f"decode failed: {tidal_core.safe_str(e)}", url))

    def _wav_to_format(self, ch: int, rate: int, block_align: int) -> tuple[AudioFormat, int, int]:
        bytes_per_sample = max(1, int(block_align) // int(ch))
        bits = bytes_per_sample * 8
        fmt = AudioFormat(channels=ch, rate=rate, bits=bits)
        return fmt, bytes_per_sample, bits

    def _apply_pause_state(self, pcm: Optional[alsaaudio.PCM]) -> None:
        try:
            if pcm is not None:
                pcm.pause(1 if self._paused else 0)
        except Exception:
            self._dbg_exc("pause pcm failed")
        try:
            if self._proc is not None and self._proc.pid:
                os.kill(self._proc.pid, signal.SIGSTOP if self._paused else signal.SIGCONT)
        except Exception:
            self._dbg_exc("pause process failed")

    def _restart_ffmpeg_playback(
        self,
        pcm: Optional[alsaaudio.PCM],
        codec: str,
        start_offset_s: float,
        inp: str,
        mpd_path: Optional[str],
        url: Optional[str],
        duration_s: float,
    ) -> tuple[alsaaudio.PCM, AudioFormat, int, float]:
        try:
            if pcm is not None:
                pcm.close()
        except Exception:
            self._dbg_exc("ffmpeg restart: pcm close failed")
        try:
            if self._proc is not None:
                self._proc.terminate()
                self._proc.wait(timeout=1)
        except Exception:
            self._dbg_exc("ffmpeg restart: terminate failed")

        self._proc = self._start_ffmpeg(inp, codec, start_s=start_offset_s, mpd_path=mpd_path)
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None

        ch, rate, bits, block_align = self._parse_wav(self._proc.stdout, url)
        frame_size = int(block_align)
        bytes_per_second = float(rate) * float(frame_size) if rate and frame_size else 0.0
        fmt, _bytes_per_sample, bits = self._wav_to_format(ch, rate, block_align)
        self.fmt_ready.emit(fmt)
        pcm = open_alsa(self._device, fmt)
        self._apply_pause_state(pcm)
        self.status.emit("Paused" if self._paused else "Playing")
        if duration_s > 0:
            self.position.emit(start_offset_s, duration_s)
        return pcm, fmt, frame_size, bytes_per_second

    def _apply_flac_pause_state(self, pcm: Optional[alsaaudio.PCM]) -> None:
        try:
            if pcm is not None:
                pcm.pause(1 if self._paused else 0)
        except Exception:
            self._dbg_exc("flac pause failed")

    def _restart_flac_playback(
        self,
        f: "sf.SoundFile",
        pcm: Optional[alsaaudio.PCM],
        fmt: AudioFormat,
        start_offset_s: float,
        rate: int,
        duration_s: float,
    ) -> alsaaudio.PCM:
        try:
            f.seek(int(start_offset_s * rate))
        except Exception:
            self._dbg_exc("flac seek failed")
        try:
            if pcm is not None:
                pcm.close()
        except Exception:
            self._dbg_exc("flac restart: pcm close failed")
        pcm = open_alsa(self._device, fmt)
        self._apply_flac_pause_state(pcm)
        self.status.emit("Paused" if self._paused else "Playing")
        if duration_s > 0:
            self.position.emit(start_offset_s, duration_s)
        return pcm

    def _play_ffmpeg(
        self,
        inp: str,
        mpd_path: Optional[str],
        url: Optional[str],
        sinfo: StreamInfo,
        duration_s: float,
    ) -> alsaaudio.PCM:
        # Use 32-bit PCM for 24-bit sources to ensure reliable playback; sample rate is preserved.
        codec = self._choose_codec(sinfo)

        self.decode_path.emit("ffmpeg")
        self._proc = self._start_ffmpeg(inp, codec, start_s=0.0, mpd_path=mpd_path)
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None

        ch, rate, bits, block_align = self._parse_wav(self._proc.stdout, url)
        fmt, bytes_per_sample, bits = self._wav_to_format(ch, rate, block_align)
        self.fmt_ready.emit(fmt)
        self._dbg(
            f"wav fmt: ch={ch} rate={rate} bits={bits} block_align={block_align} bytes_per_sample={bytes_per_sample}"
        )
        self.status.emit("Opening ALSA device…")
        try:
            pcm = open_alsa(self._device, fmt)
        except Exception as e:
            if sinfo.bit_depth == 24 and codec == "pcm_s32le":
                self._dbg(
                    "warning: ALSA rejected padded 32-bit PCM; replug the DAC or use plughw/default"
                )
            raise
        self._dbg(f"alsa device={self._device} bits={fmt.bits} rate={fmt.rate} ch={fmt.channels}")
        self.status.emit("Playing")

        frame_size = int(block_align)
        buf = bytearray()
        did_fallback = False
        bytes_written = 0
        bytes_per_second = float(rate) * float(frame_size) if rate and frame_size else 0.0
        start_offset_s = 0.0
        last_pos_emit = 0.0

        while not self._stop:
            # Handle queued commands (pause/seek/stop)
            try:
                while True:
                    cmd, arg = self._cmdq.get_nowait()
                    if cmd == "stop":
                        self._stop = True
                        break
                    if cmd == "pause_toggle":
                        self._paused = not self._paused
                        self._dbg(f"pause_toggle -> {self._paused}")
                        self._apply_pause_state(pcm)
                        self.status.emit("Paused" if self._paused else "Playing")
                    if cmd == "seek":
                        if bytes_per_second <= 0:
                            continue
                        # Seek is best-effort for streaming/DASH inputs.
                        current_pos_s = bytes_written / bytes_per_second
                        new_offset = max(0.0, start_offset_s + current_pos_s + arg)
                        if duration_s > 0:
                            new_offset = min(duration_s, new_offset)
                        start_offset_s = new_offset
                        bytes_written = 0
                        buf = bytearray()

                        self.status.emit("Seeking…")
                        self._dbg(f"seek delta={arg:.3f}s -> offset={start_offset_s:.3f}s")
                        pcm, fmt, frame_size, bytes_per_second = self._restart_ffmpeg_playback(
                            pcm,
                            codec,
                            start_offset_s,
                            inp,
                            mpd_path,
                            url,
                            duration_s,
                        )
                    if cmd == "seek_to":
                        if bytes_per_second <= 0:
                            continue
                        new_offset = max(0.0, float(arg))
                        if duration_s > 0:
                            new_offset = min(duration_s, new_offset)
                        start_offset_s = new_offset
                        bytes_written = 0
                        buf = bytearray()

                        self.status.emit("Seeking…")
                        self._dbg(f"seek_to target={start_offset_s:.3f}s")
                        pcm, fmt, frame_size, bytes_per_second = self._restart_ffmpeg_playback(
                            pcm,
                            codec,
                            start_offset_s,
                            inp,
                            mpd_path,
                            url,
                            duration_s,
                        )
            except queue.Empty:
                pass

            if self._paused:
                time.sleep(0.05)
                continue

            chunk = self._proc.stdout.read(16384)
            if not chunk:
                break
            buf.extend(chunk)
            if frame_size <= 0:
                continue
            whole = (len(buf) // frame_size) * frame_size
            if whole:
                try:
                    pcm.write(bytes(buf[:whole]))
                    bytes_written += whole
                    if duration_s > 0 and bytes_per_second > 0:
                        now = time.time()
                        if now - last_pos_emit >= 0.25:
                            self.position.emit(
                                start_offset_s + (bytes_written / bytes_per_second),
                                duration_s,
                            )
                            last_pos_emit = now
                except Exception as e:
                    msg = tidal_core.safe_str(e)
                    if (not did_fallback) and ("framesize" in msg.lower()):
                        did_fallback = True
                        self.status.emit("Retrying with 32-bit PCM…")
                        self._dbg(f"alsa framesize error: {msg}")
                        try:
                            pcm.close()
                        except Exception:
                            pass
                        try:
                            if self._proc is not None:
                                self._proc.terminate()
                                self._proc.wait(timeout=1)
                        except Exception:
                            pass

                        # Restart ffmpeg with 32-bit PCM and reopen ALSA as S32_LE.
                        self._proc = self._start_ffmpeg(
                            inp, "pcm_s32le", start_s=0.0, mpd_path=mpd_path
                        )
                        assert self._proc.stdout is not None
                        assert self._proc.stderr is not None
                        ch, rate, bits, block_align = self._parse_wav(self._proc.stdout, url)
                        fmt, bytes_per_sample, bits = self._wav_to_format(ch, rate, block_align)
                        self.fmt_ready.emit(fmt)
                        pcm = open_alsa(self._device, fmt)
                        frame_size = int(block_align)
                        bytes_per_second = (
                            float(rate) * float(frame_size) if rate and frame_size else 0.0
                        )
                        bytes_written = 0
                        buf = bytearray()
                        continue
                    raise
                del buf[:whole]

        return pcm

    def run(self) -> None:
        pcm = None
        had_error = False
        try:
            self.status.emit("Loading stream…")
            original_quality = getattr(self._session.config, "quality", None) if self._session else None

            cached_path = None
            if self._cache is not None:
                cached_path = self._cache.get_cached_audio_by_track_id(self._track_id)
            if cached_path and sf is not None:
                self._dbg(f"cached flac hit: {cached_path}")
                opened = self._open_flac_cached(cached_path)
                if opened is not None:
                    f, _path, bits, _dtype, _should_delete = opened
                    sinfo = StreamInfo(
                        track_max_quality=None,
                        audio_quality=None,
                        bit_depth=bits if bits else None,
                        sample_rate=int(getattr(f, "samplerate", 0) or 0) or None,
                    )
                    self.stream_info.emit(sinfo)
                    if self._play_flac_opened(opened, duration_s=0.0):
                        return
            if self._session is None:
                raise RuntimeError("offline: track is not cached")

            track, stream, url, sinfo, duration_s, ffmpeg_available, _chosen_q = (
                self._select_stream(original_quality)
            )
            url, mpd_path = self._resolve_input(stream, url)

            self._dbg(f"stream url: {url}")

            self.stream_info.emit(sinfo)
            if duration_s > 0:
                self.position.emit(0.0, duration_s)

            if mpd_path is None and url is not None:
                if self._play_flac(url, duration_s):
                    return
                if not ffmpeg_available:
                    raise RuntimeError(
                        "ffmpeg not found; direct stream is not FLAC or could not be decoded"
                    )
            elif mpd_path is not None:
                self._dbg("DASH manifest detected; falling back to ffmpeg")

            inp = mpd_path if mpd_path is not None else url
            assert inp is not None
            pcm = self._play_ffmpeg(inp, mpd_path, url, sinfo, duration_s)

        except Exception as e:
            had_error = True
            self.error.emit(tidal_core.safe_str(e))
        finally:
            try:
                if original_quality is not None:
                    self._session.config.quality = original_quality
            except Exception:
                self._dbg_exc("cleanup: restore quality failed")
            try:
                if "mpd_path" in locals() and mpd_path:
                    os.unlink(mpd_path)
            except Exception:
                self._dbg_exc("cleanup: remove mpd failed")
            try:
                if pcm is not None:
                    pcm.close()
            except Exception:
                self._dbg_exc("cleanup: pcm close failed")
            try:
                if self._proc is not None:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        try:
                            self._proc.kill()
                        except Exception:
                            pass
            except Exception:
                self._dbg_exc("cleanup: ffmpeg terminate failed")
            try:
                if self._session.check_login():
                    tidal_core.save_oauth(self._session)
            except Exception:
                self._dbg_exc("cleanup: save oauth failed")
            if not had_error:
                self.finished_ok.emit()


class DownloadWorker(QtCore.QThread):
    status = QtCore.Signal(str)
    log = QtCore.Signal(str)
    error = QtCore.Signal(str)
    finished = QtCore.Signal(str)

    def __init__(
        self,
        session: tidalapi.Session,
        track_id: str,
        cache_manager: CacheManager,
        track_meta: Optional[Dict[str, Any]],
        cover_bytes: Optional[bytes],
    ):
        super().__init__()
        self._session = session
        self._track_id = track_id
        self._cache = cache_manager
        self._track_meta = track_meta
        self._cover_bytes = cover_bytes
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _download_to_temp(self, url: str) -> str:
        tmp = tempfile.NamedTemporaryFile(prefix="tidal_dl_", suffix=".flac", delete=False)
        try:
            self.log.emit(f"download: direct url={url}")
            total = 0
            start = time.time()
            with urllib.request.urlopen(url, timeout=15) as resp:
                self.log.emit(f"download: status={getattr(resp, 'status', None)}")
                while True:
                    if self._stop:
                        raise RuntimeError("download stopped")
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    tmp.write(chunk)
            tmp.flush()
            if total <= 0:
                raise RuntimeError("downloaded 0 bytes")
            elapsed = max(0.0, time.time() - start)
            mb = total / (1024.0 * 1024.0)
            rate = mb / elapsed if elapsed > 0 else 0.0
            self.log.emit(f"download: wrote {mb:.1f} MB in {elapsed:.2f}s ({rate:.2f} MB/s)")
            return tmp.name
        finally:
            try:
                tmp.close()
            except Exception:
                pass

    def _download_dash_to_temp(self, manifest_bytes: bytes) -> str:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found for DASH download")
        mpd = tempfile.NamedTemporaryFile(prefix="tidal_dl_", suffix=".mpd", delete=False)
        try:
            mpd.write(manifest_bytes)
            mpd.flush()
            mpd.close()
            tmp = tempfile.NamedTemporaryFile(prefix="tidal_dl_", suffix=".flac", delete=False)
            tmp.close()
            self.log.emit(f"download: DASH via ffmpeg mpd={mpd.name}")
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-protocol_whitelist",
                "file,https,tls,tcp,crypto",
                "-i",
                mpd.name,
                "-c:a",
                "flac",
                tmp.name,
            ]
            self.log.emit(f"download: ffmpeg={' '.join(cmd)}")
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"ffmpeg failed: {err or proc.returncode}")
            if os.path.getsize(tmp.name) <= 0:
                err = proc.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"ffmpeg produced empty file: {err or 'no stderr'}")
            return tmp.name
        finally:
            try:
                os.unlink(mpd.name)
            except Exception:
                pass

    def _download_url_to_temp(self, url: str) -> str:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found for download")
        tmp = tempfile.NamedTemporaryFile(prefix="tidal_dl_", suffix=".flac", delete=False)
        tmp.close()
        self.log.emit(f"download: ffmpeg url={url}")
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-protocol_whitelist",
            "file,https,tls,tcp,crypto",
            "-i",
            url,
            "-c:a",
            "flac",
            tmp.name,
        ]
        self.log.emit(f"download: ffmpeg={' '.join(cmd)}")
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ffmpeg failed: {err or proc.returncode}")
        if os.path.getsize(tmp.name) <= 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ffmpeg produced empty file: {err or 'no stderr'}")
        return tmp.name

    def _tag_flac(self, path: str, track, cover_bytes: Optional[bytes]) -> None:
        if FLAC is None or Picture is None:
            raise RuntimeError("mutagen is not available for tagging")
        audio = FLAC(path)
        title = getattr(track, "name", None) or getattr(track, "title", None)
        artist = getattr(getattr(track, "artist", None), "name", None)
        album = getattr(getattr(track, "album", None), "name", None)
        track_no = getattr(track, "track_num", None) or getattr(track, "track_number", None)
        if title:
            audio["title"] = [str(title)]
        if artist:
            audio["artist"] = [str(artist)]
        if album:
            audio["album"] = [str(album)]
        if track_no:
            audio["tracknumber"] = [str(track_no)]
        if cover_bytes:
            pic = Picture()
            pic.type = 3  # front cover
            pic.mime = "image/jpeg"
            pic.data = cover_bytes
            audio.clear_pictures()
            audio.add_picture(pic)
        self.log.emit(
            f"download: tags title={bool(title)} artist={bool(artist)} album={bool(album)} cover={bool(cover_bytes)}"
        )
        audio.save()

    def run(self) -> None:
        try:
            self.status.emit("Fetching track info…")
            track = self._session.track(self._track_id)
            if track is None:
                raise RuntimeError("track lookup failed")
            self.log.emit(f"download: track id={getattr(track, 'id', None)}")
            url = None
            tmp_path = None
            try:
                url = tidal_core.get_stream_url(track)
            except Exception:
                url = None
            if url and url.endswith(".flac"):
                self.status.emit("Downloading FLAC…")
                tmp_path = self._download_to_temp(url)
            else:
                stream = None
                try:
                    stream = track.get_stream()
                except Exception:
                    stream = None
                _url, manifest_bytes, manifest_mime = tidal_core.resolve_stream_input(stream, url)
                self.log.emit(
                    f"download: manifest_mime={manifest_mime!r} bytes={len(manifest_bytes or b'')}"
                )
                if manifest_bytes and manifest_mime and "dash" in str(manifest_mime).lower():
                    self.status.emit("Downloading DASH stream…")
                    tmp_path = self._download_dash_to_temp(manifest_bytes)
                elif _url:
                    self.status.emit("Downloading via ffmpeg…")
                    tmp_path = self._download_url_to_temp(_url)
                else:
                    raise RuntimeError("no direct FLAC, DASH manifest, or URL available for download")
            if not tmp_path or os.path.getsize(tmp_path) <= 0:
                raise RuntimeError("download produced empty file")
            try:
                self.status.emit("Writing tags…")
                self._tag_flac(tmp_path, track, self._cover_bytes)
                saved = self._cache.store_download(
                    tmp_path,
                    self._track_id,
                    self._track_meta,
                )
                if not saved:
                    raise RuntimeError("download cache save failed")
                self.log.emit(f"download: saved {saved}")
            except Exception:
                try:
                    if tmp_path:
                        os.unlink(tmp_path)
                except Exception:
                    pass
                raise
            self.finished.emit(saved)
        except Exception as e:
            self.error.emit(tidal_core.safe_str(e))


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
        self._settings = QtCore.QSettings()
        cache_dir = os.path.expanduser("~/.cache/tidal-bitperfect")
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
        self._prefetch_worker: Optional[CoverPrefetchWorker] = None
        self._cover_cache: Dict[str, bytes] = {}
        self._cover_url_cache: Dict[str, bytes] = {}
        self._cover_prefetch_max = 10
        self._last_tracks_mode: Optional[str] = None
        self._download_worker: Optional[DownloadWorker] = None
        self._radio_worker: Optional[RadioWorker] = None
        self._radio_mode: str = "play"
        self._collection_worker: Optional[CollectionWorker] = None
        self._favorite_toggle_worker: Optional[FavoriteToggleWorker] = None
        self._favorite_tracks: List[Dict[str, Any]] = []
        self._favorite_ids: set[str] = set()
        self._favorite_album_ids: set[str] = set()
        self._favorite_playlist_ids: set[str] = set()
        self._favorite_artist_ids: set[str] = set()
        self._cache_tracks: List[Dict[str, Any]] = []
        self._download_tracks: List[Dict[str, Any]] = []
        self._artist_detail_workers: Dict[str, ArtistDetailsWorker] = {}
        self._album_tracks_workers: Dict[str, AlbumTracksWorker] = {}
        self._artist_items: Dict[str, List[QtWidgets.QTreeWidgetItem]] = {}
        self._album_items: Dict[str, List[QtWidgets.QTreeWidgetItem]] = {}
        self._loading_items: List[QtWidgets.QTreeWidgetItem] = []
        self._loading_phase = 0
        self._pending_seek_timer = QtCore.QTimer(self)
        self._pending_seek_timer.setSingleShot(True)
        self._pending_seek_timer.timeout.connect(self._commit_pending_seek)
        self._loading_timer = QtCore.QTimer(self)
        self._loading_timer.setInterval(300)
        self._loading_timer.timeout.connect(self._tick_loading_labels)
        self._offline_mode = False

        self._build_ui()
        self._start_login()

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)

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

        self.tabs = QtWidgets.QTabWidget()

        # Search tab
        search_tab = QtWidgets.QWidget()
        s_layout = QtWidgets.QVBoxLayout(search_tab)
        s_top = QtWidgets.QHBoxLayout()
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText('Search, e.g. "aphex twin flim"')
        self.search_edit.returnPressed.connect(self._do_search)
        self.search_type = QtWidgets.QComboBox()
        self.search_type.setMinimumWidth(110)
        self.search_type.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow
        )
        self.search_type.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed
        )
        self.search_type.addItems(["Tracks", "Albums", "Playlists", "Artists"])
        self.search_limit = QtWidgets.QSpinBox()
        self.search_limit.setRange(1, 50)
        self.search_limit.setValue(10)
        self.search_limit.valueChanged.connect(self._on_search_limit_changed)
        self.search_btn = QtWidgets.QPushButton("Search")
        self.search_btn.clicked.connect(self._do_search)
        s_top.addWidget(self.search_edit, 1)
        s_top.addWidget(QtWidgets.QLabel("Type:"))
        s_top.addWidget(self.search_type)
        s_top.addWidget(QtWidgets.QLabel("Limit:"))
        s_top.addWidget(self.search_limit)
        s_top.addWidget(self.search_btn)
        s_layout.addLayout(s_top)
        self.search_list = QtWidgets.QTreeWidget()
        self.search_list.setHeaderHidden(True)
        self.search_list.itemActivated.connect(self._on_tree_item_activated)
        self.search_list.itemExpanded.connect(self._on_tree_item_expanded)
        self.search_list.currentItemChanged.connect(self._on_selection_changed)
        self.search_list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.search_list.customContextMenuRequested.connect(
            lambda pos: self._show_tree_context_menu(self.search_list, pos)
        )
        s_layout.addWidget(self.search_list, 1)
        self.tabs.addTab(search_tab, "Search")

        # URL tab
        url_tab = QtWidgets.QWidget()
        u_layout = QtWidgets.QVBoxLayout(url_tab)
        u_top = QtWidgets.QHBoxLayout()
        self.url_edit = QtWidgets.QLineEdit()
        self.url_edit.setPlaceholderText("Paste a TIDAL track/album/playlist URL")
        self.url_edit.returnPressed.connect(self._do_url_load)
        self.url_load_btn = QtWidgets.QPushButton("Load")
        self.url_load_btn.clicked.connect(self._do_url_load)
        self.url_queue_btn = QtWidgets.QPushButton("Queue")
        self.url_queue_btn.clicked.connect(self._queue_url_tracks)
        u_top.addWidget(self.url_edit, 1)
        u_top.addWidget(self.url_load_btn)
        u_top.addWidget(self.url_queue_btn)
        u_layout.addLayout(u_top)
        self.url_list = QtWidgets.QTreeWidget()
        self.url_list.setHeaderHidden(True)
        self.url_list.itemActivated.connect(self._on_tree_item_activated)
        self.url_list.itemExpanded.connect(self._on_tree_item_expanded)
        self.url_list.currentItemChanged.connect(self._on_selection_changed)
        self.url_list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.url_list.customContextMenuRequested.connect(
            lambda pos: self._show_tree_context_menu(self.url_list, pos)
        )
        u_layout.addWidget(self.url_list, 1)
        self.tabs.addTab(url_tab, "URL")

        # Collection tab
        fav_tab = QtWidgets.QWidget()
        f_layout = QtWidgets.QVBoxLayout(fav_tab)
        f_top = QtWidgets.QHBoxLayout()
        self.collection_type = QtWidgets.QComboBox()
        self.collection_type.setMinimumWidth(110)
        self.collection_type.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow
        )
        self.collection_type.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed
        )
        self.collection_type.addItems(["Tracks", "Albums", "Playlists", "Artists"])
        self.collection_type.currentTextChanged.connect(self._refresh_collection)
        self.fav_refresh_btn = QtWidgets.QPushButton("Refresh")
        self.fav_refresh_btn.clicked.connect(self._refresh_collection)
        f_top.addWidget(QtWidgets.QLabel("Collection"))
        f_top.addWidget(self.collection_type)
        f_top.addStretch(1)
        f_top.addWidget(self.fav_refresh_btn)
        f_layout.addLayout(f_top)
        self.fav_list = QtWidgets.QTreeWidget()
        self.fav_list.setHeaderHidden(True)
        self.fav_list.itemActivated.connect(self._on_tree_item_activated)
        self.fav_list.itemExpanded.connect(self._on_tree_item_expanded)
        self.fav_list.currentItemChanged.connect(self._on_selection_changed)
        self.fav_list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.fav_list.customContextMenuRequested.connect(
            lambda pos: self._show_tree_context_menu(self.fav_list, pos)
        )
        f_layout.addWidget(self.fav_list, 1)
        self.tabs.addTab(fav_tab, "Collection")

        # Cache tab
        cache_tab = QtWidgets.QWidget()
        c_layout = QtWidgets.QVBoxLayout(cache_tab)

        cache_group = QtWidgets.QGroupBox("Cache")
        cache_layout = QtWidgets.QVBoxLayout(cache_group)
        c_top = QtWidgets.QHBoxLayout()
        self.cache_queue_btn = QtWidgets.QPushButton("Queue")
        self.cache_queue_btn.clicked.connect(self._queue_cache_tracks)
        self.cache_clear_btn = QtWidgets.QPushButton("Clear")
        self.cache_clear_btn.clicked.connect(self._clear_cache)
        self._cache_tab_status_label = QtWidgets.QLabel("")
        self._cache_tab_status_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        self._cache_tab_status_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed
        )
        c_top.addWidget(self._cache_tab_status_label, 1)
        c_top.addWidget(self.cache_queue_btn)
        c_top.addWidget(self.cache_clear_btn)
        cache_layout.addLayout(c_top)
        self.cache_list = QtWidgets.QListWidget()
        self.cache_list.itemActivated.connect(self._play_selected)
        self.cache_list.currentItemChanged.connect(self._on_selection_changed)
        self.cache_list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.cache_list.customContextMenuRequested.connect(
            lambda pos: self._show_track_context_menu(self.cache_list, pos)
        )
        cache_layout.addWidget(self.cache_list, 1)

        c_layout.addWidget(cache_group, 1)

        downloads_group = QtWidgets.QGroupBox("Downloads")
        d_layout = QtWidgets.QVBoxLayout(downloads_group)
        d_top = QtWidgets.QHBoxLayout()
        self._downloads_tab_status_label = QtWidgets.QLabel("")
        self._downloads_tab_status_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        self._downloads_tab_status_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed
        )
        self.downloads_queue_btn = QtWidgets.QPushButton("Queue")
        self.downloads_queue_btn.clicked.connect(self._queue_downloads_tracks)
        self.downloads_clear_btn = QtWidgets.QPushButton("Clear")
        self.downloads_clear_btn.clicked.connect(self._clear_downloads)
        d_top.addWidget(self._downloads_tab_status_label, 1)
        d_top.addWidget(self.downloads_queue_btn)
        d_top.addWidget(self.downloads_clear_btn)
        d_layout.addLayout(d_top)
        self.downloads_list = QtWidgets.QListWidget()
        self.downloads_list.itemActivated.connect(self._play_selected)
        self.downloads_list.currentItemChanged.connect(self._on_selection_changed)
        self.downloads_list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.downloads_list.customContextMenuRequested.connect(
            lambda pos: self._show_track_context_menu(self.downloads_list, pos)
        )
        d_layout.addWidget(self.downloads_list, 1)

        c_layout.addWidget(downloads_group, 1)
        self.tabs.addTab(cache_tab, "Cache")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        split.setHandleWidth(0)
        layout.addWidget(split, 1)

        left_panel = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.tabs, 1)
        split.addWidget(left_panel)

        right_panel = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 0, 0, 0)
        split.addWidget(right_panel)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        now = QtWidgets.QFrame()
        now.setObjectName("nowPlaying")
        now.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        now.setFrameShadow(QtWidgets.QFrame.Shadow.Raised)
        now_layout = QtWidgets.QVBoxLayout(now)
        now_layout.setSpacing(8)
        self.cover_label = CoverImageWidget()
        fallback = None
        icon_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "packaging",
            "linux",
            "tidal-bitperfect-transparent.svg",
        )
        if os.path.exists(icon_path):
            pix = QtGui.QPixmap(icon_path)
            if not pix.isNull():
                fallback = pix
        if fallback is None:
            icon = QtGui.QIcon.fromTheme("tidal-bitperfect")
            if icon.isNull():
                icon = QtGui.QIcon.fromTheme("audio-x-generic")
            if not icon.isNull():
                pix = icon.pixmap(512, 512)
                if not pix.isNull():
                    fallback = pix
        if fallback is not None:
            self.cover_label.set_fallback_pixmap(fallback)
        cover_row = QtWidgets.QHBoxLayout()
        cover_row.addWidget(self.cover_label, 1)
        now_layout.addLayout(cover_row)

        now_text = QtWidgets.QVBoxLayout()
        now_text.setSpacing(2)
        self.now_title = MarqueeLabel("Nothing playing")
        now_title_font = self.now_title.font()
        now_title_font.setPointSize(now_title_font.pointSize() + 2)
        now_title_font.setBold(True)
        self.now_title.setFont(now_title_font)
        title_h = self.now_title.fontMetrics().height()
        self.now_title.setMinimumHeight(title_h)
        self.now_title.setMaximumHeight(title_h)
        self.now_meta = MarqueeLabel("—")
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
        self.quality_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        self.bitrate_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        self.bitperfect_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        now_meta_left.addWidget(self.quality_label)
        now_meta_left.addWidget(self.bitrate_label)
        now_meta_left.addWidget(self.bitperfect_label)
        now_meta_row.addLayout(now_meta_left)
        now_meta_row.addStretch(1)
        now_layout.addLayout(now_meta_row)
        right_layout.addWidget(now, 1)

        controls_row = QtWidgets.QHBoxLayout()
        self.play_next_btn = QtWidgets.QPushButton("Skip")
        self.play_next_btn.clicked.connect(self._play_next_selected)
        self.pause_btn = QtWidgets.QPushButton("Play")
        self.pause_btn.clicked.connect(self._toggle_play_pause)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop_playback)
        self.stop_btn.setEnabled(False)
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

        # Seek control: full-width slider + right-aligned time label below.
        self.seek_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.seek_slider.setEnabled(False)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self.seek_slider.sliderReleased.connect(self._on_seek_released)
        right_layout.addWidget(self.seek_slider)

        self.status_label = QtWidgets.QLabel("Status: starting…")
        right_layout.addWidget(self.status_label)

        diag_row = QtWidgets.QHBoxLayout()
        self.queue_toggle = QtWidgets.QToolButton()
        self.queue_toggle.setText("Show queue")
        self.queue_toggle.setCheckable(True)
        self.queue_toggle.toggled.connect(self._toggle_queue)
        self.settings_btn = QtWidgets.QToolButton()
        self.settings_btn.setText("Settings")
        self.settings_btn.clicked.connect(self._open_settings_window)
        diag_row.addWidget(self.queue_toggle)
        diag_row.addWidget(self.settings_btn)
        diag_row.addStretch(1)
        right_layout.addLayout(diag_row)

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
        self._settings_nudge_anim: Optional[QtCore.QPropertyAnimation] = None
        self._restore_debug_state = False
        self._restore_ffmpeg_disable_state = False
        self._settings_window = None
        self._settings_window_geometry: Optional[bytes] = None
        self._settings_debug_cb: Optional[QtWidgets.QCheckBox] = None
        self._settings_ffmpeg_cb: Optional[QtWidgets.QCheckBox] = None
        self._cache_status_label: Optional[QtWidgets.QLabel] = None
        self._cache_full_label: Optional[QtWidgets.QLabel] = None
        self._cache_size_spin: Optional[QtWidgets.QSpinBox] = None

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
        self.tabs.setCurrentIndex(0)
        self.search_edit.setFocus(QtCore.Qt.FocusReason.ShortcutFocusReason)
        self.search_edit.selectAll()

    def _focus_url(self) -> None:
        self.tabs.setCurrentIndex(1)
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

    def _on_log_window_finished(self, _result: int) -> None:
        if self._log_window is not None:
            self._log_window_geometry = self._log_window.saveGeometry()
            self._settings.setValue("log_window_geometry", self._log_window_geometry)
            self._settings.sync()
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
            if self._log_window_geometry:
                win.restoreGeometry(self._log_window_geometry)
            else:
                win.resize(700, 400)
            win.finished.connect(self._on_log_window_finished)
            self._log_window = win
        self._log_window.show()
        self._log_window.raise_()
        self._log_window.activateWindow()

    def _close_log_window(self) -> None:
        if self._log_window is None:
            return
        self._log_window.close()

    def _on_settings_window_finished(self, _result: int) -> None:
        if self._settings_window is not None:
            self._settings_window_geometry = self._settings_window.saveGeometry()
            self._settings.setValue("settings_window_geometry", self._settings_window_geometry)
            self._settings.sync()
            self._settings_window = None
        self._settings_debug_cb = None
        self._settings_ffmpeg_cb = None
        self._cache_status_label = None
        self._cache_full_label = None
        self._cache_size_spin = None

    def _open_settings_window(self) -> None:
        if self._settings_window is None:
            self._cache.refresh_usage()
            win = QtWidgets.QDialog(self)
            win.setWindowTitle("TIDAL Bitperfect — Settings")
            layout = QtWidgets.QVBoxLayout(win)

            cache_group = QtWidgets.QGroupBox("Cache")
            cache_layout = QtWidgets.QVBoxLayout(cache_group)
            cache_size_row = QtWidgets.QHBoxLayout()
            cache_size_label = QtWidgets.QLabel("Max size (GB):")
            cache_size_spin = QtWidgets.QSpinBox()
            cache_size_spin.setRange(0, 128)
            cache_size_spin.setValue(self._cache_max_gb)
            cache_size_spin.valueChanged.connect(self._on_cache_size_changed)
            cache_size_row.addWidget(cache_size_label)
            cache_size_row.addWidget(cache_size_spin)
            cache_size_row.addStretch(1)
            cache_layout.addLayout(cache_size_row)

            self._cache_status_label = QtWidgets.QLabel("")
            self._cache_full_label = QtWidgets.QLabel("Cache is full; caching is disabled.")
            self._cache_full_label.setVisible(False)
            clear_btn = QtWidgets.QPushButton("Clear cache")
            clear_btn.clicked.connect(self._clear_cache)
            cache_layout.addWidget(self._cache_status_label)
            cache_layout.addWidget(self._cache_full_label)
            cache_layout.addWidget(clear_btn)

            debug_group = QtWidgets.QGroupBox("Diagnostics")
            debug_layout = QtWidgets.QGridLayout(debug_group)
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
            debug_layout.addWidget(debug_cb, 0, 0)
            debug_layout.addWidget(ffmpeg_cb, 0, 1)
            debug_layout.addWidget(cache_cb, 1, 0)
            debug_layout.addWidget(creds_cb, 1, 1)
            debug_layout.setColumnStretch(0, 1)
            debug_layout.setColumnStretch(1, 1)

            layout.addWidget(cache_group)
            layout.addWidget(debug_group)

            if self._settings_window_geometry:
                win.restoreGeometry(self._settings_window_geometry)
            else:
                win.resize(420, 260)
            win.finished.connect(self._on_settings_window_finished)
            self._settings_window = win
            self._settings_debug_cb = debug_cb
            self._settings_ffmpeg_cb = ffmpeg_cb
            self._cache_size_spin = cache_size_spin
            self._update_cache_status_ui()
        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()

    def _on_queue_window_finished(self, _result: int) -> None:
        self._queue_window = None
        self._queue_list = None
        if self.queue_toggle.isChecked():
            with QtCore.QSignalBlocker(self.queue_toggle):
                self.queue_toggle.setChecked(False)
        self.queue_toggle.setText("Show queue")

    def _open_queue_window(self) -> None:
        # Use a top-level window so we don't fight the WM, and recreate each time.
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
        self._queue_window = win
        self._refresh_queue_view()
        self._queue_window.show()
        self._queue_window.raise_()
        self._queue_window.activateWindow()

    def _close_queue_window(self) -> None:
        if self._queue_window is None:
            return
        self._queue_window.close()

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

    def _nudge_settings_button(self) -> None:
        self._nudge_button(self.settings_btn, "_settings_nudge_anim")

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
            self._nudge_settings_button()
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

    def _on_cache_write(self) -> None:
        self._cache.refresh_usage()
        self._update_cache_status_ui()
        if self.tabs.currentIndex() == 3:
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

    def _is_offline(self) -> bool:
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=0.5).close()
            return False
        except Exception:
            return True

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
        self.tabs.setTabEnabled(3, True)
        self.tabs.setCurrentIndex(3)
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
        self.status_label.setText("Status: ready")
        self._set_enabled(True)

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
        tids = [str(t["id"]) for t in self._url_tracks if t.get("id") is not None]
        if not tids:
            return
        # Append in order to preserve album/playlist sequence.
        if self._play_worker is None or not self._play_worker.isRunning():
            first, rest = tids[0], tids[1:]
            self._queue_items.extend(rest)
            self._append_log(
                f"queue: append url list count={len(rest)} (autoplay first)"
            )
            self._refresh_queue_view()
            self._nudge_queue_button()
            self._play_track_id(first)
            return
        self._queue_items.extend(tids)
        self._append_log(f"queue: append url list count={len(tids)}")
        self._refresh_queue_view()
        self._nudge_queue_button()

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
            if item is None or item.treeWidget() is None:
                continue
            item.setText(0, label)
            alive.append(item)
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
            worker = ArtistDetailsWorker(self._session, artist_key)
            worker.ready.connect(self._on_artist_details_ready)
            worker.error.connect(self._on_error)
            worker.finished.connect(lambda: self._artist_detail_workers.pop(artist_key, None))
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
        if not album_id:
            return
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 3, "loading")
        if item.childCount():
            placeholder = item.child(0)
            placeholder.setText(0, "Loading")
            self._register_loading_item(placeholder)
        album_key = str(album_id)
        worker = self._album_tracks_workers.get(album_key)
        if worker is None:
            worker = AlbumTracksWorker(self._session, album_key)
            worker.ready.connect(self._on_album_tracks_ready)
            worker.error.connect(self._on_error)
            worker.finished.connect(lambda: self._album_tracks_workers.pop(album_key, None))
            self._album_tracks_workers[album_key] = worker
            worker.start()
        items = self._album_items.setdefault(album_key, [])
        if item not in items:
            items.append(item)

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

    def _on_album_tracks_ready(self, album_id: str, tracks: List[Dict[str, Any]]) -> None:
        items = self._album_items.get(str(album_id), [])
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
            self._album_items[str(album_id)] = alive
        self._start_cover_prefetch()

    def _populate_artist_item(self, item: QtWidgets.QTreeWidgetItem, artist: Dict[str, Any]) -> None:
        if item.childCount():
            self._unregister_loading_item(item.child(0))
        item.takeChildren()
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, artist)
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 2, "loaded")
        tracks = artist.get("tracks", []) or []
        albums = artist.get("albums", []) or []
        if tracks:
            group = QtWidgets.QTreeWidgetItem(item, ["Top tracks"])
            group.setData(0, QtCore.Qt.ItemDataRole.UserRole, "group")
            for t in tracks:
                if isinstance(t, dict):
                    self._add_track_item(group, t)
        if albums:
            group = QtWidgets.QTreeWidgetItem(item, ["Albums"])
            group.setData(0, QtCore.Qt.ItemDataRole.UserRole, "group")
            for alb in albums:
                if not isinstance(alb, dict):
                    continue
                album_item = QtWidgets.QTreeWidgetItem(
                    group, [tidal_core.format_album_line(alb)]
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
        if not tracks and not albums:
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
            if tree is self.search_list:
                self._search_tracks = flat_tracks
            elif tree is self.url_list:
                self._url_tracks = flat_tracks
            self._start_cover_prefetch()
            return

        if rtype in ("album", "playlist"):
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
            if tree is self.search_list:
                self._search_tracks = flat_tracks
            elif tree is self.url_list:
                self._url_tracks = flat_tracks
            self._start_cover_prefetch()
            return

        if rtype == "artist":
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
            if tree is self.search_list:
                self._search_tracks = flat_tracks
            elif tree is self.url_list:
                self._url_tracks = flat_tracks
            self._start_cover_prefetch()
            return

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
        return payload if isinstance(payload, dict) else None

    def _selected_track_id(self) -> Optional[str]:
        widget = self.search_list if self.tabs.currentIndex() == 0 else (
            self.url_list if self.tabs.currentIndex() == 1 else (
                self.fav_list if self.tabs.currentIndex() == 2 else self._cache_active_list()
            )
        )
        if isinstance(widget, QtWidgets.QTreeWidget):
            item = widget.currentItem()
            if self._tree_item_kind(item) != "track":
                return None
            payload = self._tree_item_payload(item) or {}
            tid = payload.get("id")
            return str(tid) if tid is not None else None
        item = widget.currentItem()
        if item is None:
            return None
        tid = item.data(QtCore.Qt.ItemDataRole.UserRole)
        return str(tid) if tid is not None else None

    def _selected_track(self) -> Optional[Dict[str, Any]]:
        tid = self._selected_track_id()
        if tid is None:
            return None
        return self._track_map_all.get(str(tid))

    def _is_cached_track(self, track_id: str) -> bool:
        return bool(self._cache.get_cached_audio_by_track_id(track_id))

    def _update_open_album_btn(self) -> None:
        pass

    def _refresh_favorites(self) -> None:
        self._refresh_collection()

    def _collection_type_key(self) -> str:
        text = self.collection_type.currentText().strip().lower()
        if text.endswith("s"):
            text = text[:-1]
        return text or "track"

    def _refresh_collection(self) -> None:
        if self._session is None:
            return
        if self._collection_worker is not None and self._collection_worker.isRunning():
            return
        self.status_label.setText("Status: loading collection…")
        item_type = self._collection_type_key()
        worker = CollectionWorker(self._session, item_type, limit=200, offset=0)
        worker.ready.connect(self._on_collection_ready)
        worker.error.connect(self._on_collection_error)
        self._collection_worker = worker
        worker.start()

    def _on_collection_ready(self, item_type: str, items: List[Dict[str, Any]]) -> None:
        self.status_label.setText("Status: ready")
        self._collection_worker = None
        if item_type == "album":
            self._favorite_album_ids = {str(a.get("id")) for a in items if a.get("id") is not None}
            self._render_tree_results(self.fav_list, {"type": "album", "items": items})
            return
        if item_type == "playlist":
            self._favorite_playlist_ids = {str(p.get("id")) for p in items if p.get("id") is not None}
            self._render_tree_results(self.fav_list, {"type": "playlist", "items": items})
            return
        if item_type == "artist":
            self._favorite_artist_ids = {str(a.get("id")) for a in items if a.get("id") is not None}
            self._render_tree_results(self.fav_list, {"type": "artist", "items": items})
            return
        self._favorite_tracks = items
        self._favorite_ids = {str(t.get("id")) for t in items if t.get("id") is not None}
        self._render_tree_results(self.fav_list, {"type": "track", "items": items})

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
        if not self._cache_tracks:
            return
        tids = [str(t["id"]) for t in self._cache_tracks if t.get("id") is not None]
        if not tids:
            return
        if self._play_worker is None or not self._play_worker.isRunning():
            first, rest = tids[0], tids[1:]
            self._queue_items.extend(rest)
            self._append_log(
                f"queue: append cache list count={len(rest)} (autoplay first)"
            )
            self._refresh_queue_view()
            self._nudge_queue_button()
            self._play_track_id(first)
            return
        self._queue_items.extend(tids)
        self._append_log(f"queue: append cache list count={len(tids)}")
        self._refresh_queue_view()
        self._nudge_queue_button()

    def _queue_downloads_tracks(self) -> None:
        if not self._download_tracks:
            return
        tids = [str(t["id"]) for t in self._download_tracks if t.get("id") is not None]
        if not tids:
            return
        if self._play_worker is None or not self._play_worker.isRunning():
            first, rest = tids[0], tids[1:]
            self._queue_items.extend(rest)
            self._append_log(
                f"queue: append downloads list count={len(rest)} (autoplay first)"
            )
            self._refresh_queue_view()
            self._nudge_queue_button()
            self._play_track_id(first)
            return
        self._queue_items.extend(tids)
        self._append_log(f"queue: append downloads list count={len(tids)}")
        self._refresh_queue_view()
        self._nudge_queue_button()

    def _toggle_favorite(self, track_id: str, favorite: bool) -> None:
        if self._session is None:
            return
        if self._favorite_toggle_worker is not None and self._favorite_toggle_worker.isRunning():
            return
        self._append_log(
            f"favorite: track {track_id} -> {'add' if favorite else 'remove'}"
        )
        worker = FavoriteToggleWorker(self._session, "track", track_id, favorite)
        worker.ready.connect(self._on_favorite_toggled)
        worker.error.connect(self._on_favorite_toggle_error)
        self._favorite_toggle_worker = worker
        worker.start()

    def _on_favorite_toggled(self, item_type: str, item_id: str, favorite: bool) -> None:
        self._favorite_toggle_worker = None
        if item_type == "album":
            if favorite:
                self._favorite_album_ids.add(str(item_id))
            else:
                self._favorite_album_ids.discard(str(item_id))
        elif item_type == "playlist":
            if favorite:
                self._favorite_playlist_ids.add(str(item_id))
            else:
                self._favorite_playlist_ids.discard(str(item_id))
        elif item_type == "artist":
            if favorite:
                self._favorite_artist_ids.add(str(item_id))
            else:
                self._favorite_artist_ids.discard(str(item_id))
        else:
            if favorite:
                self._favorite_ids.add(str(item_id))
            else:
                self._favorite_ids.discard(str(item_id))
        if self.tabs.currentIndex() == 2:
            self._refresh_collection()

    def _on_favorite_toggle_error(self, msg: str) -> None:
        self._favorite_toggle_worker = None
        self._append_log(f"favorite: error {msg}")
        QtWidgets.QMessageBox.critical(self, "Favorite error", msg)

    def _toggle_album_favorite(self, album_id: str, favorite: bool) -> None:
        if self._session is None:
            return
        if self._favorite_toggle_worker is not None and self._favorite_toggle_worker.isRunning():
            return
        self._append_log(
            f"favorite: album {album_id} -> {'add' if favorite else 'remove'}"
        )
        worker = FavoriteToggleWorker(self._session, "album", album_id, favorite)
        worker.ready.connect(self._on_favorite_toggled)
        worker.error.connect(self._on_favorite_toggle_error)
        self._favorite_toggle_worker = worker
        worker.start()

    def _toggle_playlist_favorite(self, playlist_id: str, favorite: bool) -> None:
        if self._session is None:
            return
        if self._favorite_toggle_worker is not None and self._favorite_toggle_worker.isRunning():
            return
        self._append_log(
            f"favorite: playlist {playlist_id} -> {'add' if favorite else 'remove'}"
        )
        worker = FavoriteToggleWorker(self._session, "playlist", playlist_id, favorite)
        worker.ready.connect(self._on_favorite_toggled)
        worker.error.connect(self._on_favorite_toggle_error)
        self._favorite_toggle_worker = worker
        worker.start()

    def _toggle_artist_favorite(self, artist_id: str, favorite: bool) -> None:
        if self._session is None:
            return
        if self._favorite_toggle_worker is not None and self._favorite_toggle_worker.isRunning():
            return
        self._append_log(
            f"favorite: artist {artist_id} -> {'add' if favorite else 'remove'}"
        )
        worker = FavoriteToggleWorker(self._session, "artist", artist_id, favorite)
        worker.ready.connect(self._on_favorite_toggled)
        worker.error.connect(self._on_favorite_toggle_error)
        self._favorite_toggle_worker = worker
        worker.start()

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

    def _queue_append(self, track_id: str) -> None:
        if not track_id:
            return
        self._queue_items.append(track_id)
        self._append_log(f"queue: append {track_id}")
        self._refresh_queue_view()
        self._nudge_queue_button()

    def _queue_clear(self) -> None:
        self._queue_items = []
        self._append_log("queue: clear")
        self._refresh_queue_view()

    def _queue_replace(self, items: List[str]) -> None:
        self._queue_items = list(items)
        self._append_log(f"queue: replace count={len(self._queue_items)}")
        self._refresh_queue_view()
        self._nudge_queue_button()

    def _queue_play_next(self) -> None:
        if not self._queue_items:
            return
        next_tid = self._queue_items.pop(0)
        self._append_log(f"queue: play next {next_tid}")
        self._refresh_queue_view()
        self._play_track_id(str(next_tid))

    def _queue_track_ids(self, tids: List[str], autoplay: bool) -> None:
        tids = [t for t in tids if t]
        if not tids:
            return
        if autoplay and (self._play_worker is None or not self._play_worker.isRunning()):
            first, rest = tids[0], tids[1:]
            self._queue_items.extend(rest)
            self._append_log(
                f"queue: append list count={len(rest)} (autoplay first)"
            )
            self._refresh_queue_view()
            self._nudge_queue_button()
            self._play_track_id(first)
            return
        self._queue_items.extend(tids)
        self._append_log(f"queue: append list count={len(tids)}")
        self._refresh_queue_view()
        self._nudge_queue_button()

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
        if has_track and str(track.get("id")) in self._favorite_ids:
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
            make_fav = tid_str not in self._favorite_ids
            self._toggle_favorite(tid_str, make_fav)

        def do_copy_track() -> None:
            tid = track.get("id") if track else None
            if tid is None:
                return
            self._copy_to_clipboard(f"https://tidal.com/track/{tid}")

        def do_open_album() -> None:
            album_id = track.get("album_id") if track else None
            if album_id is None:
                return
            url = f"https://tidal.com/album/{album_id}"
            self.tabs.setCurrentIndex(1)
            self.url_edit.setText(url)
            self._do_url_load()

        def do_open_artist() -> None:
            artist_id = track.get("artist_id") if track else None
            if artist_id is None:
                return
            url = f"https://tidal.com/artist/{artist_id}"
            self.tabs.setCurrentIndex(1)
            self.url_edit.setText(url)
            self._do_url_load()

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
        menu.addAction(play_radio_action)
        menu.addAction(queue_radio_action)
        menu.addAction(append_action)
        menu.addSeparator()
        menu.addAction(copy_track)
        menu.addAction(open_album)
        menu.addAction(open_artist)
        if allow_download:
            menu.addSeparator()
            menu.addAction(download_track)
        menu.addAction(favorite_action)

    def _show_track_context_menu(self, widget: QtWidgets.QListWidget, pos: QtCore.QPoint) -> None:
        item = widget.itemAt(pos)
        track = self._track_for_item(item)
        menu = QtWidgets.QMenu(self)
        self._populate_track_menu(menu, track, item, widget=widget, allow_download=True)
        menu.exec(widget.mapToGlobal(pos))

    def _sanitize_filename(self, name: str) -> str:
        name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
        return name.strip() or "track"

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

        promoted = self._cache.promote_cache_to_download(str(track_id), track)
        if promoted:
            self.status_label.setText("Status: download saved")
            self._refresh_cache_tab()
            self._update_cache_status_ui()
            return

        if self._download_worker is not None and self._download_worker.isRunning():
            QtWidgets.QMessageBox.warning(
                self, "Download in progress", "Another download is already running."
            )
            return

        cover_bytes = self._cover_cache.get(str(track_id))
        if cover_bytes is None:
            cover_url = track.get("cover_url")
            if cover_url and cover_url in self._cover_url_cache:
                cover_bytes = self._cover_url_cache[cover_url]

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

    def _play_radio_next(self, track_id: str) -> None:
        if self._session is None:
            return
        if self._radio_worker is not None and self._radio_worker.isRunning():
            return
        self.status_label.setText("Status: loading radio…")
        self._append_log(f"radio: request track_id={track_id}")
        self._radio_mode = "play"
        worker = RadioWorker(self._session, track_id, limit=30)
        worker.ready.connect(self._on_radio_ready)
        worker.error.connect(self._on_radio_error)
        self._radio_worker = worker
        worker.start()

    def _queue_radio_append(self, track_id: str) -> None:
        if self._session is None:
            return
        if self._radio_worker is not None and self._radio_worker.isRunning():
            return
        self.status_label.setText("Status: loading radio…")
        self._append_log(f"radio: queue request track_id={track_id}")
        self._radio_mode = "queue"
        worker = RadioWorker(self._session, track_id, limit=30)
        worker.ready.connect(self._on_radio_ready)
        worker.error.connect(self._on_radio_error)
        self._radio_worker = worker
        worker.start()

    def _on_radio_ready(self, tracks: List[Dict[str, Any]]) -> None:
        self.status_label.setText("Status: ready")
        self._radio_worker = None
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
        QtWidgets.QMessageBox.critical(self, "Radio error", msg)

    def _on_selection_changed(self, _current, _previous) -> None:
        self._load_cover_for_selected()
        self._update_open_album_btn()

    def _on_tree_item_activated(self, item: QtWidgets.QTreeWidgetItem, _column: int) -> None:
        kind = self._tree_item_kind(item)
        if kind in ("album", "playlist", "artist"):
            item.setExpanded(not item.isExpanded())
            return
        if kind == "track":
            self._play_selected()

    def _show_tree_context_menu(self, tree: QtWidgets.QTreeWidget, pos: QtCore.QPoint) -> None:
        item = tree.itemAt(pos)
        kind = self._tree_item_kind(item)
        payload = self._tree_item_payload(item)
        menu = QtWidgets.QMenu(self)
        if kind == "album" and payload:
            self._populate_album_menu(menu, payload)
        elif kind == "playlist" and payload:
            self._populate_playlist_menu(menu, payload)
        elif kind == "artist" and payload:
            self._populate_artist_menu(menu, payload)
        elif kind == "track" and payload:
            self._populate_track_menu(menu, payload, None, widget=None, allow_download=True)
        else:
            return
        menu.exec(tree.viewport().mapToGlobal(pos))

    def _populate_album_menu(self, menu: QtWidgets.QMenu, album: Dict[str, Any]) -> None:
        play_action = QtGui.QAction("Play album", self)
        queue_action = QtGui.QAction("Queue album", self)
        favorite_action = QtGui.QAction("Favorite", self)
        copy_album = QtGui.QAction("Copy album link", self)
        open_album = QtGui.QAction("Open album", self)

        album_id = album.get("album_id") or album.get("id")
        tracks = album.get("tracks") or []
        has_tracks = bool(tracks)
        play_action.setEnabled(bool(album_id and has_tracks))
        queue_action.setEnabled(bool(album_id and has_tracks))
        copy_album.setEnabled(bool(album_id))
        open_album.setEnabled(bool(album_id))
        if album_id and str(album_id) in self._favorite_album_ids:
            favorite_action.setText("Unfavorite")

        def do_play() -> None:
            self._queue_track_ids([str(t.get("id")) for t in tracks if t.get("id")], autoplay=True)

        def do_queue() -> None:
            self._queue_track_ids([str(t.get("id")) for t in tracks if t.get("id")], autoplay=False)

        def do_copy() -> None:
            if not album_id:
                return
            self._copy_to_clipboard(f"https://tidal.com/album/{album_id}")

        def do_open() -> None:
            if not album_id:
                return
            url = f"https://tidal.com/album/{album_id}"
            self.tabs.setCurrentIndex(1)
            self.url_edit.setText(url)
            self._do_url_load()

        def do_favorite() -> None:
            if not album_id:
                return
            make_fav = str(album_id) not in self._favorite_album_ids
            self._toggle_album_favorite(str(album_id), make_fav)

        play_action.triggered.connect(do_play)
        queue_action.triggered.connect(do_queue)
        favorite_action.triggered.connect(do_favorite)
        copy_album.triggered.connect(do_copy)
        open_album.triggered.connect(do_open)

        menu.addAction(play_action)
        menu.addAction(queue_action)
        menu.addAction(favorite_action)
        menu.addSeparator()
        menu.addAction(copy_album)
        menu.addAction(open_album)

    def _populate_playlist_menu(self, menu: QtWidgets.QMenu, playlist: Dict[str, Any]) -> None:
        play_action = QtGui.QAction("Play playlist", self)
        queue_action = QtGui.QAction("Queue playlist", self)
        favorite_action = QtGui.QAction("Favorite", self)
        copy_playlist = QtGui.QAction("Copy playlist link", self)
        open_playlist = QtGui.QAction("Open playlist", self)

        playlist_id = playlist.get("id")
        tracks = playlist.get("tracks") or []
        has_tracks = bool(tracks)
        play_action.setEnabled(bool(playlist_id and has_tracks))
        queue_action.setEnabled(bool(playlist_id and has_tracks))
        copy_playlist.setEnabled(bool(playlist_id))
        open_playlist.setEnabled(bool(playlist_id))
        if playlist_id and str(playlist_id) in self._favorite_playlist_ids:
            favorite_action.setText("Unfavorite")

        def do_play() -> None:
            self._queue_track_ids([str(t.get("id")) for t in tracks if t.get("id")], autoplay=True)

        def do_queue() -> None:
            self._queue_track_ids([str(t.get("id")) for t in tracks if t.get("id")], autoplay=False)

        def do_copy() -> None:
            if not playlist_id:
                return
            self._copy_to_clipboard(f"https://tidal.com/playlist/{playlist_id}")

        def do_open() -> None:
            if not playlist_id:
                return
            url = f"https://tidal.com/playlist/{playlist_id}"
            self.tabs.setCurrentIndex(1)
            self.url_edit.setText(url)
            self._do_url_load()

        def do_favorite() -> None:
            if not playlist_id:
                return
            make_fav = str(playlist_id) not in self._favorite_playlist_ids
            self._toggle_playlist_favorite(str(playlist_id), make_fav)

        play_action.triggered.connect(do_play)
        queue_action.triggered.connect(do_queue)
        favorite_action.triggered.connect(do_favorite)
        copy_playlist.triggered.connect(do_copy)
        open_playlist.triggered.connect(do_open)

        menu.addAction(play_action)
        menu.addAction(queue_action)
        menu.addAction(favorite_action)
        menu.addSeparator()
        menu.addAction(copy_playlist)
        menu.addAction(open_playlist)

    def _populate_artist_menu(self, menu: QtWidgets.QMenu, artist: Dict[str, Any]) -> None:
        play_action = QtGui.QAction("Play artist", self)
        queue_action = QtGui.QAction("Queue artist", self)
        favorite_action = QtGui.QAction("Favorite", self)
        copy_artist = QtGui.QAction("Copy artist link", self)
        open_artist = QtGui.QAction("Open artist", self)

        artist_id = artist.get("id")
        tracks = artist.get("tracks") or []
        has_tracks = bool(tracks)
        play_action.setEnabled(bool(artist_id and has_tracks))
        queue_action.setEnabled(bool(artist_id and has_tracks))
        copy_artist.setEnabled(bool(artist_id))
        open_artist.setEnabled(bool(artist_id))
        if artist_id and str(artist_id) in self._favorite_artist_ids:
            favorite_action.setText("Unfavorite")

        def do_play() -> None:
            self._queue_track_ids([str(t.get("id")) for t in tracks if t.get("id")], autoplay=True)

        def do_queue() -> None:
            self._queue_track_ids([str(t.get("id")) for t in tracks if t.get("id")], autoplay=False)

        def do_copy() -> None:
            if not artist_id:
                return
            self._copy_to_clipboard(f"https://tidal.com/artist/{artist_id}")

        def do_open() -> None:
            if not artist_id:
                return
            url = f"https://tidal.com/artist/{artist_id}"
            self.tabs.setCurrentIndex(1)
            self.url_edit.setText(url)
            self._do_url_load()

        def do_favorite() -> None:
            if not artist_id:
                return
            make_fav = str(artist_id) not in self._favorite_artist_ids
            self._toggle_artist_favorite(str(artist_id), make_fav)

        play_action.triggered.connect(do_play)
        queue_action.triggered.connect(do_queue)
        favorite_action.triggered.connect(do_favorite)
        copy_artist.triggered.connect(do_copy)
        open_artist.triggered.connect(do_open)

        menu.addAction(play_action)
        menu.addAction(queue_action)
        menu.addAction(favorite_action)
        menu.addSeparator()
        menu.addAction(copy_artist)
        menu.addAction(open_artist)

    def _on_tab_changed(self, _index: int) -> None:
        self._load_cover_for_selected()
        self._update_open_album_btn()
        if self.tabs.currentIndex() == 2 and not self._favorite_tracks:
            self._refresh_collection()
        if self.tabs.currentIndex() == 3:
            self._refresh_cache_tab()

    def _cover_url_for_track_id(self, track_id: str) -> Optional[str]:
        track = self._track_map_all.get(track_id)
        if not track:
            return None
        return track.get("cover_url")

    def _active_tracks(self) -> List[Dict[str, Any]]:
        if self.tabs.currentIndex() == 0:
            return self._search_tracks
        if self.tabs.currentIndex() == 1:
            return self._url_tracks
        if self.tabs.currentIndex() == 3:
            active = self._cache_active_list()
            return self._download_tracks if active is self.downloads_list else self._cache_tracks
        return self._favorite_tracks

    def _load_cover_for_selected(self) -> None:
        if self._session is None and self.tabs.currentIndex() != 3:
            return
        tid = self._selected_track_id()
        if tid is None:
            return
        if self._play_worker is not None and self._play_worker.isRunning():
            if self._current_play is not None and tid != self._current_play[0]:
                return
        self._load_cover_for_track_id(tid, force=False)

    def _load_cover_for_track_id(self, tid: str, force: bool) -> None:
        cached = self._cover_cache.get(tid)
        if cached is not None:
            self._append_log(f"cover: cache hit track={tid}")
            self._cover_request_id = tid
            self._set_cover_bytes(cached)
            return
        cover_url = self._cover_url_for_track_id(tid)
        if cover_url and cover_url in self._cover_url_cache:
            self._append_log(f"cover: url cache hit track={tid}")
            data = self._cover_url_cache[cover_url]
            self._cover_cache[tid] = data
            self._cover_request_id = tid
            self._set_cover_bytes(data)
            return
        if cover_url:
            disk = self._cache.get_cover_bytes(cover_url)
            if disk:
                self._append_log(f"cover: disk cache hit track={tid}")
                self._cover_cache[tid] = disk
                self._cover_url_cache[cover_url] = disk
                self._cover_request_id = tid
                self._set_cover_bytes(disk)
                return
        if self._session is None:
            return
        if not force and self._cover_request_id == tid and self._cover_bytes is not None:
            return
        self._cover_request_id = tid
        if self._cover_worker is not None and self._cover_worker.isRunning():
            self._cover_worker.stop()
        self._set_cover_bytes(None)
        worker = CoverWorker(self._session, tid, cover_url)
        worker.ready.connect(self._on_cover_loaded)
        worker.log.connect(self._append_log)
        worker.finished.connect(lambda: self._on_cover_worker_finished(worker))
        self._cover_worker = worker
        worker.start()

    def _selected_track_max_quality(self) -> Optional[str]:
        # Kept for UI fallback, but prefer PlaybackWorker-provided max to avoid selection races.
        try:
            if self._session is None:
                return None
            tid = self._selected_track_id()
            if tid is None:
                return None
            t = self._session.track(tid)
            return getattr(t, "audio_quality", None)
        except Exception:
            return None

    def _play_next_selected(self) -> None:
        self._queue_play_next()

    def _play_selected(self) -> None:
        tid = self._selected_track_id()
        if tid is None:
            return
        if self._session is None and not self._is_cached_track(tid):
            return
        dev = self.device_combo.currentText().strip()
        if not dev:
            return

        # Guard against double-triggering (e.g. list activation emitting multiple signals)
        # which can otherwise queue a "switch" to the same track/device.
        if self._pending_play == (tid, dev):
            return
        if self._play_worker is not None and self._play_worker.isRunning():
            if self._current_play == (tid, dev):
                return

        if self._play_worker is not None and self._play_worker.isRunning():
            # Interrupt current playback and start the new selection immediately
            # after the playback thread exits.
            self._pending_play = (tid, dev)
            self.status_label.setText("Status: switching track…")
            self.stop_btn.setEnabled(False)
            self._play_worker.stop()
            return

        self._start_playback(tid, dev)

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

        self.stop_btn.setEnabled(True)
        self.pause_btn.setEnabled(True)
        self.status_label.setText("Status: starting playback…")
        self._current_play = (tid, dev)
        self._play_worker = PlaybackWorker(
            self._session,
            tid,
            dev,
            disable_ffmpeg=self._disable_ffmpeg,
            cache_manager=self._cache,
            track_meta=self._track_map_all.get(str(tid)),
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
        self._load_cover_for_track_id(tid, force=True)
        self._play_worker.start()

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
        if not dev:
            self.bitperfect_label.setText("Bit-perfect: —")
            return
        if not dev.startswith("hw:"):
            self.bitperfect_label.setText("Bit-perfect: unlikely (not hw:)")
            return
        if self._stream_info is None or self._audio_fmt is None:
            self.bitperfect_label.setText("Bit-perfect: unknown (stream/format pending)")
            return
        decode_note = ""
        if self._decode_path:
            decode_note = f" | {self._decode_path}"
        si = self._stream_info
        af = self._audio_fmt
        is_match = bool(si.sample_rate and si.bit_depth and af.rate == si.sample_rate and af.bits == si.bit_depth)
        is_bitperfect = bool(is_match)
        if is_bitperfect:
            self.bitperfect_label.setText("Bit-perfect: yes" + decode_note)
            return
        if si.sample_rate and af.rate != si.sample_rate:
            self.bitperfect_label.setText(
                f"Bit-perfect: no ({af.rate}Hz != {si.sample_rate}Hz){decode_note}"
            )
            return
        if si.bit_depth and af.bits != si.bit_depth:
            if si.bit_depth == 24 and af.bits == 32:
                self.bitperfect_label.setText(
                    f"Bit-perfect: padded (24/32 PCM){decode_note}"
                )
                return
            self.bitperfect_label.setText(
                f"Bit-perfect: no ({af.bits}-bit != {si.bit_depth}-bit){decode_note}"
            )
            return
        self.bitperfect_label.setText("Bit-perfect: likely" + decode_note)

    def _set_cover_bytes(self, data: Optional[bytes]) -> None:
        self._cover_bytes = data
        self.cover_label.set_bytes(data)

    def _on_cover_loaded(self, track_id: str, data: Optional[bytes]) -> None:
        if track_id != self._cover_request_id:
            return
        if data:
            self._cover_cache[track_id] = data
            cover_url = self._cover_url_for_track_id(track_id)
            if cover_url:
                self._cover_url_cache[cover_url] = data
                self._cache.store_cover_bytes(cover_url, data)
                self._update_cache_status_ui()
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
        if self.tabs.currentIndex() == 0:
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
                self._cover_cache[tid_str] = data
                if tid_str == self._cover_request_id:
                    self._set_cover_bytes(data)
                continue
            if cover_url:
                disk = self._cache.get_cover_bytes(cover_url)
                if disk:
                    self._cover_cache[tid_str] = disk
                    self._cover_url_cache[cover_url] = disk
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
        self._cover_cache[track_id] = data
        if cover_url:
            self._cover_url_cache[cover_url] = data
            self._cache.store_cover_bytes(cover_url, data)
            self._update_cache_status_ui()
        if track_id == self._cover_request_id:
            self._set_cover_bytes(data)

    def _on_prefetch_worker_finished(self, worker: CoverPrefetchWorker) -> None:
        if self._prefetch_worker is worker:
            self._prefetch_worker = None

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)

    def _set_now_playing(self, track: Optional[Dict[str, Any]]) -> None:
        if not track:
            self.now_title.setText("Nothing playing")
            self.now_meta.setText("—")
            return
        title = track.get("title") or "Unknown title"
        artist = track.get("artist") or "Unknown artist"
        album = track.get("album") or ""
        self.now_title.setText(title)
        self.now_meta.setText(f"{artist} - {album}" if album else artist)

    def _stop_playback(self) -> None:
        self._cancel_pending_seek()
        if self._play_worker is None:
            return
        self.status_label.setText("Status: stopping…")
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("Play")
        self.seek_slider.setEnabled(False)
        self._stopped_by_user = True
        self._pending_play = None
        self._play_worker.stop()

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
        QtWidgets.QMessageBox.critical(self, "Playback error", msg)

    def _on_playback_thread_finished(self) -> None:
        self._cancel_pending_seek()
        self._play_worker = None
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
            return
        if not self._play_had_error and self._queue_items and self._session is not None:
            next_tid = self._queue_items.pop(0)
            dev = self.device_combo.currentText().strip()
            if dev:
                self._start_playback(next_tid, dev)
                return
        self._queue_now_playing_id = None
        self._refresh_queue_view()

    def _toggle_pause(self) -> None:
        if self._play_worker is None or not self._play_worker.isRunning():
            return
        self._play_worker.toggle_pause()
        # Optimistic UI update; worker status signal will correct it if needed.
        self.pause_btn.setText("Resume" if self.pause_btn.text() == "Pause" else "Pause")

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

    def _on_seek_pressed(self) -> None:
        self._seeking = True

    def _on_seek_released(self) -> None:
        self._cancel_pending_seek()
        if self._play_worker is None or not self._play_worker.isRunning():
            self._seeking = False
            return
        if self._duration_s <= 0:
            self._seeking = False
            return
        target_s = float(self.seek_slider.value()) / 1000.0
        self._pos_s = target_s
        self.seek_time.setText(
            f"{self._format_time(self._pos_s)} / {self._format_time(self._duration_s)}"
        )
        self._play_worker.seek_to(target_s)
        self._seeking = False

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

    def closeEvent(self, event) -> None:
        try:
            self._settings.sync()
            self._cancel_pending_seek()
            if self._play_worker is not None and self._play_worker.isRunning():
                self._play_worker.stop()
                self._play_worker.wait(2000)
            if self._radio_worker is not None and self._radio_worker.isRunning():
                self._radio_worker.wait(2000)
            if self._favorites_worker is not None and self._favorites_worker.isRunning():
                self._favorites_worker.wait(2000)
            if self._favorite_toggle_worker is not None and self._favorite_toggle_worker.isRunning():
                self._favorite_toggle_worker.wait(2000)
            if self._queue_window is not None:
                self._queue_window.close()
            if self._settings_window is not None:
                self._settings_window.close()
            if self._download_worker is not None and self._download_worker.isRunning():
                self._download_worker.stop()
                self._download_worker.wait(2000)
            if self._cover_worker is not None and self._cover_worker.isRunning():
                self._cover_worker.stop()
                self._cover_worker.wait(1000)
            if self._prefetch_worker is not None and self._prefetch_worker.isRunning():
                self._prefetch_worker.stop()
                self._prefetch_worker.wait(1000)
            if hasattr(self, "_tracks_worker") and self._tracks_worker is not None:
                if self._tracks_worker.isRunning():
                    self._tracks_worker.wait(1000)
            if hasattr(self, "_login") and self._login is not None:
                if self._login.isRunning():
                    self._login.wait(1000)
        finally:
            super().closeEvent(event)


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    QtCore.QCoreApplication.setOrganizationName("tidal-bitperfect")
    QtCore.QCoreApplication.setOrganizationDomain("local")
    QtCore.QCoreApplication.setApplicationName("tidal-bitperfect")
    QtGui.QGuiApplication.setApplicationDisplayName("TIDAL Bitperfect")
    # On Wayland, this influences xdg-shell app_id, which controls window grouping/isolation.
    QtGui.QGuiApplication.setDesktopFileName("tidal-bitperfect")

    # Nicer default icon/title when launched from a desktop entry.
    app.setApplicationDisplayName("TIDAL Bitperfect")
    win = MainWindow()
    win.resize(900, 650)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
