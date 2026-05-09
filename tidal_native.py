#!/usr/bin/env python3

import os
import queue
import shutil
import subprocess
import threading
from typing import Callable, Optional, List


LineHandler = Callable[[str], Optional[str]]
PollHook = Callable[["NativePlaybackClient"], None]
StopCheck = Callable[[], bool]


def native_player_path() -> Optional[str]:
    if os.environ.get("TIDAL_DISABLE_NATIVE_PLAYER") == "1":
        return None
    override = os.environ.get("TIDAL_NATIVE_PLAYER")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    found = shutil.which("tidal-native-player")
    if found:
        return found
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in (
        "tidal-native-player",
        os.path.join("build", "tidal-native-player"),
        os.path.join("build", "native", "tidal-native-player"),
    ):
        candidate = os.path.join(here, rel)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


class NativePlaybackClient:
    """Small line-protocol client for the native playback engine."""

    def __init__(self, helper_path: Optional[str] = None):
        self.helper_path = helper_path or native_player_path()
        self.process: Optional[subprocess.Popen[str]] = None
        self._events: "queue.Queue[tuple[str, str]]" = queue.Queue()

    @property
    def available(self) -> bool:
        return bool(self.helper_path)

    def send(self, command: str) -> None:
        proc = self.process
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(command + "\n")
            proc.stdin.flush()
        except Exception:
            pass

    def play_file(
        self,
        path: str,
        device: str,
        volume_percent: int,
        *,
        on_line: LineHandler,
        poll: Optional[PollHook] = None,
        should_stop: Optional[StopCheck] = None,
    ) -> bool:
        return self._run_play_command(
            ["play_file", path, device, str(int(volume_percent))],
            on_line=on_line,
            poll=poll,
            should_stop=should_stop,
        )

    def play_ffmpeg(
        self,
        inp: str,
        device: str,
        volume_percent: int,
        codec: str,
        duration_s: float,
        protocol_whitelist: bool,
        *,
        on_line: LineHandler,
        poll: Optional[PollHook] = None,
        should_stop: Optional[StopCheck] = None,
    ) -> bool:
        return self._run_play_command(
            [
                "play_ffmpeg",
                inp,
                device,
                str(int(volume_percent)),
                codec,
                f"{max(0.0, float(duration_s)):.3f}",
                "1" if protocol_whitelist else "0",
            ],
            on_line=on_line,
            poll=poll,
            should_stop=should_stop,
        )

    def _start(self) -> None:
        if not self.helper_path:
            raise RuntimeError("native player is not available")
        self.process = subprocess.Popen(
            [self.helper_path, "--daemon"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._events = queue.Queue()
        if self.process.stdout is not None:
            threading.Thread(
                target=self._reader,
                args=(self.process.stdout, "stdout"),
                daemon=True,
                name="native-player-stdout",
            ).start()
        if self.process.stderr is not None:
            threading.Thread(
                target=self._reader,
                args=(self.process.stderr, "stderr"),
                daemon=True,
                name="native-player-stderr",
            ).start()
        self._wait_ready()

    def _reader(self, stream, name: str) -> None:
        try:
            for line in stream:
                self._events.put((name, line))
        except Exception:
            pass

    def _wait_ready(self) -> None:
        proc = self.process
        if proc is None:
            raise RuntimeError("native player was not started")
        while proc.poll() is None:
            try:
                name, line = self._events.get(timeout=5)
            except queue.Empty:
                raise RuntimeError("native player did not become ready")
            if name == "stderr":
                continue
            if line.strip() == "READY":
                return
            if line.startswith("ERROR "):
                raise RuntimeError(line[len("ERROR "):].strip())
        raise RuntimeError("native player exited before ready")

    def _run_play_command(
        self,
        fields: List[str],
        *,
        on_line: LineHandler,
        poll: Optional[PollHook],
        should_stop: Optional[StopCheck],
    ) -> bool:
        native_error: Optional[str] = None
        stderr_lines: List[str] = []
        done = False
        try:
            self._start()
            self.send("\t".join(fields))
            assert self.process is not None

            while self.process.poll() is None and not done:
                if poll is not None:
                    poll(self)
                if should_stop is not None and should_stop():
                    self.send("stop")

                try:
                    name, line = self._events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if name == "stderr":
                    stderr_lines.append(line.strip())
                    continue
                stripped = line.strip()
                if stripped == "READY":
                    continue
                if stripped == "DONE":
                    done = True
                    continue
                if stripped == "BYE":
                    done = True
                    continue
                err = on_line(line)
                if err is not None:
                    native_error = err

            if self.process.poll() is not None:
                self._drain_remaining(on_line, stderr_lines)
            rc = self.process.returncode
            if native_error is not None:
                raise RuntimeError(native_error)
            if rc not in (0, None):
                stderr = "\n".join(line for line in stderr_lines if line)
                raise RuntimeError(
                    f"native player exited with rc={rc}" + (f"\n{stderr}" if stderr else "")
                )
            return True
        finally:
            self.shutdown()

    def _drain_remaining(self, on_line: LineHandler, stderr_lines: List[str]) -> None:
        proc = self.process
        if proc is None:
            return
        while True:
            try:
                name, line = self._events.get_nowait()
            except queue.Empty:
                return
            if name == "stderr":
                stderr_lines.append(line.strip())
            elif line.strip() not in ("DONE", "READY", "BYE"):
                on_line(line)

    def shutdown(self) -> None:
        proc = self.process
        try:
            self.send("shutdown")
            if proc is not None and proc.poll() is None:
                proc.wait(timeout=1)
        except Exception:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        finally:
            self.process = None
