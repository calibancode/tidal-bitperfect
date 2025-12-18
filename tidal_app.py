#!/usr/bin/env python3

import sys
import time
import subprocess
import os
import tempfile
import queue
import signal
import select
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

import alsaaudio
import tidalapi
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
    ready = QtCore.Signal(list)  # List[Dict]
    error = QtCore.Signal(str)

    def __init__(self, session: tidalapi.Session, mode: str, text: str, limit: int):
        super().__init__()
        self._session = session
        self._mode = mode
        self._text = text
        self._limit = limit

    def run(self) -> None:
        try:
            if self._mode == "search":
                tracks = tidal_core.search_tracks(self._session, self._text, limit=self._limit)
                self.ready.emit(tracks)
                return
            if self._mode == "url":
                _kind, tracks = tidal_core.tracks_for_link(self._session, self._text)
                self.ready.emit(tracks)
                return
            raise ValueError(f"unknown mode: {self._mode}")
        except Exception as e:
            self.error.emit(tidal_core.safe_str(e))


class PlaybackWorker(QtCore.QThread):
    status = QtCore.Signal(str)
    log = QtCore.Signal(str)
    error = QtCore.Signal(str)
    fmt_ready = QtCore.Signal(object)  # AudioFormat
    stream_info = QtCore.Signal(object)  # StreamInfo
    position = QtCore.Signal(float, float)  # pos_s, duration_s (approx)
    finished_ok = QtCore.Signal()

    def __init__(self, session: tidalapi.Session, track_id: str, device: str, debug: bool):
        super().__init__()
        self._session = session
        self._track_id = track_id
        self._device = device
        self._debug = debug
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
        if self._debug:
            msg += f"\nstream url: {url}"
            msg += f"\nffmpeg rc: {rc}"
        if err:
            msg += f"\nffmpeg stderr:\n{err}"
        return msg

    def _dbg(self, msg: str) -> None:
        if self._debug:
            self.log.emit(f"debug: {msg}")

    def run(self) -> None:
        pcm = None
        had_error = False
        try:
            self.status.emit("Loading stream…")
            original_quality = getattr(self._session.config, "quality", None)

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

            # Prefer the manifest path when present (needed for HI_RES in many cases).
            manifest_bytes = None
            manifest_mime = None
            if stream is not None:
                manifest_mime = getattr(stream, "manifest_mime_type", None)
                manifest_bytes = tidal_core.decode_manifest_b64(getattr(stream, "manifest", None))

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

            if self._debug:
                self._dbg(f"stream url: {url}")

            self.stream_info.emit(sinfo)
            if duration_s > 0:
                self.position.emit(0.0, duration_s)

            # Many ALSA hw devices (incl. some USB DACs) do not accept packed 24-bit (S24_3LE).
            # Use 32-bit PCM for 24-bit sources to ensure reliable playback; sample rate is preserved.
            codec = "pcm_s16le"
            if sinfo.bit_depth == 24:
                codec = "pcm_s32le"
            elif sinfo.bit_depth == 32:
                codec = "pcm_s32le"

            inp = mpd_path if mpd_path is not None else url
            assert inp is not None
            def start_ffmpeg(codec_name: str, start_s: float = 0.0) -> subprocess.Popen:
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

            if mpd_path is not None:
                # Allow ffmpeg to fetch HTTPS segments referenced by the MPD.
                pass
            self._proc = start_ffmpeg(codec, start_s=0.0)
            assert self._proc.stdout is not None
            assert self._proc.stderr is not None

            try:
                ch, rate, bits, block_align = tidal_core.parse_wav_header(self._proc.stdout)
            except Exception as e:
                raise RuntimeError(self._ffmpeg_fail(f"decode failed: {tidal_core.safe_str(e)}", url))

            bytes_per_sample = max(1, int(block_align) // int(ch))
            bits = bytes_per_sample * 8
            fmt = AudioFormat(channels=ch, rate=rate, bits=bits)
            self.fmt_ready.emit(fmt)
            self._dbg(
                f"wav fmt: ch={ch} rate={rate} bits={bits} block_align={block_align} bytes_per_sample={bytes_per_sample}"
            )
            self.status.emit(f"Opening ALSA device…")
            pcm = open_alsa(self._device, fmt)
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
                            try:
                                if pcm is not None:
                                    pcm.pause(1 if self._paused else 0)
                            except Exception:
                                pass
                            try:
                                if self._proc is not None and self._proc.pid:
                                    os.kill(
                                        self._proc.pid,
                                        signal.SIGSTOP if self._paused else signal.SIGCONT,
                                    )
                            except Exception:
                                pass
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
                            try:
                                if pcm is not None:
                                    pcm.close()
                            except Exception:
                                pass
                            try:
                                if self._proc is not None:
                                    self._proc.terminate()
                                    self._proc.wait(timeout=1)
                            except Exception:
                                pass

                            self._proc = start_ffmpeg(codec, start_s=start_offset_s)
                            assert self._proc.stdout is not None
                            assert self._proc.stderr is not None

                            try:
                                ch, rate, bits, block_align = tidal_core.parse_wav_header(
                                    self._proc.stdout
                                )
                            except Exception as e2:
                                raise RuntimeError(
                                    self._ffmpeg_fail(
                                        f"decode failed after seek: {tidal_core.safe_str(e2)}", url
                                    )
                                )

                            frame_size = int(block_align)
                            bytes_per_second = (
                                float(rate) * float(frame_size) if rate and frame_size else 0.0
                            )
                            bytes_per_sample = max(1, int(block_align) // int(ch))
                            bits = bytes_per_sample * 8
                            fmt = AudioFormat(channels=ch, rate=rate, bits=bits)
                            self.fmt_ready.emit(fmt)
                            pcm = open_alsa(self._device, fmt)
                            try:
                                pcm.pause(1 if self._paused else 0)
                            except Exception:
                                pass
                            try:
                                if self._proc is not None and self._proc.pid and self._paused:
                                    os.kill(self._proc.pid, signal.SIGSTOP)
                            except Exception:
                                pass
                            self.status.emit("Paused" if self._paused else "Playing")
                            if duration_s > 0:
                                self.position.emit(start_offset_s, duration_s)
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
                            try:
                                if pcm is not None:
                                    pcm.close()
                            except Exception:
                                pass
                            try:
                                if self._proc is not None:
                                    self._proc.terminate()
                                    self._proc.wait(timeout=1)
                            except Exception:
                                pass

                            self._proc = start_ffmpeg(codec, start_s=start_offset_s)
                            assert self._proc.stdout is not None
                            assert self._proc.stderr is not None

                            try:
                                ch, rate, bits, block_align = tidal_core.parse_wav_header(
                                    self._proc.stdout
                                )
                            except Exception as e2:
                                raise RuntimeError(
                                    self._ffmpeg_fail(
                                        f"decode failed after seek: {tidal_core.safe_str(e2)}", url
                                    )
                                )

                            frame_size = int(block_align)
                            bytes_per_second = (
                                float(rate) * float(frame_size) if rate and frame_size else 0.0
                            )
                            bytes_per_sample = max(1, int(block_align) // int(ch))
                            bits = bytes_per_sample * 8
                            fmt = AudioFormat(channels=ch, rate=rate, bits=bits)
                            self.fmt_ready.emit(fmt)
                            pcm = open_alsa(self._device, fmt)
                            try:
                                pcm.pause(1 if self._paused else 0)
                            except Exception:
                                pass
                            try:
                                if self._proc is not None and self._proc.pid and self._paused:
                                    os.kill(self._proc.pid, signal.SIGSTOP)
                            except Exception:
                                pass
                            self.status.emit("Paused" if self._paused else "Playing")
                            if duration_s > 0:
                                self.position.emit(start_offset_s, duration_s)
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
                            self._proc = start_ffmpeg("pcm_s32le")
                            assert self._proc.stdout is not None
                            assert self._proc.stderr is not None
                            try:
                                ch, rate, bits, block_align = tidal_core.parse_wav_header(
                                    self._proc.stdout
                                )
                            except Exception as e2:
                                raise RuntimeError(
                                    self._ffmpeg_fail(
                                        f"decode failed after fallback: {tidal_core.safe_str(e2)}",
                                        url,
                                    )
                                )
                            fmt = AudioFormat(channels=ch, rate=rate, bits=bits)
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

        except Exception as e:
            had_error = True
            self.error.emit(tidal_core.safe_str(e))
        finally:
            try:
                if original_quality is not None:
                    self._session.config.quality = original_quality
            except Exception:
                pass
            try:
                if "mpd_path" in locals() and mpd_path:
                    os.unlink(mpd_path)
            except Exception:
                pass
            try:
                if pcm is not None:
                    pcm.close()
            except Exception:
                pass
            try:
                if self._proc is not None:
                    self._proc.terminate()
                    self._proc.wait(timeout=1)
            except Exception:
                pass
            try:
                if self._session.check_login():
                    tidal_core.save_oauth(self._session)
            except Exception:
                pass
            if not had_error:
                self.finished_ok.emit()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TIDAL Bitperfect (ALSA)")

        self._session: Optional[tidalapi.Session] = None
        self._tracks: List[Dict[str, Any]] = []
        self._play_worker: Optional[PlaybackWorker] = None
        self._play_had_error = False
        self._pending_play: Optional[tuple[str, str]] = None
        self._settings = QtCore.QSettings()
        self._stream_info: Optional[StreamInfo] = None
        self._audio_fmt: Optional[AudioFormat] = None
        self._duration_s: float = 0.0
        self._pos_s: float = 0.0
        self._seeking = False

        self._build_ui()
        self._start_login()

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)

        device_row = QtWidgets.QHBoxLayout()
        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.setEditable(True)
        self.device_combo.currentTextChanged.connect(self._save_device_pref)
        self.refresh_devices_btn = QtWidgets.QPushButton("Refresh devices")
        self.refresh_devices_btn.clicked.connect(self._refresh_devices)
        device_row.addWidget(QtWidgets.QLabel("ALSA device:"))
        device_row.addWidget(self.device_combo, 1)
        device_row.addWidget(self.refresh_devices_btn)
        layout.addLayout(device_row)

        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs, 1)

        # Search tab
        search_tab = QtWidgets.QWidget()
        s_layout = QtWidgets.QVBoxLayout(search_tab)
        s_top = QtWidgets.QHBoxLayout()
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText('Search, e.g. "aphex twin flim"')
        self.search_limit = QtWidgets.QSpinBox()
        self.search_limit.setRange(1, 50)
        self.search_limit.setValue(10)
        self.search_btn = QtWidgets.QPushButton("Search")
        self.search_btn.clicked.connect(self._do_search)
        s_top.addWidget(self.search_edit, 1)
        s_top.addWidget(QtWidgets.QLabel("Limit:"))
        s_top.addWidget(self.search_limit)
        s_top.addWidget(self.search_btn)
        s_layout.addLayout(s_top)
        self.search_list = QtWidgets.QListWidget()
        self.search_list.itemDoubleClicked.connect(self._play_selected)
        s_layout.addWidget(self.search_list, 1)
        self.tabs.addTab(search_tab, "Search")

        # URL tab
        url_tab = QtWidgets.QWidget()
        u_layout = QtWidgets.QVBoxLayout(url_tab)
        u_top = QtWidgets.QHBoxLayout()
        self.url_edit = QtWidgets.QLineEdit()
        self.url_edit.setPlaceholderText("Paste a TIDAL track/album/playlist URL")
        self.url_load_btn = QtWidgets.QPushButton("Load")
        self.url_load_btn.clicked.connect(self._do_url_load)
        u_top.addWidget(self.url_edit, 1)
        u_top.addWidget(self.url_load_btn)
        u_layout.addLayout(u_top)
        self.url_list = QtWidgets.QListWidget()
        self.url_list.itemDoubleClicked.connect(self._play_selected)
        u_layout.addWidget(self.url_list, 1)
        self.tabs.addTab(url_tab, "URL")

        # Put Debug in the tab bar row (top-right), under the device selector row.
        self.debug_cb = QtWidgets.QCheckBox("Debug")
        self.tabs.setCornerWidget(self.debug_cb, QtCore.Qt.Corner.TopRightCorner)

        controls_row = QtWidgets.QHBoxLayout()
        self.play_btn = QtWidgets.QPushButton("Play selected")
        self.play_btn.clicked.connect(self._play_selected)
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.pause_btn.setEnabled(False)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop_playback)
        self.stop_btn.setEnabled(False)
        controls_row.addWidget(self.play_btn)
        controls_row.addWidget(self.pause_btn)
        controls_row.addWidget(self.stop_btn)
        controls_row.addStretch(1)
        self.seek_time = QtWidgets.QLabel("0:00 / 0:00")
        self.seek_time.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        controls_row.addWidget(self.seek_time)
        layout.addLayout(controls_row)

        # Seek control: full-width slider + right-aligned time label below.
        self.seek_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.seek_slider.setEnabled(False)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self.seek_slider.sliderReleased.connect(self._on_seek_released)
        layout.addWidget(self.seek_slider)

        self.status_label = QtWidgets.QLabel("Status: starting…")
        layout.addWidget(self.status_label)

        self.quality_label = QtWidgets.QLabel("Quality: —")
        layout.addWidget(self.quality_label)
        self.bitrate_label = QtWidgets.QLabel("Bitrate: —")
        layout.addWidget(self.bitrate_label)
        self.bitperfect_label = QtWidgets.QLabel("Bit-perfect: —")
        layout.addWidget(self.bitperfect_label)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        layout.addWidget(self.log)

        self._refresh_devices()
        self._load_device_pref()
        self._set_enabled(False)

    def _set_enabled(self, enabled: bool) -> None:
        self.device_combo.setEnabled(enabled)
        self.refresh_devices_btn.setEnabled(enabled)
        self.search_edit.setEnabled(enabled)
        self.search_limit.setEnabled(enabled)
        self.search_btn.setEnabled(enabled)
        self.url_edit.setEnabled(enabled)
        self.url_load_btn.setEnabled(enabled)
        self.play_btn.setEnabled(enabled)

    def _append_log(self, msg: str) -> None:
        self.log.appendPlainText(msg)

    def _refresh_devices(self) -> None:
        current = self.device_combo.currentText()
        self.device_combo.clear()
        devs = list_playback_devices()
        devs_sorted = sorted(devs)
        self.device_combo.addItems(devs_sorted)
        preferred = self._settings.value("alsa_device", "", type=str) or ""
        if preferred:
            self.device_combo.setCurrentText(preferred)
        elif current and current in devs_sorted:
            self.device_combo.setCurrentText(current)
        elif devs_sorted:
            self.device_combo.setCurrentIndex(0)

    def _save_device_pref(self, text: str) -> None:
        t = (text or "").strip()
        if t:
            self._settings.setValue("alsa_device", t)

    def _load_device_pref(self) -> None:
        preferred = self._settings.value("alsa_device", "", type=str) or ""
        if preferred:
            self.device_combo.setCurrentText(preferred)

    def _start_login(self) -> None:
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
        self.status_label.setText("Status: ready")
        self._set_enabled(True)

    def _on_error(self, msg: str) -> None:
        self.status_label.setText("Status: error")
        self._append_log(msg)
        QtWidgets.QMessageBox.critical(self, "Error", msg)

    def _populate_tracks(self, tracks: List[Dict[str, Any]]) -> None:
        self._tracks = tracks
        self.search_list.clear()
        self.url_list.clear()
        for t in tracks:
            item = QtWidgets.QListWidgetItem(tidal_core.format_track_line(t))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, t.get("id"))
            if self.tabs.currentIndex() == 0:
                self.search_list.addItem(item)
            else:
                self.url_list.addItem(item)

    def _do_search(self) -> None:
        if self._session is None:
            return
        q = self.search_edit.text().strip()
        if not q:
            return
        self.status_label.setText("Status: searching…")
        self._append_log(f"Search: {q}")
        self._tracks_worker = TracksWorker(self._session, "search", q, self.search_limit.value())
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
        self._tracks_worker = TracksWorker(self._session, "url", u, 0)
        self._tracks_worker.ready.connect(self._on_tracks_ready)
        self._tracks_worker.error.connect(self._on_error)
        self._tracks_worker.start()

    def _on_tracks_ready(self, tracks: List[Dict[str, Any]]) -> None:
        self.status_label.setText("Status: ready")
        self._populate_tracks(tracks)

    def _selected_track_id(self) -> Optional[str]:
        widget = self.search_list if self.tabs.currentIndex() == 0 else self.url_list
        item = widget.currentItem()
        if item is None:
            return None
        tid = item.data(QtCore.Qt.ItemDataRole.UserRole)
        return str(tid) if tid is not None else None

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

    def _play_selected(self) -> None:
        if self._session is None:
            return

        tid = self._selected_track_id()
        if tid is None:
            return
        dev = self.device_combo.currentText().strip()
        if not dev:
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
        self._play_had_error = False
        self._stream_info = None
        self._audio_fmt = None
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

        self.stop_btn.setEnabled(True)
        self.pause_btn.setEnabled(True)
        self.status_label.setText("Status: starting playback…")
        self._play_worker = PlaybackWorker(self._session, tid, dev, debug=self.debug_cb.isChecked())
        self._play_worker.status.connect(lambda s: self.status_label.setText(f"Status: {s}"))
        self._play_worker.log.connect(self._append_log)
        self._play_worker.error.connect(self._on_playback_error)
        self._play_worker.fmt_ready.connect(self._on_fmt_ready)
        self._play_worker.stream_info.connect(self._on_stream_info)
        self._play_worker.position.connect(self._on_position)
        self._play_worker.finished_ok.connect(self._on_playback_done)
        self._play_worker.finished.connect(self._on_playback_thread_finished)
        self._play_worker.start()

    def _on_stream_info(self, info: StreamInfo) -> None:
        self._stream_info = info
        parts = []
        if info.audio_quality:
            parts.append(f"stream={info.audio_quality}")
        if info.bit_depth and info.sample_rate:
            parts.append(f"{info.bit_depth}-bit/{info.sample_rate} Hz")
        self.quality_label.setText("Quality: " + (" ".join(parts) if parts else "—"))
        self._update_bitperfect_label()
        self._update_bitrate_label()

    def _on_fmt_ready(self, fmt: AudioFormat) -> None:
        self._audio_fmt = fmt
        self._update_bitperfect_label()
        self._update_bitrate_label()

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
            self.bitperfect_label.setText("Bit-perfect: unlikely (use hw: for direct device)")
            return
        if self._stream_info is None or self._audio_fmt is None:
            self.bitperfect_label.setText("Bit-perfect: —")
            return
        si = self._stream_info
        af = self._audio_fmt
        if si.sample_rate and af.rate != si.sample_rate:
            self.bitperfect_label.setText(
                f"Bit-perfect: no (output {af.rate} Hz != stream {si.sample_rate} Hz)"
            )
            return
        if si.bit_depth and af.bits != si.bit_depth:
            if si.bit_depth == 24 and af.bits == 32:
                self.bitperfect_label.setText("Bit-perfect: padded (24-bit stream in 32-bit PCM)")
                return
            self.bitperfect_label.setText(
                f"Bit-perfect: no (output {af.bits}-bit != stream {si.bit_depth}-bit)"
            )
            return
        self.bitperfect_label.setText("Bit-perfect: likely")

    def _stop_playback(self) -> None:
        if self._play_worker is None:
            return
        self.status_label.setText("Status: stopping…")
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("Pause")
        self.seek_slider.setEnabled(False)
        self._pending_play = None
        self._play_worker.stop()

    def _on_playback_done(self) -> None:
        self.status_label.setText("Status: ready")
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("Pause")
        self.seek_slider.setEnabled(False)

    def _on_playback_error(self, msg: str) -> None:
        self._play_had_error = True
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("Pause")
        self.seek_slider.setEnabled(False)
        self.status_label.setText("Status: error")
        self._append_log(msg)
        QtWidgets.QMessageBox.critical(self, "Playback error", msg)

    def _on_playback_thread_finished(self) -> None:
        self._play_worker = None
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("Pause")
        self.seek_slider.setEnabled(False)
        pending = self._pending_play
        self._pending_play = None
        if pending is not None and self._session is not None:
            tid, dev = pending
            self._start_playback(tid, dev)

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

    def closeEvent(self, event) -> None:
        try:
            if self._play_worker is not None and self._play_worker.isRunning():
                self._play_worker.stop()
                self._play_worker.wait(2000)
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
