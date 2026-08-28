# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

"""Small, deliberately constrained Git subprocess wrapper for the desktop UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
import threading
from typing import Iterable


class GitError(RuntimeError):
    """Raised when Git rejects an operation."""


@dataclass(frozen=True)
class GitResult:
    args: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class GitFileStatus:
    """One path from Git's porcelain status output."""

    path: str
    index_status: str
    worktree_status: str
    original_path: str | None = None

    @property
    def staged(self) -> bool:
        return self.index_status not in {" ", "?"}

    @property
    def unstaged(self) -> bool:
        return self.worktree_status != " " or self.index_status == "?"


@dataclass(frozen=True)
class GitLogEntry:
    commit: str
    short_commit: str
    date: str
    author: str
    subject: str


class GitService:
    """Expose only the Git operations needed by the manager.

    Arguments are always passed as a list and never through a shell. File paths are
    checked to ensure they resolve inside the repository before reaching Git.
    """

    def __init__(self, repository: str | Path, git_executable: str = "git") -> None:
        self.repository = Path(repository).resolve()
        if not self.repository.is_dir():
            raise ValueError(f"仓库目录不存在：{self.repository}")
        self.git_executable = git_executable
        self._process_lock = threading.Lock()
        top = self._run_raw("rev-parse", "--show-toplevel").stdout.strip()
        if not top:
            raise ValueError(f"不是 Git 仓库：{self.repository}")
        self.repository = Path(top).resolve()

    LOCAL_TIMEOUT = 30.0
    NETWORK_TIMEOUT = 300.0

    def _run_raw(
        self, *args: str, check: bool = True, timeout: float | None = None,
    ) -> GitResult:
        command_timeout = self.LOCAL_TIMEOUT if timeout is None else timeout
        run_kwargs = {}
        if os.name == "nt":
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            run_kwargs["startupinfo"] = startup
        try:
            # Windows Git loads a sizeable DLL set. Serializing process starts
            # avoids intermittent 0xc0000142 initialization failures when the
            # UI receives several refresh/diff events close together.
            with self._process_lock:
                proc = subprocess.run(
                    [self.git_executable, *args],
                    cwd=self.repository,
                    shell=False,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=False,
                    timeout=command_timeout,
                    **run_kwargs,
                )
        except subprocess.TimeoutExpired as exc:
            operation = "git " + " ".join(args)
            raise GitError(f"Git 操作超时（{command_timeout:g} 秒）：{operation}") from exc
        except OSError as exc:
            operation = "git " + " ".join(args)
            raise GitError(f"无法启动 Git：{operation}（{exc}）") from exc
        result = GitResult(tuple(args), proc.stdout, proc.stderr, proc.returncode)
        if check and proc.returncode:
            detail = proc.stderr.strip() or proc.stdout.strip() or "未知 Git 错误"
            raise GitError(detail)
        return result

    def _paths(self, paths: Iterable[str | Path]) -> list[str]:
        safe: list[str] = []
        for value in paths:
            raw = Path(value)
            candidate = raw if raw.is_absolute() else self.repository / raw
            resolved = candidate.resolve(strict=False)
            try:
                relative = resolved.relative_to(self.repository)
            except ValueError as exc:
                raise ValueError(f"路径位于仓库之外：{value}") from exc
            if relative == Path("."):
                raise ValueError("必须选择具体文件，不能操作整个仓库")
            safe.append(relative.as_posix())
        if not safe:
            raise ValueError("请至少选择一个文件")
        return safe

    def status(self) -> str:
        return self._run_raw("status", "--short", "--branch").stdout

    def refresh_index(self) -> str:
        """Refresh the index stat cache so Git reports accurate state.

        The manager rewrites metadata atomically, which touches file mtimes.
        Under ``core.autocrlf=true`` Git may then report a file as modified
        (with an empty diff) purely on a stale stat cache until the index is
        refreshed.  ``update-index --refresh`` re-validates content and clears
        those false positives without staging anything.
        """
        return self._run_raw("update-index", "--refresh", check=False).stdout

    def is_tracked(self, path: str | Path) -> bool:
        """Return whether Git currently tracks a concrete repository file."""
        return self._run_raw("ls-files", "--error-unmatch", "--", *self._paths([path]), check=False).returncode == 0

    def move(self, source: str | Path, destination: str | Path) -> str:
        """Move a tracked file and stage it explicitly as a Git rename."""
        source_path, destination_path = self._paths([source, destination])
        return self._run_raw("mv", "--", source_path, destination_path).stdout

    def status_entries(self) -> list[GitFileStatus]:
        output = self._run_raw("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        records = output.split("\0")
        entries: list[GitFileStatus] = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if len(record) < 4 or record[2] != " ":
                raise GitError("无法解析 Git 状态输出")
            x, y, path = record[0], record[1], record[3:]
            original = None
            if x in {"R", "C"} or y in {"R", "C"}:
                if index >= len(records) or not records[index]:
                    raise GitError("无法解析 Git 重命名状态")
                original = records[index]
                index += 1
            entries.append(GitFileStatus(path, x, y, original))
        return entries

    def repository_name(self) -> str:
        return self.repository.name

    def current_branch(self) -> str:
        branch = self._run_raw("branch", "--show-current").stdout.strip()
        if branch:
            return branch
        return self._run_raw("rev-parse", "--short", "HEAD").stdout.strip() + "（分离 HEAD）"

    def default_branch(self) -> str:
        """Return the remote's default branch name (e.g. ``main``)."""
        result = self._run_raw(
            "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD",
            check=False,
        )
        name = result.stdout.strip()
        if name.startswith("origin/"):
            name = name[len("origin/"):]
        return name or "main"

    def diff(self, paths: Iterable[str | Path] = (), *, staged: bool = False) -> str:
        args = ["diff"]
        if staged:
            args.append("--cached")
        supplied = list(paths)
        if supplied:
            args.extend(["--", *self._paths(supplied)])
        return self._run_raw(*args).stdout

    def log(self, limit: int = 30) -> str:
        if not 1 <= limit <= 200:
            raise ValueError("日志条数必须在 1 到 200 之间")
        return self._run_raw("log", f"-{limit}", "--date=short", "--pretty=format:%h %ad %an %s").stdout

    def log_entries(self, limit: int = 100) -> list[GitLogEntry]:
        if not 1 <= limit <= 200:
            raise ValueError("日志条数必须在 1 到 200 之间")
        separator = "%x1f"
        output = self._run_raw(
            "log", f"-{limit}", "--date=short",
            f"--pretty=format:%H{separator}%h{separator}%ad{separator}%an{separator}%s",
        ).stdout
        result: list[GitLogEntry] = []
        for line in output.splitlines():
            parts = line.split("\x1f", 4)
            if len(parts) == 5:
                result.append(GitLogEntry(*parts))
        return result

    def stage(self, paths: Iterable[str | Path]) -> str:
        return self._run_raw("add", "--", *self._paths(paths)).stdout

    def unstage(self, paths: Iterable[str | Path]) -> str:
        safe = self._paths(paths)
        # restore --staged works with unborn repositories only after the first commit;
        # the manager targets an established repository.
        return self._run_raw("restore", "--staged", "--", *safe).stdout

    def stage_all(self) -> str:
        """Stage every change (tracked edits, deletions and new files)."""
        return self._run_raw("add", "-A").stdout

    def switch(self, branch: str) -> str:
        return self._run_raw("switch", branch).stdout

    def switch_create(self, branch: str) -> str:
        """Create ``branch`` from HEAD and switch to it, carrying changes along."""
        return self._run_raw("switch", "-c", branch).stdout

    def commit(self, message: str, description: str = "") -> str:
        message = message.strip()
        if not message:
            raise ValueError("提交信息不能为空")
        if "\x00" in message:
            raise ValueError("提交信息包含无效字符")
        if "\x00" in description:
            raise ValueError("提交描述包含无效字符")
        args = ["commit", "-m", message]
        if description.strip():
            args.extend(["-m", description.strip()])
        return self._run_raw(*args).stdout

    def fetch(self) -> str:
        result = self._run_raw("fetch", timeout=self.NETWORK_TIMEOUT)
        return result.stdout + result.stderr

    def pull_ff_only(self) -> str:
        result = self._run_raw("pull", "--ff-only", timeout=self.NETWORK_TIMEOUT)
        return result.stdout + result.stderr

    def push(self) -> str:
        result = self._run_raw("push", timeout=self.NETWORK_TIMEOUT)
        return result.stdout + result.stderr

    def push_upstream(self, branch: str) -> str:
        """Push ``branch`` to origin and set it as the upstream.

        ``git push -u`` works whether or not an upstream already exists, so a
        first-time push to a brand-new branch is handled the same as a normal
        push.  ``gh pr create --push`` is not available in every gh release, so
        the manager pushes explicitly before creating a pull request.
        """
        result = self._run_raw("push", "--set-upstream", "origin", branch, timeout=self.NETWORK_TIMEOUT)
        return result.stdout + result.stderr
