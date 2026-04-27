#!/usr/bin/env python3

import array
import json
import hashlib
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional, List, Dict, Any

import alsaaudio
import tidalapi
try:
    import soundfile as sf
except Exception:
    sf = None
try:
    from mutagen.flac import FLAC, Picture
except Exception:
    FLAC = None
    Picture = None
from PySide6 import QtCore

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


@dataclass
class OpenedFlac:
    sound_file: "sf.SoundFile"
    path: str
    bits: int
    dtype: str
    should_delete: bool


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

    def _safe_filename_part(self, text: Optional[str], fallback: str) -> str:
        if text is None:
            text = ""
        value = str(text).strip()
        if not value:
            value = fallback
        value = re.sub(r"[^0-9A-Za-z ._'-]+", "_", value)
        value = re.sub(r"\s+", " ", value).strip()
        if not value:
            value = fallback
        return value[:120]

    def _download_path(self, track_id: str, meta: Optional[Dict[str, Any]] = None) -> str:
        safe_id = re.sub(r"[^0-9A-Za-z_-]+", "_", track_id) or self._hash_key(track_id)
        if meta:
            artist = self._safe_filename_part(meta.get("artist"), "Unknown Artist")
            title = self._safe_filename_part(meta.get("title"), "Track")
            name = f"{artist} - {title} [{safe_id}]"
        else:
            name = safe_id
        return os.path.join(self._downloads_dir, f"{name}.flac")

    def _parse_download_id(self, filename: str) -> str:
        stem = os.path.splitext(filename)[0]
        match = re.search(r"\[([0-9A-Za-z_-]+)\]$", stem)
        if match:
            return match.group(1)
        return stem

    def _find_download_path(self, track_id: str) -> Optional[str]:
        downloads = self._index.get("downloads", {})
        if isinstance(downloads, dict) and str(track_id) in downloads:
            info = downloads.get(str(track_id)) or {}
            path = info.get("path")
            if path and os.path.exists(path):
                return path
        safe_id = re.sub(r"[^0-9A-Za-z_-]+", "_", track_id) or self._hash_key(track_id)
        try:
            suffix = f"[{safe_id}].flac"
            for name in os.listdir(self._downloads_dir):
                if name.endswith(suffix):
                    return os.path.join(self._downloads_dir, name)
        except Exception:
            pass
        legacy = os.path.join(self._downloads_dir, f"{safe_id}.flac")
        if os.path.exists(legacy):
            return legacy
        return None

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
            path = self._find_download_path(track_id)
            if path and os.path.exists(path):
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
                    tid = self._parse_download_id(name)
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
        dest = self._download_path(track_id, meta)
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
        dest = self._download_path(track_id, meta)
        if os.path.exists(dest):
            try:
                size = os.path.getsize(src)
            except Exception:
                size = 0
            removed_src = False
            try:
                os.unlink(src)
                removed_src = True
            except Exception:
                pass
            if removed_src:
                if size:
                    self._used_bytes = max(0, self._used_bytes - size)
                audio = self._index.get("audio", {})
                if isinstance(audio, dict):
                    audio.pop(str(track_id), None)
                self._full = self._max_bytes == 0 or self._used_bytes >= self._max_bytes
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
            entry["artists"] = meta.get("artists")
            entry["artist_display"] = meta.get("artist_display")
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
            entry["artists"] = meta.get("artists")
            entry["artist_display"] = meta.get("artist_display")
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
        path = self._find_download_path(track_id)
        return bool(path and os.path.exists(path))

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
        path = None
        if isinstance(downloads, dict) and str(track_id) in downloads:
            info = downloads.get(str(track_id)) or {}
            path = info.get("path")
            downloads.pop(str(track_id), None)
        if not path:
            path = self._find_download_path(track_id)
        if path and os.path.exists(path):
            try:
                os.unlink(path)
                removed = True
            except Exception:
                pass
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


def tag_flac_path(path: str, meta: Optional[Dict[str, Any]], cover_bytes: Optional[bytes]) -> bool:
    if FLAC is None or Picture is None:
        return False
    try:
        audio = FLAC(path)
        title = meta.get("title") if meta else None
        artists = tidal_core.artist_names(meta) if meta else []
        album = meta.get("album") if meta else None
        if title:
            audio["title"] = [str(title)]
        if artists:
            audio["artist"] = [str(artist) for artist in artists]
        if album:
            audio["album"] = [str(album)]
        if cover_bytes:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.data = cover_bytes
            audio.clear_pictures()
            audio.add_picture(pic)
        audio.save()
        return True
    except Exception:
        return False


def open_alsa(device: str, fmt: AudioFormat) -> alsaaudio.PCM:
    if fmt.bits == 16:
        alsa_fmt = alsaaudio.PCM_FORMAT_S16_LE
    elif fmt.bits == 24:
        alsa_fmt = alsaaudio.PCM_FORMAT_S24_3LE
    elif fmt.bits == 32:
        alsa_fmt = alsaaudio.PCM_FORMAT_S32_LE
    else:
        raise RuntimeError(f"unsupported bits per sample: {fmt.bits}")

    pcm_kwargs = {
        "type": alsaaudio.PCM_PLAYBACK,
        "mode": alsaaudio.PCM_NORMAL,
        "device": device,
        "channels": fmt.channels,
        "rate": fmt.rate,
        "format": alsa_fmt,
        "periodsize": 4096,
    }

    if not device.startswith("hw:"):
        with _PIPEWIRE_ALSA_ENV_LOCK:
            previous = os.environ.get("PIPEWIRE_ALSA")
            # PipeWire's ALSA shim reads stream props from PIPEWIRE_ALSA at open time.
            os.environ["PIPEWIRE_ALSA"] = _build_pipewire_alsa_env(previous)
            try:
                return alsaaudio.PCM(**pcm_kwargs)
            finally:
                if previous is None:
                    os.environ.pop("PIPEWIRE_ALSA", None)
                else:
                    os.environ["PIPEWIRE_ALSA"] = previous

    return alsaaudio.PCM(**pcm_kwargs)


_PIPEWIRE_ALSA_ENV_LOCK = threading.Lock()
_PIPEWIRE_STREAM_PROPERTIES = {
    "application.name": "TIDAL Bitperfect",
    "node.name": "tidal-bitperfect",
    "node.nick": "TIDAL",
    "node.description": "TIDAL Bitperfect Playback",
    "media.name": "TIDAL Bitperfect",
    "media.software": "TIDAL Bitperfect",
    "media.role": "Music",
}


def _quote_pipewire_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_pipewire_alsa_env(existing: Optional[str]) -> str:
    body_parts: List[str] = []
    if existing:
        stripped = existing.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            stripped = stripped[1:-1].strip()
        if stripped:
            body_parts.append(stripped)
    for key, value in _PIPEWIRE_STREAM_PROPERTIES.items():
        body_parts.append(f'{key} = "{_quote_pipewire_value(value)}"')
    return "{ " + " ".join(body_parts) + " }"


def _normalize_volume_percent(percent: float) -> float:
    normalized = max(0.0, min(1.0, float(percent) / 100.0))
    # Human loudness perception is non-linear. A simple audio taper makes the
    # slider feel closer to a typical app volume control than raw linear gain.
    return normalized * normalized


def _apply_software_volume(data: bytes, bits: int, gain: float) -> bytes:
    if not data or gain >= 0.9999:
        return data
    if gain <= 0.0:
        return bytes(len(data))
    if bits == 16:
        sample_type = "h"
        min_value = -32768
        max_value = 32767
    elif bits == 32:
        sample_type = "i"
        min_value = -2147483648
        max_value = 2147483647
    else:
        return data

    samples = array.array(sample_type)
    samples.frombytes(data)
    for idx, sample in enumerate(samples):
        scaled = int(sample * gain)
        if scaled < min_value:
            scaled = min_value
        elif scaled > max_value:
            scaled = max_value
        samples[idx] = scaled
    return samples.tobytes()


def _stream_score(info: StreamInfo) -> tuple[int, int, int]:
    return (
        tidal_core.quality_rank(info.audio_quality),
        int(info.bit_depth or 0),
        int(info.sample_rate or 0),
    )


def _build_stream_info(stream: object) -> StreamInfo:
    return StreamInfo(
        track_max_quality=None,
        audio_quality=getattr(stream, "audio_quality", None),
        bit_depth=getattr(stream, "bit_depth", None),
        sample_rate=getattr(stream, "sample_rate", None),
    )


def _resolve_track_max_quality(track: object) -> Optional[str]:
    track_max = getattr(track, "audio_quality", None)
    tags = getattr(track, "media_metadata_tags", None) or {}
    if isinstance(tags, dict):
        for key, value in tags.items():
            if not value:
                continue
            normalized = str(key).upper()
            if (
                "HIRES_LOSSLESS" in normalized
                or "HI_RES_LOSSLESS" in normalized
                or normalized == "HIRES"
            ):
                return "HI_RES_LOSSLESS"
    return track_max


def _ffmpeg_available(disable_ffmpeg: bool) -> bool:
    return not disable_ffmpeg and shutil.which("ffmpeg") is not None


def _select_best_stream(
    session: tidalapi.Session,
    track_id: str,
    disable_ffmpeg: bool,
    log: Optional[Callable[[str], None]] = None,
    require_direct_without_ffmpeg: bool = True,
) -> tuple[object, object, Optional[str], StreamInfo, float, bool]:
    original_quality = getattr(getattr(session, "config", None), "quality", None)
    candidates = []
    last_err: Optional[Exception] = None

    try:
        for quality in tidal_core.quality_preference() or [original_quality]:
            try:
                if quality is not None:
                    session.config.quality = quality
                track = session.track(track_id)
                try:
                    stream = track.get_stream()
                except Exception:
                    stream = None
                try:
                    url = tidal_core.get_stream_url(track)
                except Exception:
                    url = None
                candidates.append((quality, track, stream, url, _build_stream_info(stream)))
            except Exception as exc:
                last_err = exc
                continue
    finally:
        if original_quality is not None:
            try:
                session.config.quality = original_quality
            except Exception:
                pass

    if not candidates:
        if last_err is not None:
            raise last_err
        raise RuntimeError("could not load stream candidates")

    ffmpeg_available = _ffmpeg_available(disable_ffmpeg)
    if not ffmpeg_available:
        direct_candidates = [candidate for candidate in candidates if candidate[3] is not None]
        if direct_candidates:
            candidates = direct_candidates
            if log is not None:
                log("ffmpeg not found; using direct stream only")
        elif require_direct_without_ffmpeg:
            raise RuntimeError(
                "ffmpeg not found and no direct stream available (DASH/manifest only)"
            )

    chosen_quality, track, stream, url, info = max(candidates, key=lambda item: _stream_score(item[4]))
    info.track_max_quality = _resolve_track_max_quality(track)
    duration_s = float(getattr(track, "duration", 0) or 0)

    if log is not None:
        log(f"track id={getattr(track, 'id', None)} title={getattr(track, 'title', None)!r}")
        log(f"track max audio_quality={getattr(track, 'audio_quality', None)}")
        log(f"chosen session quality={chosen_quality}")
        log(
            f"stream audio_quality={info.audio_quality} bit_depth={info.bit_depth} sample_rate={info.sample_rate}"
        )

    return track, stream, url, info, duration_s, ffmpeg_available


def _materialize_stream_input(
    stream: object,
    url: Optional[str],
    temp_prefix: str,
    log: Optional[Callable[[str], None]] = None,
) -> tuple[str, Optional[str], Optional[str]]:
    resolved_url, manifest_bytes, manifest_mime = tidal_core.resolve_stream_input(stream, url)
    mpd_path = None
    if manifest_bytes and manifest_mime and "dash" in str(manifest_mime).lower():
        tmp = tempfile.NamedTemporaryFile(prefix=temp_prefix, suffix=".mpd", delete=False)
        tmp.write(manifest_bytes)
        tmp.flush()
        tmp.close()
        mpd_path = tmp.name
        if log is not None:
            log(f"using DASH MPD input: {mpd_path}")
    elif resolved_url is not None and log is not None:
        log("using direct URL input")

    if resolved_url is None and mpd_path is None:
        raise RuntimeError("no playable URL or manifest was available for this track")

    inp = mpd_path if mpd_path is not None else resolved_url
    assert inp is not None
    return inp, resolved_url, mpd_path


def _flac_decode_params(subtype: str) -> tuple[int, str]:
    if subtype == "PCM_16":
        return 16, "int16"
    if subtype in ("PCM_24", "PCM_32"):
        return 32, "int32"
    raise ValueError(f"unsupported FLAC subtype {subtype!r}")


def _open_flac_soundfile(path: str, should_delete: bool = False) -> OpenedFlac:
    if sf is None:
        raise RuntimeError("soundfile is not available")
    sound_file = sf.SoundFile(path, "r")
    if getattr(sound_file, "format", "").upper() != "FLAC":
        try:
            sound_file.close()
        except Exception:
            pass
        raise ValueError("not a FLAC stream")
    try:
        bits, dtype = _flac_decode_params(getattr(sound_file, "subtype", ""))
    except Exception:
        try:
            sound_file.close()
        except Exception:
            pass
        raise
    return OpenedFlac(
        sound_file=sound_file,
        path=path,
        bits=bits,
        dtype=dtype,
        should_delete=should_delete,
    )


def _download_url_to_temp(
    url: str,
    *,
    temp_prefix: str,
    timeout: int,
    stop_check: Optional[Callable[[], bool]] = None,
    stop_message: str = "download stopped",
    log: Optional[Callable[[str], None]] = None,
    log_status: bool = False,
    set_response: Optional[Callable[[Optional[object]], None]] = None,
) -> tuple[str, int, float]:
    tmp = tempfile.NamedTemporaryFile(prefix=temp_prefix, suffix=".flac", delete=False)
    try:
        start = time.time()
        total = 0
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if set_response is not None:
                set_response(resp)
            if log is not None and log_status:
                log(f"status={getattr(resp, 'status', None)}")
            while True:
                if stop_check is not None and stop_check():
                    raise RuntimeError(stop_message)
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                tmp.write(chunk)
        tmp.flush()
        return tmp.name, total, max(0.0, time.time() - start)
    except Exception:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
        raise
    finally:
        if set_response is not None:
            set_response(None)
        try:
            tmp.close()
        except Exception:
            pass


def _transcode_to_flac(
    inp: str,
    *,
    temp_prefix: str,
    protocol_whitelist: bool = False,
    log: Optional[Callable[[str], None]] = None,
    log_command_prefix: str = "",
    set_process: Optional[Callable[[Optional[subprocess.Popen]], None]] = None,
) -> tuple[str, int, float]:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found")

    out = tempfile.NamedTemporaryFile(prefix=temp_prefix, suffix=".flac", delete=False)
    out.close()
    try:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if protocol_whitelist:
            cmd += ["-protocol_whitelist", "file,https,tls,tcp,crypto"]
        cmd += ["-i", inp, "-c:a", "flac", "-f", "flac", out.name]
        if log is not None:
            log(f"{log_command_prefix}{' '.join(cmd)}")
        start = time.time()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if set_process is not None:
            set_process(proc)
        _stdout, stderr = proc.communicate()
        elapsed = max(0.0, time.time() - start)
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
            raise RuntimeError(f"ffmpeg failed: {err or proc.returncode}")
        size = os.path.getsize(out.name)
        if size <= 0:
            err = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
            raise RuntimeError(f"ffmpeg produced empty file: {err or 'no stderr'}")
        return out.name, size, elapsed
    except Exception:
        try:
            os.unlink(out.name)
        except Exception:
            pass
        raise
    finally:
        if set_process is not None:
            set_process(None)



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
    track_advanced = QtCore.Signal(str)  # track_id — gapless transition to next track

    def __init__(
        self,
        session: Optional[tidalapi.Session],
        track_id: str,
        device: str,
        volume_percent: int,
        disable_ffmpeg: bool,
        cache_manager: Optional[CacheManager],
        track_meta: Optional[Dict[str, Any]] = None,
        gapless: bool = False,
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
        self._resp: Optional[object] = None
        self._cmdq: "queue.Queue[tuple[str, float]]" = queue.Queue()
        self._paused = False
        self._software_volume_enabled = bool(device) and not device.startswith("hw:")
        self._volume = _normalize_volume_percent(volume_percent)
        self._gapless = gapless
        self._next_track_path: Optional[str] = None
        self._next_track_id: Optional[str] = None

    def stop(self) -> None:
        self._stop = True
        self._cmdq.put(("stop", 0.0))
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.kill()
            except Exception:
                pass
            try:
                if self._proc.stdout is not None:
                    self._proc.stdout.close()
            except Exception:
                pass
            try:
                if self._proc.stderr is not None:
                    self._proc.stderr.close()
            except Exception:
                pass
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:
                pass

    def toggle_pause(self) -> None:
        self._cmdq.put(("pause_toggle", 0.0))

    def seek(self, delta_s: float) -> None:
        self._cmdq.put(("seek", float(delta_s)))

    def seek_to(self, pos_s: float) -> None:
        self._cmdq.put(("seek_to", float(pos_s)))

    def set_volume(self, percent: int) -> None:
        self._cmdq.put(("set_volume", float(percent)))

    def _set_volume(self, percent: float) -> None:
        self._volume = _normalize_volume_percent(percent)

    def _apply_volume(self, data: bytes, bits: int) -> bytes:
        if not self._software_volume_enabled:
            return data
        return _apply_software_volume(data, bits, self._volume)

    def _seek_target_s(
        self,
        start_offset_s: float,
        bytes_written: int,
        bytes_per_second: float,
        duration_s: float,
        arg: float,
        *,
        absolute: bool,
    ) -> float:
        if absolute:
            target = max(0.0, float(arg))
        else:
            current_pos_s = bytes_written / bytes_per_second
            target = max(0.0, start_offset_s + current_pos_s + arg)
        if duration_s > 0:
            target = min(duration_s, target)
        return target

    def _seek_command_target_s(
        self,
        cmd: str,
        arg: float,
        start_offset_s: float,
        bytes_written: int,
        bytes_per_second: float,
        duration_s: float,
    ) -> Optional[float]:
        if bytes_per_second <= 0:
            return None
        absolute = cmd == "seek_to"
        target = self._seek_target_s(
            start_offset_s,
            bytes_written,
            bytes_per_second,
            duration_s,
            arg,
            absolute=absolute,
        )
        self.status.emit("Seeking…")
        if absolute:
            self._dbg(f"seek_to target={target:.3f}s")
        else:
            self._dbg(f"seek delta={arg:.3f}s -> offset={target:.3f}s")
        return target

    def _emit_position_if_due(
        self,
        start_offset_s: float,
        bytes_written: int,
        bytes_per_second: float,
        duration_s: float,
        last_pos_emit: float,
    ) -> float:
        if duration_s <= 0 or bytes_per_second <= 0:
            return last_pos_emit
        now = time.time()
        if now - last_pos_emit < 0.25:
            return last_pos_emit
        self.position.emit(start_offset_s + (bytes_written / bytes_per_second), duration_s)
        return now

    # ------------------------------------------------------------------
    # Gapless helpers
    # ------------------------------------------------------------------

    def _play_soundfile_to_pcm(
        self,
        f: "sf.SoundFile",
        pcm: alsaaudio.PCM,
        fmt: AudioFormat,
        dtype: str,
        duration_s: float,
    ) -> None:
        """Read *f* and write PCM to the already-open ALSA *pcm*.

        Returns normally on EOF.  Breaks out early if ``self._stop`` is set.
        Handles pause / seek commands from ``self._cmdq`` during playback.
        """
        frame_size = fmt.channels * (fmt.bits // 8)
        bytes_per_second = float(fmt.rate) * float(frame_size) if fmt.rate and frame_size else 0.0
        bytes_written = 0
        start_offset_s = 0.0
        last_pos_emit = 0.0
        chunk_frames = 4096

        while not self._stop:
            # Drain command queue
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
                    if cmd == "set_volume":
                        self._set_volume(arg)
                    if cmd == "seek":
                        target = self._seek_command_target_s(
                            cmd,
                            arg,
                            start_offset_s,
                            bytes_written,
                            bytes_per_second,
                            duration_s,
                        )
                        if target is None:
                            continue
                        start_offset_s = target
                        bytes_written = 0
                        pcm = self._restart_flac_playback(
                            f, pcm, fmt, start_offset_s, fmt.rate, duration_s
                        )
                    if cmd == "seek_to":
                        target = self._seek_command_target_s(
                            cmd,
                            arg,
                            start_offset_s,
                            bytes_written,
                            bytes_per_second,
                            duration_s,
                        )
                        if target is None:
                            continue
                        start_offset_s = target
                        bytes_written = 0
                        pcm = self._restart_flac_playback(
                            f, pcm, fmt, start_offset_s, fmt.rate, duration_s
                        )
            except queue.Empty:
                pass

            if self._paused:
                time.sleep(0.05)
                continue

            data = f.buffer_read(chunk_frames, dtype=dtype)
            if not data:
                return  # EOF — caller handles gapless continuation
            if frame_size > 0:
                whole = (len(data) // frame_size) * frame_size
                if whole:
                    pcm.write(self._apply_volume(data[:whole], fmt.bits))
                    bytes_written += whole
                    last_pos_emit = self._emit_position_if_due(
                        start_offset_s,
                        bytes_written,
                        bytes_per_second,
                        duration_s,
                        last_pos_emit,
                    )

    def _try_gapless_next(self, pcm: alsaaudio.PCM, fmt: AudioFormat) -> bool:
        """Attempt a gapless transition to a prefetched next track.

        Returns ``True`` if the next track was played to completion on *pcm*
        (caller should loop to try another).  Returns ``False`` if no suitable
        prefetch was available or formats don't match (caller falls back to the
        normal gapped path).
        """
        if not self._gapless or not self._next_track_path:
            return False

        path = self._next_track_path
        tid = self._next_track_id
        self._next_track_path = None
        self._next_track_id = None

        opened = self._open_flac_cached(path)
        if opened is None:
            self._dbg(f"gapless: could not open prefetched file {path}")
            return False

        f = opened.sound_file
        next_fmt = AudioFormat(
            channels=int(f.channels),
            rate=int(f.samplerate),
            bits=opened.bits,
        )
        if next_fmt != fmt:
            self._dbg(
                f"gapless: format mismatch ({fmt} -> {next_fmt}); falling back to gap"
            )
            try:
                f.close()
            except Exception:
                pass
            return False

        # Same format — true gapless transition
        self._dbg(f"gapless: transitioning to track {tid}")
        self.track_advanced.emit(tid)

        duration_s = float(f.frames) / float(f.samplerate) if getattr(f, "frames", 0) else 0.0
        self.fmt_ready.emit(next_fmt)
        if duration_s > 0:
            self.position.emit(0.0, duration_s)
        self.status.emit("Playing")

        try:
            self._play_soundfile_to_pcm(f, pcm, fmt, opened.dtype, duration_s)
        finally:
            try:
                f.close()
            except Exception:
                pass
        return True

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
        try:
            tmp_path, total, elapsed = _download_url_to_temp(
                url,
                temp_prefix="tidal_",
                timeout=15,
                stop_check=lambda: self._stop,
                log=self._dbg,
                set_response=lambda resp: setattr(self, "_resp", resp),
            )
            if total > 0:
                mb = total / (1024.0 * 1024.0)
                rate = mb / elapsed if elapsed > 0 else 0.0
                self._dbg(f"FLAC download: {mb:.1f} MB in {elapsed:.2f}s ({rate:.2f} MB/s)")
            return tmp_path
        except Exception:
            self._dbg_exc("flac download failed")
            return None

    def _open_flac(self, url: str) -> Optional[OpenedFlac]:
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
            opened = _open_flac_soundfile(tmp_path, should_delete=cached_path is None)
        except ValueError as exc:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            self._dbg(f"{exc}; falling back to ffmpeg")
            return None
        except Exception:
            self._dbg_exc("flac open failed")
            try:
                os.unlink(tmp_path)
            except Exception:
                self._dbg_exc("flac temp cleanup failed")
            self._dbg("FLAC open failed; falling back to ffmpeg")
            return None
        self._dbg(
            f"FLAC format: {opened.sound_file.channels}ch @ {opened.sound_file.samplerate}Hz "
            f"{getattr(opened.sound_file, 'subtype', '')}"
        )
        return opened

    def _open_flac_cached(self, path: str) -> Optional[OpenedFlac]:
        if sf is None:
            return None
        try:
            opened = _open_flac_soundfile(path)
        except ValueError as exc:
            self._dbg(f"cached {exc}")
            return None
        except Exception:
            self._dbg_exc("cached flac open failed")
            return None
        self._dbg(
            f"cached FLAC format: {opened.sound_file.channels}ch @ {opened.sound_file.samplerate}Hz "
            f"{getattr(opened.sound_file, 'subtype', '')}"
        )
        return opened

    def _play_flac_opened(
        self,
        opened: OpenedFlac,
        duration_s: float,
    ) -> bool:
        f = opened.sound_file
        pcm = None
        try:
            self.decode_path.emit("libsndfile")
            ch = int(f.channels)
            rate = int(f.samplerate)
            bytes_per_sample = opened.bits // 8
            frame_size = ch * bytes_per_sample
            fmt = AudioFormat(channels=ch, rate=rate, bits=opened.bits)
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
                        if cmd == "set_volume":
                            self._set_volume(arg)
                        if cmd == "seek":
                            target = self._seek_command_target_s(
                                cmd,
                                arg,
                                start_offset_s,
                                bytes_written,
                                bytes_per_second,
                                duration_s,
                            )
                            if target is None:
                                continue
                            start_offset_s = target
                            bytes_written = 0
                            pcm = self._restart_flac_playback(
                                f, pcm, fmt, start_offset_s, rate, duration_s
                            )
                        if cmd == "seek_to":
                            target = self._seek_command_target_s(
                                cmd,
                                arg,
                                start_offset_s,
                                bytes_written,
                                bytes_per_second,
                                duration_s,
                            )
                            if target is None:
                                continue
                            start_offset_s = target
                            bytes_written = 0
                            pcm = self._restart_flac_playback(
                                f, pcm, fmt, start_offset_s, rate, duration_s
                            )
                except queue.Empty:
                    pass

                if self._paused:
                    time.sleep(0.05)
                    continue

                data = f.buffer_read(chunk_frames, dtype=opened.dtype)
                if not data:
                    break
                if frame_size > 0:
                    whole = (len(data) // frame_size) * frame_size
                    if whole:
                        pcm.write(self._apply_volume(data[:whole], fmt.bits))
                        bytes_written += whole
                        last_pos_emit = self._emit_position_if_due(
                            start_offset_s,
                            bytes_written,
                            bytes_per_second,
                            duration_s,
                            last_pos_emit,
                        )

            # Gapless continuation: chain tracks while prefetch is ready
            if not self._stop and pcm is not None:
                try:
                    f.close()
                except Exception:
                    pass
                f = None  # type: ignore[assignment]
                while self._try_gapless_next(pcm, fmt):
                    pass

            return True
        finally:
            try:
                if f is not None:
                    f.close()
            except Exception:
                pass
            try:
                if pcm is not None:
                    pcm.close()
            except Exception:
                pass
            if opened.should_delete:
                try:
                    os.unlink(opened.path)
                except Exception:
                    pass

    def _play_flac(self, url: str, duration_s: float) -> bool:
        opened = self._open_flac(url)
        if opened is None:
            return False
        return self._play_flac_opened(opened, duration_s)

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
                    if cmd == "set_volume":
                        self._set_volume(arg)
                    if cmd == "seek":
                        # Seek is best-effort for streaming/DASH inputs.
                        target = self._seek_command_target_s(
                            cmd,
                            arg,
                            start_offset_s,
                            bytes_written,
                            bytes_per_second,
                            duration_s,
                        )
                        if target is None:
                            continue
                        start_offset_s = target
                        bytes_written = 0
                        buf = bytearray()

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
                        target = self._seek_command_target_s(
                            cmd,
                            arg,
                            start_offset_s,
                            bytes_written,
                            bytes_per_second,
                            duration_s,
                        )
                        if target is None:
                            continue
                        start_offset_s = target
                        bytes_written = 0
                        buf = bytearray()

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

            if self._stop:
                break
            chunk = self._proc.stdout.read(16384)
            if not chunk:
                # EOF — try gapless continuation before exiting
                if not self._stop and self._gapless and self._next_track_path:
                    # Terminate current ffmpeg process cleanly
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
                        pass
                    # Chain gapless tracks via soundfile on the same ALSA pcm
                    while self._try_gapless_next(pcm, fmt):
                        pass
                break
            buf.extend(chunk)
            if frame_size <= 0:
                continue
            whole = (len(buf) // frame_size) * frame_size
            if whole:
                try:
                    pcm.write(self._apply_volume(bytes(buf[:whole]), fmt.bits))
                    bytes_written += whole
                    last_pos_emit = self._emit_position_if_due(
                        start_offset_s,
                        bytes_written,
                        bytes_per_second,
                        duration_s,
                        last_pos_emit,
                    )
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
        mpd_path = None
        try:
            self.status.emit("Loading stream…")

            cached_path = None
            if self._cache is not None:
                cached_path = self._cache.get_cached_audio_by_track_id(self._track_id)
            if cached_path and sf is not None:
                self._dbg(f"cached flac hit: {cached_path}")
                opened = self._open_flac_cached(cached_path)
                if opened is not None:
                    sinfo = StreamInfo(
                        track_max_quality=None,
                        audio_quality=None,
                        bit_depth=opened.bits or None,
                        sample_rate=int(getattr(opened.sound_file, "samplerate", 0) or 0) or None,
                    )
                    self.stream_info.emit(sinfo)
                    if self._play_flac_opened(opened, duration_s=0.0):
                        return
            if self._session is None:
                raise RuntimeError("offline: track is not cached")

            _track, stream, url, sinfo, duration_s, ffmpeg_available = _select_best_stream(
                self._session,
                self._track_id,
                self._disable_ffmpeg,
                log=self._dbg,
            )
            inp, url, mpd_path = _materialize_stream_input(stream, url, "tidal_", log=self._dbg)

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

            pcm = self._play_ffmpeg(inp, mpd_path, url, sinfo, duration_s)

        except Exception as e:
            had_error = True
            self.error.emit(tidal_core.safe_str(e))
        finally:
            try:
                if mpd_path:
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


class PrefetchWorker(QtCore.QThread):
    """Pre-downloads the next queued track to cache for gapless playback.

    For direct FLAC streams the file is downloaded as-is.  For DASH/manifest
    streams ffmpeg transcodes to a FLAC temp file.  Either way the result is a
    cached FLAC path that PlaybackWorker can open via ``_open_flac_cached``.
    """

    ready = QtCore.Signal(str, str)   # (track_id, cached_flac_path)
    failed = QtCore.Signal(str)       # track_id
    log = QtCore.Signal(str)

    def __init__(
        self,
        session: tidalapi.Session,
        track_id: str,
        cache_manager: Optional[CacheManager],
        disable_ffmpeg: bool,
        track_meta: Optional[Dict[str, Any]] = None,
    ):
        super().__init__()
        self._source_session = session
        self._session: Optional[tidalapi.Session] = None
        self._track_id = track_id
        self._cache = cache_manager
        self._disable_ffmpeg = disable_ffmpeg
        self._track_meta = track_meta
        self._stop = False
        self._resp: Optional[object] = None
        self._proc: Optional[subprocess.Popen] = None

    def stop(self) -> None:
        self._stop = True
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:
                pass
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.kill()
            except Exception:
                pass

    def _dbg(self, msg: str) -> None:
        self.log.emit(f"prefetch [{self._track_id}]: {msg}")

    def _dbg_exc(self, context: str) -> None:
        self._dbg(f"{context}: {traceback.format_exc().strip()}")

    def _clone_session(self) -> Optional[tidalapi.Session]:
        """Create a prefetch-local session to avoid cross-thread config races."""
        quality = getattr(getattr(self._source_session, "config", None), "quality", None)
        config = tidalapi.Config(quality=quality) if quality is not None else tidalapi.Config()
        cloned = tidalapi.Session(config)

        token_type = getattr(self._source_session, "token_type", None)
        access_token = getattr(self._source_session, "access_token", None)
        refresh_token = getattr(self._source_session, "refresh_token", None)
        expiry_time = getattr(self._source_session, "expiry_time", None)

        if token_type and access_token:
            try:
                ok = cloned.load_oauth_session(
                    token_type, access_token, refresh_token, expiry_time
                )
                if ok and cloned.check_login():
                    return cloned
            except Exception:
                self._dbg_exc("prefetch session clone (in-memory oauth) failed")

        try:
            if tidal_core.load_saved_oauth(cloned):
                return cloned
        except Exception:
            self._dbg_exc("prefetch session clone (saved oauth) failed")
        return None

    def _ensure_session(self) -> bool:
        if self._session is not None:
            return True
        cloned = self._clone_session()
        if cloned is not None:
            self._session = cloned
            self._dbg("using isolated session for prefetch")
            return True
        self._session = self._source_session
        self._dbg("using shared session for prefetch (isolation unavailable)")
        return True

    def _resolve_stream(
        self,
    ) -> tuple[Optional[str], Optional[str], bool]:
        if self._session is None:
            return None, None, False
        try:
            if self._stop:
                return None, None, False
            _track, stream, url, _sinfo, _duration_s, _ffmpeg = _select_best_stream(
                self._session,
                self._track_id,
                self._disable_ffmpeg,
                log=self._dbg,
                require_direct_without_ffmpeg=False,
            )
            inp, _url, mpd_path = _materialize_stream_input(
                stream,
                url,
                "tidal_prefetch_",
                log=self._dbg,
            )
            return inp, mpd_path, mpd_path is not None
        except Exception:
            return None, None, False

    def _download_flac(self, url: str) -> Optional[str]:
        try:
            tmp_path, total, elapsed = _download_url_to_temp(
                url,
                temp_prefix="tidal_prefetch_",
                timeout=30,
                stop_check=lambda: self._stop,
                stop_message="prefetch stopped",
                set_response=lambda resp: setattr(self, "_resp", resp),
            )
            if total > 0:
                mb = total / (1024.0 * 1024.0)
                rate = mb / elapsed if elapsed > 0 else 0.0
                self._dbg(f"FLAC download: {mb:.1f} MB in {elapsed:.2f}s ({rate:.2f} MB/s)")
            return tmp_path
        except Exception:
            self._dbg_exc("flac download failed")
            return None

    def run(self) -> None:
        try:
            # 1. Check cache first — instant return if hit
            if self._cache is not None:
                cached = self._cache.get_cached_audio_by_track_id(self._track_id)
                if cached:
                    self._dbg(f"cache hit: {cached}")
                    self.ready.emit(self._track_id, cached)
                    return

            if self._stop:
                self.failed.emit(self._track_id)
                return

            if not self._ensure_session():
                self.failed.emit(self._track_id)
                return

            # 2. Resolve stream
            inp, mpd_path, is_dash = self._resolve_stream()
            if inp is None or self._stop:
                self.failed.emit(self._track_id)
                return

            # 3. Download / transcode to FLAC
            tmp_path: Optional[str] = None
            if not is_dash and inp is not None:
                # Try direct FLAC download first
                tmp_path = self._download_flac(inp)
                if tmp_path is not None:
                    # Verify it's actually a FLAC file
                    if sf is not None:
                        try:
                            with sf.SoundFile(tmp_path, "r") as f:
                                if getattr(f, "format", "").upper() != "FLAC":
                                    self._dbg("not a FLAC stream; trying ffmpeg transcode")
                                    os.unlink(tmp_path)
                                    tmp_path = None
                        except Exception:
                            self._dbg("downloaded file not decodable as FLAC; trying ffmpeg")
                            try:
                                os.unlink(tmp_path)
                            except Exception:
                                pass
                            tmp_path = None

            # Fall back to ffmpeg transcode for DASH or non-FLAC
            if tmp_path is None and not self._stop:
                ffmpeg_available = _ffmpeg_available(self._disable_ffmpeg)
                if ffmpeg_available:
                    try:
                        tmp_path, size, elapsed = _transcode_to_flac(
                            inp,
                            temp_prefix="tidal_prefetch_",
                            protocol_whitelist=mpd_path is not None,
                            log=self._dbg,
                            log_command_prefix="ffmpeg transcode: ",
                            set_process=lambda proc: setattr(self, "_proc", proc),
                        )
                        self._dbg(
                            f"ffmpeg transcode: {size / (1024 * 1024):.1f} MB in {elapsed:.2f}s"
                        )
                    except Exception:
                        self._dbg_exc("ffmpeg transcode failed")
                else:
                    self._dbg("ffmpeg not available; cannot prefetch this track")

            # Clean up mpd temp file
            if mpd_path is not None:
                try:
                    os.unlink(mpd_path)
                except Exception:
                    pass

            if tmp_path is None or self._stop:
                if tmp_path is not None:
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
                self.failed.emit(self._track_id)
                return

            # 4. Store in cache. PlaybackWorker treats prefetched paths as
            # cache-owned, so do not hand off an unmanaged temp file.
            if self._cache is None:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                self.failed.emit(self._track_id)
                return
            stored = self._cache.store_audio(
                tmp_path, self._track_id, "", self._track_meta
            )
            if stored:
                tmp_path = stored
            else:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                self.failed.emit(self._track_id)
                return

            self._dbg("ready")
            self.ready.emit(self._track_id, tmp_path)

        except Exception:
            self._dbg_exc("prefetch failed")
            self.failed.emit(self._track_id)


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
        self.log.emit(f"download: direct url={url}")
        tmp_path, total, elapsed = _download_url_to_temp(
            url,
            temp_prefix="tidal_dl_",
            timeout=15,
            stop_check=lambda: self._stop,
            log=self.log.emit,
            log_status=True,
        )
        if total <= 0:
            raise RuntimeError("downloaded 0 bytes")
        mb = total / (1024.0 * 1024.0)
        rate = mb / elapsed if elapsed > 0 else 0.0
        self.log.emit(f"download: wrote {mb:.1f} MB in {elapsed:.2f}s ({rate:.2f} MB/s)")
        return tmp_path

    def _tag_flac(self, path: str, track, cover_bytes: Optional[bytes]) -> None:
        if FLAC is None or Picture is None:
            raise RuntimeError("mutagen is not available for tagging")
        audio = FLAC(path)
        title = getattr(track, "name", None) or getattr(track, "title", None)
        artists = tidal_core.artist_names(track)
        album = getattr(getattr(track, "album", None), "name", None)
        track_no = getattr(track, "track_num", None) or getattr(track, "track_number", None)
        if title:
            audio["title"] = [str(title)]
        if artists:
            audio["artist"] = [str(artist) for artist in artists]
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
            f"download: tags title={bool(title)} artist={bool(artists)} album={bool(album)} cover={bool(cover_bytes)}"
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
                inp, _url, mpd_path = _materialize_stream_input(stream, url, "tidal_dl_")
                try:
                    if mpd_path is not None:
                        self.status.emit("Downloading DASH stream…")
                        self.log.emit(f"download: DASH via ffmpeg mpd={mpd_path}")
                    else:
                        self.status.emit("Downloading via ffmpeg…")
                    tmp_path, _size, _elapsed = _transcode_to_flac(
                        inp,
                        temp_prefix="tidal_dl_",
                        protocol_whitelist=True,
                        log=self.log.emit,
                        log_command_prefix="download: ffmpeg=",
                    )
                finally:
                    if mpd_path is not None:
                        try:
                            os.unlink(mpd_path)
                        except Exception:
                            pass
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
