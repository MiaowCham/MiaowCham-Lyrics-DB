# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

"""Pure tree-model helpers used by the Tk lyrics registry browser."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Iterable

from .core import LYRIC_EXTENSIONS, has_legacy_ttml


def _linked_target_value(record: dict[str, Any], key: str) -> str | None:
    target = record.get("linkedTarget")
    if not isinstance(target, dict):
        return None
    return str(target.get(key) or "") or None


@dataclass(frozen=True)
class RegistryNode:
    """A UI-independent node in the lyrics registry hierarchy."""

    kind: str
    label: str
    directory: Path | None = None
    metadata_ref: str | None = None
    file_name: str | None = None
    linked: bool = False
    linked_from: str | None = None
    linked_target_path: str | None = None
    linked_target_ref: str | None = None
    legacy: bool = False
    search_text: str = ""
    children: tuple["RegistryNode", ...] = ()


def build_registry_tree(
    lyrics_root: Path,
    directories: Iterable[Path],
    metadata_by_directory: dict[Path, dict[str, Any]],
) -> RegistryNode:
    """Build Lyrics -> artist -> directory -> entity -> file hierarchy."""
    artists: dict[str, list[RegistryNode]] = {}
    for directory in sorted(directories, key=lambda item: str(item).casefold()):
        metadata = metadata_by_directory.get(directory, {})
        relative = directory.relative_to(lyrics_root)
        artist = relative.parts[0] if relative.parts else "（未知歌手）"
        directory_label = "/".join(relative.parts[1:]) or directory.name
        file_records = {
            str(item.get("name")): item
            for item in metadata.get("files", []) if isinstance(item, dict) and item.get("name")
        }
        # External links deliberately do not occupy the source directory's
        # virtual track entity; their canonical entity lives elsewhere.
        bindings = {
            name: "" if _linked_target_value(item, "path") else str(item.get("metadataRef") or "")
            for name, item in file_records.items()
        }
        actual_files = sorted(
            (path.name for path in directory.iterdir()
             if path.is_file() and path.suffix.casefold() in LYRIC_EXTENSIONS),
            key=str.casefold,
        )
        tracks = metadata.get("tracks", {}) if isinstance(metadata.get("tracks"), dict) else {}
        entity_nodes: list[RegistryNode] = []
        for ref, track in tracks.items():
            track = track if isinstance(track, dict) else {}
            title = str(track.get("title") or "（无标题）")
            files = tuple(
                RegistryNode(
                    "file", name, directory, str(ref), name,
                    bool(file_records.get(name, {}).get("linked")),
                    str(file_records.get(name, {}).get("linkedFrom") or "") or None,
                    _linked_target_value(file_records.get(name, {}), "path"),
                    _linked_target_value(file_records.get(name, {}), "metadataRef"),
                    has_legacy_ttml(directory / name) if Path(name).suffix.casefold() == ".ttml" else False,
                    name.casefold(),
                )
                for name in actual_files if bindings.get(name) == str(ref)
            )
            searchable = json.dumps(track, ensure_ascii=False, sort_keys=True).casefold()
            entity_nodes.append(RegistryNode(
                "track", f"{title} [{ref}]", directory, str(ref),
                search_text=f"{ref} {title} {searchable}".casefold(), children=files,
            ))
        unbound = [name for name in actual_files if not bindings.get(name) or bindings[name] not in tracks]
        if unbound:
            entity_nodes.append(RegistryNode(
                "track", "（未关联）", directory, None, search_text="未关联 unbound",
                children=tuple(RegistryNode(
                    "file", name, directory, None, name,
                    bool(file_records.get(name, {}).get("linked")),
                    str(file_records.get(name, {}).get("linkedFrom") or "") or None,
                    _linked_target_value(file_records.get(name, {}), "path"),
                    _linked_target_value(file_records.get(name, {}), "metadataRef"),
                    has_legacy_ttml(directory / name) if Path(name).suffix.casefold() == ".ttml" else False,
                    name.casefold(),
                ) for name in unbound),
            ))
        directory_search = f"{relative.as_posix()} {json.dumps(metadata, ensure_ascii=False, sort_keys=True)}".casefold()
        artists.setdefault(artist, []).append(RegistryNode(
            "directory", directory_label, directory, search_text=directory_search,
            children=tuple(entity_nodes),
        ))
    artist_nodes = tuple(
        RegistryNode("artist", artist, search_text=artist.casefold(), children=tuple(children))
        for artist, children in sorted(artists.items(), key=lambda item: item[0].casefold())
    )
    return RegistryNode("root", "Lyrics", search_text="lyrics", children=artist_nodes)


def filter_registry_tree(node: RegistryNode, query: str) -> RegistryNode | None:
    """Keep matching nodes and all their ancestors, without mutating the model."""
    needle = query.strip().casefold()
    if not needle:
        return node
    children = tuple(child for item in node.children if (child := filter_registry_tree(item, needle)) is not None)
    if needle in f"{node.label} {node.search_text}".casefold() or children:
        return replace(node, children=children)
    return None
