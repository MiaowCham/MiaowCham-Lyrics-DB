# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

"""Tkinter desktop interface for browsing lyrics metadata and Git history."""

from __future__ import annotations

import json
import copy
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .git_service import GitError, GitService
from .github_pr import GitHubError, GitHubPRService, PullRequest
from .tree_model import RegistryNode, build_registry_tree, filter_registry_tree

try:
    from .core import LyricsDatabase
except ImportError:  # Allows the UI to show a useful error while core is being installed.
    LyricsDatabase = None  # type: ignore[assignment,misc]


APP_NAME = "MiaowCham's Lyrics Manager"
CONFIG_PATH = Path.home() / ".miaowcham-lyrics-manager.json"
FIELDS = (
    ("title", "曲名"), ("artists", "歌手（多个用逗号分隔）"),
    ("album", "专辑"), ("songwriters", "词曲作者（多个用逗号分隔）"),
    ("language", "原文语言"), ("isrc", "ISRC"),
    ("appleMusicId", "Apple Music ID"), ("spotifyId", "Spotify ID"),
    ("ncmMusicId", "网易云 ID"), ("qqMusicId", "QQ 音乐 ID"),
    ("source", "来源"), ("sourceUrl", "来源 URL"),
)


# --- Theme (dark-mode) support -----------------------------------------
#
# Tk/ttk exposes no cross-platform "is the OS in dark mode" query, so we sniff
# each platform's native preference.  Linux desktop environments differ enough
# that we conservatively default to light there unless the user overrides.

THEME_MODES = ("system", "light", "dark")

# ttk "clam" theme's configurable colour roles, filled from dark/light palettes.
_CLAM_COLOURS = (
    "background", "foreground", "fieldbackground", "foreground", "selectbackground",
    "selectforeground", "bordercolor", "lightcolor", "darkcolor", "focuscolor",
    "troughcolor", "insertcolor",
)

_DARK_PALETTE = {
    "background": "#1e1e1e",
    "foreground": "#dcdcdc",
    "fieldbackground": "#252526",
    "selectbackground": "#264f78",
    "selectforeground": "#ffffff",
    "bordercolor": "#3c3c3c",
    "lightcolor": "#2e2e2e",
    "darkcolor": "#151515",
    "focuscolor": "#3c3c3c",
    "troughcolor": "#2a2a2a",
    "insertcolor": "#dcdcdc",
}

_LIGHT_PALETTE = {
    "background": "#f0f0f0",
    "foreground": "#202020",
    "fieldbackground": "#ffffff",
    "selectbackground": "#0078d7",
    "selectforeground": "#ffffff",
    "bordercolor": "#c8c8c8",
    "lightcolor": "#fafafa",
    "darkcolor": "#7a7a7a",
    "focuscolor": "#c8c8c8",
    "troughcolor": "#e0e0e0",
    "insertcolor": "#202020",
}

# Widgets that do not honour the ttk theme and need explicit colours.
_LIGHT_TEXT_BG = "#ffffff"
_LIGHT_TEXT_FG = "#202020"
_DARK_TEXT_BG = "#1e1e1e"
_DARK_TEXT_FG = "#dcdcdc"
_LIGHT_LEGACY_BG = "#fff1a8"
_LIGHT_LEGACY_FG = "#5c4300"
_DARK_LEGACY_BG = "#6b5500"
_DARK_LEGACY_FG = "#ffe9a0"


def _system_prefers_dark() -> bool:
    """Detect the OS-level dark-mode preference without third-party packages."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
        except (OSError, ValueError, ImportError):
            return False
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, shell=False, timeout=3,
            )
            return result.returncode == 0 and "dark" in result.stdout.lower()
        except (OSError, subprocess.TimeoutExpired):
            return False
    return False


def _resolve_theme_mode(mode: str) -> str:
    if mode == "dark":
        return "dark"
    if mode == "light":
        return "light"
    return "dark" if _system_prefers_dark() else "light"


class _Theme:
    """Small helper holding the resolved palette for the running app."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        resolved = _resolve_theme_mode(mode)
        self.dark = resolved == "dark"
        self.palette = _DARK_PALETTE if self.dark else _LIGHT_PALETTE
        self.text_bg = _DARK_TEXT_BG if self.dark else _LIGHT_TEXT_BG
        self.text_fg = _DARK_TEXT_FG if self.dark else _LIGHT_TEXT_FG
        self.legacy_bg = _DARK_LEGACY_BG if self.dark else _LIGHT_LEGACY_BG
        self.legacy_fg = _DARK_LEGACY_FG if self.dark else _LIGHT_LEGACY_FG


def _open_path(path: Path) -> None:
    """Open a selected local path without invoking a command shell."""
    path = path.resolve(strict=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)], shell=False)
    else:
        subprocess.Popen(["xdg-open", str(path)], shell=False)


def _reveal_path(path: Path) -> None:
    """Reveal a file in the platform file manager without showing a console."""
    path = path.resolve(strict=True)
    kwargs: dict[str, Any] = {"shell": False}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(["explorer.exe", "/select,", str(path)], **kwargs)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)], **kwargs)
    else:
        subprocess.Popen(["xdg-open", str(path.parent)], **kwargs)


def link_file_record(metadata: dict[str, Any], file_name: str, target_ref: str) -> dict[str, Any]:
    """Return metadata with one file linked to an existing target track."""
    updated = copy.deepcopy(metadata)
    if target_ref not in updated.get("tracks", {}):
        raise ValueError("目标曲目实体不存在")
    for item in updated.get("files", []):
        if isinstance(item, dict) and item.get("name") == file_name:
            old_ref = str(item.get("metadataRef") or "")
            if old_ref == target_ref:
                raise ValueError("所选文件已经属于该曲目")
            item.update({"metadataRef": target_ref, "linked": True, "linkedFrom": old_ref})
            return updated
    raise ValueError("该文件不在 metadata 的文件列表中")


def link_file_to_external_target(
    source_metadata: dict[str, Any], file_name: str, target_directory_rel: str, target_ref: str,
) -> dict[str, Any]:
    """Link a local lyric file to a track entity stored in another directory.

    The lyric remains in its current folder.  Its local record retains the
    original binding in ``linkedFrom`` while ``linkedTarget`` is the canonical
    metadata location to use for display and synchronisation.
    """
    if not target_directory_rel or not target_ref:
        raise ValueError("关联目标目录和曲目实体不能为空")
    updated = copy.deepcopy(source_metadata)
    for item in updated.get("files", []):
        if isinstance(item, dict) and item.get("name") == file_name:
            old_ref = str(item.get("metadataRef") or "")
            item.update({
                "linked": True,
                "linkedFrom": old_ref,
                "linkedTarget": {"path": target_directory_rel.replace("\\", "/"), "metadataRef": target_ref},
            })
            return updated
    raise ValueError("该文件不在 metadata 的文件列表中")


def move_file_records_to_target(
    source_metadata: dict[str, Any], target_metadata: dict[str, Any], file_names: list[str], target_ref: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move file records between directories after their files were moved on disk."""
    source = copy.deepcopy(source_metadata)
    target = copy.deepcopy(target_metadata)
    if target_ref not in target.get("tracks", {}):
        raise ValueError("目标曲目实体不存在")
    wanted = set(file_names)
    records = [item for item in source.get("files", []) if isinstance(item, dict) and item.get("name") in wanted]
    found = {str(item.get("name")) for item in records}
    missing = wanted - found
    if missing:
        raise ValueError(f"源目录中找不到歌词文件：{', '.join(sorted(missing))}")
    target_names = {str(item.get("name")) for item in target.get("files", []) if isinstance(item, dict)}
    duplicates = found & target_names
    if duplicates:
        raise ValueError(f"目标目录已有同名歌词文件：{', '.join(sorted(duplicates))}")
    source["files"] = [item for item in source.get("files", []) if not (isinstance(item, dict) and item.get("name") in wanted)]
    for item in records:
        old_ref = str(item.get("linkedFrom") or item.get("metadataRef") or "")
        item["metadataRef"] = target_ref
        item["linked"] = True
        item["linkedFrom"] = old_ref
        item.pop("linkedTarget", None)
        target.setdefault("files", []).append(item)
    return source, target


def split_file_record(metadata: dict[str, Any], file_name: str, source_ref: str, new_ref: str) -> dict[str, Any]:
    """Clone a track and rebind one file as an independently editable entity."""
    updated = copy.deepcopy(metadata)
    source = updated.get("tracks", {}).get(source_ref)
    if not isinstance(source, dict):
        raise ValueError("当前曲目实体不存在")
    updated["tracks"][new_ref] = copy.deepcopy(source)
    for item in updated.get("files", []):
        if isinstance(item, dict) and item.get("name") == file_name:
            item["metadataRef"] = new_ref
            item.pop("linked", None)
            item.pop("linkedFrom", None)
            return updated
    updated.setdefault("files", []).append({
        "name": file_name,
        "format": Path(file_name).suffix.lstrip(".").casefold(),
        "metadataRef": new_ref,
    })
    return updated


def rename_file_record(metadata: dict[str, Any], old_name: str, new_name: str) -> dict[str, Any]:
    """Return metadata with filename references changed consistently."""
    if not new_name or Path(new_name).name != new_name or new_name in {"lyrics.metadata", ".metadata"}:
        raise ValueError("文件名不能为空、包含路径，或使用元数据保留名称")
    updated = copy.deepcopy(metadata)
    found = False
    for item in updated.get("files", []):
        if isinstance(item, dict) and item.get("name") == old_name:
            item["name"] = new_name
            item["format"] = Path(new_name).suffix.lstrip(".").casefold()
            found = True
    if not found:
        raise ValueError("该文件不在 metadata 的文件列表中")
    for source in updated.get("sources", []):
        if isinstance(source, dict) and source.get("file") == old_name:
            source["file"] = new_name
    return updated


def remove_file_record(metadata: dict[str, Any], file_name: str) -> dict[str, Any]:
    """Return metadata with a deleted lyric file's binding removed."""
    updated = copy.deepcopy(metadata)
    records = updated.get("files", [])
    remaining = [item for item in records if not (isinstance(item, dict) and item.get("name") == file_name)]
    if len(remaining) == len(records):
        raise ValueError("该文件不在 metadata 的文件列表中")
    updated["files"] = remaining
    for source in updated.get("sources", []):
        if isinstance(source, dict) and source.get("file") == file_name:
            source.pop("file", None)
    return updated


def remove_track_record(metadata: dict[str, Any], metadata_ref: str) -> dict[str, Any]:
    """Delete one virtual track entity while retaining its local lyric files as unbound."""
    updated = copy.deepcopy(metadata)
    tracks = updated.get("tracks", {})
    if metadata_ref not in tracks:
        raise ValueError("曲目实体不存在")
    tracks.pop(metadata_ref)
    deleted = {str(value) for value in updated.get("deletedTracks", [])}
    deleted.add(metadata_ref)
    updated["deletedTracks"] = sorted(deleted)
    for item in updated.get("files", []):
        if not isinstance(item, dict) or item.get("metadataRef") != metadata_ref:
            continue
        # An external link remains valid after its old local entity disappears.
        if isinstance(item.get("linkedTarget"), dict):
            continue
        # Keep an explicit null so discovery cannot restore the old binding.
        item["metadataRef"] = None
        item.pop("linked", None)
        item.pop("linkedFrom", None)
    return updated


def detach_external_link_record(metadata: dict[str, Any], file_name: str) -> dict[str, Any]:
    """Restore a file's local binding after its external target was removed."""
    updated = copy.deepcopy(metadata)
    for item in updated.get("files", []):
        if isinstance(item, dict) and item.get("name") == file_name:
            target = item.get("linkedTarget")
            if not isinstance(target, dict):
                raise ValueError("该文件没有外部关联")
            item["metadataRef"] = str(item.get("linkedFrom") or item.get("metadataRef") or "")
            item.pop("linked", None)
            item.pop("linkedFrom", None)
            item.pop("linkedTarget", None)
            return updated
    raise ValueError("该文件不在 metadata 的文件列表中")


def make_linked_file_independent(
    source_metadata: dict[str, Any], file_name: str, new_ref: str, track: dict[str, Any],
) -> dict[str, Any]:
    """Turn one linked lyric into an independently editable local entity."""
    updated = copy.deepcopy(source_metadata)
    if not new_ref or new_ref in updated.get("tracks", {}):
        raise ValueError("新的曲目实体标识无效或已存在")
    updated.setdefault("tracks", {})[new_ref] = copy.deepcopy(track)
    for item in updated.get("files", []):
        if isinstance(item, dict) and item.get("name") == file_name:
            item["metadataRef"] = new_ref
            item.pop("linked", None)
            item.pop("linkedFrom", None)
            item.pop("linkedTarget", None)
            return updated
    raise ValueError("该文件不在 metadata 的文件列表中")


def scan_library_data(adapter: Any, repo: Path) -> tuple[list[Path], dict[Path, dict[str, Any]], RegistryNode]:
    """Perform the complete library scan without touching Tk widgets."""
    directories = adapter.scan()
    cache = {path: adapter.load(path) for path in directories}
    return directories, cache, build_registry_tree(repo / "Lyrics", directories, cache)


class CoreAdapter:
    """Keep Tk concerns separate from the repository's metadata engine."""

    def __init__(self, root: Path) -> None:
        if LyricsDatabase is None:
            raise RuntimeError("无法导入 lyrics_manager.core.LyricsDatabase")
        self.root = root.resolve()
        self.database = LyricsDatabase(self.root)

    def scan(self) -> list[Path]:
        self.database.scan()
        lyrics = self.root / "Lyrics"
        return sorted(
            (p for p in lyrics.glob("*/*") if p.is_dir()),
            key=lambda p: str(p.relative_to(lyrics)).casefold(),
        )

    def load(self, directory: Path) -> dict[str, Any]:
        value = self.database.load_metadata(directory)
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        elif hasattr(value, "__dict__") and not isinstance(value, dict):
            value = vars(value)
        return dict(value or {})

    def save(self, directory: Path, metadata: dict[str, Any]) -> Any:
        return self.database.save_metadata(directory, metadata)

    def sync(self, directory: Path, metadata_ref: str | None = None) -> Any:
        refs = [metadata_ref] if metadata_ref else None
        return self.database.sync_to_sources(directory, metadata_refs=refs)

    def sync_file(self, path: Path, track: dict[str, Any]) -> Any:
        return self.database.sync_file_to_track(path, track)

    def preview(self, path: Path) -> list[tuple[str, str, str, str, str]]:
        result = self.database.preview(path)
        rows = result.get("lines", result) if isinstance(result, dict) else result
        normalized: list[tuple[str, str, str, str, str]] = []
        for row in rows or []:
            if isinstance(row, dict):
                normalized.append(tuple(str(row.get(k, "")) for k in
                                        ("line_number", "agent", "original", "translation", "transliteration")))
            else:
                values = list(row) if isinstance(row, (list, tuple)) else [row]
                normalized.append(tuple(str(x or "") for x in (values + ["", "", "", ""])[:5]))
        return normalized

    def convert_legacy_ttml(self, path: Path) -> dict[str, Any]:
        return self.database.convert_legacy_ttml(path)

    def restore_ttml_backup(self, path: Path) -> dict[str, Any]:
        return self.database.restore_ttml_backup(path)


class LyricsManagerApp(tk.Tk):
    def __init__(self, root_path: str | Path | None = None) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1280x780")
        self.minsize(980, 620)
        self._preferences = self._load_preferences()
        self.theme = _Theme(str(self._preferences.get("theme_mode", "system")))
        self._apply_theme()
        self.repo = Path(root_path or Path(__file__).resolve().parents[2]).resolve()
        self.adapter = CoreAdapter(self.repo)
        self.gh_executable: str | None = self._preferences.get("gh_executable") or None
        self.git_executable: str | None = self._preferences.get("git_executable") or None
        self.git: GitService | None = None
        self.git_init_error: Exception | None = None
        try:
            self.git = GitService(self.repo, git_executable=self.git_executable or "git")
        except Exception as exc:  # git missing / not a repo: stay usable, fix in Settings
            self.git_init_error = exc
        self.github = GitHubPRService(
            self.repo,
            gh_executable=self.gh_executable,
            git_executable=self.git_executable,
        )
        self.track_dirs: list[Path] = []
        self.metadata_cache: dict[Path, dict[str, Any]] = {}
        self.registry_model: RegistryNode | None = None
        self.tree_nodes: dict[str, RegistryNode] = {}
        self._drag_source_item: str | None = None
        self._tree_drag_active = False
        self._linked_edit_enabled = False
        self.current_dir: Path | None = None
        self.current_metadata: dict[str, Any] = {}
        self.current_ref: str | None = None
        self.metadata_ref_var = tk.StringVar()
        self.field_vars = {key: tk.StringVar() for key, _ in FIELDS}
        self.search_var = tk.StringVar()
        self.selected_file_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self.current_file_path: Path | None = None
        self.auto_sync_var = tk.BooleanVar(value=bool(self._preferences.get("auto_sync", False)))
        self.offer_merge_var = tk.BooleanVar(value=bool(self._preferences.get("offer_merge_after_pr", True)))
        expanded = self._preferences.get("expanded_tree_nodes", [])
        self._expanded_tree_keys: set[str] = {str(value) for value in expanded if isinstance(value, str)}
        self._tree_expansion_known = bool(self._preferences.get("tree_expansion_known", False))
        self.tree_node_keys: dict[str, str] = {}
        self._git_busy = False
        self._git_buttons: list[ttk.Button] = []
        self._commit_include: dict[str, bool] = {}  # path -> include in commit
        self._git_results: queue.Queue[tuple[Any, Exception | None]] = queue.Queue()
        self._scan_results: queue.Queue[tuple[Any, Exception | None]] = queue.Queue()
        self._scan_busy = False
        self._metadata_inputs: list[tk.Widget] = []
        self._metadata_actions: list[ttk.Button] = []
        self._github_busy = False
        self._github_results: queue.Queue[tuple[Any, Exception | None]] = queue.Queue()
        self._github_buttons: list[ttk.Button] = []
        self._gh_user_login: str = ""
        self._gh_is_owner: bool = False
        self._pr_selected: int | None = None
        self._build_ui()
        self.after_idle(self.refresh_library)

    def _load_preferences(self) -> dict[str, Any]:
        try:
            value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_preference(self) -> None:
        try:
            self._preferences.update({
                "auto_sync": self.auto_sync_var.get(),
                "expanded_tree_nodes": sorted(self._expanded_tree_keys),
                "tree_expansion_known": self._tree_expansion_known,
                "theme_mode": self.theme.mode,
                "gh_executable": self.gh_executable or "",
                "git_executable": self.git_executable or "",
                "offer_merge_after_pr": self.offer_merge_var.get(),
            })
            CONFIG_PATH.write_text(json.dumps(self._preferences, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            self._set_status(f"偏好保存失败：{exc}")

    def _apply_theme(self) -> None:
        """Apply the ttk theme and explicit widget colours for the current mode.

        The default ``"."`` style alone does not reach the more specific style
        names (``TNotebook.Tab``, ``TButton``, ``Treeview.Heading``, ``TEntry``,
        ``TCombobox``, scrollbars, etc.), so every element style is configured
        explicitly.  This keeps the tab strip, buttons, tree headers, inputs and
        combo boxes consistent with the resolved palette.
        """
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        pal = self.theme.palette
        try:
            # Base / default — inherited by plain frames and labels.
            style.configure(".", background=pal["background"], foreground=pal["foreground"],
                            fieldbackground=pal["fieldbackground"], bordercolor=pal["bordercolor"],
                            focuscolor=pal["focuscolor"], troughcolor=pal["troughcolor"],
                            lightcolor=pal["lightcolor"], darkcolor=pal["darkcolor"],
                            insertcolor=pal["insertcolor"], selectbackground=pal["selectbackground"],
                            selectforeground=pal["selectforeground"])
            # Notebook tab strip.
            style.configure("TNotebook", background=pal["background"], bordercolor=pal["bordercolor"],
                            tabmargins=(0, 4, 0, 0))
            style.configure("TNotebook.Tab", background=pal["darkcolor"], foreground=pal["foreground"],
                            padding=(14, 6), bordercolor=pal["bordercolor"],
                            lightcolor=pal["fieldbackground"], darkcolor=pal["darkcolor"])
            style.map("TNotebook.Tab",
                      background=[("selected", pal["background"]), ("active", pal["lightcolor"])],
                      foreground=[("selected", pal["foreground"])])
            # Frames / labels / label frames.
            style.configure("TFrame", background=pal["background"])
            style.configure("TLabel", background=pal["background"], foreground=pal["foreground"])
            style.configure("TLabelframe", background=pal["background"], bordercolor=pal["bordercolor"])
            style.configure("TLabelframe.Label", background=pal["background"], foreground=pal["foreground"])
            # Buttons: map every interactive state so hover/press never goes white.
            style.configure("TButton", background=pal["lightcolor"], foreground=pal["foreground"],
                            bordercolor=pal["bordercolor"], focuscolor=pal["focuscolor"],
                            padding=(10, 4), lightcolor=pal["fieldbackground"], darkcolor=pal["darkcolor"])
            style.map("TButton",
                      background=[("pressed", pal["selectbackground"]),
                                  ("active", pal["lightcolor"]),
                                  ("disabled", pal["background"])],
                      foreground=[("disabled", pal["bordercolor"])])
            # Entry / combobox (fields the user types into).
            style.configure("TEntry", fieldbackground=pal["fieldbackground"], foreground=pal["foreground"],
                            insertcolor=pal["insertcolor"], bordercolor=pal["bordercolor"],
                            lightcolor=pal["fieldbackground"], darkcolor=pal["darkcolor"])
            style.map("TEntry",
                      fieldbackground=[("focus", pal["fieldbackground"])],
                      bordercolor=[("focus", pal["selectbackground"])])
            style.configure("TCombobox", fieldbackground=pal["fieldbackground"], foreground=pal["foreground"],
                            background=pal["fieldbackground"], arrowcolor=pal["foreground"],
                            bordercolor=pal["bordercolor"], lightcolor=pal["fieldbackground"],
                            darkcolor=pal["darkcolor"], selectbackground=pal["selectbackground"],
                            selectforeground=pal["selectforeground"])
            style.map("TCombobox",
                      fieldbackground=[("readonly", pal["fieldbackground"]), ("focus", pal["fieldbackground"])],
                      foreground=[("readonly", pal["foreground"])],
                      background=[("active", pal["lightcolor"]), ("pressed", pal["selectbackground"])],
                      arrowcolor=[("active", pal["foreground"])])
            # Treeview body and headings.
            style.configure("Treeview", background=pal["fieldbackground"], fieldbackground=pal["fieldbackground"],
                            foreground=pal["foreground"], bordercolor=pal["bordercolor"],
                            lightcolor=pal["fieldbackground"], darkcolor=pal["darkcolor"])
            style.map("Treeview",
                      background=[("selected", pal["selectbackground"])],
                      foreground=[("selected", pal["selectforeground"])])
            style.configure("Treeview.Heading", background=pal["darkcolor"], foreground=pal["foreground"],
                            bordercolor=pal["bordercolor"], lightcolor=pal["fieldbackground"],
                            darkcolor=pal["darkcolor"])
            style.map("Treeview.Heading", background=[("active", pal["lightcolor"])])
            # Scrollbars.
            style.configure("Vertical.TScrollbar", background=pal["fieldbackground"], troughcolor=pal["troughcolor"],
                            bordercolor=pal["bordercolor"], arrowcolor=pal["foreground"],
                            lightcolor=pal["fieldbackground"], darkcolor=pal["darkcolor"])
            style.configure("Horizontal.TScrollbar", background=pal["fieldbackground"], troughcolor=pal["troughcolor"],
                            bordercolor=pal["bordercolor"], arrowcolor=pal["foreground"],
                            lightcolor=pal["fieldbackground"], darkcolor=pal["darkcolor"])
            # Separator.
            style.configure("TSeparator", background=pal["bordercolor"])
            # Radio / check buttons: the indicator (circle / box) is a separate
            # element that the plain ``background`` option does not reach, so it
            # is themed explicitly and its states are mapped to avoid white on
            # hover or selection.
            for radio_style in ("TRadiobutton", "TCheckbutton"):
                style.configure(radio_style, background=pal["background"], foreground=pal["foreground"],
                                indicatorbackground=pal["fieldbackground"],
                                indicatorforeground=pal["foreground"],
                                bordercolor=pal["bordercolor"], lightcolor=pal["fieldbackground"],
                                darkcolor=pal["darkcolor"])
                style.map(radio_style,
                          background=[("active", pal["lightcolor"]), ("selected", pal["background"])],
                          foreground=[("disabled", pal["bordercolor"])],
                          indicatorbackground=[("selected", pal["selectbackground"]),
                                               ("active", pal["fieldbackground"])],
                          indicatorforeground=[("selected", pal["selectforeground"])])
            # Panedwindow panels (resizable splitter).
            style.configure("TPanedwindow", background=pal["background"])
            style.configure("TPanedwindow.Pane", background=pal["background"])
            # Progress bar (background library scan).
            style.configure("Horizontal.TProgressbar", background=pal["selectbackground"],
                            troughcolor=pal["troughcolor"], bordercolor=pal["bordercolor"])
        except tk.TclError:
            pass
        self.configure(background=pal["background"])
        for widget in getattr(self, "_theme_text_widgets", []):
            try:
                widget.configure(
                    bg=self.theme.text_bg, fg=self.theme.text_fg,
                    insertbackground=self.theme.text_fg,
                )
            except tk.TclError:
                pass
        for tree, tag in getattr(self, "_theme_tree_tags", []):
            try:
                tree.tag_configure(tag, background=self.theme.legacy_bg, foreground=self.theme.legacy_fg)
            except tk.TclError:
                pass

    def _track_theme_text(self, widget: tk.Widget) -> None:
        widgets = getattr(self, "_theme_text_widgets", None)
        if widgets is None:
            widgets = []
            self._theme_text_widgets = widgets
        if widget not in widgets:
            widgets.append(widget)

    def _track_theme_tree_tag(self, tree: ttk.Treeview, tag: str) -> None:
        tags = getattr(self, "_theme_tree_tags", None)
        if tags is None:
            tags = []
            self._theme_tree_tags = tags
        pair = (tree, tag)
        if pair not in tags:
            tags.append(pair)

    def _set_theme_mode(self, mode: str) -> None:
        if mode not in THEME_MODES:
            return
        self.theme = _Theme(mode)
        self._apply_theme()
        self._save_preference()
        self._set_status(f"主题已切换为：{mode}")

    def _build_ui(self) -> None:
        self.main_notebook = ttk.Notebook(self)
        self.main_notebook.pack(fill="both", expand=True)
        library = ttk.Frame(self.main_notebook, padding=8)
        version = ttk.Frame(self.main_notebook, padding=8)
        pulls = ttk.Frame(self.main_notebook, padding=8)
        settings = ttk.Frame(self.main_notebook, padding=8)
        self.main_notebook.add(library, text="歌词库")
        self.main_notebook.add(version, text="版本管理")
        self.main_notebook.add(pulls, text="拉取请求")
        self.main_notebook.add(settings, text="设置")
        self.version_tab = version
        self.pulls_tab = pulls
        self.main_notebook.bind("<<NotebookTabChanged>>", self._on_main_tab_changed)
        self._build_library(library)
        self._build_git(version)
        self._build_pulls(pulls)
        self._build_settings(settings)
        ttk.Separator(self, orient="horizontal").pack(fill="x")
        ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(8, 4)).pack(fill="x")

    def _build_settings(self, parent: ttk.Frame) -> None:
        """Settings page: appearance, editing behaviour, and GitHub options."""
        parent.columnconfigure(0, weight=1)

        # -- Appearance ----------------------------------------------------
        box = ttk.LabelFrame(parent, text="外观", padding=12)
        box.grid(row=0, column=0, sticky="nw", padx=8, pady=(8, 4))
        box.columnconfigure(0, weight=1)
        ttk.Label(box, text="主题模式").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.theme_var = tk.StringVar(value=self.theme.mode)
        labels = {"system": "跟随系统", "light": "浅色", "dark": "暗色"}
        for row, mode in enumerate(THEME_MODES, start=1):
            ttk.Radiobutton(
                box, text=labels[mode], value=mode, variable=self.theme_var,
                command=lambda m=mode: self._set_theme_mode(m),
            ).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Label(
            box, text="更改立即生效并保存。手动选择的模式会使应用不再跟随系统。",
        ).grid(row=4, column=0, sticky="w", pady=(10, 0))

        # -- Editing --------------------------------------------------------
        edit = ttk.LabelFrame(parent, text="编辑", padding=12)
        edit.grid(row=1, column=0, sticky="nw", padx=8, pady=4)
        edit.columnconfigure(0, weight=1)
        ttk.Checkbutton(
            edit, text="保存后自动同步到源文件",
            variable=self.auto_sync_var, command=self._save_preference,
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            edit, text="开启后，保存 lyrics.metadata 会把元数据写回 TTML/LRCN 头部。",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        # -- Git -------------------------------------------------------------
        gitbox = ttk.LabelFrame(parent, text="Git", padding=12)
        gitbox.grid(row=2, column=0, sticky="nw", padx=8, pady=4)
        gitbox.columnconfigure(1, weight=1)
        ttk.Label(gitbox, text="git 可执行文件").grid(row=0, column=0, sticky="w", pady=(0, 4))
        git_controls = ttk.Frame(gitbox)
        git_controls.grid(row=1, column=0, columnspan=2, sticky="ew")
        git_controls.columnconfigure(0, weight=1)
        self.git_path_var = tk.StringVar(value=self.git_executable or "")
        self.git_path_entry = ttk.Entry(git_controls, textvariable=self.git_path_var)
        self.git_path_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(git_controls, text="选择", command=self._browse_git_executable).grid(row=0, column=1, padx=(4, 0))
        ttk.Button(git_controls, text="重置", command=self._reset_git_executable).grid(row=0, column=2, padx=(4, 0))
        ttk.Label(
            gitbox, text="留空则自动检测（推荐）。PATH 找不到 git 时在此指定。",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # -- GitHub ----------------------------------------------------------
        gh = ttk.LabelFrame(parent, text="GitHub", padding=12)
        gh.grid(row=3, column=0, sticky="nw", padx=8, pady=4)
        gh.columnconfigure(1, weight=1)
        ttk.Label(gh, text="gh 可执行文件").grid(row=0, column=0, sticky="w", pady=(0, 4))
        gh_controls = ttk.Frame(gh)
        gh_controls.grid(row=1, column=0, columnspan=2, sticky="ew")
        gh_controls.columnconfigure(0, weight=1)
        self.gh_path_var = tk.StringVar(value=self.gh_executable or "")
        self.gh_path_entry = ttk.Entry(gh_controls, textvariable=self.gh_path_var)
        self.gh_path_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(gh_controls, text="选择", command=self._browse_gh_executable).grid(row=0, column=1, padx=(4, 0))
        ttk.Button(gh_controls, text="重置", command=self._reset_gh_executable).grid(row=0, column=2, padx=(4, 0))
        ttk.Label(
            gh, text="留空则自动检测（推荐）。填写的路径会被优先使用。",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(
            gh, text="创建 PR 后询问是否立即合并",
            variable=self.offer_merge_var, command=self._save_preference,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Label(
            gh, text="仅当你的 GitHub 账户拥有该仓库时，合并选项才会出现。",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _browse_gh_executable(self) -> None:
        path = filedialog.askopenfilename(title="选择 gh 可执行文件", parent=self)
        if path:
            self._set_gh_executable(path)

    def _reset_gh_executable(self) -> None:
        self._set_gh_executable("")

    def _set_gh_executable(self, path: str) -> None:
        path = path.strip()
        if path and not Path(path).is_file():
            self._show_error("无效路径", ValueError(f"未找到文件：{path}"))
            return
        self.gh_executable = path or None
        try:
            self.gh_path_var.set(path)
        except tk.TclError:
            pass
        self.github = GitHubPRService(
            self.repo,
            gh_executable=self.gh_executable,
            git_executable=self.git_executable,
        )
        self._save_preference()
        self._set_status("gh 可执行文件已更新")

    def _browse_git_executable(self) -> None:
        path = filedialog.askopenfilename(title="选择 git 可执行文件", parent=self)
        if path:
            self._set_git_executable(path)

    def _reset_git_executable(self) -> None:
        self._set_git_executable("")

    def _set_git_executable(self, path: str) -> None:
        """Point the manager at a specific git binary (e.g. when PATH misses it).

        ``GitService`` validates git eagerly, so an unresolvable path surfaces as
        an actionable error rather than silently failing later.
        """
        path = path.strip()
        if path and not Path(path).is_file():
            self._show_error("无效路径", ValueError(f"未找到文件：{path}"))
            return
        self.git_executable = path or None
        try:
            self.git_path_var.set(path)
        except tk.TclError:
            pass
        try:
            self.git = GitService(self.repo, git_executable=self.git_executable or "git")
            self.git_init_error = None
        except Exception as exc:  # GitError/ValueError from a bad executable
            self._show_error("Git 初始化失败", exc)
            return
        self.github = GitHubPRService(
            self.repo,
            gh_executable=self.gh_executable,
            git_executable=self.git_executable,
        )
        self._save_preference()
        self._set_status("git 可执行文件已更新")

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _on_main_tab_changed(self, _event: object = None) -> None:
        """Refresh Git status whenever the user enters the version-management tab."""
        if self.main_notebook.select() == str(self.version_tab) and not self._git_busy:
            self.git_status()
        elif self.main_notebook.select() == str(self.pulls_tab):
            self._refresh_github_state()

    def _build_library(self, parent: ttk.Frame) -> None:
        panes = ttk.Panedwindow(parent, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes, width=320)
        panes.add(left, weight=0)
        ttk.Entry(left, textvariable=self.search_var, width=32).pack(fill="x")
        self.search_var.trace_add("write", lambda *_: self._filter_tracks())
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True, pady=6)
        self.registry_tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse", padding=2)
        self.registry_tree.pack(side="left", fill="both", expand=True)
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.registry_tree.yview)
        tree_scroll.pack(side="right", fill="y")
        self.registry_tree.configure(yscrollcommand=tree_scroll.set)
        self.registry_tree.tag_configure("legacy", background=self.theme.legacy_bg, foreground=self.theme.legacy_fg)
        self._track_theme_tree_tag(self.registry_tree, "legacy")
        self.registry_tree.bind("<<TreeviewSelect>>", self._select_registry_node)
        self.registry_tree.bind("<Double-Button-1>", self._open_tree_file)
        self.registry_tree.bind("<Button-3>", self._show_registry_menu)
        self.registry_tree.bind("<ButtonPress-1>", self._begin_tree_drag, add=True)
        self.registry_tree.bind("<B1-Motion>", self._update_tree_drag_cursor, add=True)
        self.registry_tree.bind("<ButtonRelease-1>", self._finish_tree_drag, add=True)
        self.registry_tree.bind("<Delete>", self.delete_selected_node)
        self.registry_tree.bind("<<TreeviewOpen>>", self._cache_tree_open_state)
        self.registry_tree.bind("<<TreeviewClose>>", self._cache_tree_open_state)
        tree_controls = ttk.Frame(left)
        tree_controls.pack(fill="x", pady=(0, 4))
        ttk.Button(tree_controls, text="全部展开", command=self.expand_all_tree).pack(side="left", fill="x", expand=True)
        ttk.Button(tree_controls, text="全部收起", command=self.collapse_all_tree).pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.refresh_button = ttk.Button(left, text="重新扫描", command=self.refresh_library)
        self.refresh_button.pack(fill="x")

        right = ttk.Notebook(panes)
        panes.add(right, weight=1)
        metadata = ttk.Frame(right, padding=10)
        preview = ttk.Frame(right, padding=8)
        right.add(metadata, text="元数据与文件")
        right.add(preview, text="逐行预览")
        metadata.columnconfigure(1, weight=1)
        ttk.Label(metadata, text="文件名").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        self.selected_file_entry = ttk.Entry(metadata, textvariable=self.selected_file_var)
        self.selected_file_entry.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        file_buttons = ttk.Frame(metadata)
        file_buttons.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Button(file_buttons, text="打开文件", command=self.open_selected_file).pack(side="left")
        ttk.Button(file_buttons, text="在文件管理器打开", command=self.reveal_selected_file).pack(side="left", padx=6)
        ttk.Button(file_buttons, text="删除文件", command=self.delete_selected_file).pack(side="left", padx=(0, 6))
        self.convert_ttml_button = ttk.Button(file_buttons, text="一键转换旧 TTML", command=self.convert_selected_ttml)
        self.convert_ttml_button.pack(side="left", padx=(0, 6))
        ttk.Button(file_buttons, text="关联到其他曲目", command=self.bind_selected_file).pack(side="left", padx=(0, 6))
        self.edit_linked_button = ttk.Button(file_buttons, text="编辑关联曲目", command=self.enable_linked_editing)
        self.edit_linked_button.pack(side="left", padx=(0, 6))
        ttk.Button(file_buttons, text="拆分为独立曲目", command=self.split_selected_file).pack(side="left")
        ttk.Label(metadata, text="曲目实体").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        self.metadata_ref_box = ttk.Combobox(metadata, textvariable=self.metadata_ref_var, state="readonly")
        self.metadata_ref_box.grid(row=2, column=1, sticky="ew", pady=3)
        self.metadata_ref_box.bind("<<ComboboxSelected>>", self._select_metadata_track)
        for row, (key, label) in enumerate(FIELDS, start=3):
            ttk.Label(metadata, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            entry = ttk.Entry(metadata, textvariable=self.field_vars[key])
            entry.grid(row=row, column=1, sticky="ew", pady=3)
            self._metadata_inputs.append(entry)
        controls = ttk.Frame(metadata)
        controls.grid(row=len(FIELDS) + 3, column=0, columnspan=2, sticky="ew", pady=(10, 6))
        self.save_metadata_button = ttk.Button(controls, text="保存 lyrics.metadata", command=self.save_metadata)
        self.save_metadata_button.pack(side="left")
        self.sync_button = ttk.Button(controls, text="同步到文件", command=self.sync_sources)
        self.sync_button.pack(side="left", padx=6)
        self._metadata_actions.append(self.save_metadata_button)
        self.link_notice = ttk.Label(metadata, text="", foreground="#a05a00")
        self.link_notice.grid(row=len(FIELDS) + 4, column=0, columnspan=2, sticky="w")
        self.legacy_notice = ttk.Label(metadata, text="", foreground="#9a6700")
        self.legacy_notice.grid(row=len(FIELDS) + 5, column=0, columnspan=2, sticky="w")
        preview.rowconfigure(0, weight=1)
        preview.columnconfigure(0, weight=1)
        self.preview_tree = ttk.Treeview(preview, columns=("line_number", "agent", "original", "translation", "romanization"), show="headings")
        for key, label in (("line_number", "行"), ("agent", "Agent"), ("original", "原文"), ("translation", "翻译"), ("romanization", "音译")):
            self.preview_tree.heading(key, text=label)
            self.preview_tree.column(key, width=55 if key in {"line_number", "agent"} else 280,
                                     stretch=key not in {"line_number", "agent"})
        self.preview_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(preview, orient="vertical", command=self.preview_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.preview_tree.configure(yscrollcommand=scroll.set)

    def _build_git(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)
        header = ttk.Frame(parent)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        self.git_repository_label = ttk.Label(header, font=("TkDefaultFont", 11, "bold"))
        self.git_repository_label.grid(row=0, column=0, sticky="w")
        self.git_busy_label = ttk.Label(header, text="")
        self.git_busy_label.grid(row=0, column=1, padx=6)
        for column, (label, command) in enumerate((
            ("刷新", self.git_status), ("Fetch", self.git_fetch),
            ("Pull（仅快进）", self.git_pull), ("创建 PR", self.git_create_pr),
            ("Push（备用）", self.git_push),
        ), start=2):
            button = ttk.Button(header, text=label, command=command)
            button.grid(row=0, column=column, padx=3)
            self._git_buttons.append(button)

        tabs = ttk.Notebook(parent)
        tabs.grid(row=1, column=0, sticky="nsew")
        changes = ttk.Frame(tabs, padding=6)
        history = ttk.Frame(tabs, padding=6)
        tabs.add(changes, text="变更")
        tabs.add(history, text="历史")

        changes.rowconfigure(0, weight=1)
        changes.columnconfigure(1, weight=1)
        change_side = ttk.Frame(changes)
        change_side.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        change_side.rowconfigure(1, weight=1)
        change_side.columnconfigure(0, weight=1)
        self.git_change_count = ttk.Label(change_side, text="变更（0）")
        self.git_change_count.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.git_changes = ttk.Treeview(
            change_side, columns=("include", "staged", "worktree"), show="tree headings",
            selectmode="extended", height=14,
        )
        self.git_changes.heading("#0", text="文件")
        self.git_changes.heading("include", text="暂存")
        self.git_changes.heading("staged", text="暂存区")
        self.git_changes.heading("worktree", text="工作区")
        self.git_changes.column("#0", width=260, stretch=True)
        self.git_changes.column("include", width=52, anchor="center", stretch=False)
        self.git_changes.column("staged", width=60, anchor="center", stretch=False)
        self.git_changes.column("worktree", width=60, anchor="center", stretch=False)
        self.git_changes.grid(row=1, column=0, sticky="nsew")
        self.git_changes.bind("<<TreeviewSelect>>", self._git_show_selected_diff)
        self.git_changes.bind("<Button-1>", self._toggle_change_include)
        action_bar = ttk.Frame(change_side)
        action_bar.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        for label, command in (("全部勾选", self._check_all_changes), ("取消全部", self._uncheck_all_changes),
                               ("暂存所选", self.git_stage), ("取消暂存", self.git_unstage)):
            button = ttk.Button(action_bar, text=label, command=command)
            button.pack(side="left", padx=(0, 5))
            self._git_buttons.append(button)

        detail = ttk.Frame(changes)
        detail.grid(row=0, column=1, sticky="nsew")
        detail.rowconfigure(1, weight=1)
        detail.columnconfigure(0, weight=1)
        self.git_diff_label = ttk.Label(detail, text="选择文件查看差异")
        self.git_diff_label.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.git_output = tk.Text(detail, wrap="none", font=("TkFixedFont", 10), padx=8, pady=8,
                                  bg=self.theme.text_bg, fg=self.theme.text_fg,
                                  insertbackground=self.theme.text_fg)
        self._track_theme_text(self.git_output)
        self.git_output.grid(row=1, column=0, sticky="nsew")
        self.git_output.configure(state="disabled")
        commit_box = ttk.LabelFrame(detail, text="提交", padding=8)
        commit_box.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        commit_box.columnconfigure(0, weight=1)
        ttk.Label(commit_box, text="摘要（必填，建议使用 feat:/fix: 标签）").grid(row=0, column=0, sticky="w")
        self.git_commit_summary = ttk.Entry(commit_box)
        self.git_commit_summary.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        ttk.Label(commit_box, text="描述（可选）").grid(row=2, column=0, sticky="w")
        self.git_commit_description = tk.Text(commit_box, height=3, wrap="word",
                                              bg=self.theme.text_bg, fg=self.theme.text_fg,
                                              insertbackground=self.theme.text_fg)
        self._track_theme_text(self.git_commit_description)
        self.git_commit_description.grid(row=3, column=0, sticky="ew", pady=(2, 6))
        self.git_commit_button = ttk.Button(commit_box, text="提交到当前分支", command=self.git_commit)
        self.git_commit_button.grid(row=4, column=0, sticky="e")
        self._git_buttons.append(self.git_commit_button)

        history.rowconfigure(1, weight=1)
        history.columnconfigure(0, weight=1)
        history_refresh = ttk.Button(history, text="刷新历史", command=self.git_log)
        history_refresh.grid(row=0, column=0, sticky="w", pady=(0, 6))
        self._git_buttons.append(history_refresh)
        self.git_history = ttk.Treeview(history, columns=("date", "author", "commit"), show="tree headings")
        self.git_history.heading("#0", text="提交")
        self.git_history.heading("date", text="日期")
        self.git_history.heading("author", text="作者")
        self.git_history.heading("commit", text="哈希")
        self.git_history.column("#0", width=600)
        self.git_history.column("date", width=100, stretch=False)
        self.git_history.column("author", width=160, stretch=False)
        self.git_history.column("commit", width=90, stretch=False)
        self.git_history.grid(row=1, column=0, sticky="nsew")
        self.after_idle(self.git_status)

    def _show_error(self, title: str, exc: Exception) -> None:
        self._set_status(f"{title}：{exc}")
        messagebox.showerror(title, str(exc), parent=self)

    def refresh_library(self) -> None:
        if self._scan_busy:
            self._set_status("歌词库正在扫描，请稍候…")
            return
        self._scan_busy = True
        self.refresh_button.configure(state="disabled")
        self._set_status("正在扫描歌词库…")

        def worker() -> None:
            try:
                self._scan_results.put((scan_library_data(self.adapter, self.repo), None))
            except Exception as exc:
                self._scan_results.put((None, exc))

        def poll() -> None:
            try:
                result, error = self._scan_results.get_nowait()
            except queue.Empty:
                self.after(30, poll)
                return
            self._scan_busy = False
            self.refresh_button.configure(state="normal")
            if error is not None:
                self._set_status(f"扫描失败：{error}")
                self._show_error("扫描失败", error)
                return
            self.track_dirs, self.metadata_cache, self.registry_model = result
            self._filter_tracks()
            file_count = sum(len(data.get("files", [])) for data in self.metadata_cache.values())
            self._set_status(f"扫描完成：{len(self.track_dirs)} 个目录，{file_count} 个歌词文件")
            if self.current_file_path:
                self._select_tree_path(self.current_file_path)

        threading.Thread(target=worker, name="lyrics-library-scan", daemon=True).start()
        self.after(30, poll)

    def _select_tree_path(self, path: Path) -> None:
        for item, node in self.tree_nodes.items():
            if node.kind == "file" and node.directory and node.file_name and node.directory / node.file_name == path:
                self.registry_tree.selection_set(item)
                self.registry_tree.focus(item)
                self.registry_tree.see(item)
                return

    def _filter_tracks(self) -> None:
        query = self.search_var.get().strip().casefold()
        if not query:
            self._capture_tree_expansion()
        self.registry_tree.delete(*self.registry_tree.get_children())
        self.tree_nodes.clear()
        self.tree_node_keys.clear()
        filtered = filter_registry_tree(self.registry_model, query) if self.registry_model else None
        if filtered:
            self._insert_registry_node("", filtered, open_node=not self._tree_expansion_known)

    def _insert_registry_node(self, parent: str, node: RegistryNode, *, open_node: bool = False) -> None:
        key = self._tree_node_key(node)
        item = self.registry_tree.insert(
            parent, "end", text=node.label,
            open=open_node or key in self._expanded_tree_keys or bool(self.search_var.get().strip()),
            tags=("legacy",) if node.legacy else (),
        )
        self.tree_nodes[item] = node
        self.tree_node_keys[item] = key
        for child in node.children:
            self._insert_registry_node(item, child)

    def _tree_node_key(self, node: RegistryNode) -> str:
        if node.kind == "root":
            return "root"
        if node.kind == "artist":
            return f"artist:{node.label}"
        relative = ""
        if node.directory:
            try:
                relative = node.directory.relative_to(self.repo / "Lyrics").as_posix()
            except ValueError:
                relative = str(node.directory)
        if node.kind == "directory":
            return f"directory:{relative}"
        if node.kind == "track":
            return f"track:{relative}:{node.metadata_ref or '__unbound__'}"
        if node.kind == "file":
            return f"file:{relative}:{node.file_name or node.label}"
        return f"{node.kind}:{relative}:{node.label}"

    def _capture_tree_expansion(self) -> None:
        if not self.tree_nodes:
            return
        self._expanded_tree_keys = {
            self.tree_node_keys[item]
            for item, node in self.tree_nodes.items()
            if node.children and self.registry_tree.exists(item) and bool(self.registry_tree.item(item, "open"))
        }
        self._tree_expansion_known = True
        self._save_preference()

    def _cache_tree_open_state(self, _event: object = None) -> None:
        if self.search_var.get().strip():
            return
        item = self.registry_tree.focus()
        key = self.tree_node_keys.get(item)
        if not key:
            return
        if bool(self.registry_tree.item(item, "open")):
            self._expanded_tree_keys.add(key)
        else:
            self._expanded_tree_keys.discard(key)
        self._tree_expansion_known = True
        self._save_preference()

    def expand_all_tree(self) -> None:
        for item, node in self.tree_nodes.items():
            if node.children:
                self.registry_tree.item(item, open=True)
                self._expanded_tree_keys.add(self.tree_node_keys[item])
        self._tree_expansion_known = True
        self._save_preference()
        self._set_status("已展开左侧全部节点")

    def collapse_all_tree(self) -> None:
        for item, node in self.tree_nodes.items():
            if node.children:
                self.registry_tree.item(item, open=False)
        self._expanded_tree_keys.clear()
        self._tree_expansion_known = True
        self._save_preference()
        self._set_status("已收起左侧全部节点")

    def _select_registry_node(self, _event: object = None) -> None:
        selection = self.registry_tree.selection()
        node = self.tree_nodes.get(selection[0]) if selection else None
        self._linked_edit_enabled = False
        self.title(f"MiaowCham's Lyrics Manager - {node.file_name}" if node and node.kind == "file" else APP_NAME)
        self.legacy_notice.configure(
            text="检测到 body 内旧式翻译/音译，建议先转换。" if node and node.kind == "file" and node.legacy else ""
        )
        if hasattr(self, "convert_ttml_button"):
            enabled = bool(node and node.kind == "file" and node.file_name and Path(node.file_name).suffix.casefold() == ".ttml")
            has_backup = bool(enabled and node.directory and (node.directory / (str(node.file_name) + ".bak")).is_file())
            self.convert_ttml_button.configure(
                text="恢复备份" if has_backup else "一键转换旧 TTML",
                state="normal" if enabled else "disabled",
            )
        if not node or not node.directory:
            return
        if node.kind != "file":
            self.current_file_path = None
            self.selected_file_var.set("")
        metadata_dir = node.directory
        preferred_ref = node.metadata_ref
        if node.kind == "file" and node.linked_target_path and node.linked_target_ref:
            candidate = (self.repo / "Lyrics" / node.linked_target_path).resolve()
            try:
                candidate.relative_to((self.repo / "Lyrics").resolve())
                if candidate.is_dir():
                    metadata_dir = candidate
                    preferred_ref = node.linked_target_ref
            except ValueError:
                pass
        self._load_directory(metadata_dir, preferred_ref, select_default=False)
        if node.kind == "file" and node.file_name:
            self.current_file_path = node.directory / node.file_name
            self.selected_file_var.set(node.file_name)
            self._set_linked_edit_state(node)
            self.preview_selected_file()
        else:
            self._set_linked_edit_state(None)

    def _set_linked_edit_state(self, node: RegistryNode | None) -> None:
        linked = bool(node and node.kind == "file" and node.linked)
        state = "normal" if not linked or self._linked_edit_enabled else "disabled"
        for widget in (*self._metadata_inputs, *self._metadata_actions):
            widget.configure(state=state)
        self.sync_button.configure(state="normal")
        self.edit_linked_button.configure(state="normal" if linked else "disabled")
        if linked and node:
            target = (f"{node.linked_target_path} / {node.linked_target_ref}"
                      if node.linked_target_path and node.linked_target_ref else str(node.metadata_ref))
            self.link_notice.configure(text=f"此文件的元数据关联到 {target}；请前往目标曲目修改，或拆分为独立曲目。")
        else:
            self.link_notice.configure(text="")

    def enable_linked_editing(self) -> None:
        selection = self.registry_tree.selection()
        node = self.tree_nodes.get(selection[0]) if selection else None
        if not node or node.kind != "file" or not node.linked:
            self._set_status("请先选择关联的歌词文件")
            return
        self._linked_edit_enabled = True
        self._set_linked_edit_state(node)
        self.link_notice.configure(text="已启用编辑；首次保存时请选择独立保存或并入目标曲目实体。")

    def _open_tree_file(self, _event: object = None) -> None:
        selection = self.registry_tree.selection()
        node = self.tree_nodes.get(selection[0]) if selection else None
        if node and node.kind == "file":
            self.open_selected_file()

    def _show_registry_menu(self, event: tk.Event) -> None:
        item = self.registry_tree.identify_row(event.y)
        if not item:
            return
        self.registry_tree.selection_set(item)
        self.registry_tree.focus(item)
        node = self.tree_nodes.get(item)
        if not node:
            return
        menu = tk.Menu(self, tearoff=False)
        if node.kind == "file":
            menu.add_command(label="打开", command=self.open_selected_file)
            menu.add_command(label="在文件管理器中查看", command=self.reveal_selected_file)
            menu.add_command(label="删除文件", command=self.delete_selected_file)
            if node.file_name and Path(node.file_name).suffix.casefold() == ".ttml":
                backup = node.directory / (node.file_name + ".bak") if node.directory else None
                menu.add_command(label="恢复备份" if backup and backup.is_file() else "转换旧 TTML 格式",
                                 command=self.convert_selected_ttml)
        elif node.kind == "track":
            menu.add_command(label="删除曲目实体（保留歌词）", command=self.delete_selected_node)
        elif node.kind in {"root", "directory", "artist"}:
            path = (self.repo / "Lyrics" if node.kind == "root" else
                    node.directory or (self.repo / "Lyrics" / node.label))
            menu.add_command(label="在文件管理器中查看", command=lambda p=path: _reveal_path(p))
        if node.kind in {"root", "artist", "directory"}:
            menu.add_command(label="删除目录及其中歌词", command=self.delete_selected_node)
        menu.add_separator()
        menu.add_command(label="刷新", command=self.refresh_library)
        menu.tk_popup(event.x_root, event.y_root)

    def _load_directory(self, directory: Path, preferred_ref: str | None = None, *, select_default: bool = True) -> None:
        self.current_dir = directory
        try:
            data = self.adapter.load(self.current_dir)
            self.current_metadata = data
            refs = list(data.get("tracks", {}))
            self.metadata_ref_box.configure(values=refs)
            chosen = preferred_ref if preferred_ref in refs else (refs[0] if select_default and refs else "")
            self.metadata_ref_var.set(chosen)
            self._select_metadata_track()
            if preferred_ref is None:
                self.current_file_path = None
                self.selected_file_var.set("")
        except Exception as exc:
            self._show_error("读取元数据失败", exc)

    def _select_metadata_track(self, _event: object = None) -> None:
        self.current_ref = self.metadata_ref_var.get() or None
        track = self.current_metadata.get("tracks", {}).get(self.current_ref, {}) if self.current_ref else {}
        platforms = track.get("platforms", {}) if isinstance(track.get("platforms"), dict) else {}
        sources = self.current_metadata.get("sources", [])
        for key, _ in FIELDS:
            value = track.get(key, platforms.get(key, ""))
            if isinstance(value, list):
                value = ", ".join(str(x) for x in value)
            if key in {"source", "sourceUrl"} and sources and isinstance(sources[0], dict):
                value = sources[0].get("name" if key == "source" else "url", value)
            self.field_vars[key].set(str(value or ""))

    def _metadata_from_form(self) -> dict[str, Any]:
        values = {key: var.get().strip() for key, var in self.field_vars.items()}
        result = json.loads(json.dumps(self.current_metadata))
        if not self.current_ref:
            raise ValueError("当前目录没有可编辑的曲目实体")
        track = result.setdefault("tracks", {}).setdefault(self.current_ref, {})
        platforms = track.setdefault("platforms", {})
        for key in ("title", "album", "language", "isrc"):
            track[key] = values[key]
        for key in ("artists", "songwriters"):
            track[key] = [x.strip() for x in values[key].split(",") if x.strip()]
        for key in ("appleMusicId", "spotifyId", "ncmMusicId", "qqMusicId"):
            platforms[key] = [x.strip() for x in values[key].split(",") if x.strip()]
        sources = result.setdefault("sources", [])
        if values["source"] or values["sourceUrl"]:
            editable = next((item for item in sources if isinstance(item, dict) and item.get("manual")), None)
            if editable is None:
                editable = {"manual": True}
                sources.insert(0, editable)
            editable.update({"name": values["source"], "url": values["sourceUrl"]})
        return result

    def _choose_linked_save_mode(self) -> str:
        dialog = tk.Toplevel(self)
        dialog.title("保存关联曲目")
        dialog.transient(self)
        dialog.grab_set()
        ttk.Label(dialog, text="首次编辑关联曲目时，选择保存方式：").pack(anchor="w", padx=16, pady=(16, 8))
        ttk.Label(dialog, text="独立保存：保留文件位置并创建新的本地曲目实体。\n并入目标实体：保存到目标实体，并将歌词移动到目标目录。").pack(anchor="w", padx=16, pady=(0, 12))
        result = ["cancel"]
        def choose(value: str) -> None:
            result[0] = value
            dialog.destroy()
        controls = ttk.Frame(dialog)
        controls.pack(fill="x", padx=16, pady=(0, 16))
        ttk.Button(controls, text="取消", command=lambda: choose("cancel")).pack(side="right")
        ttk.Button(controls, text="并入目标实体", command=lambda: choose("merge")).pack(side="right", padx=6)
        ttk.Button(controls, text="独立保存", command=lambda: choose("independent")).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))
        self.wait_window(dialog)
        return result[0]

    @staticmethod
    def _new_track_ref(metadata: dict[str, Any], base: str) -> str:
        safe = "-".join(part for part in re.split(r"[^a-z0-9]+", base.casefold()) if part) or "track"
        refs = metadata.get("tracks", {})
        candidate, ordinal = safe, 2
        while candidate in refs:
            candidate = f"{safe}-{ordinal}"
            ordinal += 1
        return candidate

    def _save_linked_edit(self) -> bool:
        """Persist an explicitly unlocked linked lyric according to the selected ownership mode."""
        selection = self.registry_tree.selection()
        node = self.tree_nodes.get(selection[0]) if selection else None
        path = self._selected_file()
        if not node or not node.linked or not path or not self.current_ref:
            return False
        mode = self._choose_linked_save_mode()
        if mode == "cancel":
            self._set_status("已取消保存关联曲目")
            return True
        metadata = self._metadata_from_form()
        track = metadata.get("tracks", {}).get(self.current_ref, {})
        if not isinstance(track, dict):
            raise ValueError("关联目标曲目实体不存在")
        source_dir = path.parent
        if mode == "independent":
            source = self.adapter.load(source_dir)
            new_ref = self._new_track_ref(source, f"{self.current_ref}-{path.stem}")
            self.adapter.save(source_dir, make_linked_file_independent(source, path.name, new_ref, track))
            self._set_status(f"已将 {path.name} 独立保存为曲目实体 {new_ref}")
        elif source_dir == self.current_dir:
            for item in metadata.get("files", []):
                if isinstance(item, dict) and item.get("name") == path.name:
                    item.pop("linked", None)
                    item.pop("linkedFrom", None)
                    item.pop("linkedTarget", None)
            self.adapter.save(self.current_dir, metadata)
            self._set_status(f"已将 {path.name} 并入曲目实体 {self.current_ref}")
        else:
            self._move_files_to_target(source_dir, self.current_dir, [path.name], self.current_ref,
                                       target_metadata=metadata)
            self._set_status(f"已将 {path.name} 并入目标曲目实体 {self.current_ref}")
        self._linked_edit_enabled = False
        self.refresh_library()
        return True

    def save_metadata(self) -> None:
        if not self.current_dir:
            self._set_status("请先选择曲目")
            return
        original_path = self.current_file_path
        renamed_path: Path | None = None
        renamed_backup: tuple[Path, Path] | None = None
        try:
            selection = self.registry_tree.selection()
            node = self.tree_nodes.get(selection[0]) if selection else None
            if node and node.linked and self._linked_edit_enabled and self._save_linked_edit():
                return
            metadata = self._metadata_from_form()
            if original_path:
                requested_name = self.selected_file_var.get().strip()
                if requested_name != original_path.name:
                    if not messagebox.askyesno(
                        "确认重命名", f"将文件重命名为“{requested_name}”？\n选择“否”会恢复原文件名。", parent=self,
                    ):
                        self.selected_file_var.set(original_path.name)
                    else:
                        metadata = rename_file_record(metadata, original_path.name, requested_name)
                        candidate = original_path.with_name(requested_name)
                        if candidate.exists():
                            raise FileExistsError(f"目标文件已存在：{candidate.name}")
                        old_backup = original_path.with_name(original_path.name + ".bak")
                        new_backup = candidate.with_name(candidate.name + ".bak")
                        if old_backup.exists() and new_backup.exists():
                            raise FileExistsError(f"目标备份已存在：{new_backup.name}")
                        original_path.rename(candidate)
                        if old_backup.exists():
                            old_backup.rename(new_backup)
                            renamed_backup = (old_backup, new_backup)
                        renamed_path = candidate
                        self.current_file_path = candidate
                        self.title(f"{APP_NAME} - {candidate.name}")
            result = self.adapter.save(self.current_dir, metadata)
            self.current_metadata = self.adapter.load(self.current_dir)
            details = f"已保存 {self.current_dir / 'lyrics.metadata'}"
            if renamed_path:
                details += f"；已重命名为 {renamed_path.name}"
            if self.auto_sync_var.get():
                details += "；" + self._format_sync_result(self.adapter.sync(self.current_dir, self.current_ref)).replace("\n", " ")
            self._set_status(details + (f"；{result}" if result else ""))
            self.refresh_library()
        except Exception as exc:
            if renamed_backup and renamed_backup[1].exists() and not renamed_backup[0].exists():
                try:
                    renamed_backup[1].rename(renamed_backup[0])
                except OSError as backup_rollback_exc:
                    exc = RuntimeError(f"{exc}；备份文件名回滚失败：{backup_rollback_exc}")
            if renamed_path and original_path and renamed_path.exists() and not original_path.exists():
                try:
                    renamed_path.rename(original_path)
                    self.current_file_path = original_path
                    self.selected_file_var.set(original_path.name)
                    self.title(f"{APP_NAME} - {original_path.name}")
                except OSError as rollback_exc:
                    exc = RuntimeError(f"{exc}；文件名回滚失败：{rollback_exc}")
            self._show_error("保存失败", exc)

    @staticmethod
    def _format_sync_result(result: Any) -> str:
        if isinstance(result, dict):
            changed = result.get("changed_files", result.get("changed", []))
            warnings = result.get("warnings", [])
            return "已变更：\n" + ("\n".join(map(str, changed)) or "（无）") + "\n警告：\n" + ("\n".join(map(str, warnings)) or "（无）")
        return str(result or "同步完成（无文件变更）")

    def sync_sources(self) -> None:
        if not self.current_dir:
            self._set_status("请先选择曲目")
            return
        if not messagebox.askyesno("确认同步", "这会根据 lyrics.metadata 修改支持的歌词源文件，是否继续？", parent=self):
            return
        try:
            selection = self.registry_tree.selection()
            node = self.tree_nodes.get(selection[0]) if selection else None
            if node and node.kind == "file" and node.linked:
                track = self.current_metadata.get("tracks", {}).get(self.current_ref, {}) if self.current_ref else {}
                if not isinstance(track, dict):
                    raise ValueError("关联目标曲目实体不存在")
                path = self._selected_file()
                if not path:
                    raise ValueError("找不到关联歌词文件")
                if node.linked_target_path:
                    result = self.adapter.sync_file(path, track)
                else:
                    result = self.adapter.sync(self.current_dir, self.current_ref)
                self._set_status(self._format_sync_result(result).replace("\n", " "))
                return
            metadata = self._metadata_from_form()
            self.adapter.save(self.current_dir, metadata)
            self.current_metadata = self.adapter.load(self.current_dir)
            self._set_status(self._format_sync_result(self.adapter.sync(self.current_dir, self.current_ref)).replace("\n", " "))
        except Exception as exc:
            self._show_error("同步失败", exc)

    def bind_selected_file(self) -> None:
        path = self._selected_file()
        if not path or not self.current_dir:
            self._set_status("请先选择歌词文件")
            return
        source_dir = path.parent
        target = self._choose_link_target(source_dir)
        if not target:
            return
        target_dir, target_ref = target
        try:
            source_metadata = self.adapter.load(source_dir)
            if target_dir == source_dir:
                updated = link_file_record(source_metadata, path.name, target_ref)
            else:
                relative_target = target_dir.relative_to(self.repo / "Lyrics").as_posix()
                updated = link_file_to_external_target(source_metadata, path.name, relative_target, target_ref)
            self.adapter.save(source_dir, updated)
            old_ref = next((str(item.get("linkedFrom") or item.get("metadataRef") or "") for item in source_metadata.get("files", [])
                            if isinstance(item, dict) and item.get("name") == path.name), "")
            self._prompt_remove_empty_track(source_dir, old_ref)
            self._set_status(f"{path.name} 已关联到 {target_ref}；拆分前请在目标曲目实体修改元数据")
            if target_dir != source_dir and messagebox.askyesno(
                "迁移歌词", f"关联已建立。是否将 {path.name} 移动到目标目录\n{target_dir.relative_to(self.repo / 'Lyrics')}？", parent=self,
            ):
                self._move_files_to_target(source_dir, target_dir, [path.name], target_ref)
            self.refresh_library()
        except Exception as exc:
            self._show_error("关联失败", exc)

    def _choose_link_target(self, source_dir: Path) -> tuple[Path, str] | None:
        """Show a registry-style picker containing lyric files outside source_dir."""
        candidates: dict[tuple[Path, str], tuple[Path, str]] = {}
        for directory, metadata in self.metadata_cache.items():
            if directory == source_dir:
                continue
            for item in metadata.get("files", []):
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                target = item.get("linkedTarget") if isinstance(item.get("linkedTarget"), dict) else {}
                target_dir = directory
                target_ref = str(item.get("metadataRef") or "")
                if target.get("path") and target.get("metadataRef"):
                    candidate_dir = (self.repo / "Lyrics" / str(target["path"])).resolve()
                    if candidate_dir.is_dir():
                        target_dir, target_ref = candidate_dir, str(target["metadataRef"])
                if target_ref:
                    candidates[(directory, str(item["name"]))] = (target_dir, target_ref)
        if not candidates:
            self._set_status("没有可关联的其他目录歌词")
            return None
        dialog = tk.Toplevel(self)
        dialog.title("选择要关联的歌词")
        dialog.geometry("560x480")
        dialog.transient(self)
        dialog.grab_set()
        ttk.Label(dialog, text="选择原目录以外的目标歌词：").pack(anchor="w", padx=12, pady=(12, 4))
        tree_frame = ttk.Frame(dialog)
        tree_frame.pack(fill="both", expand=True, padx=12)
        tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        scrollbar.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scrollbar.set)
        picker_nodes: dict[str, tuple[Path, str] | None] = {}
        root = tree.insert("", "end", text="Lyrics", open=True)
        groups: dict[str, str] = {}
        for (directory, name), target_value in sorted(candidates.items(), key=lambda item: str(item[0][0]).casefold() + item[0][1].casefold()):
            relative = directory.relative_to(self.repo / "Lyrics")
            artist = relative.parts[0] if relative.parts else "（未知歌手）"
            artist_item = groups.setdefault(artist, tree.insert(root, "end", text=artist, open=True))
            directory_item = tree.insert(artist_item, "end", text="/".join(relative.parts[1:]) or directory.name, open=True)
            target_dir, target_ref = target_value
            track = self.metadata_cache.get(target_dir, {}).get("tracks", {}).get(target_ref, {})
            title = str(track.get("title") or target_ref) if isinstance(track, dict) else target_ref
            leaf = tree.insert(directory_item, "end", text=f"{name}  →  {title} [{target_ref}]")
            picker_nodes[leaf] = target_value
        result: list[tuple[Path, str]] = []
        def confirm() -> None:
            selection = tree.selection()
            target_value = picker_nodes.get(selection[0]) if selection else None
            if target_value:
                result.append(target_value)
                dialog.destroy()
            else:
                self._set_status("请在弹窗中选择一个歌词文件")
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=12, pady=12)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="确认关联", command=confirm).pack(side="right", padx=6)
        tree.bind("<Double-Button-1>", lambda _event: confirm())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.wait_window(dialog)
        return result[0] if result else None

    def _move_files_to_target(self, source_dir: Path, target_dir: Path, file_names: list[str], target_ref: str,
                              *, target_metadata: dict[str, Any] | None = None) -> None:
        """Move lyrics on disk and update both directory metadata files atomically where possible."""
        if source_dir == target_dir:
            raise ValueError("源目录与目标目录相同；请使用关联歌词")
        source_before = self.adapter.load(source_dir)
        target_before = self.adapter.load(target_dir)
        target_working = target_metadata if target_metadata is not None else target_before
        source_after, target_after = move_file_records_to_target(source_before, target_working, file_names, target_ref)
        moves = [(source_dir / name, target_dir / name) for name in file_names]
        for original, destination in moves:
            if not original.is_file():
                raise FileNotFoundError(f"找不到源歌词文件：{original.name}")
            if destination.exists():
                raise FileExistsError(f"目标目录已有同名文件：{destination.name}")
        moved: list[tuple[Path, Path, bool]] = []
        try:
            for original, destination in moves:
                git_marked = self.git.is_tracked(original)
                if git_marked:
                    self.git.move(original, destination)
                else:
                    original.rename(destination)
                moved.append((original, destination, git_marked))
            self.adapter.save(target_dir, target_after)
            self.adapter.save(source_dir, source_after)
            staged = sum(1 for _, _, git_marked in moved if git_marked)
            suffix = f"；Git 已标记 {staged} 个重命名" if staged else ""
            self._set_status(f"已移动 {len(moved)} 个歌词文件到 {target_dir.relative_to(self.repo / 'Lyrics')}{suffix}")
        except Exception:
            for original, destination, git_marked in reversed(moved):
                if destination.exists() and not original.exists():
                    try:
                        if git_marked:
                            self.git.move(destination, original)
                        else:
                            destination.rename(original)
                    except OSError:
                        pass
            # Metadata writes are atomic.  Restore both copies if either write failed.
            try:
                self.adapter.save(target_dir, target_before)
                self.adapter.save(source_dir, source_before)
            except Exception:
                pass
            raise

    def _begin_tree_drag(self, event: tk.Event) -> None:
        item = self.registry_tree.identify_row(event.y)
        node = self.tree_nodes.get(item)
        self._drag_source_item = item if node and node.kind in {"file", "track"} else None
        self._tree_drag_active = False

    def _update_tree_drag_cursor(self, _event: tk.Event) -> None:
        if self._drag_source_item and not self._tree_drag_active:
            self._tree_drag_active = True
            self.registry_tree.configure(cursor="hand2")

    def _finish_tree_drag(self, event: tk.Event) -> None:
        source_item = self._drag_source_item
        self._drag_source_item = None
        self._tree_drag_active = False
        self.registry_tree.configure(cursor="")
        if not source_item:
            return
        target_item = self.registry_tree.identify_row(event.y)
        if not target_item or target_item == source_item:
            return
        source = self.tree_nodes.get(source_item)
        target = self.tree_nodes.get(target_item)
        if not source or not target or target.kind not in {"file", "track"}:
            return
        if not source.directory or not target.directory:
            return
        if source.kind == "file":
            file_names = [str(source.file_name)] if source.file_name else []
        else:
            file_names = [str(child.file_name) for child in source.children if child.kind == "file" and child.file_name]
        if not file_names:
            self._set_status("拖拽的曲目实体没有可处理的歌词文件")
            return
        target_dir, target_ref = target.directory, target.metadata_ref
        if target.kind == "file" and target.linked_target_path and target.linked_target_ref:
            target_dir = (self.repo / "Lyrics" / target.linked_target_path).resolve()
            target_ref = target.linked_target_ref
        if not target_ref:
            self._set_status("请拖放到一个已绑定曲目实体的歌词或曲目节点")
            return
        if source.directory == target_dir and source.metadata_ref == target_ref:
            self._set_status("源与目标已经是同一曲目实体")
            return
        action = self._ask_drop_action(len(file_names), target_dir, target_ref)
        if action == "cancel":
            return
        try:
            if action == "link":
                source_metadata = self.adapter.load(source.directory)
                updated = source_metadata
                for file_name in file_names:
                    if source.directory == target_dir:
                        updated = link_file_record(updated, file_name, target_ref)
                    else:
                        relative = target_dir.relative_to(self.repo / "Lyrics").as_posix()
                        updated = link_file_to_external_target(updated, file_name, relative, target_ref)
                self.adapter.save(source.directory, updated)
                old_refs = {
                    str(item.get("linkedFrom") or item.get("metadataRef") or "")
                    for item in source_metadata.get("files", [])
                    if isinstance(item, dict) and item.get("name") in file_names
                }
                for old_ref in sorted(old_refs):
                    self._prompt_remove_empty_track(source.directory, old_ref)
                self._set_status(f"已关联 {len(file_names)} 个歌词到 {target_ref}")
            elif action == "move":
                self._move_files_to_target(source.directory, target_dir, file_names, target_ref)
            self.refresh_library()
        except Exception as exc:
            self._show_error("拖拽操作失败", exc)

    def _ask_drop_action(self, file_count: int, target_dir: Path, target_ref: str) -> str:
        """Ask whether a drop means metadata linking, physical move, or cancellation."""
        dialog = tk.Toplevel(self)
        dialog.title("处理拖放的歌词")
        dialog.transient(self)
        dialog.grab_set()
        relative = target_dir.relative_to(self.repo / "Lyrics")
        ttk.Label(dialog, text=f"将 {file_count} 个歌词拖放到 {relative} / {target_ref}").pack(padx=16, pady=(16, 8))
        ttk.Label(dialog, text="请选择操作：关联只共享元数据；移动会更改文件所在目录。").pack(padx=16, pady=(0, 12))
        result = ["cancel"]
        def choose(action: str) -> None:
            result[0] = action
            dialog.destroy()
        controls = ttk.Frame(dialog)
        controls.pack(fill="x", padx=16, pady=(0, 16))
        ttk.Button(controls, text="取消", command=lambda: choose("cancel")).pack(side="right")
        ttk.Button(controls, text="移动歌词", command=lambda: choose("move")).pack(side="right", padx=6)
        ttk.Button(controls, text="关联歌词", command=lambda: choose("link")).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))
        self.wait_window(dialog)
        return result[0]

    def _external_link_dependents(self, target_dir: Path, target_ref: str) -> list[tuple[Path, str]]:
        relative = target_dir.relative_to(self.repo / "Lyrics").as_posix()
        dependents: list[tuple[Path, str]] = []
        for directory in self.track_dirs:
            metadata = self.adapter.load(directory)
            for item in metadata.get("files", []):
                target = item.get("linkedTarget") if isinstance(item, dict) else None
                if isinstance(target, dict) and target.get("path") == relative and target.get("metadataRef") == target_ref:
                    dependents.append((directory, str(item.get("name") or "")))
        return [(directory, name) for directory, name in dependents if name]

    def _prompt_remove_empty_track(self, directory: Path, metadata_ref: str) -> None:
        """Offer to remove a virtual entity once linking leaves it with no local lyrics."""
        if not metadata_ref:
            return
        metadata = self.adapter.load(directory)
        if metadata_ref not in metadata.get("tracks", {}):
            return
        has_local_files = any(
            isinstance(item, dict) and item.get("metadataRef") == metadata_ref and not isinstance(item.get("linkedTarget"), dict)
            for item in metadata.get("files", [])
        )
        if not has_local_files and messagebox.askyesno(
            "删除空曲目实体", f"关联后曲目实体“{metadata_ref}”已不再包含本地歌词。是否删除该曲目实体？",
            parent=self,
        ):
            self._delete_track_entity(directory, metadata_ref)

    def _delete_track_entity(self, directory: Path, metadata_ref: str) -> None:
        metadata = self.adapter.load(directory)
        dependents = self._external_link_dependents(directory, metadata_ref)
        updated = remove_track_record(metadata, metadata_ref)
        self.adapter.save(directory, updated)
        for dependent_dir, file_name in dependents:
            if dependent_dir == directory:
                continue
            dependent = self.adapter.load(dependent_dir)
            self.adapter.save(dependent_dir, detach_external_link_record(dependent, file_name))
        suffix = f"；已解除 {len(dependents)} 个外部关联" if dependents else ""
        self._set_status(f"已删除曲目实体 {metadata_ref}，歌词保留为未绑定状态{suffix}")

    def delete_selected_node(self, _event: object = None) -> None:
        """Delete the currently selected registry node with type-appropriate safeguards."""
        selection = self.registry_tree.selection()
        node = self.tree_nodes.get(selection[0]) if selection else None
        if not node:
            self._set_status("请先在左侧选择要删除的节点")
            return
        if node.kind == "file":
            self.delete_selected_file()
            return
        if node.kind == "track":
            if not node.directory or not node.metadata_ref:
                file_names = [str(child.file_name) for child in node.children if child.kind == "file" and child.file_name]
                if not node.directory or not file_names:
                    self._set_status("该未绑定节点没有可删除的歌词文件")
                    return
                if not messagebox.askyesno(
                    "删除未绑定歌词", f"“{node.label}”不是曲目实体。是否删除其下的 {len(file_names)} 个歌词文件？",
                    icon="warning", parent=self,
                ):
                    self._set_status("已取消删除")
                    return
                try:
                    before = self.adapter.load(node.directory)
                    after = before
                    for file_name in file_names:
                        after = remove_file_record(after, file_name)
                    paths = [node.directory / file_name for file_name in file_names]
                    if not all(path.is_file() for path in paths):
                        raise FileNotFoundError("部分待删除歌词文件已经不存在")
                    self.adapter.save(node.directory, after)
                    try:
                        for path in paths:
                            path.unlink()
                    except Exception:
                        self.adapter.save(node.directory, before)
                        raise
                    self._set_status(f"已删除 {len(paths)} 个未绑定歌词文件")
                    self.refresh_library()
                except Exception as exc:
                    self._show_error("删除未绑定歌词失败", exc)
                return
            dependents = self._external_link_dependents(node.directory, node.metadata_ref)
            extra = f"\n并解除 {len(dependents)} 个外部关联。" if dependents else ""
            if messagebox.askyesno(
                "删除曲目实体", f"删除曲目实体“{node.label}”？\n歌词文件会保留并变为未绑定状态。{extra}",
                icon="warning", parent=self,
            ):
                try:
                    self._delete_track_entity(node.directory, node.metadata_ref)
                    self.refresh_library()
                except Exception as exc:
                    self._show_error("删除曲目实体失败", exc)
            return
        if node.kind not in {"root", "artist", "directory"}:
            return
        lyrics_root = (self.repo / "Lyrics").resolve()
        target = lyrics_root if node.kind == "root" else (node.directory or (lyrics_root / node.label)).resolve()
        try:
            target.relative_to(lyrics_root)
        except ValueError:
            self._show_error("删除失败", ValueError("拒绝删除歌词库以外的路径"))
            return
        if not target.exists():
            self._set_status("目标目录已经不存在")
            return
        description = "整个歌词库内容" if node.kind == "root" else str(target.relative_to(lyrics_root))
        if not messagebox.askyesno(
            "确认删除目录", f"确定删除“{description}”及其所有歌词、元数据吗？\n此操作不可撤销。",
            icon="warning", parent=self,
        ):
            self._set_status("已取消删除")
            return
        try:
            if node.kind == "root":
                for child in list(target.iterdir()):
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            else:
                shutil.rmtree(target)
            self.current_file_path = None
            self.current_dir = None
            self.selected_file_var.set("")
            self._set_status(f"已删除 {description}")
            self.refresh_library()
        except Exception as exc:
            self._show_error("删除目录失败", exc)

    def split_selected_file(self) -> None:
        """Clone the selected entity and bind only the selected file to it."""
        path = self._selected_file()
        if not path or not self.current_dir:
            self._set_status("请先从左侧树选择歌词文件")
            return
        if not self.current_ref:
            self._set_status("请先在曲目实体下拉框选择复制来源")
            return
        tracks = self.current_metadata.get("tracks", {})
        source = tracks.get(self.current_ref)
        if not isinstance(source, dict):
            self._set_status("无法拆分：当前曲目实体不存在")
            return
        base = f"{self.current_ref}-{path.stem}".casefold()
        safe = "-".join(part for part in re.split(r"[^a-z0-9]+", base) if part) or "track"
        new_ref = safe
        ordinal = 2
        while new_ref in tracks:
            new_ref = f"{safe}-{ordinal}"
            ordinal += 1
        if not messagebox.askyesno("确认拆分", f"复制 {self.current_ref} 为 {new_ref}，并仅重新绑定 {path.name}？", parent=self):
            return
        updated = split_file_record(self.current_metadata, path.name, self.current_ref, new_ref)
        try:
            self.adapter.save(self.current_dir, updated)
            self._set_status(f"已创建独立曲目实体 {new_ref}")
            self.refresh_library()
        except Exception as exc:
            self._show_error("拆分失败", exc)

    def _selected_file(self) -> Path | None:
        selected = self.registry_tree.selection()
        node = self.tree_nodes.get(selected[0]) if selected else None
        if node and node.kind == "file" and node.directory and node.file_name:
            path = node.directory / node.file_name
            if self.current_file_path and self.current_file_path.parent == path.parent:
                return self.current_file_path
            return path
        return None

    def open_selected_file(self) -> None:
        path = self._selected_file()
        if not path:
            self._set_status("请先选择文件")
            return
        try:
            _open_path(path)
        except Exception as exc:
            self._show_error("打开失败", exc)

    def reveal_selected_file(self) -> None:
        path = self._selected_file()
        if not path:
            self._set_status("请先选择文件")
            return
        try:
            _reveal_path(path)
        except Exception as exc:
            self._show_error("打开文件管理器失败", exc)

    def delete_selected_file(self) -> None:
        """Delete the selected lyric and remove its per-directory metadata record."""
        path = self._selected_file()
        if not path:
            self._set_status("请先选择要删除的歌词文件")
            return
        if not messagebox.askyesno(
            "确认删除", f"确定永久删除“{path.name}”吗？\n\n其在 lyrics.metadata 中的关联也会一并解除。",
            icon="warning", parent=self,
        ):
            self._set_status("已取消删除")
            return
        directory = path.parent
        try:
            before = self.adapter.load(directory)
            after = remove_file_record(before, path.name)
            # Persist the unbinding first; if deleting the file fails, restore it.
            self.adapter.save(directory, after)
            try:
                path.unlink()
            except Exception:
                self.adapter.save(directory, before)
                raise
            self.current_file_path = None
            self.selected_file_var.set("")
            self._set_status(f"已删除 {path.name}，并解除其元数据关联")
            self.refresh_library()
        except Exception as exc:
            self._show_error("删除失败", exc)

    def convert_selected_ttml(self) -> None:
        path = self._selected_file()
        if not path or path.suffix.casefold() != ".ttml":
            self._set_status("请先选择 TTML 文件")
            return
        backup = path.with_name(path.name + ".bak")
        if backup.is_file():
            if not messagebox.askyesno("恢复备份", f"用 {backup.name} 恢复当前 TTML？", parent=self):
                return
            try:
                self.adapter.restore_ttml_backup(path)
                self._set_status("已恢复原文件并移除备份")
                selected = self.registry_tree.selection()
                if selected:
                    self.registry_tree.item(selected[0], tags=("legacy",))
                self.legacy_notice.configure(text="检测到 body 内旧式翻译/音译，建议先转换。")
                self.convert_ttml_button.configure(text="一键转换旧 TTML")
                self.preview_selected_file()
            except Exception as exc:
                self._show_error("恢复失败", exc)
            return
        if not messagebox.askyesno("转换旧 TTML", "将 body 内旧式翻译/音译迁移到 head。是否继续？", parent=self):
            return
        try:
            result = self.adapter.convert_legacy_ttml(path)
            warnings = "\n".join(result.get("warnings", []))
            message = "转换完成" if result.get("changed") else "未发现需要转换的旧格式"
            if warnings:
                message += "\n\n警告：\n" + warnings
            if result.get("backup"):
                message += f"\n\n已备份：{Path(result['backup']).name}"
            self._set_status(message.replace("\n", " "))
            if result.get("changed"):
                selected = self.registry_tree.selection()
                if selected:
                    self.registry_tree.item(selected[0], tags=())
                self.legacy_notice.configure(text="")
                self.convert_ttml_button.configure(text="恢复备份")
            self.preview_selected_file()
        except Exception as exc:
            self._show_error("转换失败", exc)

    def preview_selected_file(self) -> None:
        path = self._selected_file()
        if not path:
            self._set_status("请先选择文件")
            return
        if path.suffix.casefold() == ".ass":
            self._set_status("ASS 暂不提供逐行预览")
            return
        try:
            self.preview_tree.delete(*self.preview_tree.get_children())
            for row in self.adapter.preview(path):
                self.preview_tree.insert("", "end", values=row)
        except Exception as exc:
            self._show_error("预览失败", exc)

    def _set_git_output(self, text: str) -> None:
        self.git_output.configure(state="normal")
        self.git_output.delete("1.0", "end")
        self.git_output.insert("1.0", text or "（无输出）")
        self.git_output.configure(state="disabled")

    def _run_git_task(self, label: str, action: Any, on_success: Any) -> bool:
        """Run Git away from Tk; marshal every UI callback onto Tk's thread."""
        if self._git_busy:
            return False
        if self.git is None:
            detail = str(self.git_init_error or "")
            self._set_status("Git 不可用：请到「设置」页指定 git 可执行文件")
            messagebox.showwarning(
                "Git 不可用",
                "未找到可用的 git，无法执行版本管理操作。\n\n"
                "请到「设置」页的 Git 组指定 git 可执行文件路径后重试。"
                + (f"\n\n原因：{detail}" if detail else ""),
                parent=self,
            )
            return False
        self._git_busy = True
        self.git_busy_label.configure(text=f"{label}…")
        self._set_status(f"Git：{label}…")
        previous_states = [str(button.cget("state")) for button in self._git_buttons]
        for button in self._git_buttons:
            button.configure(state="disabled")

        def finish(result: Any = None, error: Exception | None = None) -> None:
            self._git_busy = False
            self.git_busy_label.configure(text="")
            for button, state in zip(self._git_buttons, previous_states):
                button.configure(state=state)
            if error is not None:
                self._set_status(f"Git {label}失败：{error}")
                self._show_error("Git 操作失败", error)
            else:
                on_success(result)
                self._set_status(f"Git：{label}完成")

        def worker() -> None:
            try:
                result = action()
            except Exception as exc:
                self._git_results.put((None, exc))
            else:
                self._git_results.put((result, None))

        def poll_result() -> None:
            try:
                result, error = self._git_results.get_nowait()
            except queue.Empty:
                self.after(30, poll_result)
            else:
                finish(result, error)

        threading.Thread(target=worker, name=f"git-{label}", daemon=True).start()
        self.after(30, poll_result)
        return True

    def _git_snapshot(self) -> tuple[Any, str]:
        self.git.refresh_index()
        return self.git.status_entries(), self.git.current_branch()

    def _apply_git_snapshot(self, payload: tuple[Any, str]) -> None:
        entries, branch = payload
        self.git_repository_label.configure(text=f"{self.git.repository_name()}  ·  {branch}")
        self.git_changes.delete(*self.git_changes.get_children())
        fresh_paths: set[str] = set()
        for entry in entries:
            staged = entry.index_status if entry.staged else "—"
            unstaged = entry.worktree_status if entry.unstaged else "—"
            checked = self._commit_include.get(entry.path, True)
            fresh_paths.add(entry.path)
            self.git_changes.insert("", "end", text=entry.path,
                                    values=(self._check_mark(checked), staged, unstaged))
        # Drop stale include choices for files no longer in the change set.
        for path in list(self._commit_include):
            if path not in fresh_paths:
                self._commit_include.pop(path, None)
        self.git_change_count.configure(text=f"变更（{len(entries)}）")
        any_checked = any(self._commit_include.get(e.path, True) for e in entries)
        self.git_commit_button.configure(state="normal" if any_checked else "disabled")
        if not entries:
            self.git_diff_label.configure(text="没有本地变更")
            self._set_git_output("工作区干净")

    @staticmethod
    def _check_mark(checked: bool) -> str:
        return "☑" if checked else "☐"

    def _set_change_checked(self, item: str, checked: bool) -> None:
        path = self.git_changes.item(item, "text")
        self._commit_include[path] = checked
        self.git_changes.set(item, "include", self._check_mark(checked))
        self._update_commit_button_state()

    def _update_commit_button_state(self) -> None:
        any_checked = any(
            self._commit_include.get(self.git_changes.item(item, "text"), True)
            for item in self.git_changes.get_children()
        )
        self.git_commit_button.configure(state="normal" if any_checked else "disabled")

    def _toggle_change_include(self, event: tk.Event) -> None:
        region = self.git_changes.identify_region(event.x, event.y)
        if region != "cell":
            return
        column = self.git_changes.identify_column(event.x)
        if column != "#1":  # the "暂存" checkbox column
            return
        item = self.git_changes.identify_row(event.y)
        if not item:
            return
        path = self.git_changes.item(item, "text")
        self._set_change_checked(item, not self._commit_include.get(path, True))

    def _check_all_changes(self) -> None:
        for item in self.git_changes.get_children():
            self._commit_include[self.git_changes.item(item, "text")] = True
            self.git_changes.set(item, "include", self._check_mark(True))
        self._update_commit_button_state()

    def _uncheck_all_changes(self) -> None:
        for item in self.git_changes.get_children():
            self._commit_include[self.git_changes.item(item, "text")] = False
            self.git_changes.set(item, "include", self._check_mark(False))
        self._update_commit_button_state()

    def _git_paths(self) -> list[str]:
        return [self.git_changes.item(item, "text") for item in self.git_changes.selection()]

    def git_status(self) -> None:
        if self.git is None:
            self._set_status("Git 不可用：请到「设置」页的 Git 组指定 git 可执行文件")
            self.git_repository_label.configure(text="Git 不可用")
            self.github_state_label.configure(text="Git 不可用")
            return
        self._run_git_task("刷新", self._git_snapshot, self._apply_git_snapshot)

    def _git_show_selected_diff(self, _event: object = None) -> None:
        paths = self._git_paths()
        if not paths:
            return
        def load_diff() -> tuple[str, str]:
            return self.git.diff(paths), self.git.diff(paths, staged=True)
        def show_diff(result: tuple[str, str]) -> None:
            unstaged, staged = result
            sections = []
            if staged:
                sections.append("=== 已暂存 ===\n" + staged)
            if unstaged:
                sections.append("=== 未暂存 ===\n" + unstaged)
            self.git_diff_label.configure(text="、".join(paths))
            self._set_git_output("\n".join(sections) or "该文件没有可显示的文本差异")
        self._run_git_task("读取差异", load_diff, show_diff)

    def git_diff(self) -> None: self._git_show_selected_diff()
    def git_log(self) -> None:
        def show(entries: Any) -> None:
            self.git_history.delete(*self.git_history.get_children())
            for entry in entries:
                self.git_history.insert(
                    "", "end", text=entry.subject,
                    values=(entry.date, entry.author, entry.short_commit),
                )
        self._run_git_task("刷新历史", self.git.log_entries, show)
    def git_stage(self) -> None:
        paths = self._git_paths()
        if paths and messagebox.askyesno("确认暂存", "暂存所选文件？", parent=self):
            self._run_git_task("暂存", lambda: (self.git.stage(paths), self._git_snapshot()),
                               lambda result: (self._set_git_output(result[0]), self._apply_git_snapshot(result[1])))
    def git_unstage(self) -> None:
        paths = self._git_paths()
        if paths and messagebox.askyesno("确认取消暂存", "取消暂存所选文件？", parent=self):
            self._run_git_task("取消暂存", lambda: (self.git.unstage(paths), self._git_snapshot()),
                               lambda result: (self._set_git_output(result[0]), self._apply_git_snapshot(result[1])))
    def _checked_paths(self) -> list[str]:
        """Paths whose "暂存" checkbox is on, in list order."""
        paths: list[str] = []
        for item in self.git_changes.get_children():
            path = self.git_changes.item(item, "text")
            if self._commit_include.get(path, True):
                paths.append(path)
        return paths

    def git_commit(self) -> None:
        message = self.git_commit_summary.get().strip()
        if not message:
            self._set_status("请填写提交摘要")
            return
        description = self.git_commit_description.get("1.0", "end-1c")
        checked = self._checked_paths()
        if not checked:
            self._set_status("未勾选任何变更文件，无法提交")
            messagebox.showinfo("没有勾选文件", "请先勾选要提交的文件（☑），再点击「提交」。", parent=self)
            return
        if messagebox.askyesno(
            "确认提交",
            f"将暂存并提交 {len(checked)} 个勾选文件：\n{message}",
            parent=self,
        ):
            def task() -> Any:
                stage_output = self.git.stage(checked)
                commit_output = self.git.commit(message, description)
                return (stage_output + commit_output, self._git_snapshot(), self.git.log_entries())

            def done(result: Any) -> None:
                output, snapshot, history = result
                self._set_git_output(output)
                self._apply_git_snapshot(snapshot)
                self.git_commit_summary.delete(0, "end")
                self.git_commit_description.delete("1.0", "end")
                self.git_history.delete(*self.git_history.get_children())
                for entry in history:
                    self.git_history.insert("", "end", text=entry.subject,
                                            values=(entry.date, entry.author, entry.short_commit))
            self._run_git_task("提交", task, done)
    def git_fetch(self) -> None:
        if messagebox.askyesno("确认 Fetch", "从已配置远端获取最新对象和引用？", parent=self):
            self._run_git_task("Fetch", lambda: (self.git.fetch(), self._git_snapshot()),
                               lambda result: (self._set_git_output(result[0]), self._apply_git_snapshot(result[1])))
    def git_pull(self) -> None:
        if messagebox.askyesno("确认拉取", "执行 git pull --ff-only？", parent=self):
            self._run_git_task("Pull", lambda: (self.git.pull_ff_only(), self._git_snapshot()),
                               lambda result: (self._set_git_output(result[0]), self._apply_git_snapshot(result[1])))
    def git_push(self) -> None:
        if not messagebox.askyesno(
            "推荐使用 Pull Request",
            "建议改用「创建 PR」将变更提交到分支并通过拉取请求合并，\n"
            "以便审阅与讨论。\n\n"
            "仍要直接推送当前分支到远端吗？",
            parent=self,
        ):
            return
        if messagebox.askyesno("确认推送", "将当前分支推送到已配置远端？", parent=self):
            self._run_git_task("Push", self.git.push, self._set_git_output)

    def git_create_pr(self) -> None:
        """Guide the user toward opening a pull request for the current work."""
        if self._git_busy:
            return
        self._ensure_gh()
        if not messagebox.askyesno(
            "创建 Pull Request",
            "这将把当前分支推送到远端并立即开启一个 Pull Request。\n\n"
            "建议先提交所有变更（暂存 + 提交），再继续。是否继续？",
            parent=self,
        ):
            return
        self._open_pr_dialog()

    def _ensure_gh(self) -> None:
        """Surface an actionable error when gh is missing or not authenticated."""
        if not self.github.is_available():
            messagebox.showwarning(
                "需要 GitHub CLI",
                "未检测到 GitHub CLI（gh）。\n\n"
                "请到 https://cli.github.com 安装后重试。\n"
                "安装完成后可在「拉取请求」页点击「登录 GitHub」。",
                parent=self,
            )
            raise GitHubError("未安装 gh")
        status = self.github.auth_status()
        if not status.get("authenticated"):
            if messagebox.askyesno(
                "需要登录 GitHub",
                "当前 gh 尚未登录 GitHub。是否现在登录？\n\n"
                "（将打开浏览器进行授权）",
                parent=self,
            ):
                self.main_notebook.select(self.pulls_tab)
                self.github_login()
            raise GitHubError("未登录 GitHub")

    def _open_pr_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("创建 Pull Request")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("520x340")
        dialog.configure(background=self.theme.palette["background"])
        dialog.columnconfigure(0, weight=1)

        result: dict[str, str] = {}

        def collect() -> None:
            result["title"] = title_entry.get().strip()
            result["body"] = body_text.get("1.0", "end-1c").strip()
            dialog.destroy()

        def cancel() -> None:
            result.clear()
            dialog.destroy()

        base = ""
        try:
            base = self.github.repository_info().default_branch
        except GitHubError:
            base = ""

        ttk.Label(dialog, text="PR 标题（必填）").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 2))
        title_entry = ttk.Entry(dialog)
        title_entry.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))
        ttk.Label(dialog, text="描述（可选，留空则使用提交摘要）").grid(row=2, column=0, sticky="w", padx=12, pady=(0, 2))
        body_text = tk.Text(dialog, height=6, wrap="word",
                            bg=self.theme.text_bg, fg=self.theme.text_fg,
                            insertbackground=self.theme.text_fg)
        body_text.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 6))
        ttk.Label(dialog, text=f"基准分支：{base or '（默认分支）'}").grid(row=4, column=0, sticky="w", padx=12, pady=(0, 6))

        buttons = ttk.Frame(dialog)
        buttons.grid(row=5, column=0, sticky="e", padx=12, pady=12)
        ttk.Button(buttons, text="创建并推送", command=collect).pack(side="left", padx=4)
        ttk.Button(buttons, text="取消", command=cancel).pack(side="left", padx=4)

        self.wait_window(dialog)
        if not result or not result["title"]:
            return

        # Resolve the base/current branch and decide the workflow.
        base_default = base or ""
        current = ""
        try:
            current = self.git.current_branch()
        except Exception as exc:
            self._show_error("读取当前分支失败", exc)
            return
        try:
            entries = self.git.status_entries()
        except Exception:
            entries = []
        has_changes = bool(entries)
        same_as_base = bool(base_default and current == base_default and "分离" not in current)

        if same_as_base:
            if not messagebox.askyesno(
                "当前在默认分支上",
                f"你当前在默认分支 {base_default} 上，无法直接对其开 PR。\n\n"
                "是否自动创建临时分支，把变更提交上去，推送并开 PR，"
                f"再切回 {base_default}？",
                parent=self,
            ):
                self._set_status("已取消创建 PR")
                return
            temp_branch = self._temp_branch_name(result["title"], base_default)
        else:
            temp_branch = ""

        def do_create() -> Any:
            git = self.git
            if same_as_base:
                original = git.current_branch()
                git.switch_create(temp_branch)
                if has_changes:
                    git.stage_all()
                    git.commit(result["title"], result["body"] or "Changes for PR")
                push_output = git.push_upstream(temp_branch)
                created = self.github.create(
                    result["title"], body=result["body"], base=base_default,
                )
                try:
                    git.switch(original)
                except Exception:
                    pass
                created["temp_branch"] = temp_branch
                created["original_branch"] = original
                created["push_output"] = push_output
                return created
            return self.github.create(
                result["title"], body=result["body"], base=base_default or None, push=True,
            )

        def on_created(payload: Any) -> None:
            url = payload.get("url", "")
            number = payload.get("number", 0)
            temp = payload.get("temp_branch", "")
            output = payload.get("output", "")
            if temp:
                output = (output + f"\n\n（变更已提交到临时分支 {temp} 并推送）").strip()
            self._set_git_output(output)
            note = f"（临时分支 {temp}）" if temp else ""
            self._set_status(f"PR 已创建：{url} {note}")
            self._after_pr_created(number, url)

        self._run_git_task("创建 PR", do_create, on_created)

    def _temp_branch_name(self, title: str, base: str) -> str:
        slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", title).strip("-").lower() or "pr"
        slug = slug[:40].strip("-") or "pr"
        return f"pr/{slug}-{int(time.time())}"

    def _after_pr_created(self, number: int, url: str) -> None:
        """Offer immediate merge when the actor is the repository owner."""
        show_url = url or (f"见 #{number}" if number else "（无 URL）")
        if not self.github.is_owner():
            messagebox.showinfo(
                "Pull Request 已创建",
                f"{show_url}\n\n可在「拉取请求」页查看进度、审阅与合并。",
                parent=self,
            )
            return
        if self.offer_merge_var.get() and messagebox.askyesno(
            "立即合并",
            f"PR {show_url} 已创建。\n\n"
            "你拥有该仓库，是否立即以 squash 方式合并此 PR？",
            parent=self,
        ):
            self._merge_pr(number)
            return
        messagebox.showinfo(
            "Pull Request 已创建",
            f"{show_url}\n\n可在「拉取请求」页查看进度、审阅与合并。",
            parent=self,
        )

    def _merge_pr(self, number: int) -> None:
        def do_merge() -> Any:
            return self.github.merge(number, method="squash")

        def on_merged(result: Any) -> None:
            self._set_git_output(result.get("output", "已合并"))
            self._set_status(f"PR #{number} 已合并")
            messagebox.showinfo("合并完成", f"PR #{number} 已以 squash 方式合并。", parent=self)

        self._run_git_task("合并 PR", do_merge, on_merged)

    # --- Pull request page ----------------------------------------------

    def _build_pulls(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        header = ttk.Frame(parent)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)

        self.github_state_label = ttk.Label(header, text="未连接 GitHub")
        self.github_state_label.grid(row=0, column=0, sticky="w")
        self.github_busy_label = ttk.Label(header, text="")
        self.github_busy_label.grid(row=0, column=1, padx=6)

        for column, (label, command) in enumerate((
            ("登录 GitHub", self.github_login), ("退出登录", self.github_logout),
            ("诊断 GitHub", self.github_diagnose), ("刷新", self.github_refresh_prs),
        ), start=2):
            button = ttk.Button(header, text=label, command=command)
            button.grid(row=0, column=column, padx=3)
            self._github_buttons.append(button)

        body = ttk.Panedwindow(parent, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew")

        left = ttk.Frame(body)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        body.add(left, weight=1)
        self.github_state_selector = ttk.Combobox(
            left, state="readonly", values=("open", "closed", "all"),
        )
        self.github_state_selector.set("open")
        self.github_state_selector.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.github_state_selector.bind("<<ComboboxSelected>>", lambda _e: self.github_refresh_prs())
        self.github_pr_list = ttk.Treeview(left, columns=("num", "author", "base", "head"), show="tree headings")
        self.github_pr_list.heading("#0", text="标题")
        self.github_pr_list.heading("num", text="#")
        self.github_pr_list.heading("author", text="作者")
        self.github_pr_list.heading("base", text="基准")
        self.github_pr_list.heading("head", text="来源")
        self.github_pr_list.column("#0", width=320)
        self.github_pr_list.column("num", width=55, anchor="center", stretch=False)
        self.github_pr_list.column("author", width=110, stretch=False)
        self.github_pr_list.column("base", width=100, stretch=False)
        self.github_pr_list.column("head", width=100, stretch=False)
        self.github_pr_list.grid(row=1, column=0, sticky="nsew")
        self.github_pr_list.bind("<<TreeviewSelect>>", self._github_show_selected_pr)

        pr_scroll = ttk.Scrollbar(left, orient="vertical", command=self.github_pr_list.yview)
        pr_scroll.grid(row=1, column=1, sticky="ns")
        self.github_pr_list.configure(yscrollcommand=pr_scroll.set)

        right = ttk.Frame(body)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        body.add(right, weight=2)

        self.github_pr_title = ttk.Label(right, text="选择一个 PR 查看详情", font=("TkDefaultFont", 11, "bold"))
        self.github_pr_title.grid(row=0, column=0, sticky="w", pady=(0, 4))

        detail_nb = ttk.Notebook(right)
        detail_nb.grid(row=1, column=0, sticky="nsew")
        overview = ttk.Frame(detail_nb, padding=6)
        changes = ttk.Frame(detail_nb, padding=6)
        review = ttk.Frame(detail_nb, padding=6)
        detail_nb.add(overview, text="概览")
        detail_nb.add(changes, text="变更")
        detail_nb.add(review, text="审阅")

        for frame in (overview, changes, review):
            frame.rowconfigure(1, weight=1)
            frame.columnconfigure(0, weight=1)

        self.github_pr_overview = tk.Text(overview, wrap="word", state="disabled",
                                          bg=self.theme.text_bg, fg=self.theme.text_fg)
        self.github_pr_overview.grid(row=1, column=0, sticky="nsew")
        self.github_pr_diff = tk.Text(changes, wrap="none", state="disabled",
                                      bg=self.theme.text_bg, fg=self.theme.text_fg,
                                      font=("TkFixedFont", 10))
        self.github_pr_diff.grid(row=1, column=0, sticky="nsew")
        self.github_pr_review = tk.Text(review, wrap="word", state="disabled",
                                        bg=self.theme.text_bg, fg=self.theme.text_fg)
        self.github_pr_review.grid(row=1, column=0, sticky="nsew")
        for widget in (self.github_pr_overview, self.github_pr_diff, self.github_pr_review):
            self._track_theme_text(widget)

        action_bar = ttk.Frame(right)
        action_bar.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(action_bar, text="在浏览器打开",
                   command=self._github_open_pr_url).pack(side="left", padx=(0, 5))
        self.github_merge_button = ttk.Button(action_bar, text="合并（squash）",
                                              command=self._github_merge_selected, state="disabled")
        self.github_merge_button.pack(side="left", padx=(0, 5))
        self._github_buttons.append(self.github_merge_button)

        self._set_text(self.github_pr_overview, "请先登录 GitHub 并点击“刷新”加载 PR。")

    def _run_github_task(self, label: str, action: Any, on_success: Any) -> bool:
        """Run a GitHub operation off the Tk thread, mirroring :meth:`_run_git_task`."""
        if self._github_busy:
            return False
        self._github_busy = True
        self.github_busy_label.configure(text=f"{label}…")
        self._set_status(f"GitHub：{label}…")
        previous_states = [str(button.cget("state")) for button in self._github_buttons]
        for button in self._github_buttons:
            button.configure(state="disabled")

        def finish(result: Any = None, error: Exception | None = None) -> None:
            self._github_busy = False
            self.github_busy_label.configure(text="")
            for button, state in zip(self._github_buttons, previous_states):
                button.configure(state=state)
            if error is not None:
                self._set_status(f"GitHub {label}失败：{error}")
                self._show_error("GitHub 操作失败", error)
            else:
                on_success(result)
                self._set_status(f"GitHub：{label}完成")

        def worker() -> None:
            try:
                result = action()
            except Exception as exc:
                self._github_results.put((None, exc))
            else:
                self._github_results.put((result, None))

        def poll_result() -> None:
            try:
                result, error = self._github_results.get_nowait()
            except queue.Empty:
                self.after(30, poll_result)
            else:
                finish(result, error)

        threading.Thread(target=worker, name=f"github-{label}", daemon=True).start()
        self.after(30, poll_result)
        return True

    def _set_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text or "（无内容）")
        widget.configure(state="disabled")

    def _refresh_github_state(self) -> None:
        """Update the login/owner label and (if usable) the PR list."""
        if self._github_busy:
            return
        if not self.github.is_available():
            self.github_state_label.configure(text="未检测到 GitHub CLI（gh）")
            return

        def do_state() -> Any:
            status = self.github.auth_status()
            owner = False
            login = status.get("login", "")
            if status.get("authenticated"):
                try:
                    owner = self.github.is_owner()
                except GitHubError:
                    owner = False
            return {"authenticated": status.get("authenticated", False),
                    "login": login, "owner": owner}

        def on_state(state: dict[str, Any]) -> None:
            self._gh_user_login = state.get("login", "")
            self._gh_is_owner = state.get("owner", False)
            if state.get("authenticated"):
                who = self._gh_user_login or "已登录"
                owner_tag = "（仓库所有者）" if self._gh_is_owner else ""
                self.github_state_label.configure(text=f"已登录：{who}{owner_tag}")
                self.github_refresh_prs()
            else:
                self.github_state_label.configure(text="未登录 GitHub")

        self._run_github_task("检查登录状态", do_state, on_state)

    def github_login(self) -> None:
        if not self.github.is_available():
            messagebox.showwarning(
                "需要 GitHub CLI",
                "未检测到 GitHub CLI（gh）。\n\n请先访问 https://cli.github.com 安装。",
                parent=self,
            )
            return

        def do_login() -> Any:
            return self.github.login()

        def on_login(result: dict[str, Any]) -> None:
            if result.get("success"):
                self._gh_user_login = result.get("login", "")
                messagebox.showinfo(
                    "登录成功",
                    f"已登录 GitHub：{self._gh_user_login or '（已登录）'}",
                    parent=self,
                )
                self._refresh_github_state()
            else:
                self._show_error("登录失败", GitHubError(result.get("output", "登录未完成")))

        self._run_github_task("登录 GitHub", do_login, on_login)

    def github_logout(self) -> None:
        if not messagebox.askyesno("退出登录", "确定退出 GitHub 登录？", parent=self):
            return

        def do_logout() -> Any:
            return self.github.logout()

        def on_logout(result: Any) -> None:
            self._gh_user_login = ""
            self._gh_is_owner = False
            self.github_state_label.configure(text="未登录 GitHub")
            self.github_pr_list.delete(*self.github_pr_list.get_children())

        self._run_github_task("退出登录", do_logout, on_logout)

    def github_diagnose(self) -> None:
        """Show a one-screen GitHub integration diagnostic dump."""
        def do_diagnose() -> Any:
            return self.github.diagnose()

        def on_diagnose(text: str) -> None:
            self.github_pr_title.configure(text="GitHub 诊断")
            self._set_text(self.github_pr_overview, text)
            self._set_text(self.github_pr_diff, "（诊断信息已显示在「概览」页）")
            self._set_text(self.github_pr_review, "（无）")
            self._set_status("GitHub 诊断完成")

        self._run_github_task("诊断 GitHub", do_diagnose, on_diagnose)

    def github_refresh_prs(self) -> None:
        state = self.github_state_selector.get().strip() or "open"

        def do_list() -> Any:
            return self.github.list_prs(state=state)

        def on_list(prs: list[PullRequest]) -> None:
            self.github_pr_list.delete(*self.github_pr_list.get_children())
            for pr in prs:
                draft = "（草稿）" if pr.is_draft else ""
                self.github_pr_list.insert(
                    "", "end", text=f"{pr.title}{draft}",
                    values=(pr.number, pr.author, pr.base, pr.head),
                    tags=(str(pr.number),),
                )
            self._set_status(f"已加载 {len(prs)} 个 PR")

        self._run_github_task("刷新 PR 列表", do_list, on_list)

    def _selected_pr_number(self) -> int | None:
        selection = self.github_pr_list.selection()
        if not selection:
            return None
        return int(self.github_pr_list.item(selection[0], "values")[0])

    def _github_show_selected_pr(self, _event: object = None) -> None:
        number = self._selected_pr_number()
        if number is None:
            return
        self._pr_selected = number

        def do_load() -> Any:
            info = self.github.view(number)
            diff_text = ""
            try:
                diff_text = self.github.diff(number)
            except GitHubError:
                pass
            reviews = ""
            try:
                reviews = self.github.reviews(number)
            except GitHubError:
                pass
            return {"info": info, "diff": diff_text, "reviews": reviews}

        def on_load(payload: dict[str, Any]) -> None:
            info = payload.get("info", {})
            self.github_pr_title.configure(text=f"#{number} · {info.get('title', '')}")
            overview_lines = [
                f"标题：{info.get('title', '')}",
                f"状态：{info.get('state', '')}",
                f"作者：{info.get('authorLogin', '')}",
                f"分支：{info.get('headRefName', '')} → {info.get('baseRefName', '')}",
                f"URL：{info.get('url', '')}",
                "",
                str(info.get('body', '') or '（无描述）'),
            ]
            self._set_text(self.github_pr_overview, "\n".join(overview_lines))
            self._set_text(self.github_pr_diff, payload.get("diff", "") or "（无差异）")
            self._set_text(self.github_pr_review, payload.get("reviews", "") or "（暂无审阅）")
            mergeable = bool(info.get("mergeable"))
            can_merge = self._gh_is_owner and mergeable
            self.github_merge_button.configure(state="normal" if can_merge else "disabled")

        self._run_github_task("读取 PR 详情", do_load, on_load)

    def _github_merge_selected(self) -> None:
        number = self._pr_selected or self._selected_pr_number()
        if number is None:
            return
        if not self._gh_is_owner:
            messagebox.showwarning("无权限", "仅仓库所有者可以合并 PR。", parent=self)
            return
        if not messagebox.askyesno("确认合并", f"以 squash 方式合并 PR #{number}？", parent=self):
            return
        self._merge_pr(number)

    def _github_open_pr_url(self) -> None:
        number = self._pr_selected or self._selected_pr_number()
        if number is None:
            return
        try:
            info = self.github.view(number)
            url = info.get("url", "")
        except GitHubError:
            url = ""
        if not url:
            self._show_error("打开失败", GitHubError("无法获取 PR URL"))
            return
        # open the URL in the default browser without a shell
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url], shell=False)
        else:
            subprocess.Popen(["xdg-open", url], shell=False)


def main() -> None:
    try:
        if os.name == "nt":
            # Prevent Windows Error Reporting from opening a modal system
            # dialog when a child git.exe fails during DLL initialization.
            import ctypes
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
        LyricsManagerApp().mainloop()
    except Exception as exc:
        root = tk.Tk(); root.withdraw()
        messagebox.showerror(APP_NAME, str(exc), parent=root)
        root.destroy()


if __name__ == "__main__":
    main()
