# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0. See http://www.apache.org/licenses/LICENSE-2.0

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.lyrics_manager.git_service import GitError, GitService


class GitServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "初始提交")
        self.service = GitService(self.repo)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=self.repo, text=True, encoding="utf-8",
            capture_output=True, check=True,
        )

    def test_status_diff_stage_unstage_and_commit(self) -> None:
        (self.repo / "tracked.txt").write_text("two\n", encoding="utf-8")
        self.assertIn("tracked.txt", self.service.status())
        self.assertIn("+two", self.service.diff(["tracked.txt"]))
        self.service.stage(["tracked.txt"])
        self.assertIn("+two", self.service.diff(staged=True))
        self.service.unstage(["tracked.txt"])
        self.assertIn("+two", self.service.diff())
        self.service.stage(["tracked.txt"])
        self.assertIn("提交测试", self.service.commit("提交测试"))
        self.assertIn("提交测试", self.service.log())

    def test_rejects_path_outside_repository(self) -> None:
        outside = self.repo.parent / "outside.txt"
        with self.assertRaises(ValueError):
            self.service.stage([outside])

    def test_rejects_empty_paths_and_commit_message(self) -> None:
        with self.assertRaises(ValueError):
            self.service.stage([])
        with self.assertRaises(ValueError):
            self.service.commit("  ")

    def test_structured_status_distinguishes_staged_and_unstaged(self) -> None:
        (self.repo / "tracked.txt").write_text("two\n", encoding="utf-8")
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        self.service.stage(["new.txt"])
        entries = {entry.path: entry for entry in self.service.status_entries()}
        self.assertTrue(entries["new.txt"].staged)
        self.assertFalse(entries["new.txt"].unstaged)
        self.assertFalse(entries["tracked.txt"].staged)
        self.assertTrue(entries["tracked.txt"].unstaged)

    def test_move_stages_a_tracked_file_as_rename(self) -> None:
        target = self.repo / "moved.txt"
        self.assertTrue(self.service.is_tracked("tracked.txt"))
        self.service.move("tracked.txt", target)
        self.assertFalse((self.repo / "tracked.txt").exists())
        self.assertTrue(target.exists())
        entry = next(item for item in self.service.status_entries() if item.path == "moved.txt")
        self.assertEqual(entry.index_status, "R")
        self.assertEqual(entry.original_path, "tracked.txt")

    def test_commit_description_and_structured_log(self) -> None:
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        self.service.stage(["new.txt"])
        self.service.commit("feat: 新功能", "补充说明")
        latest = self.service.log_entries(1)[0]
        self.assertEqual(latest.subject, "feat: 新功能")
        self.assertEqual(latest.author, "Test User")

    def test_timeout_is_reported_as_git_error(self) -> None:
        with patch("tools.lyrics_manager.git_service.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 1)):
            with self.assertRaisesRegex(GitError, "Git 操作超时"):
                self.service.status()

    def test_run_uses_timeout_and_windows_hidden_window_flag(self) -> None:
        completed = subprocess.CompletedProcess(["git"], 0, "", "")
        with patch("tools.lyrics_manager.git_service.os.name", "nt"), patch(
            "tools.lyrics_manager.git_service.subprocess.run", return_value=completed,
        ) as run:
            self.service.status()
        self.assertEqual(run.call_args.kwargs["timeout"], self.service.LOCAL_TIMEOUT)
        self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
