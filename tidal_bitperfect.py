#!/usr/bin/env python3
# tidal_bitperfect.py

import datetime
import argparse
import json
import os
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Optional, Tuple, List

import tidalapi
import alsaaudio  # pip: pyalsaaudio


CRED_PATH = os.path.expanduser("~/.config/tidal/credentials.json")


def printer(msg: str) -> None:
    # avoids prints getting swallowed in some environments
    sys.stderr.write(str(msg) + "\n")
    sys.stderr.flush()


def load_saved_oauth(session: tidalapi.Session) -> bool:
    if not os.path.exists(CRED_PATH):
        return False

    try:
        with open(CRED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        expiry = data["expiry_time"]
        if isinstance(expiry, (int, float)):
            expiry = datetime.datetime.fromtimestamp(expiry)

        ok = session.load_oauth_session(
            data["token_type"],
            data["access_token"],
            data.get("refresh_token"),
            expiry,
        )
        return bool(ok) and session.check_login()

    except Exception:
        return False

def save_oauth(session: tidalapi.Session) -> None:
    os.makedirs(os.path.dirname(CRED_PATH), exist_ok=True)

    expiry = session.expiry_time
    if isinstance(expiry, datetime.datetime):
        expiry = expiry.timestamp()

    data = {
        "token_type": session.token_type,
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expiry_time": expiry,
    }

    with open(CRED_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def login_or_reuse(config: tidalapi.Config) -> tidalapi.Session:
    session = tidalapi.Session(config)

    if load_saved_oauth(session):
        return session

    # first-time login (prints a link and waits)
    session.login_oauth_simple(fn_print=printer)  # :contentReference[oaicite:1]{index=1}
    if not session.check_login():
        raise RuntimeError("tidal login failed")

    save_oauth(session)  # :contentReference[oaicite:2]{index=2}
    return session


def pick_quality():
    # best-effort: tidalapi’s enum names have varied a bit across versions
    q = getattr(tidalapi, "Quality", None)
    if q is None:
        return None
    for name in ("hi_res", "high_res", "max", "lossless"):
        if hasattr(q, name):
            return getattr(q, name)
    return getattr(q, "lossless", None)


def find_default_btr5_device() -> str:
    devs = list_btr5_devices()
    if devs:
        return devs[0]

    pcms = set(alsaaudio.pcms(alsaaudio.PCM_PLAYBACK))
    raise RuntimeError(
        "could not find btr5 alsa pcm. available playback pcms:\n  "
        + "\n  ".join(sorted(pcms))
    )


def list_btr5_devices() -> list[str]:
    pcms = list(alsaaudio.pcms(alsaaudio.PCM_PLAYBACK))
    preferred = [
        "sysdefault:CARD=BTR5",
        "front:CARD=BTR5,DEV=0",
        "hw:CARD=BTR5,DEV=0",
        "plughw:CARD=BTR5,DEV=0",
        "usbstream:CARD=BTR5",
    ]
    seen = set()
    ordered = []

    for d in preferred:
        # Some valid device strings (notably hw/plughw) may not show up in
        # alsaaudio.pcms() but are still openable. Try them anyway.
        if d not in seen:
            ordered.append(d)
            seen.add(d)

    for d in pcms:
        if "btr5" in d.lower() and d not in seen:
            ordered.append(d)
            seen.add(d)

    return ordered


def parse_wav_header(stream) -> Tuple[int, int, int, int]:
    # minimal riff/wave parser: returns (channels, sample_rate, bits_per_sample)
    def read_exact(n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)

    hdr = read_exact(12)
    if len(hdr) < 12:
        raise RuntimeError("short read on wav header")
    riff, _size, wave = struct.unpack("<4sI4s", hdr)
    if riff != b"RIFF" or wave != b"WAVE":
        raise RuntimeError("not a riff/wave stream")

    channels = rate = bits = block_align = None

    while True:
        ch = read_exact(8)
        if len(ch) < 8:
            raise RuntimeError("could not find fmt/data chunks in wav")
        chunk_id, chunk_size = struct.unpack("<4sI", ch)
        if chunk_id == b"fmt ":
            chunk_data = read_exact(chunk_size)
            if len(chunk_data) < chunk_size:
                cid = chunk_id.decode("ascii", errors="replace")
                raise RuntimeError(
                    f"short read in wav chunk {cid!r} (wanted {chunk_size}, got {len(chunk_data)})"
                )
            if chunk_size % 2 == 1:
                _ = read_exact(1)

            if chunk_size < 16:
                raise RuntimeError("invalid fmt chunk")
            audio_fmt, channels, rate, _byte_rate, block_align, container_bits = struct.unpack(
                "<HHIIHH", chunk_data[:16]
            )
            bits = container_bits
            # 1 = PCM, 65534 = WAVE_FORMAT_EXTENSIBLE (common for hi-res PCM)
            if audio_fmt == 65534:
                if chunk_size >= 40:
                    valid_bits = struct.unpack("<H", chunk_data[18:20])[0]
                    guid = chunk_data[24:40]
                    if len(guid) == 16:
                        subformat = struct.unpack("<I", guid[0:4])[0]
                        if subformat == 1:
                            audio_fmt = 1
                        elif subformat == 3:
                            raise RuntimeError("unsupported wav subformat IEEE_FLOAT")
                else:
                    audio_fmt = 1

            if audio_fmt != 1:
                raise RuntimeError(
                    f"unexpected wav audio format {audio_fmt} (wanted pcm=1 or extensible=65534)"
                )

            if bits not in (16, 24, 32):
                if bits <= 16:
                    bits = 16
                elif bits <= 24:
                    bits = 24
                elif bits <= 32:
                    bits = 32
                else:
                    raise RuntimeError(f"unsupported bits per sample: {bits}")

        elif chunk_id == b"data":
            # For streaming output, ffmpeg may write a placeholder size of 0xFFFFFFFF.
            # We only need the fmt info; leave the stream positioned at the start
            # of audio data so callers can start reading PCM immediately.
            if channels is None or rate is None or bits is None:
                raise RuntimeError("saw data chunk before fmt chunk")
            if block_align is None:
                raise RuntimeError("missing block_align in wav fmt chunk")
            return channels, rate, bits, block_align
        else:
            # skip any other chunk types
            chunk_data = read_exact(chunk_size)
            if len(chunk_data) < chunk_size:
                cid = chunk_id.decode("ascii", errors="replace")
                raise RuntimeError(
                    f"short read in wav chunk {cid!r} (wanted {chunk_size}, got {len(chunk_data)})"
                )
            if chunk_size % 2 == 1:
                _ = read_exact(1)


def open_alsa(device: str, channels: int, rate: int, bits: int) -> alsaaudio.PCM:
    # for 24-bit, ffmpeg typically outputs s32_le (24-in-32). keep it clean.
    if bits == 16:
        fmt = alsaaudio.PCM_FORMAT_S16_LE
    elif bits in (24, 32):
        fmt = alsaaudio.PCM_FORMAT_S32_LE
    else:
        raise RuntimeError(f"unsupported bits per sample: {bits}")

    pcm = alsaaudio.PCM(
        type=alsaaudio.PCM_PLAYBACK,
        mode=alsaaudio.PCM_NORMAL,
        device=device,
        channels=channels,
        rate=rate,
        format=fmt,
        periodsize=4096,
    )
    return pcm


def open_alsa_with_fallback(
    device: Optional[str], channels: int, rate: int, bits: int
) -> Tuple[str, alsaaudio.PCM]:
    if device is not None:
        return device, open_alsa(device, channels, rate, bits)

    tried = []
    saw_busy = False
    for dev in list_btr5_devices():
        try:
            return dev, open_alsa(dev, channels, rate, bits)
        except alsaaudio.ALSAAudioError as e:
            if "Device or resource busy" in _safe_str(e):
                saw_busy = True
            tried.append(f"{dev}: {_safe_str(e)}")
            continue

    fallback = None
    try:
        fallback = find_default_btr5_device()
        return fallback, open_alsa(fallback, channels, rate, bits)
    except Exception as e:
        if fallback is not None:
            tried.append(f"{fallback}: {_safe_str(e)}")

    msg = "could not open any BTR5 ALSA device:\n  " + "\n  ".join(tried)
    if saw_busy:
        msg += (
            "\n\nThe device looks busy (another process has the ALSA device open). "
            "ALSA cannot 'take over' without closing the other client.\n"
            "Try:\n"
            "  - Close anything using the BTR5 (browser, player, PipeWire/PulseAudio).\n"
            "  - See who holds it: `fuser -v /dev/snd/*`.\n"
            "  - If you use PulseAudio: `pasuspender -- python tidal_bitperfect.py ...`.\n"
        )
    raise RuntimeError(msg)


 

def _start_ffmpeg_wav(url: str) -> Tuple[List[str], subprocess.Popen]:
    ff_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", url, "-f", "wav", "pipe:1"]
    ff = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert ff.stdout is not None
    assert ff.stderr is not None
    return ff_cmd, ff


def _stop_proc(proc: subprocess.Popen, timeout_s: float = 2.0) -> None:
    try:
        proc.terminate()
    except Exception:
        return
    try:
        proc.wait(timeout=timeout_s)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _ffmpeg_fail_message(
    why: str, url: str, ff_cmd: List[str], ff: subprocess.Popen, debug: bool
) -> str:
    err = ""
    rc = None
    try:
        rc = ff.poll()
        if rc is None:
            _stop_proc(ff, timeout_s=1.0)
        try:
            _out, _err = ff.communicate(timeout=1)
        except Exception:
            _out, _err = (b"", b"")
        err = (_err or b"").decode("utf-8", errors="replace").strip()
    except Exception:
        pass

    msg = f"ffmpeg decode failed: {why}"
    if debug:
        msg += f"\nstream url: {url}"
        msg += f"\nffmpeg rc: {rc}"
        msg += f"\nffmpeg cmd: {' '.join(ff_cmd)}"
    if err:
        msg += f"\nffmpeg stderr:\n{err}"
    return msg


def play_stream(*, url: str, alsa_device: Optional[str], debug: bool) -> None:
    if debug:
        print(f"stream url: {url}")

    def start_ffmpeg(codec_name: Optional[str] = None):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", url]
        if codec_name:
            cmd += ["-c:a", codec_name]
        cmd += ["-f", "wav", "pipe:1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        assert proc.stderr is not None
        return cmd, proc

    ff_cmd, ff = _start_ffmpeg_wav(url)
    try:
        did_fallback = False
        while True:
            try:
                channels, rate, bits, block_align = parse_wav_header(ff.stdout)
            except Exception as e:
                raise SystemExit(_ffmpeg_fail_message(_safe_str(e), url, ff_cmd, ff, debug))

            print(f"decoded format: {channels}ch @ {rate}hz, {bits}bit")

            try:
                opened_dev, pcm = open_alsa_with_fallback(alsa_device, channels, rate, bits)
            except Exception as e:
                raise SystemExit(f"alsa open failed: {_safe_str(e)}")
            print(f"alsa: {opened_dev}")

            frame_size = int(block_align)
            buf = bytearray()

            try:
                while True:
                    chunk = ff.stdout.read(16384)
                    if not chunk:
                        return
                    buf.extend(chunk)
                    if frame_size <= 0:
                        continue
                    whole = (len(buf) // frame_size) * frame_size
                    if not whole:
                        continue
                    try:
                        pcm.write(bytes(buf[:whole]))
                    except alsaaudio.ALSAAudioError as e:
                        msg = _safe_str(e).lower()
                        if (not did_fallback) and ("multiple of framesize" in msg):
                            did_fallback = True
                            if debug:
                                print("alsa framesize error; retrying with 32-bit PCM")
                            _stop_proc(ff)
                            ff_cmd, ff = start_ffmpeg("pcm_s32le")
                            break
                        raise
                    del buf[:whole]
            finally:
                try:
                    pcm.close()
                except Exception:
                    pass

            if did_fallback:
                continue
            return
    except alsaaudio.ALSAAudioError as e:
        if "Interrupted system call" not in _safe_str(e):
            raise
    except KeyboardInterrupt:
        pass
    finally:
        _stop_proc(ff)


def get_stream_url(track):
    # tidalapi has changed some naming across versions; try a few shapes.
    for meth in ("get_stream_url", "stream_url", "get_url"):
        if hasattr(track, meth) and callable(getattr(track, meth)):
            try:
                u = getattr(track, meth)()
                if isinstance(u, str) and u.startswith("http"):
                    return u
            except Exception:
                pass

    if hasattr(track, "stream") and callable(track.stream):
        s = track.stream()
        for attr in ("url", "stream_url"):
            if hasattr(s, attr):
                u = getattr(s, attr)
                if isinstance(u, str) and u.startswith("http"):
                    return u

        if hasattr(s, "manifest"):
            raise RuntimeError(
                "tidalapi returned a manifest (dash/hls) instead of a direct url. "
                "you’ll need manifest parsing for your tidalapi version."
            )

    raise RuntimeError("could not derive a playable stream url from the track object")


def _safe_str(x) -> str:
    try:
        return str(x)
    except Exception:
        return repr(x)


def _resolve_redirects(url: str, timeout_s: float = 10.0, max_hops: int = 10) -> str:
    current = url
    for _ in range(max_hops):
        req = urllib.request.Request(
            current,
            method="HEAD",
            headers={"User-Agent": "tidal-bitperfect/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                final = resp.geturl()
        except Exception:
            # some endpoints don’t like HEAD; fall back to GET
            req = urllib.request.Request(
                current,
                method="GET",
                headers={"User-Agent": "tidal-bitperfect/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                final = resp.geturl()

        if final == current:
            return final
        current = final
    return current


def _parse_tidal_link(url: str) -> Tuple[str, str]:
    """
    Returns (kind, id) where kind is one of: track, album, playlist.
    Raises ValueError if we can't parse.
    """
    resolved = _resolve_redirects(url) if "link.tidal.com" in url else url
    parsed = urllib.parse.urlparse(resolved)
    path = parsed.path.strip("/")
    parts = [p for p in path.split("/") if p]

    # Examples:
    # - tidal.com/browse/track/123
    # - tidal.com/track/123
    # - listen.tidal.com/track/123
    if len(parts) >= 2 and parts[0] == "browse":
        parts = parts[1:]

    if len(parts) >= 2 and parts[0] in ("track", "album", "playlist"):
        kind = parts[0]
        item_id = parts[1].split("-")[0]  # guard against sluggy variants
        if not item_id:
            raise ValueError(f"missing {kind} id in url: {url}")
        return kind, item_id

    raise ValueError(f"unrecognized tidal url format: {url}")


def _search_tracks(session: tidalapi.Session, query: str, limit: int) -> list:
    def _extract_tracks(res) -> list:
        if isinstance(res, dict):
            return res.get("tracks") or []
        return getattr(res, "tracks", None) or []

    # explicit models list avoids version-specific defaults
    track_model = getattr(getattr(tidalapi, "media", None), "Track", None) or getattr(
        tidalapi, "Track", None
    )
    models = [track_model] if track_model is not None else None

    queries = [query]
    if " - " in query:
        queries.append(query.replace(" - ", " "))
    if "-" in query and " - " not in query:
        queries.append(query.replace("-", " "))

    seen_ids = set()
    tracks = []
    for q in queries:
        res = session.search(q, models=models, limit=limit)
        candidates = _extract_tracks(res)
        for t in candidates:
            tid = getattr(t, "id", None)
            if tid is not None and tid in seen_ids:
                continue
            if tid is not None:
                seen_ids.add(tid)
            tracks.append(t)
        if tracks:
            break
    return tracks


def _format_track_line(t) -> str:
    artist = getattr(getattr(t, "artist", None), "name", None) or "?"
    title = getattr(t, "name", None) or "?"
    album = getattr(getattr(t, "album", None), "name", None)
    tid = getattr(t, "id", None)
    extra = f" — {album}" if album else ""
    return f"{artist} – {title}{extra} (id={tid})"


def _pick_from_list(lines: List[str], default_index: int = 0) -> int:
    for i, line in enumerate(lines):
        print(f"[{i}] {line}")
    while True:
        raw = input(f"pick index [{default_index}]: ").strip()
        if raw == "":
            return default_index
        try:
            idx = int(raw)
        except ValueError:
            print("enter a number")
            continue
        if 0 <= idx < len(lines):
            return idx
        print(f"index out of range (0..{len(lines)-1})")


def _tracks_for_link(session: tidalapi.Session, url: str) -> Tuple[str, list]:
    kind, item_id = _parse_tidal_link(url)
    if kind == "track":
        return kind, [session.track(item_id)]
    if kind == "album":
        album = session.album(item_id)
        # tidalapi supports both .tracks() and .tracks
        ts = album.tracks() if callable(getattr(album, "tracks", None)) else getattr(album, "tracks", None)
        return kind, list(ts or [])
    if kind == "playlist":
        pl = session.playlist(item_id)
        ts = pl.tracks() if callable(getattr(pl, "tracks", None)) else getattr(pl, "tracks", None)
        return kind, list(ts or [])
    raise ValueError(f"unsupported tidal link kind: {kind}")


def main():
    ap = argparse.ArgumentParser(description="tidalapi -> ffmpeg -> alsa (bitperfect-ish) player")
    ap.add_argument("--alsa", default=None, help='alsa pcm device (default: auto-detect btr5)')
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--query", help='search query, e.g. "artist - title"')
    src.add_argument("--url", help='tidal link, e.g. "https://tidal.com/browse/track/..."')
    ap.add_argument("--index", type=int, default=0, help="which search result track to play")
    ap.add_argument("--limit", type=int, default=10, help="search result limit")
    ap.add_argument("--list", action="store_true", help="list results and exit")
    ap.add_argument("--pick", action="store_true", help="show results and prompt for an index")
    ap.add_argument("--debug", action="store_true", help="print stream URL and ffmpeg stderr on failure")
    args = ap.parse_args()

    quality = pick_quality()
    config = tidalapi.Config(quality=quality) if quality is not None else tidalapi.Config()
    session = login_or_reuse(config)

    if args.url:
        try:
            kind, tracks = _tracks_for_link(session, args.url)
        except Exception as e:
            raise SystemExit(f"could not load from url: {_safe_str(e)}")

        if not tracks:
            raise SystemExit(f"no tracks found for {kind} url")
        lines = [_format_track_line(t) for t in tracks]
        if args.list:
            for i, line in enumerate(lines):
                print(f"[{i}] {line}")
            return

        if args.pick:
            idx = _pick_from_list(lines, default_index=min(args.index, len(lines) - 1))
        else:
            idx = min(args.index, len(tracks) - 1)

        track = tracks[idx]
    else:
        tracks = _search_tracks(session, args.query, limit=args.limit)
        if not tracks:
            raise SystemExit(
                'no tracks found. try a simpler query like "aphex twin flim", '
                "or pass a direct --url."
            )

        lines = [_format_track_line(t) for t in tracks]
        if args.list and not args.pick:
            for i, line in enumerate(lines):
                print(f"[{i}] {line}")
            return

        if args.pick:
            idx = _pick_from_list(lines, default_index=min(args.index, len(tracks) - 1))
        else:
            idx = min(args.index, len(tracks) - 1)
        track = tracks[idx]

    print(f"playing: {_format_track_line(track)}")
    url = get_stream_url(track)
    play_stream(url=url, alsa_device=args.alsa, debug=args.debug)

    # refresh stored tokens if tidal rotated/extended them during runtime
    try:
        if session.check_login():
            save_oauth(session)
    except Exception:
        pass


if __name__ == "__main__":
    main()
