"""Tests for how `_save_json` writes a credential file.

Real files in a temporary directory of their own, holding fictional tokens.
"""

import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from monzo_mcp import auth


@unittest.skipIf(sys.platform == "win32", "POSIX mode bits; Windows uses ACLs")
class TestCredentialFilesAreOwnerOnly(unittest.TestCase):
    """A token file must never be readable by other local users.

    mkstemp creates the file 0600. A plain open follows the umask, which by
    default gives 0644, for a file holding a refresh token.

    POSIX only: on Windows the mode mkstemp asks for is ignored and access is
    governed by inherited ACLs, so the assertions below would fail there for a
    reason that says nothing about this code.
    """

    def test_a_new_token_file_is_created_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "monzo_tokens.json"
            auth._save_json(target, {"refresh_token": "fictional"})
            self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600")

    def test_rewriting_does_not_loosen_an_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "monzo_tokens.json"
            auth._save_json(target, {"refresh_token": "first"})
            auth._save_json(target, {"refresh_token": "second"})
            self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600")
            self.assertEqual(json.loads(target.read_text())["refresh_token"], "second")


class TestTheTokenFileIsReplacedInOneStep(unittest.TestCase):
    """A reader sees the old file or the new one, never part of either.

    A truncate-and-write save shows every reader an empty or partial file for
    the length of the write, and a failure part-way leaves it that way, after
    Monzo has already rotated the refresh token it held.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        # Resolved: the save follows symlinks, and on macOS the temp directory
        # sits behind one.
        self.dir = pathlib.Path(os.path.realpath(self._dir.name))
        self.path = self.dir / "monzo_tokens.json"

    def tearDown(self):
        self._dir.cleanup()

    @staticmethod
    def _writes_that(action):
        """An `os.fdopen` whose handle calls `action(fd)` before each write."""
        real_fdopen = os.fdopen

        class _Handle:
            def __init__(self, handle):
                self._handle = handle

            def __enter__(self):
                self._handle.__enter__()
                return self

            def __exit__(self, *exc):
                return self._handle.__exit__(*exc)

            def write(self, text):
                action(self._handle.fileno())
                return self._handle.write(text)

            def __getattr__(self, name):
                return getattr(self._handle, name)

        return patch.object(
            auth.os, "fdopen", lambda fd, *a, **k: _Handle(real_fdopen(fd, *a, **k))
        )

    def test_the_target_holds_the_whole_old_file_until_the_new_one_replaces_it(self):
        self.path.write_text('{"refresh_token": "old"}')
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append((self.path.read_text(), json.loads(pathlib.Path(src).read_text())))
            return real_replace(src, dst)

        with patch.object(auth.os, "replace", spy):
            auth._save_json(self.path, {"refresh_token": "new"})

        self.assertEqual(seen, [('{"refresh_token": "old"}', {"refresh_token": "new"})])
        self.assertEqual(json.loads(self.path.read_text()), {"refresh_token": "new"})

    def test_a_failed_write_leaves_the_old_file_and_nothing_else(self):
        """The temp file holds the new refresh token, so one left behind is a
        live credential lying in the config directory."""
        for failure in (OSError(28, "No space left on device"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                self.path.write_text('{"refresh_token": "old"}')

                def fail(fd, failure=failure):
                    raise failure

                with self._writes_that(fail), self.assertRaises(type(failure)):
                    auth._save_json(self.path, {"refresh_token": "new"})

                self.assertEqual(self.path.read_text(), '{"refresh_token": "old"}')
                self.assertEqual(list(self.dir.iterdir()), [self.path])

    def test_a_failed_replace_leaves_the_old_file_and_nothing_else(self):
        self.path.write_text('{"refresh_token": "old"}')

        def refuse(src, dst):
            raise OSError(13, "Permission denied")

        with patch.object(auth.os, "replace", refuse), self.assertRaises(OSError):
            auth._save_json(self.path, {"refresh_token": "new"})

        self.assertEqual(self.path.read_text(), '{"refresh_token": "old"}')
        self.assertEqual(list(self.dir.iterdir()), [self.path])

    @unittest.skipIf(sys.platform == "win32", "POSIX mode bits; Windows uses ACLs")
    def test_the_new_file_is_owner_only_before_the_token_is_written(self):
        """Checked at the write: a chmod at any point after it gives the same
        final mode while leaving the token readable in between."""
        seen = []
        with self._writes_that(lambda fd: seen.append(oct(os.fstat(fd).st_mode & 0o777))):
            auth._save_json(self.path, {"refresh_token": "fictional"})

        self.assertEqual(seen, ["0o600"])

    @unittest.skipIf(sys.platform == "win32", "POSIX mode bits; Windows uses ACLs")
    def test_a_loose_existing_file_is_replaced_by_an_owner_only_one(self):
        """Files written by a release that set no mode can be world-readable."""
        self.path.write_text("{}")
        os.chmod(self.path, 0o644)
        auth._save_json(self.path, {"refresh_token": "fictional"})
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_each_save_publishes_its_own_temp_file_from_the_same_directory(self):
        """A fixed temp name lets one writer publish another's half-written
        file, and a rename from another directory is not atomic."""
        published = []
        real_replace = os.replace

        def spy(src, dst):
            published.append(pathlib.Path(src))
            return real_replace(src, dst)

        with patch.object(auth.os, "replace", spy):
            auth._save_json(self.path, {"refresh_token": "a"})
            auth._save_json(self.path, {"refresh_token": "b"})

        self.assertEqual(len(set(published)), 2)
        self.assertTrue(all(p.parent == self.dir and p != self.path for p in published))

    @unittest.skipIf(sys.platform == "win32", "symlinks need privileges on Windows")
    def test_a_symlinked_token_file_is_saved_where_the_link_points(self):
        """Replacing the link would leave its target, which anything else using
        the real path reads, on a refresh token already spent."""
        real = self.dir / "secrets" / "monzo_tokens.json"
        real.parent.mkdir()
        real.write_text('{"refresh_token": "old"}')
        self.path.symlink_to(real)
        published = []
        real_replace = os.replace

        def spy(src, dst):
            published.append(pathlib.Path(src).parent)
            return real_replace(src, dst)

        with patch.object(auth.os, "replace", spy):
            auth._save_json(self.path, {"refresh_token": "new"})

        self.assertTrue(self.path.is_symlink())
        self.assertEqual(json.loads(real.read_text()), {"refresh_token": "new"})
        # Beside the real file, since a rename cannot cross filesystems.
        self.assertEqual(published, [real.parent])

    def test_a_failed_cleanup_does_not_hide_why_the_save_failed(self):
        """A scanner can block the unlink as well as the replace on Windows."""

        def refuse_replace(src, dst):
            raise OSError(28, "No space left on device")

        def refuse_unlink(path):
            raise PermissionError(13, "Access is denied")

        with (
            patch.object(auth.os, "replace", refuse_replace),
            patch.object(auth.os, "unlink", refuse_unlink),
            self.assertRaises(OSError) as raised,
        ):
            auth._save_json(self.path, {"refresh_token": "new"})
        self.assertEqual(raised.exception.errno, 28)

    def test_the_new_file_is_synced_before_it_replaces_the_old_one(self):
        """Unsynced, a crash soon after the rename can leave an empty file on
        some filesystems, after the refresh token it replaced was spent."""
        data = {"refresh_token": "new"}
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            # The whole file has to be in it by now, or the sync covers less.
            events.append(("fsync", os.fstat(fd).st_size))
            return real_fsync(fd)

        def replace(src, dst):
            events.append(("replace", None))
            return real_replace(src, dst)

        with patch.object(auth.os, "fsync", fsync), patch.object(auth.os, "replace", replace):
            auth._save_json(self.path, data)

        # Against the saved file rather than the JSON: Windows writes `\r\n`.
        self.assertEqual(events, [("fsync", self.path.stat().st_size), ("replace", None)])
        self.assertGreater(self.path.stat().st_size, 0)

    @unittest.skipUnless(sys.platform == "win32", "the sharing rule being retried is Windows'")
    def test_a_save_waits_out_a_reader_holding_the_file_open(self):
        """The real refusal, not a stand-in for it: a reader holds the file
        while the save runs, and lets go within the retries."""
        refused = []
        real_replace = os.replace

        def watch(src, dst):
            try:
                return real_replace(src, dst)
            except PermissionError:
                refused.append(src)
                raise

        self.path.write_text('{"refresh_token": "old"}')
        reader = open(self.path)
        timer = threading.Timer(0.1, reader.close)
        timer.start()
        try:
            with patch.object(auth.os, "replace", watch):
                auth._save_json(self.path, {"refresh_token": "new"})
        finally:
            timer.join()
            reader.close()
        self.assertEqual(json.loads(self.path.read_text()), {"refresh_token": "new"})
        # Without a refusal this proves nothing about the retry.
        self.assertTrue(refused, "Windows replaced a file another process had open")

    def test_windows_retries_a_replace_another_reader_is_blocking(self):
        """Windows refuses to replace a file another process has open, and a
        server sharing the file opens it on every request; `auth` cannot redo
        the save once the authorisation code is spent."""
        attempts = []
        real_replace = os.replace

        def busy_twice(src, dst):
            attempts.append(src)
            if len(attempts) <= 2:
                raise PermissionError(13, "Access is denied")
            return real_replace(src, dst)

        with (
            patch.object(auth.sys, "platform", "win32"),
            patch.object(auth.os, "replace", busy_twice),
            patch.object(auth.time, "sleep") as sleep,
        ):
            auth._save_json(self.path, {"refresh_token": "new"})

        self.assertEqual(len(attempts), 3)
        self.assertEqual(json.loads(self.path.read_text()), {"refresh_token": "new"})
        # Back-to-back retries would be over before a reader let go.
        delays = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(len(delays), 2)
        self.assertTrue(0 < delays[0] < delays[1], delays)

    def test_a_refused_replace_is_not_retried_elsewhere_or_forever(self):
        """Off Windows the refusal is a real permission fault, and on Windows a
        file that stays locked has to fail rather than hang the server."""
        for platform, expected in (("linux", 1), ("win32", auth._REPLACE_ATTEMPTS)):
            with self.subTest(platform=platform):
                attempts = []

                def refuse(src, dst, attempts=attempts):
                    attempts.append(src)
                    raise PermissionError(13, "Access is denied")

                with (
                    patch.object(auth.sys, "platform", platform),
                    patch.object(auth.os, "replace", refuse),
                    patch.object(auth.time, "sleep"),
                    self.assertRaises(PermissionError),
                ):
                    auth._save_json(self.path, {"refresh_token": "new"})
                self.assertEqual(len(attempts), expected)
                self.assertEqual(list(self.dir.iterdir()), [])
