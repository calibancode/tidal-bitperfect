#!/usr/bin/env python3
"""Dynamic smoke checks for native queued FLAC handoff."""

from __future__ import annotations

import argparse
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def encode_field(value: str) -> str:
    out: list[str] = []
    for char in value:
        code = ord(char)
        if char in "%=\n\r" or code < 0x20 or code > 0x7E:
            out.append(f"%{code:02X}")
        else:
            out.append(char)
    return "".join(out)


def decode_field(value: str) -> str:
    raw = bytearray()
    i = 0
    while i < len(value):
        if value[i] == "%" and i + 2 < len(value):
            try:
                raw.append(int(value[i + 1 : i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        raw.extend(value[i].encode("utf-8"))
        i += 1
    return raw.decode("utf-8", errors="replace")


def frame(message_type: str, **fields: object) -> bytes:
    payload = message_type + "\n"
    for key, value in fields.items():
        payload += f"{key}={encode_field(str(value))}\n"
    return f"{len(payload)}\n{payload}".encode("utf-8")


def parse_frames(buffer: bytes) -> tuple[list[tuple[str, dict[str, str]]], bytes]:
    events: list[tuple[str, dict[str, str]]] = []
    while True:
        newline = buffer.find(b"\n")
        if newline < 0:
            return events, buffer
        header = buffer[:newline]
        if not header.isdigit():
            raise AssertionError(f"invalid native frame header: {header!r}")
        payload_size = int(header)
        payload_start = newline + 1
        if len(buffer) < payload_start + payload_size:
            return events, buffer
        payload = buffer[payload_start : payload_start + payload_size].decode("utf-8")
        lines = payload.split("\n")
        fields: dict[str, str] = {}
        for line in lines[1:]:
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key] = decode_field(value)
        events.append((lines[0], fields))
        buffer = buffer[payload_start + payload_size :]


def make_flac(ffmpeg: str, path: Path, frequency: int, sample_rate: int, silent: bool) -> None:
    source = f"anullsrc=r={sample_rate}:cl=stereo" if silent else f"sine=frequency={frequency}:duration=0.5"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        source,
    ]
    if silent:
        command += ["-t", "0.5"]
    else:
        command += ["-ar", str(sample_rate), "-ac", "2"]
    command += ["-sample_fmt", "s16", str(path), "-y"]
    subprocess.run(command, check=True)


def collect_events(
    process: subprocess.Popen[bytes],
    buffer: bytes,
    events: list[tuple[str, dict[str, str]]],
    timeout: float,
) -> bytes:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = process.stdout.read1(4096)
        if not chunk:
            break
        new_events, buffer = parse_frames(buffer + chunk)
        events.extend(new_events)
        if any(event == "DONE" for event, _ in new_events):
            break
    return buffer


def run_daemon_case(
    native_player: Path,
    play_message: bytes,
    next_path: Path,
    expect_advanced: bool,
    expect_mismatch: bool,
) -> list[tuple[str, dict[str, str]]]:
    process = subprocess.Popen(
        [str(native_player), "--daemon"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    events: list[tuple[str, dict[str, str]]] = []
    buffer = b""
    try:
        buffer = collect_events(process, buffer, events, 0.5)
        if not any(event == "READY" for event, _ in events):
            raise AssertionError("native daemon did not report READY")
        assert process.stdin is not None
        process.stdin.write(play_message + frame("next", track_id="next", path=next_path))
        process.stdin.flush()
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not any(event == "DONE" for event, _ in events):
            buffer = collect_events(process, buffer, events, 0.25)
        process.stdin.write(frame("shutdown"))
        process.stdin.flush()
        buffer = collect_events(process, buffer, events, 0.5)
        process.wait(timeout=2.0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2.0)

    stderr = process.stderr.read().decode("utf-8", errors="replace").strip() if process.stderr else ""
    if stderr:
        raise AssertionError(f"native daemon stderr was not empty: {stderr}")
    if not any(event == "DONE" for event, _ in events):
        raise AssertionError("native daemon did not finish playback")
    advanced = [fields for event, fields in events if event == "ADVANCED"]
    mismatch_logs = [
        fields.get("message", "")
        for event, fields in events
        if event == "LOG" and "format mismatch" in fields.get("message", "")
    ]
    if bool(advanced) != expect_advanced:
        raise AssertionError(f"ADVANCED expectation failed: got {advanced!r}")
    if bool(mismatch_logs) != expect_mismatch:
        raise AssertionError(f"format-mismatch expectation failed: got {mismatch_logs!r}")
    if expect_advanced:
        event_types = [event for event, _ in events]
        if event_types.index("ADVANCED") > event_types.index("DONE"):
            raise AssertionError("ADVANCED was emitted after DONE")
    return events


def run_split_seek_tail_case(native_player: Path, first: Path, device: str) -> list[tuple[str, dict[str, str]]]:
    process = subprocess.Popen(
        [str(native_player), "--daemon"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    events: list[tuple[str, dict[str, str]]] = []
    buffer = b""
    try:
        buffer = collect_events(process, buffer, events, 0.5)
        if not any(event == "READY" for event, _ in events):
            raise AssertionError("native daemon did not report READY")
        assert process.stdin is not None
        process.stdin.write(frame("play_file", path=first, device=device, volume=100))
        process.stdin.flush()
        time.sleep(0.1)
        seek_frame = frame("seek_to", seconds="0.1")
        process.stdin.write(seek_frame[:8])
        process.stdin.flush()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not any(event == "DONE" for event, _ in events):
            buffer = collect_events(process, buffer, events, 0.25)
        process.stdin.write(seek_frame[8:] + frame("shutdown"))
        process.stdin.flush()
        buffer = collect_events(process, buffer, events, 0.5)
        process.wait(timeout=2.0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2.0)

    stderr = process.stderr.read().decode("utf-8", errors="replace").strip() if process.stderr else ""
    if stderr:
        raise AssertionError(f"native daemon stderr was not empty: {stderr}")
    if any(event == "ERROR" for event, _ in events):
        raise AssertionError(f"native daemon emitted ERROR: {events!r}")
    if not any(event == "BYE" for event, _ in events):
        raise AssertionError("native daemon did not accept shutdown after split seek tail")
    return events


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "native_player",
        nargs="?",
        type=Path,
        default=ROOT / "build" / "tidal-native-player",
        help="path to the built tidal-native-player binary",
    )
    parser.add_argument(
        "--device",
        default="null",
        help="ALSA playback device for the dynamic probe; defaults to null",
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help="generate silent FLAC fixtures, useful when probing real hardware devices",
    )
    args = parser.parse_args()

    native_player = args.native_player.resolve()
    if not native_player.exists():
        raise SystemExit(f"native player not found: {native_player}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is required for native gapless smoke")

    with tempfile.TemporaryDirectory(prefix="tidal_gapless_smoke_") as tmp:
        tmpdir = Path(tmp)
        first = tmpdir / "first.flac"
        second = tmpdir / "second.flac"
        mismatch = tmpdir / "mismatch.flac"
        make_flac(ffmpeg, first, 440, 44100, args.silent)
        make_flac(ffmpeg, second, 660, 44100, args.silent)
        make_flac(ffmpeg, mismatch, 880, 48000, args.silent)

        cases = [
            (
                "file->file same-format handoff",
                frame("play_file", path=first, device=args.device, volume=100),
                second,
                True,
                False,
            ),
            (
                "ffmpeg->file same-format handoff",
                frame(
                    "play_ffmpeg",
                    input=first,
                    device=args.device,
                    volume=100,
                    codec="pcm_s16le",
                    duration="0.5",
                ),
                second,
                True,
                False,
            ),
            (
                "ffmpeg->file mismatch fallback",
                frame(
                    "play_ffmpeg",
                    input=first,
                    device=args.device,
                    volume=100,
                    codec="pcm_s16le",
                    duration="0.5",
                ),
                mismatch,
                False,
                True,
            ),
        ]
        for name, play_message, next_path, expect_advanced, expect_mismatch in cases:
            events = run_daemon_case(native_player, play_message, next_path, expect_advanced, expect_mismatch)
            event_types = [event for event, _ in events]
            print(f"{name}: PASS ({', '.join(event_types)})")
        events = run_split_seek_tail_case(native_player, first, args.device)
        event_types = [event for event, _ in events]
        print(f"split seek frame tail resync: PASS ({', '.join(event_types)})")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"native gapless smoke: {exc}", file=sys.stderr)
        raise SystemExit(1)
