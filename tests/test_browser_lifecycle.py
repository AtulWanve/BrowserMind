"""Lifecycle tests for issue #1. Stdlib only (unittest + mock)."""
import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import browser_controller as bc


VERSION_PAYLOAD = {
    "Browser": "Chrome/154.0.0.0",
    "Protocol-Version": "1.3",
    "webSocketDebuggerUrl": "ws://127.0.0.1:19999/devtools/browser/fake-id",
}

PAGE_PAYLOAD = [
    {
        "id": "PAGE1",
        "type": "page",
        "title": "about:blank",
        "url": "about:blank",
        "webSocketDebuggerUrl": "ws://127.0.0.1:19999/devtools/page/PAGE1",
    }
]


class FakePopen:
    instances = []

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 424200 + len(FakePopen.instances)
        self._alive = True
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
        FakePopen.instances.append(self)
        # Allow tests to force early exit:
        if getattr(self, "_force_early_exit", False):
            self._alive = False
            self.returncode = 1

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self._alive = False
        self.returncode = -15

    def kill(self):
        self.kill_calls += 1
        self._alive = False
        self.returncode = -9

    def wait(self, timeout=None):
        if self._alive:
            raise subprocess.TimeoutExpired(self.args, timeout or 0)
        return self.returncode


class DeadPopen(FakePopen):
    def __init__(self, args, **kwargs):
        super().__init__(args, **kwargs)
        self._alive = False
        self.returncode = 1


class FakeConnection:
    instances = []

    def __init__(self, ws_url, timeout=5.0):
        self.ws_url = ws_url
        self.timeout = timeout
        self.connect_calls = 0
        self.close_calls = 0
        self._connected = False
        FakeConnection.instances.append(self)

    @property
    def connected(self):
        return self._connected

    def connect(self, timeout=None):
        self.connect_calls += 1
        self._connected = True

    def send_command(self, method, params=None, timeout=None):
        return {}

    def close(self):
        self.close_calls += 1
        self._connected = False


def fake_http_json(host, port, path, timeout=2.0):
    if path == "/json/version":
        return dict(VERSION_PAYLOAD)
    if path == "/json/list":
        return [dict(p) for p in PAGE_PAYLOAD]
    if path.startswith("/json/new"):
        return dict(PAGE_PAYLOAD[0])
    raise AssertionError(f"unexpected path {path}")


def make_controller(**overrides):
    kwargs = dict(executable=r"C:\fake\chrome.exe", port=19999, timeout=5.0)
    kwargs.update(overrides)
    return bc.BrowserController(**kwargs)


class TestNoThirdPartyDeps(unittest.TestCase):
    def test_only_stdlib_imports(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "browser_controller.py"),
                  encoding="utf-8") as f:
            tree = ast.parse(f.read())
        stdlib = set(sys.stdlib_module_names)
        stdlib.add("__future__")
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    top = a.name.split(".")[0]
                    if top not in stdlib:
                        bad.append(a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    if top not in stdlib:
                        bad.append(node.module)
        self.assertEqual(bad, [], f"non-stdlib imports: {bad}")

    def test_no_automation_libs_mentioned(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "browser_controller.py"),
                  encoding="utf-8") as f:
            tree = ast.parse(f.read())
        forbidden = ("playwright", "selenium", "puppeteer")
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0].lower() in forbidden:
                        found.append(a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0].lower() in forbidden:
                    found.append(node.module)
        # Also catch dynamic imports: __import__("playwright"), importlib.import_module.
        with open(os.path.join(os.path.dirname(__file__), "..", "browser_controller.py"),
                  encoding="utf-8") as f:
            text = f.read()
        for lib in forbidden:
            if f'__import__("{lib}")' in text or f"import_module('{lib}')" in text:
                found.append(lib)
        self.assertEqual(found, [], f"forbidden runtime dep: {found}")


class TestDiscovery(unittest.TestCase):
    def test_explicit_valid(self):
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            path = tf.name
        try:
            self.assertEqual(bc.find_chromium_executable(path), path)
        finally:
            os.unlink(path)

    def test_explicit_missing_raises(self):
        with self.assertRaises(bc.BrowserLaunchError):
            bc.find_chromium_executable(r"C:\definitely\not\here\chrome.exe")

    def test_env_var_used(self):
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            path = tf.name
        try:
            with mock.patch.dict(os.environ, {"BROWSERMIND_CHROME_EXE": path}):
                with mock.patch.object(shutil, "which", return_value=None):
                    # well-known paths may exist on this machine; env must win.
                    self.assertEqual(bc.find_chromium_executable(), path)
        finally:
            os.unlink(path)

    def test_env_var_missing_file_raises(self):
        with mock.patch.dict(os.environ,
                             {"BROWSERMIND_CHROME_EXE": r"C:\nope\chrome.exe"}):
            with self.assertRaises(bc.BrowserLaunchError):
                bc.find_chromium_executable()


class TestLifecycleWithFakes(unittest.TestCase):
    def setUp(self):
        FakePopen.instances.clear()
        FakeConnection.instances.clear()

    def _patch_all(self, popen_cls=FakePopen):
        return (
            mock.patch.object(bc, "find_chromium_executable",
                              return_value=r"C:\fake\chrome.exe"),
            mock.patch.object(bc, "_find_free_port", return_value=19999),
            mock.patch.object(bc.subprocess, "Popen", popen_cls),
            mock.patch.object(bc, "_http_get_json", side_effect=fake_http_json),
            mock.patch.object(bc, "CdpConnection", FakeConnection),
        )

    def test_initial_state(self):
        c = make_controller()
        self.assertIsNone(c.browser)
        self.assertIsNone(c.connection)
        self.assertIsNone(c.active_page)
        self.assertFalse(c.is_running)
        self.assertIsNone(c.pid)

    def test_launch_and_close(self):
        patches = self._patch_all()
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        c = make_controller()
        c.launch()
        self.assertTrue(c.is_running)
        self.assertIsNotNone(c.browser)
        self.assertIn("webSocketDebuggerUrl", c.browser)
        self.assertIsNotNone(c.connection)
        self.assertTrue(c.connection.connected)
        self.assertIsNotNone(c.active_page)
        self.assertEqual(c.active_page["type"], "page")
        self.assertIsNotNone(c.pid)
        profile = c._profile_dir
        self.assertTrue(profile and os.path.isdir(profile))

        c.close()
        self.assertFalse(c.is_running)
        self.assertIsNone(c.browser)
        self.assertIsNone(c.connection)
        self.assertIsNone(c.active_page)
        self.assertFalse(os.path.exists(profile), "temp profile must be removed")
        # Process was terminated, not orphaned.
        self.assertEqual(len(FakePopen.instances), 1)
        proc = FakePopen.instances[0]
        self.assertIsNotNone(proc.poll())
        self.assertEqual(len(FakeConnection.instances), 1)
        self.assertEqual(FakeConnection.instances[0].close_calls, 1)

    def test_close_is_idempotent(self):
        patches = self._patch_all()
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        c = make_controller()
        c.launch()
        proc = FakePopen.instances[0]
        c.close()
        term_calls = proc.terminate_calls
        c.close()
        c.close()
        self.assertEqual(proc.terminate_calls, term_calls)

    def test_close_without_launch_is_harmless(self):
        c = make_controller()
        c.close()
        c.close()
        self.assertFalse(c.is_running)

    def test_relaunch_after_close(self):
        patches = self._patch_all()
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        c = make_controller()
        c.launch()
        c.close()
        c.launch()
        self.assertTrue(c.is_running)
        self.assertEqual(len(FakePopen.instances), 2)

    def test_launch_idempotent_when_running(self):
        patches = self._patch_all()
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        c = make_controller()
        c.launch()
        c.launch()
        self.assertEqual(len(FakePopen.instances), 1)

    def test_launch_failure_early_exit(self):
        patches = (
            mock.patch.object(bc, "find_chromium_executable",
                              return_value=r"C:\fake\chrome.exe"),
            mock.patch.object(bc, "_find_free_port", return_value=19999),
            mock.patch.object(bc.subprocess, "Popen", DeadPopen),
            mock.patch.object(bc, "CdpConnection", FakeConnection),
        )
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        c = make_controller()
        with self.assertRaises(bc.BrowserLaunchError):
            c.launch()
        self.assertFalse(c.is_running)
        self.assertIsNone(c.browser)

    def test_launch_failure_timeout(self):
        def always_refused(host, port, path, timeout=2.0):
            raise ConnectionRefusedError("refused")

        patches = (
            mock.patch.object(bc, "find_chromium_executable",
                              return_value=r"C:\fake\chrome.exe"),
            mock.patch.object(bc, "_find_free_port", return_value=19999),
            mock.patch.object(bc.subprocess, "Popen", FakePopen),
            mock.patch.object(bc, "_http_get_json", side_effect=always_refused),
            mock.patch.object(bc, "CdpConnection", FakeConnection),
        )
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        c = make_controller(timeout=0.5)
        with self.assertRaises(bc.BrowserLaunchError) as ctx:
            c.launch()
        self.assertIn("timed out", str(ctx.exception).lower())
        self.assertFalse(c.is_running)
        # Failed launch must not orphan the child.
        self.assertEqual(len(FakePopen.instances), 1)
        self.assertIsNotNone(FakePopen.instances[0].poll())

    def test_context_manager_cleans_up(self):
        patches = self._patch_all()
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        with make_controller() as c:
            self.assertTrue(c.is_running)
            profile = c._profile_dir
        self.assertFalse(c.is_running)
        self.assertFalse(os.path.exists(profile))

    def test_start_stop_aliases(self):
        patches = self._patch_all()
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        c = make_controller()
        c.start()
        self.assertTrue(c.is_running)
        c.stop()
        self.assertFalse(c.is_running)


class TestRealBrowser(unittest.TestCase):
    """Integration: uses the real installed browser. Skipped when absent."""

    @unittest.skipUnless(
        os.environ.get("BROWSERMIND_INTEGRATION", "1") == "1",
        "integration disabled",
    )
    def test_launch_close_real(self):
        try:
            exe = bc.find_chromium_executable()
        except bc.BrowserLaunchError as exc:
            self.skipTest(str(exc))
        c = bc.BrowserController(executable=exe, timeout=20.0)
        try:
            c.launch()
        except bc.BrowserLaunchError as exc:
            self.skipTest(f"browser launch failed in this env: {exc}")
        try:
            self.assertTrue(c.is_running)
            self.assertIsNotNone(c.browser)
            self.assertIsNotNone(c.debugger_url)
            self.assertIsNotNone(c.connection)
            self.assertTrue(c.connection.connected)
            # Exercise a real CDP round-trip on the browser-level target.
            result = c.connection.send_command("Target.getTargets", timeout=5.0)
            self.assertIn("targetInfos", result)
            self.assertIsNotNone(c.active_page)
            pid = c.pid
            self.assertIsNotNone(pid)
        finally:
            c.close()
            c.close()  # idempotent
        self.assertFalse(c.is_running)
        self.assertIsNone(c.pid)


if __name__ == "__main__":
    unittest.main()
