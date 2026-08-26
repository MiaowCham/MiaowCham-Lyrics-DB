# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0. See http://www.apache.org/licenses/LICENSE-2.0

"""Metadata, indexing, preview, and source synchronization primitives.

The module deliberately uses only the Python standard library.  Source-file
updates are surgical text edits: lyric bodies and unknown metadata survive
round trips byte-for-byte (apart from the specifically edited tags).
"""

from __future__ import annotations

import copy
import html
import json
import os
import re
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2
METADATA_NAME = "lyrics.metadata"
LEGACY_METADATA_NAME = ".metadata"
INDEX_NAME = "lyrics-index.json"
LYRIC_EXTENSIONS = {
    ".ttml", ".lrcn", ".lrc", ".lys", ".qrc", ".ass", ".json", ".txt",
    ".lnt", ".xml", ".lqe",
}

TTML_NS = "http://www.w3.org/ns/ttml"
TTM_NS = "http://www.w3.org/ns/ttml#metadata"
XML_NS = "http://www.w3.org/XML/1998/namespace"
AMLL_NS = "http://www.example.com/ns/amll"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _atomic_bytes(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n")


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        value = str(value).strip()
        if value and value not in result:
            result.append(value)
    return result


def _manual_merge(discovered: Any, manual: Any) -> Any:
    """Deep merge, treating every existing/manual value as authoritative."""
    if isinstance(discovered, dict) and isinstance(manual, dict):
        result = copy.deepcopy(discovered)
        for key, value in manual.items():
            result[key] = _manual_merge(result[key], value) if key in result else copy.deepcopy(value)
        return result
    # Empty lists are meaningful manual choices too; never reconstruct them.
    return copy.deepcopy(manual)


def _identity(value: str) -> str:
    """Loose but deterministic song identity used only for initial grouping."""
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"^(?:applettml|amllttml|applejson)\.", "", value)
    value = re.sub(r"\s*[-–—]\s*(?:format|trans|translation|音译|翻译)\b.*$", "", value)
    value = re.sub(r"(?:[_\s-]+)(?:converted|format|trans|translation|音译|翻译)$", "", value)
    value = re.sub(r"\s*\((?:\d+|正式版本[^)]*)\)\s*$", "", value)
    return re.sub(r"[^\w]+", "", value)


def _metadata_ref(title: str, ordinal: int = 1) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()).strip("-")
    return (stem or "track") + (f"-{ordinal}" if ordinal > 1 else "")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_ttml(path: str | Path) -> dict[str, Any]:
    """Extract shared track fields and platform/source fields from TTML."""
    path = Path(path)
    result: dict[str, Any] = {"track": {}, "platforms": {}, "source": {"format": "ttml"}}
    try:
        root = ET.fromstring(_read_text(path))
    except (OSError, ET.ParseError, UnicodeError) as exc:
        result["source"]["warning"] = str(exc)
        return result

    track = result["track"]
    language = root.attrib.get(f"{{{XML_NS}}}lang")
    if language:
        track["language"] = language
    title = root.find(f".//{{{TTM_NS}}}title")
    if title is not None and (title.text or "").strip():
        track["title"] = (title.text or "").strip()

    amll: dict[str, list[str]] = {}
    for node in root.iter():
        if _local_name(node.tag) == "meta" and "key" in node.attrib:
            amll.setdefault(node.attrib["key"], []).append(node.attrib.get("value", ""))
    aliases = {
        "artists": "artists", "artist": "artists", "album": "album",
        "songName": "title", "musicName": "title", "title": "title", "songwriters": "songwriters",
        "songwriter": "songwriters", "isrc": "isrc",
    }
    for source_key, target_key in aliases.items():
        values = _unique(v for raw in amll.get(source_key, []) for v in raw.split(","))
        if values:
            track[target_key] = values if target_key in {"artists", "songwriters"} else values[0]
    writers = _unique((node.text or "") for node in root.iter() if _local_name(node.tag) == "songwriter")
    if writers:
        track["songwriters"] = writers
    # Keep platform IDs generic because repositories use evolving amll keys.
    for key, values in amll.items():
        lowered = key.lower()
        if lowered.endswith("id") and values:
            result["platforms"][key] = values if len(values) > 1 else values[0]
    result["source"]["metadata"] = {k: v if len(v) > 1 else v[0] for k, v in amll.items()}
    return result


_LRCN_HEADER = re.compile(r"^\[([^\],]+?)(?::([^\]]*))?\]\s*$")
_TIMED_LINE = re.compile(r"^\[(?:\d|\d+:\d|x-bg|song-part)")


def _split_typed_value(value: str) -> tuple[str | None, str]:
    if "@" in value:
        kind, actual = value.split("@", 1)
        return kind.strip() or None, actual.strip()
    return None, value.strip()


def parse_lrcn(path: str | Path) -> dict[str, Any]:
    """Extract Lyrics Next v2.3 header metadata, retaining repeatable values."""
    path = Path(path)
    result: dict[str, Any] = {"track": {}, "platforms": {}, "source": {"format": "lrcn", "metadata": {}}}
    try:
        lines = _read_text(path).splitlines()
    except (OSError, UnicodeError) as exc:
        result["source"]["warning"] = str(exc)
        return result
    raw: dict[str, list[Any]] = {}
    for line in lines:
        if _TIMED_LINE.match(line):
            break
        match = _LRCN_HEADER.match(line.strip())
        if not match:
            continue
        key, value = match.groups()
        if value is None:
            raw.setdefault(key, []).append(True)
        else:
            kind, actual = _split_typed_value(value)
            raw.setdefault(key, []).append({"type": kind, "value": actual} if kind else actual)
    result["source"]["metadata"] = raw
    track = result["track"]
    scalar = {"ti": "title", "al": "album", "lang": "language", "isrc": "isrc"}
    for key, target in scalar.items():
        if raw.get(key):
            value = raw[key][0]
            track[target] = value.get("value", "") if isinstance(value, dict) else str(value)
    for key, target in (("ar", "artists"), ("songwriter", "songwriters")):
        values = []
        for item in raw.get(key, []):
            value = item.get("value", "") if isinstance(item, dict) else str(item)
            values.extend(value.split(","))
        if values:
            track[target] = _unique(values)
    for item in raw.get("platform", []):
        if isinstance(item, dict) and item.get("type"):
            result["platforms"].setdefault(item["type"], []).append(item["value"])
    return result


def _plain_lrc_text(value: str) -> str:
    return re.sub(r"<[^>]*>", "", value).strip()


def _transliteration_lrc_text(value: str) -> str:
    segments = [part.strip() for part in re.findall(r"<[^>]*>([^<]*)", value) if part.strip()]
    return "\\".join(segments) if segments else _plain_lrc_text(value)


def _transliteration_xml_text(node: ET.Element) -> str:
    spans = [child for child in node.iter() if _local_name(child.tag) == "span"
             and not any(_local_name(nested.tag) == "span" for nested in child)]
    segments = ["".join(span.itertext()).strip() for span in spans if "".join(span.itertext()).strip()]
    return "\\".join(segments) if segments else "".join(node.itertext()).strip()


def _role(node: ET.Element) -> str:
    return next((value for key, value in node.attrib.items() if _local_name(key) == "role"), "").lower()


def _lyric_text(node: ET.Element) -> str:
    """Text belonging to a lyric node, excluding attached/bg payloads."""
    parts: list[str] = [node.text or ""]
    for child in node:
        if _role(child) not in {"x-translation", "x-transliteration", "x-roman", "x-bg"}:
            parts.append(_lyric_text(child))
        parts.append(child.tail or "")
    return "".join(parts).strip()


def _preview_row(line_id: str, original: str, translation: str = "", transliteration: str = "",
                 *, line_number: int | str = "", agent: str = "") -> dict[str, str]:
    return {"line_id": line_id, "line_number": str(line_number), "agent": agent,
            "original": original, "translation": translation, "transliteration": transliteration}


def preview_lrcn(path: str | Path) -> list[dict[str, str]]:
    """Return aligned original/translation/transliteration columns."""
    sections: dict[str, dict[str, str]] = {"lyrics": {}, "translate": {}, "transliteration": {}}
    current: str | None = None
    order: list[str] = []
    positional = 0
    last_main = ""
    bg_counts: dict[tuple[str, str], int] = {}
    for line in _read_text(Path(path)).splitlines():
        marker = re.match(r"^\[(lyrics|translate|transliteration):", line, re.I)
        if marker:
            current = marker.group(1).lower()
            positional = 0
            continue
        match = re.match(r"^\[([^\]]+)\](.*)$", line)
        if not match:
            continue
        tag, content = match.groups()
        if current is None:
            # A format marker is optional for a standalone LRCN.  Only a
            # genuinely timed line starts that implicit lyrics section.
            if not re.match(r"^\d+(?::\d+)*(?:\.\d+)?(?:,|$)", tag):
                continue
            current = "lyrics"
        if tag in {"Lyrics Next", "song-part"}:
            continue
        parts = [part.strip() for part in tag.split(",")]
        if tag == "x-bg":
            if not last_main:
                continue
            counter_key = (current, last_main)
            bg_counts[counter_key] = bg_counts.get(counter_key, 0) + 1
            line_id = f"{last_main}:bg{bg_counts[counter_key]}"
        else:
            line_id = next((part for part in reversed(parts) if re.match(r"^L[^,]+$", part, re.I)), "")
        if not line_id:
            line_id = f"@{positional}"
        if tag != "x-bg":
            last_main = line_id
        positional += 1
        text = _transliteration_lrc_text(content) if current == "transliteration" else _plain_lrc_text(content)
        sections[current][line_id] = text
        if current == "lyrics" and line_id not in order:
            order.append(line_id)
    rows = []
    number = 0
    # Re-read the primary section to retain agent/bg semantics.
    primary: dict[str, tuple[str, bool]] = {}
    current = None
    positional = 0
    for line in _read_text(Path(path)).splitlines():
        marker = re.match(r"^\[(lyrics|translate|transliteration):", line, re.I)
        if marker:
            current = marker.group(1).lower(); positional = 0
            continue
        match = re.match(r"^\[([^\]]+)\](.*)$", line)
        if not match or current not in {None, "lyrics"}:
            continue
        tag = match.group(1); parts = [p.strip() for p in tag.split(",")]
        if not (tag == "x-bg" or re.match(r"^\d+(?::\d+)*(?:\.\d+)?(?:,|$)", tag)):
            continue
        if tag == "x-bg":
            previous = next((key for key in reversed(order[:positional]) if ":bg" not in key), "")
            count = sum(1 for key in order[:positional] if key.startswith(previous + ":bg")) + 1
            line_id = f"{previous}:bg{count}"
        else:
            line_id = next((p for p in reversed(parts) if re.match(r"^L.+", p, re.I)), f"@{positional}")
        positional += 1
        primary[line_id] = (parts[2] if len(parts) > 2 else "", tag == "x-bg")
    for key in order:
        agent, bg = primary.get(key, ("", False))
        if not bg: number += 1
        rows.append(_preview_row(key, sections["lyrics"].get(key, ""), sections["translate"].get(key, ""),
                                 sections["transliteration"].get(key, ""),
                                 line_number="bg" if bg else number, agent=agent))
    return rows


def preview_ttml(path: str | Path) -> list[dict[str, str]]:
    root = ET.fromstring(_read_text(Path(path)))
    translations: dict[str, str] = {}
    transliterations: dict[str, str] = {}
    for parent in root.iter():
        name = _local_name(parent.tag).lower()
        target = translations if "translation" in name else transliterations if "transliteration" in name else None
        if target is None:
            continue
        for node in parent.iter():
            line_id = node.attrib.get("for")
            if line_id:
                target[line_id] = (_transliteration_xml_text(node) if target is transliterations
                                   else "".join(node.itertext()).strip())
    rows = []
    number = 0
    for node in root.iter():
        if _local_name(node.tag) != "p":
            continue
        line_id = (node.attrib.get(f"{{{XML_NS}}}id") or node.attrib.get("id") or
                   next((value for key, value in node.attrib.items() if _local_name(key) == "key"), None) or
                   f"@{len(rows)}")
        agent = next((value for key, value in node.attrib.items() if _local_name(key) == "agent"), "")
        legacy_translation = next(("".join(child.itertext()).strip() for child in node
                                   if _role(child) == "x-translation"), "")
        legacy_roman = next((_transliteration_xml_text(child) for child in node
                             if _role(child) in {"x-transliteration", "x-roman"}), "")
        number += 1
        rows.append(_preview_row(line_id, _lyric_text(node), translations.get(line_id, legacy_translation),
                                 transliterations.get(line_id, legacy_roman), line_number=number, agent=agent))
        # A bg is attached to its p and is deliberately emitted after the main
        # row even when its begin time precedes the main line.
        for bg_index, bg in enumerate((c for c in node if _role(c) == "x-bg"), 1):
            bg_id = f"{line_id}:bg{bg_index}"
            bg_translation = translations.get(bg_id, next(("".join(c.itertext()).strip() for c in bg if _role(c) == "x-translation"), ""))
            bg_roman = transliterations.get(bg_id, next((_transliteration_xml_text(c) for c in bg if _role(c) in {"x-transliteration", "x-roman"}), ""))
            rows.append(_preview_row(bg_id, _lyric_text(bg), bg_translation, bg_roman,
                                     line_number="bg", agent=agent))
    return rows


def preview_text(path: str | Path) -> list[dict[str, str]]:
    """Tolerant line preview for LRC-like and otherwise plain lyric files."""
    rows = []
    number = 0
    for raw in _read_text(Path(path)).splitlines():
        line = raw.strip()
        if not line or re.match(r"^\[[A-Za-z][^\]]*:\s*[^\]]*\]$", line):
            continue
        match = re.match(r"^\[([^\]]+)\](.*)$", line)
        tag, value = match.groups() if match else ("", line)
        parts = [p.strip() for p in tag.split(",")]
        agent = parts[2] if len(parts) > 2 else ""
        # Lyricify Syllable uses a leading numeric singer marker.  3/4/7/8 are bg.
        lys = re.match(r"^\[(\d+)\](.*)$", line)
        if lys:
            agent, value = lys.groups()
        bg = tag == "x-bg" or agent in {"3", "4", "7", "8"}
        if not bg: number += 1
        rows.append(_preview_row(f"@{len(rows)}", _plain_lrc_text(value),
                                 line_number="bg" if bg else number, agent=agent))
    return rows


def _lyricify_word_text(value: str) -> str:
    """Remove Lyricify word timestamps while retaining the lyric text and spaces."""
    return re.sub(r"\(\d+\s*,\s*\d+\)", "", value).strip()


def preview_lyricify(path: str | Path, *, qrc: bool) -> list[dict[str, str]]:
    """Preview Lyricify QRC or Lyricify Syllable timing syntax.

    QRC uses ``[start,duration]`` while LYS uses a numeric property in the
    brackets.  The property itself is retained in the Agent column; only 6,
    7 and 8 denote background vocals.
    """
    rows: list[dict[str, str]] = []
    number = 0
    pattern = r"^\[(\d+)\s*,\s*(\d+)\](.*)$" if qrc else r"^\[([0-8])\](.*)$"
    for raw in _read_text(Path(path)).splitlines():
        match = re.match(pattern, raw.strip())
        if not match:
            continue
        if qrc:
            _start, _duration, value = match.groups()
            agent = ""
            bg = False
        else:
            agent, value = match.groups()
            bg = agent in {"6", "7", "8"}
        if not bg:
            number += 1
        rows.append(_preview_row(
            f"@{len(rows)}", _lyricify_word_text(value),
            line_number="bg" if bg else number, agent=agent,
        ))
    return rows


def preview_file(path: str | Path) -> list[dict[str, str]]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".ass":
        return []
    if suffix in {".ttml", ".xml"}:
        try:
            return preview_ttml(path)
        except ET.ParseError:
            return preview_text(path)
    if suffix in {".lrcn", ".lnt"}:
        return preview_lrcn(path)
    if suffix == ".qrc":
        rows = preview_lyricify(path, qrc=True)
        return rows or preview_text(path)
    if suffix == ".lys":
        rows = preview_lyricify(path, qrc=False)
        return rows or preview_text(path)
    if suffix == ".json":
        try:
            value = json.loads(_read_text(path))
            lines = value.get("lines", value) if isinstance(value, dict) else value
            if isinstance(lines, list):
                return [_preview_row(str(i), str(v.get("original", v.get("text", ""))) if isinstance(v, dict) else str(v),
                                     str(v.get("translation", "")) if isinstance(v, dict) else "",
                                     str(v.get("transliteration", "")) if isinstance(v, dict) else "",
                                     line_number=i) for i, v in enumerate(lines, 1)]
        except (ValueError, TypeError):
            pass
    return preview_text(path)


def has_legacy_ttml(path: str | Path) -> bool:
    """Return whether TTML body still contains legacy attachment roles."""
    try:
        root = ET.fromstring(_read_text(Path(path)))
    except (OSError, UnicodeError, ET.ParseError):
        return False
    body = next((node for node in root.iter() if _local_name(node.tag) == "body"), None)
    return bool(body is not None and any(
        _role(node) in {"x-translation", "x-transliteration", "x-roman"}
        for node in body.iter()
    ))


def convert_legacy_ttml(path: str | Path) -> dict[str, Any]:
    """Explicitly migrate body attachment spans to iTunesMetadata containers."""
    path = Path(path)
    text = _read_text(path)
    root = ET.fromstring(text)
    warnings: list[str] = []
    legacy: list[tuple[ET.Element, ET.Element, str, str, str]] = []
    for p in (n for n in root.iter() if _local_name(n.tag) == "p"):
        line_id = (p.attrib.get(f"{{{XML_NS}}}id") or p.attrib.get("id") or
                   next((v for k, v in p.attrib.items() if _local_name(k) == "key"), ""))
        if not line_id:
            if any(_role(n) in {"x-translation", "x-transliteration", "x-roman"} for n in p.iter()):
                warnings.append("发现无行 ID 的旧附属歌词，已保留")
            continue
        for parent in p.iter():
            for child in list(parent):
                role = _role(child)
                if role in {"x-translation", "x-transliteration", "x-roman"}:
                    kind = "translations" if role == "x-translation" else "transliterations"
                    language = child.attrib.get(f"{{{XML_NS}}}lang", "")
                    target_id = line_id
                    if _role(parent) == "x-bg":
                        backgrounds = [node for node in p if _role(node) == "x-bg"]
                        target_id = f"{line_id}:bg{backgrounds.index(parent) + 1}"
                    legacy.append((parent, child, target_id, kind, language))
    if not legacy:
        return {"changed": False, "warnings": warnings}
    metadata = next((n for n in root.iter() if _local_name(n.tag) == "metadata"), None)
    if metadata is None:
        head = next((n for n in root if _local_name(n.tag) == "head"), None)
        if head is None:
            head = ET.Element(f"{{{TTML_NS}}}head"); root.insert(0, head)
        metadata = ET.SubElement(head, f"{{{TTML_NS}}}metadata")
    itunes = next((n for n in metadata if _local_name(n.tag) == "iTunesMetadata"), None)
    if itunes is None:
        itunes = ET.SubElement(metadata, "{http://music.apple.com/lyric-ttml-internal}iTunesMetadata")
    for parent, child, line_id, kind, language in legacy:
        container = next((n for n in itunes if _local_name(n.tag) == kind), None)
        if container is None:
            container = ET.SubElement(itunes, f"{{http://music.apple.com/lyric-ttml-internal}}{kind}")
        singular = kind[:-1]
        group = next((n for n in container if _local_name(n.tag) == singular and
                      n.attrib.get(f"{{{XML_NS}}}lang", "") == language), None)
        if group is None:
            group = ET.SubElement(container, f"{{http://music.apple.com/lyric-ttml-internal}}{singular}")
            if language: group.set(f"{{{XML_NS}}}lang", language)
        target = ET.SubElement(group, f"{{http://music.apple.com/lyric-ttml-internal}}text", {"for": line_id})
        target.text = child.text
        for nested in list(child):
            target.append(copy.deepcopy(nested))
        parent.remove(child)
    ET.register_namespace("", TTML_NS); ET.register_namespace("ttm", TTM_NS)
    ET.register_namespace("amll", AMLL_NS); ET.register_namespace("itunes", "http://music.apple.com/lyric-ttml-internal")
    output = ET.tostring(root, encoding="unicode")
    ET.fromstring(output)
    backup = path.with_name(path.name + ".bak")
    if backup.exists():
        raise FileExistsError(f"备份已存在，未覆盖：{backup.name}")
    _atomic_bytes(backup, path.read_bytes())
    _atomic_text(path, output)
    ET.fromstring(_read_text(path))
    return {"changed": True, "backup": str(backup), "warnings": warnings}


def restore_ttml_backup(path: str | Path) -> dict[str, Any]:
    """Validate and atomically restore ``<name>.bak`` created by conversion."""
    path = Path(path)
    backup = path.with_name(path.name + ".bak")
    if not backup.is_file():
        raise FileNotFoundError(f"未找到备份：{backup.name}")
    data = backup.read_bytes()
    ET.fromstring(data.decode("utf-8-sig"))
    _atomic_bytes(path, data)
    ET.fromstring(_read_text(path))
    backup.unlink()
    return {"restored": True, "warnings": []}


def _replace_attr(tag: str, attr: str, value: str) -> str:
    escaped = html.escape(value, quote=True)
    pattern = re.compile(rf"(\s{re.escape(attr)}\s*=\s*)([\"']).*?\2", re.S)
    if pattern.search(tag):
        return pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{escaped}{m.group(2)}", tag, count=1)
    return tag[:-1] + f' {attr}="{escaped}">' if tag.endswith(">") else tag


def _remove_attr(tag: str, attr: str) -> str:
    return re.sub(rf"\s+{re.escape(attr)}\s*=\s*([\"']).*?\1", "", tag, count=1, flags=re.S)


def _replace_amll_values(text: str, key: str, values: Iterable[str]) -> str:
    pattern = re.compile(rf"<amll:meta\b(?=[^>]*\bkey\s*=\s*([\"']){re.escape(key)}\1)[^>]*?/?>", re.I)
    text = pattern.sub("", text)
    nodes = "".join(f'<amll:meta key="{html.escape(key, quote=True)}" value="{html.escape(str(value), quote=True)}" />'
                    for value in values if str(value) != "")
    return re.sub(r"</metadata\s*>", nodes + "</metadata>", text, count=1) if nodes else text


def _sync_ttml_text(text: str, track: dict[str, Any]) -> str:
    if "language" in track:
        text = re.sub(r"<tt\b[^>]*>", lambda m: (_replace_attr(m.group(0), "xml:lang", str(track["language"]))
                                                      if track.get("language") else _remove_attr(m.group(0), "xml:lang")), text, count=1)
    title = str(track.get("title") or "")
    if "title" in track and title:
        escaped = html.escape(title)
        if re.search(r"<ttm:title\b[^>]*>.*?</ttm:title\s*>", text, re.S):
            text = re.sub(r"(<ttm:title\b[^>]*>).*?(</ttm:title\s*>)", rf"\g<1>{escaped}\g<2>", text, count=1, flags=re.S)
        else:
            text = re.sub(r"(<metadata\b[^>]*>)", rf"\1<ttm:title>{escaped}</ttm:title>", text, count=1)
    elif "title" in track:
        text = re.sub(r"<ttm:title\b[^>]*>.*?</ttm:title\s*>", "", text, count=1, flags=re.S)
    mapped: dict[str, list[str]] = {}
    if "artists" in track:
        mapped["artists"] = [str(value) for value in track.get("artists", [])]
    if "album" in track:
        mapped["album"] = [str(track["album"])] if track.get("album") else []
    if "songwriters" in track:
        mapped["songwriters"] = [str(value) for value in track.get("songwriters", [])]
    if "isrc" in track:
        mapped["isrc"] = [str(track["isrc"])] if track.get("isrc") else []
    for key, value in track.get("platforms", {}).items():
        mapped[str(key)] = [str(item) for item in (value if isinstance(value, list) else [value])]
    for key, values in mapped.items():
        text = _replace_amll_values(text, key, values)
    # Keep the standard iTunes songwriter container aligned when present.
    if "songwriters" in track and re.search(r"<songwriters\b[^>]*>.*?</songwriters\s*>", text, re.S):
        writers = "".join(f"<songwriter>{html.escape(str(value))}</songwriter>" for value in track.get("songwriters", []))
        text = re.sub(r"(<songwriters\b[^>]*>).*?(</songwriters\s*>)", rf"\g<1>{writers}\g<2>", text, count=1, flags=re.S)
    return text


def _lrcn_tags(track: dict[str, Any], platforms: dict[str, Any]) -> dict[str, list[str]]:
    tags: dict[str, list[str]] = {}
    if "title" in track:
        tags["ti"] = [str(track["title"])] if track.get("title") else []
    if "artists" in track:
        tags["ar"] = [str(v) for v in track["artists"]]
    if "album" in track:
        tags["al"] = [str(track["album"])] if track.get("album") else []
    if "language" in track:
        tags["lang"] = [str(track["language"])] if track.get("language") else []
    if "songwriters" in track:
        tags["songwriter"] = [str(v) for v in track["songwriters"]]
    if "isrc" in track:
        tags["isrc"] = [str(track["isrc"])] if track.get("isrc") else []
    platform_values = []
    for platform, ids in platforms.items():
        for value in ids if isinstance(ids, list) else [ids]:
            platform_values.append(f"{platform}@{value}")
    tags["platform"] = platform_values
    return tags


def _sync_lrcn_text(text: str, track: dict[str, Any], platforms: dict[str, Any]) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    boundary = next((i for i, line in enumerate(lines) if _TIMED_LINE.match(line)), len(lines))
    header, body = lines[:boundary], lines[boundary:]
    replacements = _lrcn_tags(track, platforms)
    emitted: set[str] = set()
    output = []
    for line in header:
        match = _LRCN_HEADER.match(line.strip())
        key = match.group(1) if match else None
        if key not in replacements:
            output.append(line)
        elif key not in emitted:
            output.extend(f"[{key}:{value}]" for value in replacements[key])
            emitted.add(key)
    additions = [f"[{key}:{value}]" for key, values in replacements.items()
                 if key not in emitted for value in values]
    # New header fields belong before the first embedded-lyrics section
    # declaration, not inside that section.
    insertion = next((i for i, line in enumerate(output)
                      if re.match(r"^\[(lyrics|translate|transliteration):", line, re.I)), len(output))
    output[insertion:insertion] = additions
    result = newline.join(output + body)
    return result + newline if trailing else result


class LyricsDatabase:
    """Repository-level facade used by both the GUI and command-line tools."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.lyrics_root = self.root / "Lyrics"
        self.index_path = self.root / INDEX_NAME

    def _track_dirs(self) -> list[Path]:
        if not self.lyrics_root.is_dir():
            return []
        return sorted({path.parent for path in self.lyrics_root.rglob("*")
                       if path.is_file() and path.name not in {METADATA_NAME, LEGACY_METADATA_NAME}
                       and path.suffix.lower() in LYRIC_EXTENSIONS},
                      key=lambda p: p.as_posix().casefold())

    def discover(self, directory: str | Path) -> dict[str, Any]:
        directory = Path(directory).resolve()
        relative = directory.relative_to(self.lyrics_root)
        parts = relative.parts
        files = sorted((p for p in directory.iterdir()
                        if p.is_file() and p.name not in {METADATA_NAME, LEGACY_METADATA_NAME}
                        and p.suffix.lower() in LYRIC_EXTENSIONS), key=lambda p: p.name.casefold())
        candidates: list[dict[str, Any]] = []
        source_by_name: dict[str, dict[str, Any]] = {}
        for path in files:
            parsed = parse_ttml(path) if path.suffix.lower() == ".ttml" else parse_lrcn(path) if path.suffix.lower() == ".lrcn" else {"track": {}, "platforms": {}, "source": {"format": path.suffix.lstrip(".").lower()}}
            candidate = {"path": path, **parsed}
            candidates.append(candidate)
            source_by_name[path.name] = {"file": path.name, **parsed.get("source", {})}

        groups: list[dict[str, Any]] = []
        # Metadata-bearing files establish identities first. Metadata-free files
        # then attach by identical/containing filename stem; ambiguous files get
        # their own editable entity rather than risking cross-song mutation.
        ordered = sorted(candidates, key=lambda c: (not bool(c.get("track", {}).get("title")), c["path"].name.casefold()))
        for candidate in ordered:
            title = str(candidate.get("track", {}).get("title", ""))
            title_id = _identity(title)
            stem_id = _identity(candidate["path"].stem)
            platform_pairs = {(str(k).casefold(), str(vv)) for k, value in candidate.get("platforms", {}).items()
                              for vv in (value if isinstance(value, list) else [value])}
            matches = []
            for group in groups:
                same_title = bool(title_id and title_id == group["title_id"])
                same_stem = stem_id in group["stems"]
                stem_mentions_title = bool(group["title_id"] and group["title_id"] in stem_id)
                same_platform = bool(platform_pairs & group["platform_pairs"])
                if same_title or same_stem or same_platform or (not title_id and stem_mentions_title):
                    matches.append(group)
            group = matches[0] if len(matches) == 1 else None
            if group is None:
                group = {"title_id": title_id, "stems": set(), "platform_pairs": set(), "candidates": []}
                groups.append(group)
            if title_id and not group["title_id"]:
                group["title_id"] = title_id
            group["stems"].add(stem_id)
            group["platform_pairs"].update(platform_pairs)
            group["candidates"].append(candidate)

        tracks: dict[str, dict[str, Any]] = {}
        file_records = []
        used_refs: set[str] = set()
        for group in groups:
            default_title = next((str(c.get("track", {}).get("title")) for c in group["candidates"] if c.get("track", {}).get("title")), parts[-1])
            ordinal = 1
            ref = _metadata_ref(default_title, ordinal)
            while ref in used_refs:
                ordinal += 1
                ref = _metadata_ref(default_title, ordinal)
            used_refs.add(ref)
            track: dict[str, Any] = {"title": default_title, "artists": [parts[0]] if parts else []}
            platforms: dict[str, Any] = {}
            for candidate in group["candidates"]:
                for key, value in candidate.get("track", {}).items():
                    if value not in (None, "", []):
                        track.setdefault(key, copy.deepcopy(value))
                for key, value in candidate.get("platforms", {}).items():
                    platforms.setdefault(key, copy.deepcopy(value))
                source = source_by_name[candidate["path"].name]
                file_records.append({"name": candidate["path"].name,
                                     "format": source.get("format", candidate["path"].suffix.lstrip(".").lower()),
                                     "metadataRef": ref})
            tracks[ref] = {**track, "platforms": platforms}
        file_records.sort(key=lambda item: item["name"].casefold())
        return {"schemaVersion": SCHEMA_VERSION, "path": relative.as_posix(), "tracks": tracks,
                "files": file_records, "sources": list(source_by_name.values())}

    def _migrate(self, value: dict[str, Any], discovered: dict[str, Any]) -> dict[str, Any]:
        if value.get("schemaVersion", 1) >= 2 and isinstance(value.get("tracks"), dict):
            return value
        legacy_track = copy.deepcopy(value.get("track", {}))
        if value.get("platforms"):
            legacy_track["platforms"] = copy.deepcopy(value["platforms"])
        # A v1 document represented exactly one entity. If discovery also sees
        # one entity, reuse its ref so manual values override rather than create
        # a duplicate. Multi-song legacy data remains explicitly one binding,
        # which is the only lossless interpretation of that old schema.
        ref = (next(iter(discovered["tracks"])) if len(discovered.get("tracks", {})) == 1
               else _metadata_ref(str(legacy_track.get("title") or Path(discovered["path"]).name)))
        legacy_files = value.get("files", [])
        names = [item if isinstance(item, str) else item.get("name") for item in legacy_files]
        if not names:
            names = [item["name"] for item in discovered.get("files", [])]
        migrated = {key: copy.deepcopy(val) for key, val in value.items()
                    if key not in {"track", "platforms", "files", "schemaVersion"}}
        migrated.update({"schemaVersion": SCHEMA_VERSION, "tracks": {ref: legacy_track},
                         "files": [{"name": name, "format": Path(name).suffix.lstrip(".").lower(), "metadataRef": ref}
                                   for name in names if name]})
        return migrated

    def load_metadata(self, directory: str | Path) -> dict[str, Any]:
        directory = Path(directory).resolve()
        discovered = self.discover(directory)
        path = directory / METADATA_NAME
        if not path.exists() and (directory / LEGACY_METADATA_NAME).exists():
            path = directory / LEGACY_METADATA_NAME
        if not path.exists():
            return discovered
        manual = json.loads(_read_text(path))
        if not isinstance(manual, dict):
            raise ValueError(f"{path} must contain a JSON object")
        manual = self._migrate(manual, discovered)
        result = copy.deepcopy(discovered)
        # Track objects are manually authoritative, while newly discovered
        # fields remain available. A user may also create wholly manual tracks.
        for ref, track in manual.get("tracks", {}).items():
            result["tracks"][ref] = _manual_merge(result["tracks"].get(ref, {}), track)
        # A manually deleted entity must not be resurrected from an unchanged
        # lyric file when the library is scanned again.
        deleted_tracks = {str(ref) for ref in manual.get("deletedTracks", [])}
        for ref in deleted_tracks:
            result["tracks"].pop(ref, None)
        existing_files = {item.get("name"): item for item in manual.get("files", []) if isinstance(item, dict)}
        result["files"] = [_manual_merge(item, existing_files.get(item["name"], {})) for item in result["files"]]
        for key, value in manual.items():
            if key not in {"tracks", "files", "schemaVersion"}:
                result[key] = _manual_merge(result.get(key), value) if key in result else copy.deepcopy(value)
        result["schemaVersion"] = SCHEMA_VERSION
        return result

    def save_metadata(self, directory: str | Path, metadata: dict[str, Any], *, rebuild_index: bool = True) -> Path:
        """Save only lyrics.metadata (and optionally the index); never mutate sources."""
        directory = Path(directory).resolve()
        directory.relative_to(self.lyrics_root)
        value = copy.deepcopy(metadata)
        if value.get("schemaVersion", 1) < 2 or "tracks" not in value:
            value = self._migrate(value, self.discover(directory))
        value["schemaVersion"] = SCHEMA_VERSION
        value.setdefault("path", directory.relative_to(self.lyrics_root).as_posix())
        path = directory / METADATA_NAME
        _atomic_json(path, value)
        if rebuild_index:
            self.build_index()
        return path

    def scan(self, *, write: bool = True) -> list[dict[str, Any]]:
        records = []
        for directory in self._track_dirs():
            record = self.load_metadata(directory)
            records.append(record)
            if write:
                _atomic_json(directory / METADATA_NAME, record)
        if write:
            self.build_index(records)
        return records

    def build_index(self, records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        records = records if records is not None else [self.load_metadata(path) for path in self._track_dirs()]
        compact = []
        for item in records:
            files_by_ref: dict[str, list[str]] = {}
            for file_item in item.get("files", []):
                if isinstance(file_item, dict):
                    files_by_ref.setdefault(str(file_item.get("metadataRef", "")), []).append(str(file_item.get("name", "")))
            for ref, track in item.get("tracks", {}).items():
                compact.append({"path": item.get("path"), "metadataRef": ref,
                                "track": {k: v for k, v in track.items() if k != "platforms"},
                                "platforms": track.get("platforms", {}), "files": files_by_ref.get(ref, [])})
        value = {"schemaVersion": SCHEMA_VERSION, "tracks": compact}
        _atomic_json(self.index_path, value)
        return value

    def preview(self, path: str | Path) -> list[dict[str, str]]:
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        return preview_file(path)

    def convert_legacy_ttml(self, path: str | Path) -> dict[str, Any]:
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        path.resolve().relative_to(self.lyrics_root.resolve())
        if path.suffix.casefold() != ".ttml":
            raise ValueError("仅 TTML 文件支持旧格式转换")
        return convert_legacy_ttml(path)

    def restore_ttml_backup(self, path: str | Path) -> dict[str, Any]:
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        path.resolve().relative_to(self.lyrics_root.resolve())
        if path.suffix.casefold() != ".ttml":
            raise ValueError("仅 TTML 文件支持恢复备份")
        return restore_ttml_backup(path)

    def sync_to_sources(self, directory: str | Path, metadata: dict[str, Any] | None = None,
                        *, metadata_refs: Iterable[str] | None = None) -> dict[str, Any]:
        """Explicitly sync shared metadata into TTML/LRCN sources.

        Returns changed file paths and non-fatal warnings.  No source is touched
        when its resulting content is identical.
        """
        directory = Path(directory).resolve()
        directory.relative_to(self.lyrics_root)
        metadata = metadata if metadata is not None else self.load_metadata(directory)
        if metadata.get("schemaVersion", 1) < 2 or "tracks" not in metadata:
            metadata = self._migrate(metadata, self.discover(directory))
        tracks = metadata.get("tracks", {})
        selected_refs = set(metadata_refs) if metadata_refs is not None else None
        file_bindings = {item.get("name"): item.get("metadataRef") for item in metadata.get("files", [])
                         if isinstance(item, dict) and item.get("name")}
        changed: list[str] = []
        warnings: list[str] = []
        for path in sorted(directory.iterdir(), key=lambda p: p.name.casefold()):
            if path.suffix.lower() not in {".ttml", ".lrcn"}:
                continue
            try:
                ref = file_bindings.get(path.name)
                if not ref:
                    warnings.append(f"{path.name}: 未绑定 metadataRef，已跳过")
                    continue
                if ref not in tracks:
                    warnings.append(f"{path.name}: metadataRef {ref!r} 不存在，已跳过")
                    continue
                if selected_refs is not None and ref not in selected_refs:
                    continue
                track = tracks[ref]
                platforms = track.get("platforms", {})
                before = _read_text(path)
                after = _sync_ttml_text(before, track) if path.suffix.lower() == ".ttml" else _sync_lrcn_text(before, track, platforms)
                if before != after:
                    _atomic_text(path, after)
                    changed.append(path.relative_to(self.root).as_posix())
            except (OSError, UnicodeError, ValueError) as exc:
                warnings.append(f"{path.name}: {exc}")
        return {"changed_files": changed, "warnings": warnings}

    def sync_file_to_track(self, path: str | Path, track: dict[str, Any]) -> dict[str, Any]:
        """Synchronise one linked TTML/LRCN file from its canonical track metadata."""
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        path.relative_to(self.lyrics_root)
        if path.suffix.casefold() not in {".ttml", ".lrcn"}:
            return {"changed_files": [], "warnings": [f"{path.name}: 此格式不支持元数据同步"]}
        try:
            before = _read_text(path)
            platforms = track.get("platforms", {}) if isinstance(track.get("platforms"), dict) else {}
            after = _sync_ttml_text(before, track) if path.suffix.casefold() == ".ttml" else _sync_lrcn_text(before, track, platforms)
            if before != after:
                _atomic_text(path, after)
                return {"changed_files": [path.relative_to(self.root).as_posix()], "warnings": []}
            return {"changed_files": [], "warnings": []}
        except (OSError, UnicodeError, ValueError) as exc:
            return {"changed_files": [], "warnings": [f"{path.name}: {exc}"]}


DatabaseManager = LyricsDatabase
