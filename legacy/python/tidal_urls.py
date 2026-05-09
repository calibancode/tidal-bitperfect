#!/usr/bin/env python3

import urllib.parse
import urllib.request
from typing import Tuple


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

    if len(parts) >= 2 and parts[0] in ("track", "album", "playlist", "artist"):
        kind = parts[0]
        if kind == "playlist":
            item_id = parts[1]
        else:
            item_id = parts[1].split("-")[0]
        if not item_id:
            raise ValueError(f"missing {kind} id in url: {url}")
        return kind, item_id

    raise ValueError(f"unrecognized tidal url format: {url}")
