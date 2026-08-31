# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0. See http://www.apache.org/licenses/LICENSE-2.0

from pathlib import Path
import json
import subprocess
import unittest
from unittest.mock import patch

from tools.lyrics_manager.github_pr import GitHubError, GitHubPRService, PullRequest, RepositoryInfo


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


class GitHubPRServiceTests(unittest.TestCase):
    """Command-construction and output-parsing tests.

    No real ``gh`` binary or network is used; ``subprocess.run`` is patched so
    the tests are hermetic and independent of the sandbox's filesystem policy.
    """

    def setUp(self) -> None:
        # The service only requires the directory to exist; it does not touch
        # Git itself (current_branch constructs GitService lazily and is not
        # exercised by these tests).
        self.service = GitHubPRService(Path(__file__).resolve().parent, gh_executable="gh")
        self.service._process_lock = _NullLock()

    # -- availability -----------------------------------------------------

    def test_missing_gh_raises_actionable_error(self) -> None:
        with patch("tools.lyrics_manager.github_pr._gh_available", return_value=None):
            service = GitHubPRService(Path("."), gh_executable=None)
        self.assertFalse(service.is_available())
        with self.assertRaisesRegex(GitHubError, "GitHub CLI"):
            service.ensure_available()

    def test_gh_available_detected(self) -> None:
        with patch("tools.lyrics_manager.github_pr._gh_available", return_value="/usr/bin/gh"):
            service = GitHubPRService(Path("."))
        self.assertTrue(service.is_available())

    # -- repository info --------------------------------------------------

    def test_repository_info_parses_owner_repo(self) -> None:
        name = "MiaowCham/MiaowCham-Lyrics-DB"
        with patch("tools.lyrics_manager.github_pr.GitHubPRService.output",
                   side_effect=[name, "main", "MiaowCham"]):
            info = self.service.repository_info()
        self.assertEqual(info, RepositoryInfo("MiaowCham", "MiaowCham-Lyrics-DB", "main", "MiaowCham"))

    def test_repository_info_rejects_missing_owner(self) -> None:
        with patch("tools.lyrics_manager.github_pr.GitHubPRService.output", return_value=""):
            with self.assertRaisesRegex(GitHubError, "仓库归属"):
                self.service.repository_info()

    # -- command construction --------------------------------------------

    def _capture_run(self, outputs: list[subprocess.CompletedProcess[str]]):
        """Return a side_effect that pops one result per invocation."""
        iterator = iter(outputs)

        def fake_run(*args, **kwargs):
            try:
                return next(iterator)
            except StopIteration:
                return _completed()
        return fake_run

    def test_create_uses_explicit_push_and_parses_url(self) -> None:
        output = "https://github.com/MiaowCham/MiaowCham-Lyrics-DB/pull/42\n"
        fake = self._capture_run([_completed(output)])
        with patch("tools.lyrics_manager.github_pr.GitHubPRService._run", side_effect=fake) as run:
            with patch("tools.lyrics_manager.github_pr.GitService") as git_cls:
                git_instance = git_cls.return_value
                git_instance.current_branch.return_value = "feature-x"
                git_instance.push_upstream.return_value = "pushed ok"
                result = self.service.create("feat: 歌词", body="说明", base="main", push=True)
        self.assertEqual(result["number"], 42)
        self.assertEqual(result["url"], "https://github.com/MiaowCham/MiaowCham-Lyrics-DB/pull/42")
        self.assertEqual(result["push_output"], "pushed ok")
        args = run.call_args.args
        # The branch is pushed explicitly, not via `gh pr create --push`.
        self.assertNotIn("--push", args)
        self.assertIn("--base", args)
        self.assertIn("--title", args)
        git_instance.push_upstream.assert_called_once_with("feature-x")

    def test_create_with_push_rejects_detached_head(self) -> None:
        with patch("tools.lyrics_manager.github_pr.GitService") as git_cls:
            git_instance = git_cls.return_value
            git_instance.current_branch.return_value = "abc1234（分离 HEAD）"
            with self.assertRaisesRegex(GitHubError, "分离 HEAD"):
                self.service.create("title", push=True)

    def test_create_rejects_empty_title(self) -> None:
        with self.assertRaisesRegex(GitHubError, "标题"):
            self.service.create("   ")

    def test_create_without_body_uses_fill(self) -> None:
        fake = self._capture_run([_completed("https://github.com/o/r/pull/1\n")])
        with patch("tools.lyrics_manager.github_pr.GitHubPRService._run", side_effect=fake) as run:
            self.service.create("title")
        self.assertIn("--fill", run.call_args.args)

    def test_create_nonzero_exit_raises(self) -> None:
        with patch("tools.lyrics_manager.github_pr.GitHubPRService._run",
                   side_effect=[_completed("", "permission denied", returncode=1)]):
            with self.assertRaisesRegex(GitHubError, "permission denied"):
                self.service.create("title")

    # -- PR list parsing --------------------------------------------------

    def test_list_prs_parses_fields(self) -> None:
        payload = [
            {
                "number": 7, "title": "feat: x", "state": "OPEN",
                "author": {"login": "alice"}, "baseRefName": "main",
                "headRefName": "topic", "url": "https://u/7",
                "createdAt": "2026-01-01", "isDraft": False,
            },
            "not-a-dict",
            {
                "number": 8, "title": "draft pr", "state": "OPEN",
                "author": None, "baseRefName": "main",
                "headRefName": "wip", "url": "https://u/8",
                "createdAt": "2026-01-02", "isDraft": True,
            },
        ]
        with patch("tools.lyrics_manager.github_pr.GitHubPRService._json", return_value=payload):
            prs = self.service.list_prs()
        self.assertEqual(len(prs), 2)
        self.assertEqual(prs[0].number, 7)
        self.assertEqual(prs[0].author, "alice")
        self.assertFalse(prs[0].is_draft)
        self.assertEqual(prs[1].author, "")
        self.assertTrue(prs[1].is_draft)

    def test_list_prs_handles_non_list(self) -> None:
        with patch("tools.lyrics_manager.github_pr.GitHubPRService._json", return_value={"error": True}):
            self.assertEqual(self.service.list_prs(), [])

    # -- view / merge -----------------------------------------------------

    def test_view_injects_author_login(self) -> None:
        payload = {"number": 3, "title": "t", "author": {"login": "bob"}, "mergeable": True}
        with patch("tools.lyrics_manager.github_pr.GitHubPRService._json", return_value=payload):
            info = self.service.view(3)
        self.assertEqual(info["authorLogin"], "bob")

    def test_merge_validates_method(self) -> None:
        with self.assertRaisesRegex(GitHubError, "合并方式"):
            self.service.merge(1, method="bogus")

    def test_merge_success_returns_truthy(self) -> None:
        with patch("tools.lyrics_manager.github_pr.GitHubPRService._run",
                   return_value=_completed("Merged")):
            result = self.service.merge(5, method="squash")
        self.assertTrue(result["merged"])

    def test_merge_failure_raises(self) -> None:
        with patch("tools.lyrics_manager.github_pr.GitHubPRService._run",
                   return_value=_completed("", "not mergeable", returncode=1)):
            with self.assertRaisesRegex(GitHubError, "not mergeable"):
                self.service.merge(5)

    # -- is_owner ---------------------------------------------------------

    def test_is_owner_true_for_admin(self) -> None:
        payload = {"permission": "admin"}
        with patch("tools.lyrics_manager.github_pr.GitHubPRService.logged_in_user", return_value="boss"), \
             patch("tools.lyrics_manager.github_pr.GitHubPRService.repository_info",
                   return_value=RepositoryInfo("o", "r", "main", "boss")), \
             patch("tools.lyrics_manager.github_pr.GitHubPRService._json", return_value=payload):
            self.assertTrue(self.service.is_owner())

    def test_is_owner_false_for_write(self) -> None:
        payload = {"permission": "write"}
        with patch("tools.lyrics_manager.github_pr.GitHubPRService.logged_in_user", return_value="collab"), \
             patch("tools.lyrics_manager.github_pr.GitHubPRService.repository_info",
                   return_value=RepositoryInfo("o", "r", "main", "collab")), \
             patch("tools.lyrics_manager.github_pr.GitHubPRService._json", return_value=payload):
            self.assertFalse(self.service.is_owner())

    def test_is_owner_false_when_not_logged_in(self) -> None:
        with patch("tools.lyrics_manager.github_pr.GitHubPRService.logged_in_user", return_value=""):
            self.assertFalse(self.service.is_owner())

    # -- error wrapping ---------------------------------------------------

    def test_timeout_wrapped_as_github_error(self) -> None:
        with patch("tools.lyrics_manager.github_pr.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("gh", 1)):
            with self.assertRaisesRegex(GitHubError, "超时"):
                self.service.output("pr", "list")

    def test_oserror_wrapped_as_github_error(self) -> None:
        with patch("tools.lyrics_manager.github_pr.subprocess.run",
                   side_effect=OSError("boom")):
            with self.assertRaisesRegex(GitHubError, "无法启动 gh"):
                self.service.output("pr", "list")

    # -- auth-friendly error and diagnosis --------------------------------

    def test_run_rewraps_auth_error_into_actionable_hint(self) -> None:
        completed = subprocess.CompletedProcess(
            ["gh"], 1, "",
            "To get started with GitHub CLI, please run: gh auth login",
        )
        with patch("tools.lyrics_manager.github_pr.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(GitHubError, "未登录"):
                self.service.output("pr", "list")

    def test_run_does_not_rewrap_unrelated_error(self) -> None:
        completed = subprocess.CompletedProcess(["gh"], 1, "", "permission denied")
        with patch("tools.lyrics_manager.github_pr.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(GitHubError, "permission denied"):
                self.service.output("pr", "list")

    def test_diagnose_reports_missing_gh(self) -> None:
        with patch("tools.lyrics_manager.github_pr._gh_available", return_value=None):
            service = GitHubPRService(Path("."), gh_executable=None)
        text = service.diagnose()
        self.assertIn("未在 PATH 找到", text)
        self.assertIn("https://cli.github.com", text)

    def test_diagnose_includes_auth_and_repo_lines_when_gh_present(self) -> None:
        version = subprocess.CompletedProcess(["gh"], 0, "gh version 2.50.0\n", "")
        auth = subprocess.CompletedProcess(["gh"], 0, "accounts:\n  github.com: logged in\n", "")
        repo = subprocess.CompletedProcess(["gh"], 0, "MiaowCham/MiaowCham-Lyrics-DB\n", "")
        responses = [version, auth, repo]

        def fake_run(*args, check=True, timeout=None, input_text=None):
            return responses.pop(0) if responses else subprocess.CompletedProcess(["gh"], 0, "", "")

        with patch.object(self.service, "_run", side_effect=fake_run):
            text = self.service.diagnose()
        self.assertIn("gh 版本：gh version 2.50.0", text)
        self.assertIn("已登录", text)
        self.assertIn("MiaowCham/MiaowCham-Lyrics-DB", text)


class _NullLock:
    """Stand-in for threading.Lock that never blocks (test-only)."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *args) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
