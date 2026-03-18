#!/usr/bin/env python3

import datetime
import json
import os
import struct
import base64
from typing import Callable, Optional, Tuple, List, Dict, Any

import tidalapi
import tidal_urls


CRED_PATH = os.path.expanduser("~/.config/tidal/credentials.json")
CREDS_DISABLED = False


def load_saved_oauth(session: tidalapi.Session) -> bool:
    if CREDS_DISABLED:
        return False
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
    if CREDS_DISABLED:
        return
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


def search_albums(session: tidalapi.Session, query: str, limit: int = 10) -> List[Dict[str, Any]]:
    def _extract_albums(res) -> list:
        if isinstance(res, dict):
            return res.get("albums") or []
        return getattr(res, "albums", None) or []

    album_model = getattr(getattr(tidalapi, "media", None), "Album", None) or getattr(tidalapi, "Album", None)
    models = [album_model] if album_model is not None else None

    res = session.search(query, models=models, limit=limit)
    albums = _extract_albums(res)
    out: List[Dict[str, Any]] = []
    for a in albums:
        out.append(album_to_dict(a))
    return out


def search_playlists(session: tidalapi.Session, query: str, limit: int = 10) -> List[Dict[str, Any]]:
    def _extract_playlists(res) -> list:
        if isinstance(res, dict):
            return res.get("playlists") or []
        return getattr(res, "playlists", None) or []

    playlist_model = getattr(getattr(tidalapi, "media", None), "Playlist", None) or getattr(tidalapi, "Playlist", None)
    models = [playlist_model] if playlist_model is not None else None

    res = session.search(query, models=models, limit=limit)
    playlists = _extract_playlists(res)
    out: List[Dict[str, Any]] = []
    for p in playlists:
        out.append(playlist_to_dict(p))
    return out


def _get_attr(obj: object, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _request_json(session: tidalapi.Session, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    req_obj = getattr(session, "request", None)
    if req_obj is None:
        req_obj = getattr(session, "_api", None)
    try:
        if callable(req_obj):
            resp = req_obj("GET", path, params=params)
        elif hasattr(req_obj, "request") and callable(getattr(req_obj, "request")):
            resp = req_obj.request("GET", path, params=params)
        else:
            return None
    except Exception:
        return None
    if isinstance(resp, dict):
        return resp
    if hasattr(resp, "json") and callable(getattr(resp, "json")):
        try:
            return resp.json()
        except Exception:
            return None
    return None


def _extract_ids(payload: Any, keys: Tuple[str, ...]) -> List[str]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items") or payload.get("data") or payload.get("tracks") or payload.get("albums") or []
    ids: List[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        candidate = None
        sub = item.get("item")
        if isinstance(sub, dict):
            candidate = sub.get("id")
        if candidate is None:
            for key in keys:
                if key in item:
                    candidate = item.get(key)
                    break
        if candidate is None:
            candidate = item.get("id")
        if candidate is not None:
            ids.append(str(candidate))
    return ids


def _artist_top_tracks(
    session: tidalapi.Session, artist_id: str, limit: int = 20
) -> List[Dict[str, Any]]:
    params = {"limit": int(limit)} if limit else None
    payload = None
    for path in (
        f"artists/{artist_id}/toptracks",
        f"artists/{artist_id}/top_tracks",
        f"artists/{artist_id}/topTracks",
    ):
        payload = _request_json(session, path, params=params)
        if payload:
            break
    ids = _extract_ids(payload, ("trackId", "track_id"))
    out: List[Dict[str, Any]] = []
    for tid in ids:
        try:
            out.append(track_to_dict(session.track(tid)))
        except Exception:
            continue
    return out


def _artist_albums(
    session: tidalapi.Session, artist_id: str, limit: int = 20
) -> List[Dict[str, Any]]:
    params = {"limit": int(limit)} if limit else None
    payload = None
    for path in (
        f"artists/{artist_id}/albums",
        f"artists/{artist_id}/albums?offset=0",
    ):
        payload = _request_json(session, path, params=params)
        if payload:
            break
    ids = _extract_ids(payload, ("albumId", "album_id"))
    out: List[Dict[str, Any]] = []
    for aid in ids:
        try:
            out.append(album_to_dict(session.album(aid), include_tracks=False))
        except Exception:
            continue
    return out


def search_artists(session: tidalapi.Session, query: str, limit: int = 10) -> List[Dict[str, Any]]:
    def _extract_artists(res) -> list:
        if isinstance(res, dict):
            return res.get("artists") or []
        return getattr(res, "artists", None) or []

    artist_model = getattr(getattr(tidalapi, "media", None), "Artist", None) or getattr(tidalapi, "Artist", None)
    models = [artist_model] if artist_model is not None else None

    res = session.search(query, models=models, limit=limit)
    artists = _extract_artists(res)
    out: List[Dict[str, Any]] = []
    for a in artists:
        artist_obj = a
        aid = _get_attr(a, "id", None)
        if aid is not None:
            try:
                artist_obj = session.artist(str(aid))
            except Exception:
                artist_obj = a
        out.append(artist_to_dict(artist_obj, session=session, include_details=False))
    return out


def track_to_dict(t) -> Dict[str, Any]:
    artist = getattr(getattr(t, "artist", None), "name", None) or "?"
    artist_id = getattr(getattr(t, "artist", None), "id", None)
    title = getattr(t, "name", None) or "?"
    album = getattr(getattr(t, "album", None), "name", None)
    album_id = getattr(getattr(t, "album", None), "id", None)
    cover_url = None
    try:
        album_obj = getattr(t, "album", None)
        if album_obj is not None:
            try:
                cover_url = album_obj.image("origin")
            except Exception:
                cover_url = None
    except Exception:
        cover_url = None
    tid = getattr(t, "id", None)
    return {
        "id": tid,
        "artist": artist,
        "artist_id": artist_id,
        "title": title,
        "album": album,
        "album_id": album_id,
        "cover_url": cover_url,
    }


def album_to_dict(a, include_tracks: bool = True) -> Dict[str, Any]:
    title = getattr(a, "name", None) or "?"
    artist = getattr(getattr(a, "artist", None), "name", None) or "?"
    cover_url = None
    try:
        cover_url = a.image("origin")
    except Exception:
        cover_url = None
    aid = getattr(a, "id", None)
    year = getattr(a, "year", None)
    release_type = getattr(a, "type", None)
    version = getattr(a, "version", None) or None
    explicit = getattr(a, "explicit", None)
    audio_modes = getattr(a, "audio_modes", None) or []
    num_tracks = getattr(a, "num_tracks", None)
    tracks = []
    if include_tracks:
        try:
            ts = a.tracks() if callable(getattr(a, "tracks", None)) else getattr(a, "tracks", None)
            tracks = [track_to_dict(t) for t in list(ts or [])]
        except Exception:
            tracks = []
    return {
        "id": aid,
        "album_id": aid,
        "title": title,
        "artist": artist,
        "cover_url": cover_url,
        "year": year,
        "release_type": release_type,
        "version": version,
        "explicit": explicit,
        "audio_modes": audio_modes,
        "num_tracks": num_tracks,
        "tracks": tracks,
    }


def playlist_to_dict(p) -> Dict[str, Any]:
    title = getattr(p, "name", None) or "?"
    creator = getattr(getattr(p, "creator", None), "name", None)
    cover_url = None
    try:
        cover_url = p.image("origin")
    except Exception:
        cover_url = None
    pid = getattr(p, "id", None)
    tracks = []
    try:
        ts = p.tracks() if callable(getattr(p, "tracks", None)) else getattr(p, "tracks", None)
        tracks = [track_to_dict(t) for t in list(ts or [])]
    except Exception:
        tracks = []
    return {
        "id": pid,
        "title": title,
        "creator": creator,
        "cover_url": cover_url,
        "tracks": tracks,
    }


def artist_to_dict(
    a, session: Optional[tidalapi.Session] = None, include_details: bool = False
) -> Dict[str, Any]:
    name = _get_attr(a, "name", None) or "?"
    cover_url = None
    for attr in ("image", "picture"):
        try:
            img_fn = getattr(a, attr, None)
            if callable(img_fn):
                cover_url = img_fn("origin")
                break
        except Exception:
            continue
    aid = _get_attr(a, "id", None)
    tracks = []
    albums = []
    ep_singles = []
    if include_details:
        try:
            top_tracks_fn = getattr(a, "get_top_tracks", None)
            if callable(top_tracks_fn):
                tracks = [track_to_dict(t) for t in list(top_tracks_fn() or [])]
        except Exception:
            tracks = []
        try:
            albums_fn = getattr(a, "get_albums", None)
            if callable(albums_fn):
                albums = [album_to_dict(alb, include_tracks=False) for alb in list(albums_fn() or [])]
        except Exception:
            albums = []
        try:
            ep_fn = getattr(a, "get_ep_singles", None)
            if callable(ep_fn):
                ep_singles = [album_to_dict(alb, include_tracks=False) for alb in list(ep_fn() or [])]
                ep_singles.sort(key=lambda d: d.get("year") or 0, reverse=True)
        except Exception:
            ep_singles = []
        if session is not None and aid is not None:
            if not tracks:
                tracks = _artist_top_tracks(session, str(aid))
            if not albums:
                albums = _artist_albums(session, str(aid))
    return {
        "id": aid,
        "name": name,
        "cover_url": cover_url,
        "tracks": tracks,
        "albums": albums,
        "ep_singles": ep_singles,
    }


def artist_details(session: tidalapi.Session, artist_id: str) -> Dict[str, Any]:
    artist = session.artist(artist_id)
    return artist_to_dict(artist, session=session, include_details=True)


def format_track_line(d: Dict[str, Any]) -> str:
    extra = f" — {d['album']}" if d.get("album") else ""
    return f"{d.get('artist','?')} – {d.get('title','?')}{extra}"


_RELEASE_TYPE_LABELS: Dict[str, str] = {
    "ALBUM": "Album",
    "EP": "EP",
    "SINGLE": "Single",
}


def _release_type_label(release_type: Optional[str], default: str = "Album") -> str:
    if not release_type:
        return default
    return _RELEASE_TYPE_LABELS.get(release_type.upper(), release_type.title())


def _release_tags(d: Dict[str, Any]) -> str:
    tags = []
    if d.get("version"):
        tags.append(d["version"])
    if d.get("explicit"):
        tags.append("Explicit")
    modes = d.get("audio_modes") or []
    if "DOLBY_ATMOS" in modes:
        tags.append("Atmos")
    elif "SONY_360RA" in modes:
        tags.append("360 RA")
    return f" [{' · '.join(tags)}]" if tags else ""


def format_album_line(d: Dict[str, Any]) -> str:
    artist = d.get("artist", "?")
    title = d.get("title", "?")
    year = d.get("year")
    year_str = f" ({year})" if year else ""
    return f"Album — {artist} – {title}{year_str}{_release_tags(d)}"


def format_ep_line(d: Dict[str, Any]) -> str:
    artist = d.get("artist", "?")
    title = d.get("title", "?")
    label = _release_type_label(d.get("release_type"), default="EP")
    year = d.get("year")
    year_str = f" ({year})" if year else ""
    return f"{label} — {artist} – {title}{year_str}{_release_tags(d)}"


def format_playlist_line(d: Dict[str, Any]) -> str:
    title = d.get("title", "?")
    creator = d.get("creator")
    if creator:
        return f"Playlist — {title} (by {creator})"
    return f"Playlist — {title}"


def format_artist_line(d: Dict[str, Any]) -> str:
    name = d.get("name", "?")
    return f"Artist — {name}"


def tracks_for_link(session: tidalapi.Session, url: str) -> Tuple[str, List[Dict[str, Any]]]:
    kind, item_id = tidal_urls.parse_tidal_link(url)
    if kind == "track":
        return kind, [track_to_dict(session.track(item_id))]
    if kind == "album":
        album = session.album(item_id)
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


def link_to_result(session: tidalapi.Session, url: str) -> Dict[str, Any]:
    kind, item_id = tidal_urls.parse_tidal_link(url)
    if kind == "track":
        return {"type": "track", "items": [track_to_dict(session.track(item_id))]}
    if kind == "album":
        album = session.album(item_id)
        return {"type": "album", "items": [album_to_dict(album)]}
    if kind == "playlist":
        pl = session.playlist(item_id)
        return {"type": "playlist", "items": [playlist_to_dict(pl)]}
    if kind == "artist":
        artist = session.artist(item_id)
        return {"type": "artist", "items": [artist_to_dict(artist, session=session, include_details=False)]}
    raise ValueError(f"unsupported tidal link kind: {kind}")


def track_radio(session: tidalapi.Session, track_id: str, limit: int = 30) -> List[Dict[str, Any]]:
    """
    Fetch track radio recommendations for a given track id.
    """
    params = {"limit": int(limit)} if limit else None
    paths = [
        f"tracks/{track_id}/relationships/radio",
        f"tracks/{track_id}/radio",
        f"tracks/{track_id}/recommendations",
        f"track/{track_id}/radio",
    ]
    resp = None
    last_err: Optional[Exception] = None

    def do_request(p: str):
        if hasattr(session, "request"):
            req_obj = getattr(session, "request")
            if callable(req_obj):
                return req_obj("GET", p, params=params)
            if hasattr(req_obj, "request") and callable(getattr(req_obj, "request")):
                return req_obj.request("GET", p, params=params)
        if hasattr(session, "_api") and hasattr(session._api, "request"):
            return session._api.request("GET", p, params=params)
        raise RuntimeError("tidalapi session does not expose a request method")

    for path in paths:
        try:
            resp = do_request(path)
            if resp is not None:
                break
        except Exception as e:
            last_err = e
            continue
    if resp is None:
        raise RuntimeError(safe_str(last_err) if last_err else "radio request failed")

    data = None
    payload = None
    if isinstance(resp, dict):
        payload = resp
    elif hasattr(resp, "json") and callable(getattr(resp, "json")):
        try:
            payload = resp.json()
        except Exception:
            payload = None
    elif hasattr(resp, "get"):
        try:
            payload = resp
        except Exception:
            payload = None
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("items") or payload.get("tracks") or []
    elif isinstance(payload, list):
        data = payload
    if data is None:
        data = []

    ids: List[str] = []
    for item in data:
        if isinstance(item, dict):
            tid = item.get("id") or item.get("track_id")
            if tid is None and "resource" in item and isinstance(item["resource"], dict):
                tid = item["resource"].get("id")
            if tid is not None:
                ids.append(str(tid))

    tracks = []
    if hasattr(session, "tracks") and callable(getattr(session, "tracks")) and ids:
        try:
            tracks = list(session.tracks(ids))
        except Exception:
            tracks = []
    if not tracks:
        for tid in ids:
            try:
                tracks.append(session.track(tid))
            except Exception:
                continue
    return [track_to_dict(t) for t in tracks if t is not None]


def artist_radio(session: tidalapi.Session, artist_id: str, limit: int = 30) -> List[Dict[str, Any]]:
    """Fetch artist radio recommendations for a given artist id."""
    artist = session.artist(artist_id)
    tracks = artist.get_radio(limit=limit)
    return [track_to_dict(t) for t in tracks if t is not None]


def mix_to_dict(m) -> Dict[str, Any]:
    mid = getattr(m, "id", None)
    title = getattr(m, "title", None) or "?"
    sub_title = getattr(m, "sub_title", None) or ""
    mix_type = None
    mt = getattr(m, "mix_type", None)
    if mt is not None:
        mix_type = mt.value if hasattr(mt, "value") else str(mt)
    cover_url = None
    try:
        cover_url = m.image(320)
    except Exception:
        try:
            images = getattr(m, "images", None)
            if images:
                cover_url = getattr(images, "small", None) or getattr(images, "medium", None)
        except Exception:
            pass
    return {
        "id": mid,
        "title": title,
        "sub_title": sub_title,
        "mix_type": mix_type,
        "cover_url": cover_url,
        "tracks": [],
    }


def format_mix_line(d: Dict[str, Any]) -> str:
    title = d.get("title", "?")
    sub = d.get("sub_title", "")
    suffix = f" · {sub}" if sub else ""
    return f"Mix — {title}{suffix}"


def playlist_tracks(session: tidalapi.Session, playlist_id: str) -> List[Dict[str, Any]]:
    pl = session.playlist(playlist_id)
    ts = pl.tracks() if callable(getattr(pl, "tracks", None)) else getattr(pl, "tracks", None)
    return [track_to_dict(t) for t in list(ts or [])]


def mix_tracks(session: tidalapi.Session, mix_id: str) -> List[Dict[str, Any]]:
    import tidalapi.mix as _mix_mod
    m = _mix_mod.Mix(session, mix_id)
    items = m.items()
    out = []
    for t in items:
        try:
            if getattr(t, "id", None) is not None and not getattr(t, "video_cover", None):
                out.append(track_to_dict(t))
        except Exception:
            continue
    return out


def _convert_home_item(item) -> Optional[Dict[str, Any]]:
    try:
        import tidalapi.media as _media
        if isinstance(item, _media.Track):
            return {"type": "track", "data": track_to_dict(item)}
    except Exception:
        pass
    try:
        import tidalapi.album as _album_mod
        if isinstance(item, _album_mod.Album):
            return {"type": "album", "data": album_to_dict(item, include_tracks=False)}
    except Exception:
        pass
    try:
        import tidalapi.mix as _mix_mod
        if isinstance(item, (_mix_mod.Mix, _mix_mod.MixV2)):
            return {"type": "mix", "data": mix_to_dict(item)}
    except Exception:
        pass
    try:
        import tidalapi.playlist as _playlist_mod
        if isinstance(item, _playlist_mod.Playlist):
            pid = getattr(item, "id", None)
            ptitle = getattr(item, "name", None) or "?"
            cover_url = None
            try:
                cover_url = item.image("origin")
            except Exception:
                pass
            return {"type": "playlist", "data": {"id": pid, "title": ptitle, "cover_url": cover_url, "tracks": []}}
    except Exception:
        pass
    return None


def home_page(session: tidalapi.Session, items_per_section: int = 12) -> List[Dict[str, Any]]:
    # --- Attempt 1: V2 home feed, parse categories individually to tolerate unknown types ---
    sections: List[Dict[str, Any]] = []
    try:
        params = {"deviceType": "BROWSER", "locale": session.locale, "platform": "WEB"}
        resp = session.request.request(
            "GET", "home/feed/static",
            base_url=session.config.api_v2_location,
            params=params,
        )
        json_obj = resp.json()
        pcat_v2 = session.page.page_category_v2
        for item_json in json_obj.get("items", []):
            try:
                category = pcat_v2.parse_item(item_json)
            except Exception:
                continue
            title = getattr(category, "title", None) or ""
            raw_items = getattr(category, "items", None) or []
            converted = []
            for item in list(raw_items)[:items_per_section]:
                d = _convert_home_item(item)
                if d:
                    converted.append(d)
            if converted:
                sections.append({"title": title, "items": converted})
    except Exception:
        pass

    if sections:
        return sections

    # --- Fallback: for_you V1 page ---
    try:
        page = session.for_you()
        sections = _home_sections_from_page(page, items_per_section)
    except Exception:
        pass

    if sections:
        return sections

    raise RuntimeError("could not load home feed")


def _home_sections_from_page(page, items_per_section: int) -> List[Dict[str, Any]]:
    sections = []
    for category in (getattr(page, "categories", None) or []):
        title = getattr(category, "title", None) or ""
        raw_items = getattr(category, "items", None) or []
        converted = []
        for item in list(raw_items)[:items_per_section]:
            d = _convert_home_item(item)
            if d:
                converted.append(d)
        if converted:
            sections.append({"title": title, "items": converted})
    return sections


def album_tracks(session: tidalapi.Session, album_id: str) -> List[Dict[str, Any]]:
    album = session.album(album_id)
    try:
        album_cover = album.image("origin")
    except Exception:
        album_cover = None
    ts = album.tracks() if callable(getattr(album, "tracks", None)) else getattr(album, "tracks", None)
    out = [track_to_dict(t) for t in list(ts or [])]
    if album_cover:
        for t in out:
            t["cover_url"] = album_cover
    return out


def _get_user_id(session: tidalapi.Session) -> Optional[str]:
    user = getattr(session, "user", None)
    if user is not None:
        uid = getattr(user, "id", None)
        if uid is not None:
            return str(uid)
    uid = getattr(session, "user_id", None)
    return str(uid) if uid is not None else None


def list_favorite_tracks(
    session: tidalapi.Session, limit: int = 100, offset: int = 0
) -> List[Dict[str, Any]]:
    user = getattr(session, "user", None)
    favorites = getattr(user, "favorites", None) if user is not None else None
    if favorites is not None:
        try:
            order = getattr(getattr(tidalapi, "user", None), "ItemOrder", None)
            direction = getattr(getattr(tidalapi, "user", None), "OrderDirection", None)
            order_val = order.Date if order is not None else None
            dir_val = direction.Descending if direction is not None else None
            tracks = favorites.tracks(
                limit=limit,
                offset=offset,
                order=order_val,
                order_direction=dir_val,
            )
            return [track_to_dict(t) for t in tracks if t is not None]
        except Exception:
            pass
    user_id = _get_user_id(session)
    if not user_id:
        raise RuntimeError("tidalapi session has no user id")
    path = f"users/{user_id}/favorites/tracks"
    params = {"limit": int(limit), "offset": int(offset), "order": "DATE", "orderDirection": "DESC"}
    req_obj = getattr(session, "request", None)
    if req_obj is None or not hasattr(req_obj, "request"):
        raise RuntimeError("tidalapi session does not expose a request method")
    resp = req_obj.request("GET", path, params=params)

    payload = None
    if isinstance(resp, dict):
        payload = resp
    elif hasattr(resp, "json") and callable(getattr(resp, "json")):
        try:
            payload = resp.json()
        except Exception:
            payload = None
    if payload is None:
        return []

    items = payload.get("items") or payload.get("data") or []
    ids: List[str] = []
    for item in items:
        if isinstance(item, dict):
            tid = item.get("item", {}).get("id") if isinstance(item.get("item"), dict) else None
            if tid is None:
                tid = item.get("id") or item.get("track_id")
            if tid is not None:
                ids.append(str(tid))

    tracks = []
    if hasattr(session, "tracks") and callable(getattr(session, "tracks")) and ids:
        try:
            tracks = list(session.tracks(ids))
        except Exception:
            tracks = []
    if not tracks:
        for tid in ids:
            try:
                tracks.append(session.track(tid))
            except Exception:
                continue
    return [track_to_dict(t) for t in tracks if t is not None]


def list_favorite_albums(
    session: tidalapi.Session, limit: int = 100, offset: int = 0
) -> List[Dict[str, Any]]:
    user = getattr(session, "user", None)
    favorites = getattr(user, "favorites", None) if user is not None else None
    if favorites is not None:
        try:
            order = getattr(getattr(tidalapi, "user", None), "ItemOrder", None)
            direction = getattr(getattr(tidalapi, "user", None), "OrderDirection", None)
            order_val = order.Date if order is not None else None
            dir_val = direction.Descending if direction is not None else None
            albums = favorites.albums(
                limit=limit,
                offset=offset,
                order=order_val,
                order_direction=dir_val,
            )
            return [album_to_dict(a) for a in albums if a is not None]
        except Exception:
            pass
    user_id = _get_user_id(session)
    if not user_id:
        raise RuntimeError("tidalapi session has no user id")
    path = f"users/{user_id}/favorites/albums"
    params = {"limit": int(limit), "offset": int(offset), "order": "DATE", "orderDirection": "DESC"}
    req_obj = getattr(session, "request", None)
    if req_obj is None or not hasattr(req_obj, "request"):
        raise RuntimeError("tidalapi session does not expose a request method")
    resp = req_obj.request("GET", path, params=params)

    payload = None
    if isinstance(resp, dict):
        payload = resp
    elif hasattr(resp, "json") and callable(getattr(resp, "json")):
        try:
            payload = resp.json()
        except Exception:
            payload = None
    if payload is None:
        return []

    items = payload.get("items") or payload.get("data") or []
    ids: List[str] = []
    for item in items:
        if isinstance(item, dict):
            aid = item.get("item", {}).get("id") if isinstance(item.get("item"), dict) else None
            if aid is None:
                aid = item.get("id") or item.get("album_id")
            if aid is not None:
                ids.append(str(aid))

    albums = []
    for aid in ids:
        try:
            albums.append(session.album(aid))
        except Exception:
            continue
    return [album_to_dict(a) for a in albums if a is not None]


def list_favorite_playlists(
    session: tidalapi.Session, limit: int = 100, offset: int = 0
) -> List[Dict[str, Any]]:
    user = getattr(session, "user", None)
    favorites = getattr(user, "favorites", None) if user is not None else None
    if favorites is not None:
        try:
            order = getattr(getattr(tidalapi, "user", None), "ItemOrder", None)
            direction = getattr(getattr(tidalapi, "user", None), "OrderDirection", None)
            order_val = order.Date if order is not None else None
            dir_val = direction.Descending if direction is not None else None
            playlists = favorites.playlists(
                limit=limit,
                offset=offset,
                order=order_val,
                order_direction=dir_val,
            )
            return [playlist_to_dict(p) for p in playlists if p is not None]
        except Exception:
            pass
    user_id = _get_user_id(session)
    if not user_id:
        raise RuntimeError("tidalapi session has no user id")
    path = f"users/{user_id}/favorites/playlists"
    params = {"limit": int(limit), "offset": int(offset), "order": "DATE", "orderDirection": "DESC"}
    req_obj = getattr(session, "request", None)
    if req_obj is None or not hasattr(req_obj, "request"):
        raise RuntimeError("tidalapi session does not expose a request method")
    resp = req_obj.request("GET", path, params=params)

    payload = None
    if isinstance(resp, dict):
        payload = resp
    elif hasattr(resp, "json") and callable(getattr(resp, "json")):
        try:
            payload = resp.json()
        except Exception:
            payload = None
    if payload is None:
        return []

    items = payload.get("items") or payload.get("data") or []
    ids: List[str] = []
    for item in items:
        if isinstance(item, dict):
            pid = item.get("item", {}).get("id") if isinstance(item.get("item"), dict) else None
            if pid is None:
                pid = item.get("id") or item.get("playlist_id")
            if pid is not None:
                ids.append(str(pid))

    playlists = []
    for pid in ids:
        try:
            playlists.append(session.playlist(pid))
        except Exception:
            continue
    return [playlist_to_dict(p) for p in playlists if p is not None]


def list_favorite_artists(
    session: tidalapi.Session, limit: int = 100, offset: int = 0
) -> List[Dict[str, Any]]:
    user = getattr(session, "user", None)
    favorites = getattr(user, "favorites", None) if user is not None else None
    if favorites is not None:
        try:
            order = getattr(getattr(tidalapi, "user", None), "ItemOrder", None)
            direction = getattr(getattr(tidalapi, "user", None), "OrderDirection", None)
            order_val = order.Date if order is not None else None
            dir_val = direction.Descending if direction is not None else None
            artists = favorites.artists(
                limit=limit,
                offset=offset,
                order=order_val,
                order_direction=dir_val,
            )
            out: List[Dict[str, Any]] = []
            for a in artists:
                if a is None:
                    continue
                artist_obj = a
                aid = _get_attr(a, "id", None)
                if aid is not None:
                    try:
                        artist_obj = session.artist(str(aid))
                    except Exception:
                        artist_obj = a
                out.append(artist_to_dict(artist_obj, session=session, include_details=False))
            return out
        except Exception:
            pass
    user_id = _get_user_id(session)
    if not user_id:
        raise RuntimeError("tidalapi session has no user id")
    path = f"users/{user_id}/favorites/artists"
    params = {"limit": int(limit), "offset": int(offset), "order": "DATE", "orderDirection": "DESC"}
    req_obj = getattr(session, "request", None)
    if req_obj is None or not hasattr(req_obj, "request"):
        raise RuntimeError("tidalapi session does not expose a request method")
    resp = req_obj.request("GET", path, params=params)

    payload = None
    if isinstance(resp, dict):
        payload = resp
    elif hasattr(resp, "json") and callable(getattr(resp, "json")):
        try:
            payload = resp.json()
        except Exception:
            payload = None
    if payload is None:
        return []

    items = payload.get("items") or payload.get("data") or []
    ids: List[str] = []
    for item in items:
        if isinstance(item, dict):
            aid = item.get("item", {}).get("id") if isinstance(item.get("item"), dict) else None
            if aid is None:
                aid = item.get("id") or item.get("artist_id")
            if aid is not None:
                ids.append(str(aid))

    artists = []
    for aid in ids:
        try:
            artists.append(session.artist(aid))
        except Exception:
            continue
    return [artist_to_dict(a, session=session, include_details=False) for a in artists if a is not None]


def set_track_favorite(session: tidalapi.Session, track_id: str, favorite: bool) -> None:
    user = getattr(session, "user", None)
    favorites = getattr(user, "favorites", None) if user is not None else None
    if favorites is not None:
        ok = favorites.add_track(str(track_id)) if favorite else favorites.remove_track(str(track_id))
        if ok:
            return
    user_id = _get_user_id(session)
    if not user_id:
        raise RuntimeError("tidalapi session has no user id")
    path = f"users/{user_id}/favorites/tracks"
    req_obj = getattr(session, "request", None)
    if req_obj is None or not hasattr(req_obj, "request"):
        raise RuntimeError("tidalapi session does not expose a request method")
    method = "POST" if favorite else "DELETE"
    data = {"trackId": str(track_id)} if favorite else None
    if favorite:
        req_obj.request(method, path, data=data)
    else:
        req_obj.request(method, f"{path}/{track_id}")


def set_album_favorite(session: tidalapi.Session, album_id: str, favorite: bool) -> None:
    user = getattr(session, "user", None)
    favorites = getattr(user, "favorites", None) if user is not None else None
    if favorites is not None:
        ok = favorites.add_album(str(album_id)) if favorite else favorites.remove_album(str(album_id))
        if ok:
            return
    user_id = _get_user_id(session)
    if not user_id:
        raise RuntimeError("tidalapi session has no user id")
    path = f"users/{user_id}/favorites/albums"
    req_obj = getattr(session, "request", None)
    if req_obj is None or not hasattr(req_obj, "request"):
        raise RuntimeError("tidalapi session does not expose a request method")
    method = "POST" if favorite else "DELETE"
    data = {"albumId": str(album_id)} if favorite else None
    if favorite:
        req_obj.request(method, path, data=data)
    else:
        req_obj.request(method, f"{path}/{album_id}")


def set_playlist_favorite(session: tidalapi.Session, playlist_id: str, favorite: bool) -> None:
    user = getattr(session, "user", None)
    favorites = getattr(user, "favorites", None) if user is not None else None
    if favorites is not None:
        ok = favorites.add_playlist(str(playlist_id)) if favorite else favorites.remove_playlist(str(playlist_id))
        if ok:
            return
    user_id = _get_user_id(session)
    if not user_id:
        raise RuntimeError("tidalapi session has no user id")
    path = f"users/{user_id}/favorites/playlists"
    req_obj = getattr(session, "request", None)
    if req_obj is None or not hasattr(req_obj, "request"):
        raise RuntimeError("tidalapi session does not expose a request method")
    method = "POST" if favorite else "DELETE"
    data = {"playlistId": str(playlist_id)} if favorite else None
    if favorite:
        req_obj.request(method, path, data=data)
    else:
        req_obj.request(method, f"{path}/{playlist_id}")


def set_artist_favorite(session: tidalapi.Session, artist_id: str, favorite: bool) -> None:
    user = getattr(session, "user", None)
    favorites = getattr(user, "favorites", None) if user is not None else None
    if favorites is not None:
        ok = favorites.add_artist(str(artist_id)) if favorite else favorites.remove_artist(str(artist_id))
        if ok:
            return
    user_id = _get_user_id(session)
    if not user_id:
        raise RuntimeError("tidalapi session has no user id")
    path = f"users/{user_id}/favorites/artists"
    req_obj = getattr(session, "request", None)
    if req_obj is None or not hasattr(req_obj, "request"):
        raise RuntimeError("tidalapi session does not expose a request method")
    method = "POST" if favorite else "DELETE"
    data = {"artistId": str(artist_id)} if favorite else None
    if favorite:
        req_obj.request(method, path, data=data)
    else:
        req_obj.request(method, f"{path}/{artist_id}")

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
