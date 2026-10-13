"""Stdlib-only Chromium lifecycle management via Chrome DevTools Protocol.

Scope is deliberately limited to browser lifecycle (issue #1):

- discover a Chromium-based executable
- launch it with remote debugging enabled
- expose browser / connection / active-page state
- close processes and resources safely (idempotent)
- raise clear errors when startup fails

No third-party runtime dependencies. Only the Python standard library.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse

__all__ = [
    "BrowserLaunchError",
    "BrowserNotRunningError",
    "CdpConnection",
    "BrowserController",
    "find_chromium_executable",
]

_DEFAULT_TIMEOUT = 30.0
_POLL_INTERVAL = 0.1

_ENV_VARS = (
    "BROWSERMIND_CHROME_EXE",
    "BROWSERMIND_CHROMIUM_EXE",
    "CHROME_EXE",
    "CHROMIUM_EXE",
)

_WHICH_NAMES = (
    "chrome",
    "chrome.exe",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "msedge",
    "microsoft-edge",
    "edge",
)


def _well_known_paths() -> list[str]:
    candidates: list[str] = []
    if os.name == "nt":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local_app = os.environ.get("LOCALAPPDATA", "")
        candidates += [
            os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(program_files, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(program_files_x86, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
        ]
        if local_app:
            candidates += [
                os.path.join(local_app, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(local_app, "Microsoft", "Edge", "Application", "msedge.exe"),
            ]
    elif sys_platform_darwin():
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        candidates += [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/microsoft-edge",
            "/snap/bin/chromium",
        ]
    return candidates


def sys_platform_darwin() -> bool:
    # Small indirection so tests can patch platform behaviour if needed.
    import sys

    return sys.platform == "darwin"


class BrowserLaunchError(RuntimeError):
    """Raised when the browser cannot be started or connected."""


class BrowserNotRunningError(RuntimeError):
    """Raised when an operation requires a running browser."""


def find_chromium_executable(explicit: str | None = None) -> str:
    """Return a usable Chromium-based executable path.

    Order: explicit arg -> env vars -> PATH -> well-known locations.
    Raises BrowserLaunchError when nothing usable is found.
    """
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        raise BrowserLaunchError(f"configured browser executable not found: {explicit}")

    for var in _ENV_VARS:
        value = os.environ.get(var, "").strip()
        if not value:
            continue
        if os.path.isfile(value):
            return value
        raise BrowserLaunchError(f"env {var} points to missing file: {value}")

    for name in _WHICH_NAMES:
        found = shutil.which(name)
        if found and os.path.isfile(found):
            return found

    for path in _well_known_paths():
        if path and os.path.isfile(path):
            return path

    raise BrowserLaunchError(
        "no Chromium-based executable found; set BROWSERMIND_CHROME_EXE "
        "or install Chrome/Edge/Chromium"
    )


def _find_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _http_get_json(host: str, port: int, path: str, timeout: float = 2.0):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise ConnectionError(f"GET {path} -> HTTP {resp.status}")
        return json.loads(body.decode("utf-8"))
    finally:
        conn.close()


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("websocket closed during recv")
        buf += chunk
    return buf


class _WebSocket:
    """Minimal RFC6455 client (text frames + ping/pong + close). Stdlib only."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None

    def connect(self, url: str, timeout: float = 5.0) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise BrowserLaunchError(f"unsupported websocket scheme: {parsed.scheme}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        raw = socket.create_connection((host, port), timeout=timeout)
        if parsed.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        raw.settimeout(timeout)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        raw.sendall(request.encode("ascii"))

        header = b""
        while b"\r\n\r\n" not in header:
            chunk = raw.recv(4096)
            if not chunk:
                raw.close()
                raise BrowserLaunchError("websocket handshake failed (no response)")
            header += chunk
            if len(header) > 16384:
                raw.close()
                raise BrowserLaunchError("websocket handshake header too large")
        head_text = header.decode("latin1")
        if " 101 " not in head_text.split("\r\n", 1)[0]:
            raw.close()
            raise BrowserLaunchError(f"websocket handshake rejected: {head_text.splitlines()[0]}")
        # Note: Chromium's DevTools endpoint does not return a strict
        # RFC6455 Sec-WebSocket-Accept derived from our key (it answers
        # 101 with its own value). We accept any 101 here; the connection
        # is proven by the subsequent CDP round-trip, not the accept key.
        self._sock = raw

    def send_text(self, text: str) -> None:
        if self._sock is None:
            raise BrowserNotRunningError("websocket is not connected")
        data = text.encode("utf-8")
        mask = os.urandom(4)
        length = len(data)
        frame = bytearray([0x81])  # FIN + text opcode
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame += struct.pack("!H", length)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack("!Q", length)
        frame += mask
        frame += bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._sock.sendall(bytes(frame))

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        assert self._sock is not None
        mask = os.urandom(4)
        frame = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame += struct.pack("!H", length)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack("!Q", length)
        frame += mask
        frame += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(frame))

    def recv_message(self, timeout: float = 5.0) -> str:
        if self._sock is None:
            raise BrowserNotRunningError("websocket is not connected")
        self._sock.settimeout(timeout)
        fragments: list[bytes] = []
        while True:
            hdr = _recv_exactly(self._sock, 2)
            b1, b2 = hdr[0], hdr[1]
            fin = (b1 >> 7) & 1
            opcode = b1 & 0x0F
            masked = (b2 >> 7) & 1
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", _recv_exactly(self._sock, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", _recv_exactly(self._sock, 8))[0]
            mask_key = _recv_exactly(self._sock, 4) if masked else None
            payload = _recv_exactly(self._sock, length) if length else b""
            if masked and mask_key:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong -> ignore
                continue
            if opcode == 0x8:  # close
                try:
                    self._send_frame(0x8, payload)
                except OSError:
                    pass
                raise ConnectionError("websocket closed by peer")
            if opcode in (0x1, 0x0):  # text / continuation
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8")
                continue
            if opcode == 0x2:  # binary: accumulate similarly
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")
                continue
            # Unknown control opcode: ignore per RFC.

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            try:
                # Best-effort close frame; ignore failures.
                mask = os.urandom(4)
                sock.settimeout(1.0)
                sock.sendall(bytes([0x88, 0x80 | 0]) + mask)
            except OSError:
                pass
        finally:
            try:
                sock.close()
            except OSError:
                pass


class CdpConnection:
    """Thin CDP channel over a browser-level websocket."""

    def __init__(self, ws_url: str, timeout: float = 5.0) -> None:
        self.ws_url = ws_url
        self.timeout = timeout
        self._ws = _WebSocket()
        self._next_id = 1
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self, timeout: float | None = None) -> None:
        if self._connected:
            return
        self._ws.connect(self.ws_url, timeout=timeout or self.timeout)
        self._connected = True

    def send_command(self, method: str, params: dict | None = None,
                     timeout: float | None = None):
        """Send one CDP command and wait for its matching id."""
        if not self._connected:
            raise BrowserNotRunningError("CDP connection is not open")
        cmd_id = self._next_id
        self._next_id += 1
        payload = {"id": cmd_id, "method": method}
        if params:
            payload["params"] = params
        self._ws.send_text(json.dumps(payload))
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"CDP command timed out: {method}")
            text = self._ws.recv_message(timeout=remaining)
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == cmd_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP error for {method}: {msg['error']}")
                return msg.get("result")
            # Otherwise it is an event/broadcast: ignore for lifecycle use.

    def close(self) -> None:
        self._connected = False
        try:
            self._ws.close()
        except OSError:
            pass


class BrowserController:
    """Launch, track, and safely close one Chromium process."""

    def __init__(
        self,
        executable: str | None = None,
        port: int = 0,
        headless: bool = True,
        timeout: float = _DEFAULT_TIMEOUT,
        extra_args: list[str] | None = None,
        user_data_dir: str | None = None,
        keep_profile: bool = False,
    ) -> None:
        self._executable_arg = executable
        self._requested_port = int(port or 0)
        self._headless = bool(headless)
        self._timeout = float(timeout)
        self._extra_args = list(extra_args or [])
        self._profile_arg = user_data_dir
        self._keep_profile = bool(keep_profile)

        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._profile_dir: str | None = None
        self._owns_profile = False
        self._browser_info: dict | None = None
        self._connection: CdpConnection | None = None
        self._active_page: dict | None = None
        self._resolved_executable: str | None = None
        self._active_port: int | None = None
        self._log_path: str | None = None

    # ---- state -----------------------------------------------------
    @property
    def browser(self) -> dict | None:
        """Browser version/info from /json/version (None when closed)."""
        return dict(self._browser_info) if self._browser_info else None

    @property
    def connection(self) -> CdpConnection | None:
        """Live CDP connection object (None when closed)."""
        return self._connection

    @property
    def active_page(self) -> dict | None:
        """Active page target dict from /json/list (None when closed/unknown)."""
        return dict(self._active_page) if self._active_page else None

    @property
    def is_running(self) -> bool:
        proc = self._process
        return proc is not None and proc.poll() is None

    @property
    def pid(self) -> int | None:
        proc = self._process
        if proc is None or proc.poll() is not None:
            return None
        return proc.pid

    @property
    def port(self) -> int:
        return self._active_port if self._active_port else self._requested_port

    @property
    def debugger_url(self) -> str | None:
        if self._browser_info:
            return self._browser_info.get("webSocketDebuggerUrl")
        return None

    @property
    def executable(self) -> str | None:
        return self._resolved_executable or self._executable_arg

    # ---- lifecycle -------------------------------------------------
    def launch(self, timeout: float | None = None) -> "BrowserController":
        """Launch the browser and connect via CDP. Idempotent when running."""
        with self._lock:
            if self.is_running:
                return self
            # Allow relaunch after close(): drop stale state first.
            self._reset_state_keep_config()

            exe = find_chromium_executable(self._executable_arg)
            self._resolved_executable = exe

            if self._requested_port:
                self._active_port = self._assert_port_free(self._requested_port)
            else:
                self._active_port = _find_free_port()
            assert self._active_port is not None

            if self._profile_arg:
                self._profile_dir = self._profile_arg
                self._owns_profile = False
                os.makedirs(self._profile_dir, exist_ok=True)
            else:
                self._profile_dir = tempfile.mkdtemp(prefix="browsermind-profile-")
                self._owns_profile = True

            self._log_path = os.path.join(self._profile_dir, "chrome.log")
            args = self._build_args(exe, self._active_port, self._profile_dir)

            effective_timeout = float(timeout if timeout is not None else self._timeout)
            try:
                proc = self._spawn(args)
                self._process = proc
                info = self._wait_for_debugger(
                    "127.0.0.1", self._active_port, proc, effective_timeout
                )
                self._browser_info = info
                ws_url = info.get("webSocketDebuggerUrl")
                if not ws_url:
                    raise BrowserLaunchError("debugger did not report webSocketDebuggerUrl")
                conn = CdpConnection(ws_url)
                try:
                    conn.connect(timeout=5.0)
                except Exception as exc:
                    raise BrowserLaunchError(f"CDP websocket connect failed: {exc}") from exc
                self._connection = conn
                self._active_page = self._resolve_active_page("127.0.0.1", self._active_port)
                return self
            except Exception:
                self._cleanup_locked()
                raise

    def close(self) -> None:
        """Close connection + browser process + temp profile. Safe to repeat."""
        with self._lock:
            self._cleanup_locked()

    # Backwards-compatible aliases for the previous controller.
    def start(self, timeout: float | None = None) -> "BrowserController":
        return self.launch(timeout=timeout)

    def stop(self) -> None:
        self.close()

    def __enter__(self) -> "BrowserController":
        self.launch()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---- internals (kept small + patchable for tests) --------------
    def _build_args(self, exe: str, port: int, profile: str) -> list[str]:
        args = [
            exe,
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            f"--user-data-dir={profile}",
        ]
        if self._headless:
            args.append("--headless=new")
            args += ["--disable-gpu", "--disable-dev-shm-usage"]
        args += list(self._extra_args)
        # Open a neutral page; keeps /json/list non-empty deterministically.
        args.append("about:blank")
        return args

    def _spawn(self, args: list[str]) -> subprocess.Popen:
        kwargs: dict = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        # Avoid a console window on Windows for headless automation.
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW")
        # Route child stderr to a log file when we own the profile, so
        # early-startup failures are diagnosable. Parent fd is closed
        # right after spawn; the child keeps its own copy.
        log_file = None
        if self._log_path:
            try:
                log_file = open(self._log_path, "w", encoding="utf-8", errors="replace")
                kwargs["stderr"] = log_file
            except OSError:
                log_file = None
        try:
            return subprocess.Popen(args, **kwargs)
        finally:
            if log_file is not None:
                try:
                    log_file.close()
                except OSError:
                    pass

    def _wait_for_debugger(self, host: str, port: int,
                           proc: subprocess.Popen, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        last_error = "no response yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                detail = self._read_log_tail()
                raise BrowserLaunchError(
                    f"browser exited on startup (code {proc.returncode})"
                    + (f": {detail}" if detail else "")
                )
            try:
                return _http_get_json(host, port, "/json/version", timeout=2.0)
            except Exception as exc:  # poll until timeout: refused/timeout/parse
                last_error = str(exc) or type(exc).__name__
                time.sleep(_POLL_INTERVAL)
        # Timeout: make sure we do not orphan the child.
        raise BrowserLaunchError(
            f"timed out waiting for browser debugger on {host}:{port}: {last_error}"
        )

    def _resolve_active_page(self, host: str, port: int) -> dict | None:
        try:
            targets = _http_get_json(host, port, "/json/list", timeout=2.0)
        except Exception:
            return None
        if isinstance(targets, list):
            pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
            if pages:
                return pages[0]
        # No page yet: ask the browser to open one via the HTTP endpoint.
        try:
            _http_get_json(host, port, "/json/new?about:blank", timeout=2.0)
        except Exception:
            pass
        try:
            targets = _http_get_json(host, port, "/json/list", timeout=2.0)
        except Exception:
            return None
        if isinstance(targets, list):
            for t in targets:
                if isinstance(t, dict) and t.get("type") == "page":
                    return t
        return None

    def _read_log_tail(self, limit: int = 2000) -> str:
        if not self._log_path or not os.path.isfile(self._log_path):
            return ""
        try:
            with open(self._log_path, encoding="utf-8", errors="replace") as f:
                data = f.read()
            return data[-limit:].strip().splitlines()[-1] if data.strip() else ""
        except OSError:
            return ""

    @staticmethod
    def _assert_port_free(port: int, host: str = "127.0.0.1") -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
            except OSError:
                raise BrowserLaunchError(f"debug port {port} is already in use")
        return port

    def _reset_state_keep_config(self) -> None:
        self._process = None
        self._browser_info = None
        self._connection = None
        self._active_page = None
        self._resolved_executable = self._resolved_executable  # keep for introspection
        self._active_port = None
        # profile dir is recreated on next launch when we own it.

    def _cleanup_locked(self) -> None:
        conn, self._connection = self._connection, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

        proc, self._process = self._process, None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except (subprocess.TimeoutExpired, OSError):
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            pass
            except OSError:
                pass

        self._browser_info = None
        self._active_page = None
        self._active_port = None

        profile, owns = self._profile_dir, self._owns_profile
        self._profile_dir = None if owns else self._profile_dir
        self._owns_profile = False
        if profile and owns and not self._keep_profile:
            try:
                shutil.rmtree(profile, ignore_errors=True)
            except OSError:
                pass
