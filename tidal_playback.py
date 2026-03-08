#!/usr/bin/env python3

import json
import hashlib
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import time
import traceback
import urllib.request
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

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
        artist = meta.get("artist") if meta else None
        album = meta.get("album") if meta else None
        if title:
            audio["title"] = [str(title)]
        if artist:
            audio["artist"] = [str(artist)]
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

    return alsaaudio.PCM(
        type=alsaaudio.PCM_PLAYBACK,
        mode=alsaaudio.PCM_NORMAL,
        device=device,
        channels=fmt.channels,
        rate=fmt.rate,
        format=alsa_fmt,
        periodsize=4096,
    )



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
                            f, pcm, fmt, start_offset_s, fmt.rate, duration_s
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

        f, _path, bits, dtype, _should_delete = opened
        next_fmt = AudioFormat(channels=int(f.channels), rate=int(f.samplerate), bits=bits)
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
            self._play_soundfile_to_pcm(f, pcm, fmt, dtype, duration_s)
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
        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(prefix="tidal_", suffix=".flac", delete=False)
            start = time.time()
            total = 0
            with urllib.request.urlopen(url, timeout=15) as resp:
                self._resp = resp
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
            self._resp = None
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

    # ------------------------------------------------------------------
    # Stream resolution (mirrors PlaybackWorker._select_stream / _resolve_input)
    # ------------------------------------------------------------------

    def _resolve_stream(
        self,
    ) -> tuple[Optional[str], Optional[str], bool]:
        """Resolve the best stream for this track.

        Returns ``(url_or_mpd_input, mpd_path_or_None, is_dash)``.
        """
        if self._session is None:
            return None, None, False
        session = self._session
        original_quality = getattr(session.config, "quality", None)
        try:
            candidates: list = []
            for q in tidal_core.quality_preference() or [original_quality]:
                if self._stop:
                    return None, None, False
                try:
                    if q is not None:
                        session.config.quality = q
                    track = session.track(self._track_id)
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
                except Exception:
                    continue

            if not candidates:
                return None, None, False

            ffmpeg_available = shutil.which("ffmpeg") is not None
            if self._disable_ffmpeg:
                ffmpeg_available = False

            if not ffmpeg_available:
                direct = [c for c in candidates if c[3] is not None]
                if direct:
                    candidates = direct

            def _score(item):  # type: ignore[no-untyped-def]
                _q, _t, _s, _u, info = item
                return (
                    tidal_core.quality_rank(info.audio_quality),
                    int(info.bit_depth or 0),
                    int(info.sample_rate or 0),
                )

            _chosen_q, _track, stream, url, _sinfo = sorted(
                candidates, key=_score, reverse=True
            )[0]

            url, manifest_bytes, manifest_mime = tidal_core.resolve_stream_input(
                stream, url
            )
            mpd_path: Optional[str] = None
            is_dash = False
            if manifest_bytes and manifest_mime and "dash" in str(manifest_mime).lower():
                tmp = tempfile.NamedTemporaryFile(
                    prefix="tidal_prefetch_", suffix=".mpd", delete=False
                )
                tmp.write(manifest_bytes)
                tmp.flush()
                tmp.close()
                mpd_path = tmp.name
                is_dash = True
                self._dbg("DASH manifest detected")

            inp = mpd_path if mpd_path is not None else url
            return inp, mpd_path, is_dash
        finally:
            if original_quality is not None:
                try:
                    session.config.quality = original_quality
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Download helpers
    # ------------------------------------------------------------------

    def _download_flac(self, url: str) -> Optional[str]:
        """Download a direct FLAC stream to a temp file.  Returns path or None."""
        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(
                prefix="tidal_prefetch_", suffix=".flac", delete=False
            )
            start = time.time()
            total = 0
            with urllib.request.urlopen(url, timeout=30) as resp:
                self._resp = resp
                while True:
                    if self._stop:
                        raise RuntimeError("prefetch stopped")
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
                    pass
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass
            return None
        finally:
            self._resp = None
            if tmp is not None:
                try:
                    tmp.close()
                except Exception:
                    pass

    def _transcode_to_flac(self, inp: str, mpd_path: Optional[str]) -> Optional[str]:
        """Run ffmpeg to transcode a DASH/non-FLAC stream to a temp FLAC file."""
        out = None
        try:
            out = tempfile.NamedTemporaryFile(
                prefix="tidal_prefetch_", suffix=".flac", delete=False
            )
            out.close()
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
            if mpd_path is not None:
                cmd += ["-protocol_whitelist", "file,https,tls,tcp,crypto"]
            cmd += ["-i", inp, "-c:a", "flac", "-f", "flac", out.name]
            self._dbg(f"ffmpeg transcode: {' '.join(cmd)}")
            start = time.time()
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            _stdout, stderr = self._proc.communicate()
            rc = self._proc.returncode
            self._proc = None
            elapsed = max(0.0, time.time() - start)
            if rc != 0:
                err = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
                self._dbg(f"ffmpeg transcode failed (rc={rc}): {err}")
                try:
                    os.unlink(out.name)
                except Exception:
                    pass
                return None
            size = os.path.getsize(out.name)
            self._dbg(
                f"ffmpeg transcode: {size / (1024*1024):.1f} MB in {elapsed:.2f}s"
            )
            return out.name
        except Exception:
            self._dbg_exc("ffmpeg transcode failed")
            if out is not None:
                try:
                    os.unlink(out.name)
                except Exception:
                    pass
            return None

    # ------------------------------------------------------------------

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
                ffmpeg_available = shutil.which("ffmpeg") is not None and not self._disable_ffmpeg
                if ffmpeg_available:
                    tmp_path = self._transcode_to_flac(inp, mpd_path)
                else:
                    self._dbg("ffmpeg not available; cannot prefetch this track")

            # Clean up mpd temp file
            if mpd_path is not None:
                try:
                    os.unlink(mpd_path)
                except Exception:
                    pass

            if tmp_path is None or self._stop:
                self.failed.emit(self._track_id)
                return

            # 4. Store in cache
            if self._cache is not None:
                stored = self._cache.store_audio(
                    tmp_path, self._track_id, "", self._track_meta
                )
                if stored:
                    tmp_path = stored

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
