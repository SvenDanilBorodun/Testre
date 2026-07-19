"""Contract tests for the GUI elevation helpers (gui_app.py module level).

Headless like test_gui_robot_type: the module-level helpers are extracted from
gui_app.py via ast and exec'd into a namespace of test doubles, so a runner
without tkinter still exercises them (importing gui_app pulls in tkinter).

Pinned invariants:
  * _ps_single_quote doubles apostrophes (a username like O'Brien must not
    terminate a single-quoted PowerShell token early).
  * _run_privileged / _elevate_and_wait return (None, False, 'not supported')
    off Windows.
  * _run_privileged's elevated-direct path builds `"exe" args`, passes NO
    timeout (a timeout kill only reaches powershell.exe — the wsl.exe/msiexec
    grandchildren keep running detached, and killing `wsl --import` mid-copy
    risks the corrupt-VHDX state the import script's disk gate exists to
    prevent), and returns (rc, False, None).
  * _run_privileged wraps the UAC path in try/except so a ctypes failure
    surfaces as a launch-failure tuple instead of killing the worker thread.
  * _SE_ERR_REASONS covers exactly the documented ShellExecute failure codes.
  * EVERY manual-fallback command shown to the student contains no `$` (an
    interactive PowerShell would interpolate it before the child runs), the
    $-laden ps_cmd is never handed to the fallback dialog, and the ps_args the
    finalize fallback DOES hand over is $-free by construction.
"""

import ast
import os
import re
import types
import unittest

_GUI_SRC = os.path.join(
    os.path.dirname(__file__), "..", "gui", "app", "gui_app.py"
)


def _source():
    with open(_GUI_SRC, "r", encoding="utf-8") as fh:
        return fh.read()


def _extract(names, ns):
    """Exec the named module-level defs/assigns from gui_app.py into ``ns``."""
    src = _source()
    tree = ast.parse(src)
    wanted = set(names)
    segments = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in wanted:
                segments.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in wanted:
                    segments.append(ast.get_source_segment(src, node))
    exec("\n\n".join(segments), ns)  # noqa: S102 - test-only, own source
    return ns


class TestPsSingleQuote(unittest.TestCase):
    def setUp(self):
        self.ns = _extract(["_ps_single_quote"], {})

    def test_plain_string_unchanged(self):
        self.assertEqual(self.ns["_ps_single_quote"]("C:\\Temp\\x"), "C:\\Temp\\x")

    def test_apostrophe_doubled(self):
        self.assertEqual(
            self.ns["_ps_single_quote"]("C:\\Users\\O'Brien"),
            "C:\\Users\\O''Brien",
        )

    def test_multiple_apostrophes(self):
        self.assertEqual(self.ns["_ps_single_quote"]("a'b'c"), "a''b''c")

    def test_non_string_coerced(self):
        self.assertEqual(self.ns["_ps_single_quote"](42), "42")


class TestSeErrReasons(unittest.TestCase):
    # ShellExecute's documented sub-32 failure codes (hInstApp).
    KNOWN = {0, 2, 3, 5, 8, 26, 27, 28, 29, 30, 31, 32}

    def test_codes_are_documented_ones(self):
        ns = _extract(["_SE_ERR_REASONS"], {})
        reasons = ns["_SE_ERR_REASONS"]
        self.assertTrue(set(reasons) <= self.KNOWN, set(reasons) - self.KNOWN)
        for v in reasons.values():
            self.assertTrue(v.strip())


class _FakeSys(types.SimpleNamespace):
    pass


def _priv_ns(platform, elevated, run_stub=None, elevate_stub=None):
    """Build a namespace for _run_privileged with controllable doubles."""
    ns = {
        "sys": _FakeSys(platform=platform),
        "os": os,  # the direct path resolves powershell.exe under %SystemRoot%
        "subprocess": types.SimpleNamespace(run=run_stub),
        "_is_elevated": lambda: elevated,
        "_elevate_and_wait": elevate_stub
        or (lambda exe, args, show=1: (0, False, None)),
    }
    return _extract(["_run_privileged"], ns)


class TestRunPrivileged(unittest.TestCase):
    def test_non_windows_not_supported(self):
        ns = _priv_ns("linux", elevated=False)
        self.assertEqual(
            ns["_run_privileged"]("x.exe", "-a"), (None, False, "not supported")
        )

    def test_not_elevated_routes_to_uac_path(self):
        calls = []

        def elevate(exe, args, show=1):
            calls.append((exe, args, show))
            return (7, False, None)

        ns = _priv_ns("win32", elevated=False, elevate_stub=elevate)
        self.assertEqual(ns["_run_privileged"]("p.exe", "-x"), (7, False, None))
        self.assertEqual(calls, [("p.exe", "-x", 1)])

    def test_uac_path_exception_becomes_launch_failure(self):
        def boom(exe, args, show=1):
            raise OSError("ctypes broke")

        ns = _priv_ns("win32", elevated=False, elevate_stub=boom)
        code, cancelled, err = ns["_run_privileged"]("p.exe", "-x")
        self.assertIsNone(code)
        self.assertFalse(cancelled)
        self.assertIn("ctypes broke", err)

    def test_elevated_direct_path_contract(self):
        seen = {}

        def run(cmdline, **kwargs):
            seen["cmdline"] = cmdline
            seen["kwargs"] = kwargs
            return types.SimpleNamespace(returncode=3)

        ns = _priv_ns("win32", elevated=True, run_stub=run)
        self.assertEqual(
            ns["_run_privileged"]("powershell.exe", "-File x"), (3, False, None)
        )
        # The ELEVATED spawn must resolve powershell.exe to its System32 home:
        # plain CreateProcess includes the process CWD in its search order, so
        # a bare name could execute a planted binary from a user-writable
        # directory — with admin rights.
        expected_exe = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
        )
        self.assertEqual(seen["cmdline"], f'"{expected_exe}" -File x')
        # NO timeout: a kill only reaches powershell.exe, never the wsl.exe
        # grandchild — and killing `wsl --import` mid-copy risks a corrupt
        # VHDX. Deliberately matches the UAC path's INFINITE wait.
        self.assertNotIn("timeout", seen["kwargs"])

    def test_elevated_direct_path_exception_is_reported(self):
        def run(cmdline, **kwargs):
            raise OSError("spawn failed")

        ns = _priv_ns("win32", elevated=True, run_stub=run)
        code, cancelled, err = ns["_run_privileged"]("p.exe", "-x")
        self.assertIsNone(code)
        self.assertFalse(cancelled)
        self.assertIn("spawn failed", err)


class TestElevateAndWaitNonWindows(unittest.TestCase):
    def test_non_windows_not_supported(self):
        ns = _extract(["_elevate_and_wait"], {"sys": _FakeSys(platform="linux")})
        self.assertEqual(
            ns["_elevate_and_wait"]("x.exe", "-a"), (None, False, "not supported")
        )


class TestManualFallbackPasteSafety(unittest.TestCase):
    """The command handed to _show_manual_elevation_fallback must survive being
    pasted into an interactive PowerShell: no `$` inside it (the OUTER shell
    interpolates $vars in double quotes before the child parses them — the
    original ps_cmd handed the child ` = ;` fragments and nothing ran).

    Every block is checked, not just the first: gui_app grew a SECOND
    `manual_cmd = (` (the bind-devices one) precisely to FIX a paste-safety bug,
    and the old `src.index("manual_cmd = (")` guard pinned only the first — so
    the block the test existed for was the one block it never looked at."""

    @staticmethod
    def _assignment_blocks(src, name):
        """Every ``<name> = (`` ... ``)`` parenthesised assignment, in order."""
        blocks = []
        for m in re.finditer(rf"^[ \t]*{re.escape(name)} = \(\n", src, re.M):
            rest = src[m.end():]
            blocks.append(rest[: rest.index(")\n")])
        return blocks

    def test_ps_cmd_never_reaches_fallback_dialog(self):
        src = _source()
        self.assertNotIn('f"powershell.exe {ps_cmd}"', src)

    def test_finalize_fallback_ps_args_is_dollar_free(self):
        # The finalize fallback DOES hand `f"powershell.exe {ps_args}"` to the
        # dialog (two sites), so the ps_cmd absence assertion above says nothing
        # about the command the student actually gets there. ps_args must
        # therefore be $-free by construction, exactly like manual_cmd.
        src = _source()
        self.assertIn(
            'f"powershell.exe {ps_args}"', src,
            "the finalize fallback no longer passes ps_args — re-point this "
            "guard at whatever it hands the dialog now")
        blocks = self._assignment_blocks(src, "ps_args")
        self.assertGreaterEqual(len(blocks), 2, "expected the finalize + "
                                                "bind-devices ps_args blocks")
        for block in blocks:
            self.assertNotIn(
                "$", block,
                "a $ in ps_args is interpolated by the interactive PowerShell "
                f"the student pastes into, before the child parses it:\n{block}")
        for line in src.splitlines():
            if line.strip().startswith("ps_args +="):
                self.assertNotIn("$", line, f"appended $ into ps_args: {line}")

    def test_every_manual_cmd_has_no_dollar(self):
        src = _source()
        blocks = self._assignment_blocks(src, "manual_cmd")
        self.assertGreaterEqual(len(blocks), 2, "expected the usbipd-repair + "
                                                "bind-devices manual_cmd blocks")
        for block in blocks:
            self.assertNotIn(
                "$", block.replace("_ps_single_quote", ""),
                f"manual_cmd must be paste-safe (no $):\n{block}")

    def test_preflight_decodes_utf8_with_replace(self):
        # PS 5.1 writes redirected stdout in the OEM codepage; the GUI forces
        # UTF-8 via [Console]::OutputEncoding and must decode tolerantly so a
        # stray byte can never blank the whole diagnosis (0x81 = 'ü' in cp850
        # is undefined in cp1252 and raised under the old text=True decode).
        src = _source()
        self.assertIn("[Console]::OutputEncoding", src)
        self.assertIn('encoding="utf-8"', src)
        self.assertIn('errors="replace"', src)


if __name__ == "__main__":
    unittest.main()
