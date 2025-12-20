#!/usr/bin/env python3

import datetime
import json
import os
import struct
import urllib.parse
import urllib.request
import base64
from typing import Callable, Optional, Tuple, List, Dict, Any

import tidalapi


CRED_PATH = os.path.expanduser("~/.config/tidal/credentials.json")


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
    try:
        os.chmod(CRED_PATH, 0o600)
    except Exception:
        pass


def pick_quality():
    q = getattr(tidalapi, "Quality", None)
    if q is None:
        return None
    # tidalapi 0.8.x uses these names
    for name in ("hi_res_lossless", "high_lossless", "low_320k", "low_96k"):
        if hasattr(q, name):
            return getattr(q, name)
    return None


def quality_preference() -> List[str]:
    q = getattr(tidalapi, "Quality", None)
    if q is None:
        return []
    prefs = []
    for name in ("hi_res_lossless", "high_lossless", "low_320k", "low_96k"):
        if hasattr(q, name):
            prefs.append(getattr(q, name))
    return prefs


def quality_rank(audio_quality: Optional[str]) -> int:
    if audio_quality is None:
        return -1
    # tidalapi 0.8.x values: LOW, HIGH, LOSSLESS, HI_RES_LOSSLESS
    if audio_quality == getattr(getattr(tidalapi, "Quality", None), "hi_res_lossless", "HI_RES_LOSSLESS"):
        return 3
    if audio_quality == getattr(getattr(tidalapi, "Quality", None), "high_lossless", "LOSSLESS"):
        return 2
    if audio_quality == "HIGH":
        return 1
    if audio_quality == "LOW":
        return 0
    return 0


def decode_manifest_b64(manifest_b64: Optional[str]) -> Optional[bytes]:
    if not manifest_b64:
        return None
    try:
        raw = manifest_b64.encode("utf-8") if isinstance(manifest_b64, str) else manifest_b64
        return base64.b64decode(raw)
    except Exception:
        return None


def resolve_stream_input(stream, url: Optional[str]) -> tuple[Optional[str], Optional[bytes], Optional[str]]:
    """
    Returns (url, manifest_bytes, manifest_mime). If the stream exposes a DASH manifest,
    url may be None and manifest_bytes/mime will be populated.
    """
    manifest_bytes = None
    manifest_mime = None
    if stream is not None:
        manifest_mime = getattr(stream, "manifest_mime_type", None)
        manifest_bytes = decode_manifest_b64(getattr(stream, "manifest", None))
    return url, manifest_bytes, manifest_mime


def login_or_reuse(config: tidalapi.Config, on_message: Optional[Callable[[str], None]] = None) -> tidalapi.Session:
    session = tidalapi.Session(config)

    if load_saved_oauth(session):
        return session

    fn_print = on_message or (lambda _msg: None)
    login, future = session.login_oauth()
    fn_print(
        "TIDAL login: open this link and authorize:\n"
        f"  {login.verification_uri_complete}\n"
        f"Code: {login.user_code} (expires in {int(login.expires_in)}s)"
    )

    try:
        future.result()
    except Exception as e:
        raise RuntimeError(f"tidal login failed: {safe_str(e)}")

    if not session.check_login():
        raise RuntimeError("tidal login failed")

    save_oauth(session)
    return session


def safe_str(x) -> str:
    try:
        return str(x)
    except Exception:
        return repr(x)


def resolve_redirects(url: str, timeout_s: float = 10.0, max_hops: int = 10) -> str:
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


def parse_tidal_link(url: str) -> Tuple[str, str]:
    resolved = resolve_redirects(url) if "link.tidal.com" in url else url
    parsed = urllib.parse.urlparse(resolved)
    path = parsed.path.strip("/")
    parts = [p for p in path.split("/") if p]

    if len(parts) >= 2 and parts[0] == "browse":
        parts = parts[1:]

    if len(parts) >= 2 and parts[0] in ("track", "album", "playlist"):
        kind = parts[0]
        item_id = parts[1].split("-")[0]
        if not item_id:
            raise ValueError(f"missing {kind} id in url: {url}")
        return kind, item_id

    raise ValueError(f"unrecognized tidal url format: {url}")


def search_tracks(session: tidalapi.Session, query: str, limit: int = 10) -> List[Dict[str, Any]]:
    def _extract_tracks(res) -> list:
        if isinstance(res, dict):
            return res.get("tracks") or []
        return getattr(res, "tracks", None) or []

    track_model = getattr(getattr(tidalapi, "media", None), "Track", None) or getattr(tidalapi, "Track", None)
    models = [track_model] if track_model is not None else None

    queries = [query]
    if " - " in query:
        queries.append(query.replace(" - ", " "))
    if "-" in query and " - " not in query:
        queries.append(query.replace("-", " "))

    seen = set()
    out: List[Dict[str, Any]] = []
    for q in queries:
        res = session.search(q, models=models, limit=limit)
        tracks = _extract_tracks(res)
        for t in tracks:
            tid = getattr(t, "id", None)
            if tid is None or tid in seen:
                continue
            seen.add(tid)
            out.append(track_to_dict(t))
        if out:
            break
    return out


def track_to_dict(t) -> Dict[str, Any]:
    artist = getattr(getattr(t, "artist", None), "name", None) or "?"
    title = getattr(t, "name", None) or "?"
    album = getattr(getattr(t, "album", None), "name", None)
    album_id = getattr(getattr(t, "album", None), "id", None)
    cover_url = None
    try:
        album_obj = getattr(t, "album", None)
        if album_obj is not None:
            cover_url = album_obj.image("origin")
    except Exception:
        cover_url = None
    tid = getattr(t, "id", None)
    return {
        "id": tid,
        "artist": artist,
        "title": title,
        "album": album,
        "album_id": album_id,
        "cover_url": cover_url,
    }


def format_track_line(d: Dict[str, Any]) -> str:
    extra = f" — {d['album']}" if d.get("album") else ""
    return f"{d.get('artist','?')} – {d.get('title','?')}{extra}"


def tracks_for_link(session: tidalapi.Session, url: str) -> Tuple[str, List[Dict[str, Any]]]:
    kind, item_id = parse_tidal_link(url)
    if kind == "track":
        return kind, [track_to_dict(session.track(item_id))]
    if kind == "album":
        album = session.album(item_id)
        album_cover = None
        try:
            album_cover = album.image("origin")
        except Exception:
            album_cover = None
        ts = album.tracks() if callable(getattr(album, "tracks", None)) else getattr(album, "tracks", None)
        out = [track_to_dict(t) for t in list(ts or [])]
        if album_cover:
            for t in out:
                t["cover_url"] = album_cover
        return kind, out
    if kind == "playlist":
        pl = session.playlist(item_id)
        ts = pl.tracks() if callable(getattr(pl, "tracks", None)) else getattr(pl, "tracks", None)
        return kind, [track_to_dict(t) for t in list(ts or [])]
    raise ValueError(f"unsupported tidal link kind: {kind}")


def get_stream_url(track) -> str:
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


def parse_wav_header(stream) -> Tuple[int, int, int, int]:
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
                # Parse extensible header if present: valid bits + subformat GUID
                if chunk_size >= 40:
                    # WAVEFORMATEX has cbSize at [16:18], then WAVEFORMATEXTENSIBLE fields.
                    # validBitsPerSample is at [18:20], channelMask at [20:24].
                    valid_bits = struct.unpack("<H", chunk_data[18:20])[0]
                    guid = chunk_data[24:40]
                    if len(guid) == 16:
                        subformat = struct.unpack("<I", guid[0:4])[0]
                        # 1 = PCM, 3 = IEEE_FLOAT
                        if subformat == 1:
                            audio_fmt = 1
                        elif subformat == 3:
                            raise RuntimeError("unsupported wav subformat IEEE_FLOAT")
                else:
                    # If no extension is present, best-effort accept and use the container bit depth.
                    audio_fmt = 1

            if audio_fmt != 1:
                raise RuntimeError(
                    f"unexpected wav audio format {audio_fmt} (wanted pcm=1 or extensible=65534)"
                )

            # Normalize to a bit depth we can open in ALSA.
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
            if channels is None or rate is None or bits is None:
                raise RuntimeError("saw data chunk before fmt chunk")
            if block_align is None:
                raise RuntimeError("missing block_align in wav fmt chunk")
            return channels, rate, bits, block_align

        else:
            chunk_data = read_exact(chunk_size)
            if len(chunk_data) < chunk_size:
                cid = chunk_id.decode("ascii", errors="replace")
                raise RuntimeError(
                    f"short read in wav chunk {cid!r} (wanted {chunk_size}, got {len(chunk_data)})"
                )
            if chunk_size % 2 == 1:
                _ = read_exact(1)
