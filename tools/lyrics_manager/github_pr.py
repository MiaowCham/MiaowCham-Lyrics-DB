# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

"""GitHub pull-request integration driven by the official ``gh`` CLI.

The manager itself stays dependency-free: every GitHub operation is delegated
to ``gh`` (https://cli.github.com), which handles authentication, API access
and device-flow login.  When ``gh`` is missing or not logged in, the wrapper
raises :class:`GitHubError` with an actionable Chinese message so the UI can
offer a one-click install / login path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git_service import GitService


class GitHubError(RuntimeError):
    """Raised when the GitHub integration cannot complete an operation."""


@dataclass(frozen=True)
class PullRequest:
    """A flattened view of one pull request, safe to render in the UI."""

    number: int
    title: str
    state: str
    author: str
    base: str
    head: str
    url: str
    created_at: str
    is_draft: bool = False


@dataclass(frozen=True)
class RepositoryInfo:
    """Resolved ``owner/repo`` plus the login of the authenticated user."""

    owner: str
    repo: str
    default_branch: str
    login: str


_PR_LIST_FIELDS = "number,title,state,author,baseRefName,headRefName,url,createdAt,isDraft"


def _gh_available() -> str | None:
    """Return the ``gh`` executable path, or ``None`` when it is missing."""
    return shutil.which("gh")


def _parse_pr_item(item: Any) -> PullRequest | None:
    if not isinstance(item, dict):
        return None
    author = item.get("author") or {}
    author_login = author.get("login") if isinstance(author, dict) else ""
    number = item.get("number")
    if not isinstance(number, int):
        return None
    return PullRequest(
        number=number,
        title=str(item.get("title", "")),
        state=str(item.get("state", "")),
        author=str(author_login or ""),
        base=str(item.get("baseRefName", "")),
        head=str(item.get("headRefName", "")),
        url=str(item.get("url", "")),
        created_at=str(item.get("createdAt", "")),
        is_draft=bool(item.get("isDraft", False)),
    )


class GitHubPRService:
    """Thin, deliberately constrained wrapper around the ``gh`` CLI.

    Every command runs without a shell, in the repository working directory,
    with hard timeouts, UTF-8 output and (on Windows) a hidden console window.
    Commands are serialized through one lock, mirroring :class:`GitService`,
    to avoid the same 0xc0000142-style DLL races under Windows.
    """

    NETWORK_TIMEOUT = 120.0
    LOGIN_TIMEOUT = 600.0

    def __init__(self, repository: str | Path, gh_executable: str | None = None) -> None:
        self.repository = Path(repository).resolve()
        if not self.repository.is_dir():
            raise ValueError(f"仓库目录不存在：{self.repository}")
        self.gh_executable = gh_executable or _gh_available()
        self._process_lock = threading.Lock()

    # -- infrastructure ---------------------------------------------------

    def is_available(self) -> bool:
        return self.gh_executable is not None

    def ensure_available(self) -> None:
        if self.gh_executable is None:
            raise GitHubError(
                "未检测到 GitHub CLI（gh）。请先安装：https://cli.github.com，"
                "然后在本应用中点击“登录 GitHub”。"
            )

    def _run(
        self,
        *args: str,
        check: bool = True,
        timeout: float | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a ``gh`` command and wrap failures into :class:`GitHubError`."""
        self.ensure_available()
        command_timeout = self.NETWORK_TIMEOUT if timeout is None else timeout
        run_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            run_kwargs["startupinfo"] = startup
        command = [self.gh_executable, *args]
        try:
            with self._process_lock:
                proc = subprocess.run(
                    command,
                    cwd=self.repository,
                    shell=False,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    input=input_text,
                    capture_output=True,
                    check=False,
                    timeout=command_timeout,
                    **run_kwargs,
                )
        except subprocess.TimeoutExpired as exc:
            raise GitHubError(f"gh 操作超时（{command_timeout:g} 秒）：{' '.join(args)}") from exc
        except OSError as exc:
            raise GitHubError(f"无法启动 gh：{' '.join(args)}（{exc}）") from exc
        if check and proc.returncode:
            detail = (proc.stderr or proc.stdout or "未知错误").strip()
            lowered = detail.lower()
            if any(sig in lowered for sig in (
                "gh auth login", "not logged in", "authentication token",
                "gh_token", "unauthenticated", "401",
            )):
                detail = (
                    "GitHub CLI 未登录或认证失败。\n\n"
                    "请在本仓库目录打开终端运行：\n"
                    "    gh auth login\n"
                    "并选择 GitHub.com、HTTPS、浏览器授权。\n"
                    "若已登录但仍出现此提示，请在「设置」页确认 gh 可执行文件路径，"
                    "或在「拉取请求」页点击「诊断 GitHub」查看真实状态。\n\n"
                    f"原始信息：{detail}"
                )
            raise GitHubError(detail)
        return proc

    def _json(self, *args: str, timeout: float | None = None) -> Any:
        """Run a ``gh`` command whose stdout is expected to be structured data."""
        proc = self._run(*args, timeout=timeout)
        text = proc.stdout.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    def output(self, *args: str, timeout: float | None = None) -> str:
        return (self._run(*args, timeout=timeout).stdout or "").strip()

    def diagnose(self) -> str:
        """Return a one-screen diagnostic dump for the GitHub integration.

        Every probe is run with ``check=False`` so the report never raises; if
        gh is missing the report says so.  This is what the UI shows on a
        "诊断 GitHub" action so the real cause of an auth failure is visible.
        """
        lines: list[str] = []
        lines.append(f"gh 路径：{self.gh_executable or '（未在 PATH 找到）'}")
        lines.append(f"gh 可用：{'是' if self.is_available() else '否'}")
        if not self.is_available():
            lines.append("提示：请安装 GitHub CLI（https://cli.github.com）后重试。")
            return "\n".join(lines)
        version = self._run("--version", check=False)
        lines.append("gh 版本：" + (version.stdout.strip() or version.stderr.strip() or "（未知）"))
        auth = self._run("auth", "status", check=False)
        lines.append("gh 登录状态：" + ("已登录" if auth.returncode == 0 else "未登录"))
        lines.append("--- gh auth status ---")
        auth_text = (auth.stdout + auth.stderr).strip() or "（无输出）"
        lines.extend(auth_text.splitlines())
        repo = self._run("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner", check=False)
        repo_text = (repo.stdout.strip() or repo.stderr.strip() or "（无法确定）")
        lines.append(f"远端仓库：{repo_text}")
        lines.append(f"GH_TOKEN 环境变量：{'已设置' if os.environ.get('GH_TOKEN') else '未设置'}")
        lines.append(f"GH_CONFIG_DIR 环境变量：{os.environ.get('GH_CONFIG_DIR') or '未设置'}")
        return "\n".join(lines)

    # -- authentication ---------------------------------------------------

    def auth_status(self) -> dict[str, Any]:
        """Return whether ``gh`` holds a GitHub.com authentication."""
        proc = self._run("auth", "status", check=False)
        data: dict[str, Any] = {
            "authenticated": proc.returncode == 0,
            "output": (proc.stdout or proc.stderr or "").strip(),
        }
        if proc.returncode == 0:
            login = self.logged_in_user()
            if login:
                data["login"] = login
        return data

    def logged_in_user(self) -> str:
        return self.output("api", "user", "--jq", ".login")

    def login(self) -> dict[str, Any]:
        """Begin an interactive GitHub device-flow login in the browser.

        This blocks until the user finishes the flow, so it must run off the
        Tk thread (see the UI's background-task helper).
        """
        proc = self._run(
            "auth", "login",
            "--hostname", "github.com",
            "--git-protocol", "https",
            "--web",
            check=False,
            timeout=self.LOGIN_TIMEOUT,
        )
        data: dict[str, Any] = {
            "success": proc.returncode == 0,
            "output": (proc.stdout or proc.stderr or "").strip(),
        }
        if proc.returncode == 0:
            login = self.logged_in_user()
            if login:
                data["login"] = login
        return data

    def logout(self) -> str:
        return self.output("auth", "logout", "--hostname", "github.com")

    # -- repository info --------------------------------------------------

    def repository_info(self) -> RepositoryInfo:
        """Resolve owner/repo, default branch and the acting user login."""
        name_with_owner = self.output(
            "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner",
        )
        if not name_with_owner or "/" not in name_with_owner:
            raise GitHubError(f"无法确定仓库归属：{name_with_owner or '（空）'}")
        owner, repo = name_with_owner.split("/", 1)
        default_branch = self.output(
            "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name",
        )
        login = self.logged_in_user()
        if not default_branch:
            raise GitHubError("无法确定仓库默认分支")
        return RepositoryInfo(owner=owner, repo=repo, default_branch=default_branch, login=login)

    def current_branch(self) -> str:
        return GitService(self.repository).current_branch()

    def is_owner(self) -> bool:
        """Return whether the authenticated user may administer this repo.

        A repository owner holds the ``admin`` role.  ``maintain``/``write``
        collaborators can also merge, but "immediate merge" is gated by the
        stricter concept of ownership requested by the user.
        """
        try:
            login = self.logged_in_user()
            if not login:
                return False
            permission = self._json(
                "api",
                f"repos/{self.repository_info().owner}/{self.repository_info().repo}"
                f"/collaborators/{login}/permission",
            )
            role = permission.get("permission", "") if isinstance(permission, dict) else ""
            return role == "admin"
        except GitHubError:
            return False

    # -- pull requests ----------------------------------------------------

    def list_prs(self, state: str = "open", limit: int = 50) -> list[PullRequest]:
        payload = self._json(
            "pr", "list",
            "--state", state,
            "--limit", str(limit),
            "--json", _PR_LIST_FIELDS,
        )
        if not isinstance(payload, list):
            return []
        return [pr for pr in (_parse_pr_item(item) for item in payload) if pr is not None]

    def create(
        self,
        title: str,
        body: str = "",
        base: str | None = None,
        draft: bool = False,
        *, push: bool = False,
    ) -> dict[str, Any]:
        """Open a pull request, optionally pushing the current branch first.

        With ``push=True``, ``gh`` pushes the current branch to its remote
        tracking branch (or origin with the same name) before creating the PR —
        this is the "commit to a branch and open a PR now" flow.
        """
        title = title.strip()
        if not title:
            raise GitHubError("PR 标题不能为空")
        if "\x00" in title or "\x00" in body:
            raise GitHubError("PR 内容包含无效字符")
        args: list[str] = ["pr", "create", "--title", title]
        if body.strip():
            args.extend(["--body", body.strip()])
        else:
            args.append("--fill")
        if base:
            args.extend(["--base", base])
        if draft:
            args.append("--draft")
        if push:
            args.append("--push")
        proc = self._run(*args, check=False)
        output = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode:
            raise GitHubError(output or "创建 PR 失败")
        url = ""
        for line in output.splitlines():
            candidate = line.strip()
            if candidate.startswith("http"):
                url = candidate
                break
        number = 0
        match = re.search(r"/pull/(\d+)", url)
        if match:
            number = int(match.group(1))
        if not url:
            url = self._run("pr", "view", "--json", "url", "--jq", ".url").stdout.strip()
        return {"url": url, "number": number, "output": output}

    def view(self, number: int) -> dict[str, Any]:
        payload = self._json(
            "pr", "view", str(number),
            "--json",
            "number,title,state,body,url,baseRefName,headRefName,author,isDraft,"
            "mergeable,mergeStateStatus,reviewDecision",
        )
        if isinstance(payload, dict):
            author = payload.get("author") or {}
            payload["authorLogin"] = author.get("login") if isinstance(author, dict) else ""
        return payload if isinstance(payload, dict) else {}

    def diff(self, number: int) -> str:
        return self.output("pr", "diff", str(number))

    def status(self, number: int) -> str:
        return self.output("pr", "status", str(number))

    def checks(self, number: int) -> str:
        return self.output("pr", "checks", str(number))

    def reviews(self, number: int) -> str:
        """Collect reviews and comments into one readable block."""
        payload = self._json("pr", "view", str(number), "--json", "reviews,comments")
        if not isinstance(payload, dict):
            return "（无法读取审阅信息）"
        parts: list[str] = []
        for review in payload.get("reviews") or []:
            if isinstance(review, dict):
                author = review.get("author") if isinstance(review.get("author"), dict) else {}
                parts.append(f"{author.get('login', '')}: {review.get('state', '')}")
        if not parts:
            parts.append("（暂无审阅）")
        return "\n".join(parts)

    def mergeable(self, number: int) -> bool:
        info = self.view(number)
        return bool(info.get("mergeable"))

    def merge(self, number: int, method: str = "squash") -> dict[str, Any]:
        if method not in {"merge", "squash", "rebase"}:
            raise GitHubError(f"不支持的合并方式：{method}")
        proc = self._run(
            "pr", "merge", str(number), f"--{method}", "--delete-branch",
            check=False,
        )
        output = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode:
            raise GitHubError(output or "合并 PR 失败")
        return {"merged": True, "output": output}


__all__ = [
    "GitHubError",
    "PullRequest",
    "RepositoryInfo",
    "GitHubPRService",
]
