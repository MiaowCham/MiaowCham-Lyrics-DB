# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

"""TTML one-click formatter.

Turns a compressed (minified, single-line) TTML lyric file into a more standard
form, normalising syllable/pronunciation spans:

1. Normal transliteration spans get ``xmlns="http://www.w3.org/ns/ttml"``.
2. Transliteration spans with ``ttm:role="x-bg"`` become the canonical
   background wrapper: the outer span carries ``xmlns:ttm``, ``ttm:role`` and the
   default ``xmlns``; inner spans stay plain.
3. Spaces trapped inside a syllable are moved outside it.
4. For romanisation languages (ja-Latn, zh-Latn-pinyin, zh-Latn-jyutping,
   ko-Latn) with no syllable-internal or syllable-gap spaces, spaces are
   inserted between all syllables (unless a pronunciation syllable matches the
   original syllable's letters, in which case the original spacing decides).
5. For other pronunciation languages, the original syllable spacing decides.

The script scans a lyrics root for compressed TTML files (few line breaks),
backs the original up to ``.bak``, rewrites a normalised single-line version in
place, and also emits a pretty-printed ``-Format.ttml`` sibling.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

TTML_NS = "http://www.w3.org/ns/ttml"
TTM_NS = "http://www.w3.org/ns/ttml#metadata"
AMLL_NS = "http://www.example.com/ns/amll"
ITUNES_NS = "http://music.apple.com/lyric-ttml-internal"
XML_NS = "http://www.w3.org/XML/1998/namespace"
TTS_NS = "http://www.w3.org/ns/ttml#styling"

ROMANISATION_LANGS = {"ja-Latn", "zh-Latn-pinyin", "zh-Latn-jyutping", "ko-Latn"}

_SPAN_OPEN = re.compile(r"<span\b[^>]*>")
_SPAN_CLOSE = re.compile(r"</span\s*>")
_TEXT_BLOCK = re.compile(r"<text\b[^>]*>.*?</text\s*>", re.S)
_P_BLOCK = re.compile(r"<p\b[^>]*>.*?</p\s*>", re.S)
_KEY = re.compile(r"\bitunes:key\s*=\s*([\"'])(.*?)\1")
_LANG = re.compile(r"\bxml:lang\s*=\s*([\"'])(.*?)\1", re.S)

_COMPRESSED_MAX_NEWLINES = 8


def _extract_spans(text: str) -> list[tuple[int, int]]:
    """Top-level span (start, end) positions, nesting-aware."""
    spans: list[tuple[int, int]] = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _SPAN_OPEN.search(text, pos)
        if not m:
            break
        end = _matching_close(text, m.end())
        spans.append((m.start(), end))
        pos = end
    return spans


def _matching_close(text: str, open_end: int) -> int:
    depth = 1
    pos = open_end
    n = len(text)
    while pos < n and depth > 0:
        nxt_open = _SPAN_OPEN.search(text, pos)
        nxt_close = _SPAN_CLOSE.search(text, pos)
        if nxt_close and (not nxt_open or nxt_open.start() > nxt_close.start()):
            depth -= 1
            pos = nxt_close.end()
        elif nxt_open:
            depth += 1
            pos = nxt_open.end()
        else:
            break
    return pos


class Span:
    """One span element: open tag, inner content, closing tag, parsed children."""

    __slots__ = ("open_tag", "inner", "close", "children")

    def __init__(self, open_tag: str, inner: str, close: str, children: list["Span"] | None = None):
        self.open_tag = open_tag
        self.inner = inner
        self.close = close
        self.children = children

    def role(self) -> str | None:
        m = re.search(r"\bttm:role\s*=\s*([\"'])(.*?)\1", self.open_tag, re.S)
        return m.group(2) if m else None

    @property
    def text(self) -> str:
        """Leaf text ignoring nested children (for syllable comparison)."""
        if self.children:
            return re.sub(r"<[^>]+>", "", self.inner)
        return self.inner

    def render(self) -> str:
        return f"{self.open_tag}{self.inner}{self.close}"


def parse_span(text: str, start: int) -> Span:
    m = _SPAN_OPEN.match(text, start)
    open_tag = m.group(0)
    end = _matching_close(text, m.end())
    close_len = len(_SPAN_CLOSE.search(text, end - len("</span>"), end).group(0))
    close_start = end - close_len
    inner = text[m.end():close_start]
    close_tag = text[close_start:end]
    children = []
    inner_spans = _extract_spans(inner)
    if inner_spans:
        for (s, _e) in inner_spans:
            children.append(parse_span(inner, s))
    return Span(open_tag, inner, close_tag, children or None)


def _attr(open_tag: str, name: str) -> str | None:
    m = re.search(rf"\b{re.escape(name)}\s*=\s*([\"'])(.*?)\1", open_tag, re.S)
    return m.group(2) if m else None


def _strip_attr(open_tag: str, name: str) -> str:
    return re.sub(rf"\s+{re.escape(name)}\s*=\s*([\"']).*?\1", "", open_tag, count=1, flags=re.S)


def _normalise_plain_span(open_tag: str) -> str:
    """Rule 1: append ``xmlns=TTML`` (drop any existing default xmlns)."""
    tag = _strip_attr(_strip_attr(open_tag, "xmlns"), "xmlns:ttm")
    return f"{tag.rstrip()[:-1]} xmlns=\"{TTML_NS}\">" if tag.rstrip().endswith(">") else tag


def normalise_span(span: Span) -> str:
    role = span.role()
    if role == "x-bg":
        # Rule 2: canonical background wrapper.
        inner = normalise_inner(span.inner)
        base = _strip_attr(_strip_attr(span.open_tag, "ttm:role"), "xmlns")
        base = _strip_attr(base, "xmlns:ttm")
        outer = f"{base.rstrip()[:-1]} xmlns:ttm=\"{TTM_NS}\" ttm:role=\"x-bg\" xmlns=\"{TTML_NS}\">"
        return f"{outer}{inner}{span.close}"
    return f"{_normalise_plain_span(span.open_tag)}{span.inner}{span.close}"


def normalise_inner(inner: str) -> str:
    """Normalise plain child spans inside a wrapper: strip default xmlns only."""
    spans = _extract_spans(inner)
    if not spans:
        return inner
    out: list[str] = []
    pos = 0
    for (s, e) in spans:
        out.append(inner[pos:s])
        child = parse_span(inner, s)
        plain_open = _strip_attr(_strip_attr(child.open_tag, "xmlns"), "xmlns:ttm")
        out.append(f"{plain_open}{child.inner}{child.close}")
        pos = e
    out.append(inner[pos:])
    return "".join(out)


def normalise_text_content(content: str) -> str:
    """Apply rules 1/2/3 to the content of one ``<text>`` element."""
    spans = _extract_spans(content)
    if not spans:
        return content
    out: list[str] = []
    pos = 0
    for (s, e) in spans:
        out.append(content[pos:s])
        out.append(normalise_span(parse_span(content, s)))
        pos = e
    out.append(content[pos:])
    return move_spaces_out_of_spans("".join(out))


def move_spaces_out_of_spans(content: str) -> str:
    """Rule 3: relocate spaces trapped inside a syllable to between spans."""
    spans = _extract_spans(content)
    if not spans:
        return content
    out: list[str] = []
    pos = 0
    for (s, e) in spans:
        out.append(content[pos:s])
        out.append(_relocate_span_spaces(content[s:e]))
        pos = e
    out.append(content[pos:])
    return "".join(out)


def _relocate_span_spaces(span_text: str) -> str:
    m = _SPAN_OPEN.match(span_text)
    if not m:
        return span_text
    open_tag = m.group(0)
    body = span_text[m.end(): -len("</span>")]
    left_space = len(body) - len(body.lstrip(" "))
    right_space = len(body) - len(body.rstrip(" "))
    inner = body[left_space:len(body) - right_space]
    return f"{' ' * left_space}{open_tag}{inner}</span>{' ' * right_space}"


# --- rules 4 & 5: pronunciation syllable spacing ---------------------------

def _letters(value: str) -> str:
    """Only alpha letters, for the rule-4 original-vs-pronunciation comparison."""
    return "".join(ch for ch in value if ch.isalpha())


def syllables(content: str) -> tuple[list[str], list[bool]]:
    """Return (span_texts, gap_after_bool) for the top-level spans in ``content``.

    ``gap_after[i]`` is True when there is literal whitespace between span ``i``
    and span ``i + 1``.
    """
    spans = _extract_spans(content)
    texts: list[str] = []
    gaps: list[bool] = []
    pos = 0
    for index, (s, e) in enumerate(spans):
        span = parse_span(content, s)
        texts.append(span.text)
        next_start = spans[index + 1][0] if index + 1 < len(spans) else len(content)
        gaps.append(bool(content[e:next_start].strip(" ")))
    return texts, gaps


def _key_of_tag(open_tag: str, attr: str) -> str | None:
    m = re.search(rf"\b{re.escape(attr)}\s*=\s*([\"'])(.*?)\1", open_tag, re.S)
    return m.group(2) if m else None


def original_syllable_map(text: str) -> dict[str, tuple[list[str], list[bool]]]:
    """Map ``itunes:key`` -> (syllable texts, gaps) from body ``<p>`` blocks."""
    result: dict[str, tuple[list[str], list[bool]]] = {}
    for m in _P_BLOCK.finditer(text):
        block = m.group(0)
        open_tag = re.match(r"<p\b[^>]*>", block)
        if not open_tag:
            continue
        key = _key_of_tag(open_tag.group(0), "itunes:key")
        if key is None:
            continue
        texts, gaps = syllables(block)
        result[key] = (texts, gaps)
    return result


def transliteration_lang(text: str) -> str | None:
    """Pronunciation language from ``<transliteration xml:lang="..">``."""
    lang = re.search(r"<transliteration\b[^>]*\bxml:lang\s*=\s*([\"'])(.*?)\1", text, re.S)
    if lang:
        return lang.group(2)
    tt_open = re.match(r"<tt\b[^>]*>", text)
    if tt_open:
        return _key_of_tag(tt_open.group(0), "xml:lang")
    return None


def apply_syllable_spacing(
    content: str,
    lang: str | None,
    original: tuple[list[str], list[bool]] | None,
) -> str:
    """Decide syllable gaps per rules 4/5 and rebuild ``<text>`` content."""
    spans = _extract_spans(content)
    if not spans:
        return content
    texts, gaps = syllables(content)
    n = len(texts)
    if n < 2:
        return content

    orig_texts, orig_gaps = original if original else ([], [])
    has_internal_space = any(" " in t for t in texts)
    has_gap_space = any(gaps)
    new_gaps: list[bool] = list(gaps)

    if lang in ROMANISATION_LANGS:
        # Rule 4: with no internal or gap spaces, add spaces between all
        # syllables, unless a pronunciation syllable matches the original's
        # letters (then follow the original spacing).
        if not has_internal_space and not has_gap_space:
            for i in range(n - 1):
                orig = orig_texts[i] if i < len(orig_texts) else ""
                if orig and _letters(texts[i]) == _letters(orig):
                    new_gaps[i] = bool(orig_gaps[i]) if i < len(orig_gaps) else False
                else:
                    new_gaps[i] = True
            # otherwise the pronunciation already carries spaces -> leave as-is
    else:
        # Rule 5: other languages follow the original syllable spacing.
        for i in range(n - 1):
            new_gaps[i] = bool(orig_gaps[i]) if i < len(orig_gaps) else False

    return _rebuild_with_gaps(content, new_gaps)


def _rebuild_with_gaps(content: str, new_gaps: list[bool]) -> str:
    spans = _extract_spans(content)
    if not spans:
        return content
    prefix = content[:spans[0][0]]
    suffix = content[spans[-1][1]:]
    pieces = [prefix]
    for i, (s, e) in enumerate(spans):
        pieces.append(content[s:e])
        if i < len(spans) - 1:
            pieces.append(" " if new_gaps[i] else "")
    pieces.append(suffix)
    return "".join(pieces)


# --- file-level -------------------------------------------------------------

def is_compressed_ttml(path: Path) -> bool:
    if path.suffix.lower() != ".ttml":
        return False
    if "-Format" in path.name:
        return False
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return False
    return text.count("\n") <= _COMPRESSED_MAX_NEWLINES


def find_compressed_ttml(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.ttml") if is_compressed_ttml(p))


def _resolve_root(root: str | Path | None) -> Path:
    """Resolve the scan root, defaulting to this repo's ``Lyrics`` directory."""
    if root:
        return Path(root).resolve()
    repo_root = Path(__file__).resolve().parents[2]
    for candidate in (repo_root / "Lyrics", repo_root):
        if candidate.is_dir():
            return candidate.resolve()
    return Path.cwd().resolve()


# --- legacy TTML migration --------------------------------------------------

_LEGACY_ROLES = {"x-translation", "x-transliteration", "x-roman"}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _role(node: ET.Element) -> str:
    return next((value for key, value in node.attrib.items() if _local_name(key) == "role"), "").lower()


def has_legacy(text: str) -> bool:
    """Return whether ``text`` still carries legacy body attachment roles."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return False
    body = next((node for node in root.iter() if _local_name(node.tag) == "body"), None)
    return bool(body is not None and any(_role(node) in _LEGACY_ROLES for node in body.iter()))


def convert_legacy(text: str) -> str:
    """Migrate body attachment spans into iTunesMetadata containers (string level).

    Mirrors ``core.convert_legacy_ttml`` but works on a string and never writes
    or backs up; the file-level driver owns backup/write.  Returns ``text``
    unchanged when there is nothing to migrate.
    """
    root = ET.fromstring(text)
    legacy: list[tuple[ET.Element, ET.Element, str, str, str]] = []
    for p in (node for node in root.iter() if _local_name(node.tag) == "p"):
        line_id = (p.attrib.get(f"{{{XML_NS}}}id") or p.attrib.get("id") or
                   next((v for k, v in p.attrib.items() if _local_name(k) == "key"), ""))
        if not line_id:
            continue
        backgrounds = [node for node in p if _role(node) == "x-bg"]
        for parent in p.iter():
            for child in list(parent):
                role = _role(child)
                if role in _LEGACY_ROLES:
                    kind = "translations" if role == "x-translation" else "transliterations"
                    language = child.attrib.get(f"{{{XML_NS}}}lang", "")
                    target_id = line_id
                    if _role(parent) == "x-bg":
                        target_id = f"{line_id}:bg{backgrounds.index(parent) + 1}"
                    legacy.append((parent, child, target_id, kind, language))
    if not legacy:
        return text
    metadata = next((node for node in root.iter() if _local_name(node.tag) == "metadata"), None)
    if metadata is None:
        head = next((node for node in root if _local_name(node.tag) == "head"), None)
        if head is None:
            head = ET.Element(f"{{{TTML_NS}}}head")
            root.insert(0, head)
        metadata = ET.SubElement(head, f"{{{TTML_NS}}}metadata")
    itunes = next((node for node in metadata if _local_name(node.tag) == "iTunesMetadata"), None)
    if itunes is None:
        itunes = ET.SubElement(metadata, f"{{{ITUNES_NS}}}iTunesMetadata")
    for parent, child, line_id, kind, language in legacy:
        container = next((node for node in itunes if _local_name(node.tag) == kind), None)
        if container is None:
            container = ET.SubElement(itunes, f"{{{ITUNES_NS}}}{kind}")
        singular = kind[:-1]
        group = next((node for node in container if _local_name(node.tag) == singular and
                      node.attrib.get(f"{{{XML_NS}}}lang", "") == language), None)
        if group is None:
            group = ET.SubElement(container, f"{{{ITUNES_NS}}}{singular}")
            if language:
                group.set(f"{{{XML_NS}}}lang", language)
        target = ET.SubElement(group, f"{{{ITUNES_NS}}}text", {"for": line_id})
        target.text = child.text
        for nested in list(child):
            target.append(nested)
        parent.remove(child)
    for prefix, uri in (("", TTML_NS), ("ttm", TTM_NS), ("amll", AMLL_NS),
                        ("itunes", ITUNES_NS), ("tts", TTS_NS)):
        ET.register_namespace(prefix, uri)
    output = ET.tostring(root, encoding="unicode")
    ET.fromstring(output)
    return output


# --- file-level -------------------------------------------------------------


def format_ttml(text: str) -> str:
    """Normalise transliteration spans in a compressed TTML (rules 1-5)."""
    original = original_syllable_map(text)
    lang = transliteration_lang(text)

    def fix_text(m: "re.Match[str]") -> str:
        content = m.group(0)
        content = normalise_text_content(content)                     # rules 1, 2, 3
        open_tag = re.match(r"<text\b[^>]*>", content)
        key = _key_of_tag(open_tag.group(0), "for") if open_tag else None
        content = apply_syllable_spacing(content, lang, original.get(key))  # rules 4, 5
        return content

    def fix_block(blk: "re.Match[str]") -> str:
        return re.sub(_TEXT_BLOCK, fix_text, blk.group(0))

    new = re.sub(r"<transliterations\b.*?</transliterations\s*>",
                 fix_block, text, count=1, flags=re.S)
    return new


# --- pretty-printed -Format.ttml --------------------------------------------

_INDENT = "    "


def pretty_format_ttml(text: str) -> str:
    """Produce a multi-line, indented ``-Format`` version of TTML ``text``.

    Inter-span literal spaces inside transliteration ``<text>`` and body ``<p>``
    become ``<space/>`` elements, and spans are placed on their own indented
    lines.  Everything else (head/metadata, attributes and their order) is
    preserved verbatim.
    """
    # 1. Convert inter-span literal spaces into <space/> inside lyrics/prose.
    text = _spaces_to_space_el(text)
    # 2. Pretty-print the document.
    return _pretty_indent(text)


def _spaces_to_space_el(text: str) -> str:
    def convert(block: "re.Match[str]") -> str:
        body = block.group(0)
        spans = _extract_spans(body)
        if not spans:
            return body
        pieces = [body[:spans[0][0]]]
        for index, (s, e) in enumerate(spans):
            pieces.append(body[s:e])
            if index + 1 < len(spans):
                gap = body[e:spans[index + 1][0]]
                pieces.append("<space/>" if gap and gap.isspace() else gap)
        pieces.append(body[spans[-1][1]:])
        return "".join(pieces)

    out = re.sub(_TEXT_BLOCK, convert, text)
    return re.sub(_P_BLOCK, convert, out)

    out = re.sub(_TEXT_BLOCK, convert, text)
    return re.sub(_P_BLOCK, convert, out)


def _pretty_indent(text: str) -> str:
    """Pretty-print XML text, preserving attribute order and inline spans.

    Elements whose content is a single non-whitespace text run stay on one line
    (``<span>ashi</span>``); elements that nest children are indented.  The
    result stays valid XML (validated in tests).
    """
    tokens = _tokenize(text)
    out: list[str] = []
    stack: list[str] = []          # open element names that are containers
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        kind = token[0]
        value = token[1]
        if kind == "comment":
            out.append(value)
            i += 1
            continue
        if kind == "close":
            name = _tag_name(value)
            if stack and stack[-1] == name:
                stack.pop()
            indent = "\n" + _INDENT * len(stack)
            out.append(f"{indent}{value}")
            i += 1
            continue
        if kind == "open":
            name = _tag_name(value)
            self_close = _is_self_close(value)
            indent = "\n" + _INDENT * len(stack)
            if self_close:
                out.append(f"{indent}{value}")
                i += 1
                continue
            # Look ahead for a single text run directly inside -> inline element.
            j = i + 1
            while j < n and tokens[j][0] == "comment":
                j += 1
            inline_text = tokens[j][0] == "text" and tokens[j][1].strip()
            if inline_text and (j + 1 >= n or tokens[j + 1][0] == "close"):
                # inline: open + text + close on one line.
                close_value = tokens[j + 1][1] if j + 1 < n else ""
                out.append(f"{indent}{value}{tokens[j][1]}{close_value}")
                i = j + 2
                continue
            out.append(f"{indent}{value}")
            stack.append(name)
            i += 1
            continue
        # text
        if value.strip():
            out.append(value)
        i += 1
    return "".join(out)


def _tokenize(text: str) -> list[tuple[str, str]]:
    """Return (kind, value) tokens: open, close, self_close, text, comment."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    n = len(text)
    while pos < n:
        if text.startswith("<!--", pos):
            end = text.find("-->", pos) + 3
            tokens.append(("comment", text[pos:end]))
            pos = end
            continue
        if text[pos] == "<":
            if text.startswith("</", pos):
                end = text.find(">", pos) + 1
                tokens.append(("close", text[pos:end]))
            else:
                end = text.find(">", pos) + 1
                raw = text[pos:end]
                tokens.append(("self_close", raw) if _is_self_close(raw) else ("open", raw))
            pos = end
            continue
        end = text.find("<", pos)
        if end < 0:
            end = n
        tokens.append(("text", text[pos:end]))
        pos = end
    return tokens


def _tag_name(token: str) -> str:
    m = re.match(r"</?\s*([^\s/>]+)", token)
    return m.group(1) if m else ""


def _is_self_close(token: str) -> bool:
    return bool(re.match(r"<[^>]*/>", token))


def process_file(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8-sig")
    # Migrate legacy body attachment spans first (translations/transliterations),
    # then normalise the transliteration spans (rules 1-5).
    if has_legacy(text):
        text = convert_legacy(text)
    new = format_ttml(text)
    changed = new != text
    format_path = path.with_name(path.stem + "-Format.ttml")
    if changed:
        bak = path.with_name(path.name + ".bak")
        if bak.exists():
            bak.unlink()
        path.replace(bak)
        path.write_text(new, encoding="utf-8")
    pretty = pretty_format_ttml(new if changed else text)
    if format_path.exists():
        fb = format_path.with_name(format_path.name + ".bak")
        if fb.exists():
            fb.unlink()
        format_path.replace(fb)
    format_path.write_text(pretty, encoding="utf-8")
    return {"path": str(path), "changed": changed, "format": str(format_path)}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Format compressed TTML lyrics.")
    parser.add_argument("root", nargs="?", default=None,
                        help="lyrics root to scan (default: this repo's Lyrics dir)")
    parser.add_argument("--list", action="store_true", help="only list candidates")
    args = parser.parse_args(argv)
    root = _resolve_root(args.root)
    candidates = find_compressed_ttml(root)
    if args.list:
        for p in candidates:
            print(p)
        return 0
    changed = 0
    for p in candidates:
        try:
            result = process_file(p)
        except Exception as exc:  # noqa: BLE001
            print(f"skipped: {p} ({exc})", file=sys.stderr)
            continue
        if result.get("changed"):
            changed += 1
        print(f"formatted: {p}")
        print(f"    -> {result.get('format')}")
    print(f"\n{changed} content-changed, {len(candidates)} candidates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
