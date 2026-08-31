# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0. See http://www.apache.org/licenses/LICENSE-2.0

import re
import unittest

from tools.lyrics_manager import ttml_formatter as fmt

TTML = ("http://www.w3.org/ns/ttml")
TTM = ("http://www.w3.org/ns/ttml#metadata")


class SpanNormalisationTests(unittest.TestCase):
    def test_rule1_normal_span_gets_xmlns(self) -> None:
        inp = '<span begin="36.626" end="37.988">mo</span>'
        out = fmt.normalise_span(fmt.parse_span(inp, 0))
        self.assertEqual(out, f'<span begin="36.626" end="37.988" xmlns="{TTML}">mo</span>')

    def test_rule2_xbg_wrapper(self) -> None:
        inp = ('<span ttm:role="x-bg">'
               '<span begin="00:38.631" end="00:39.089">(One</span> '
               '<span begin="00:39.089" end="00:39.563">two</span> '
               '<span begin="00:39.563" end="00:39.789">three)</span></span>')
        out = fmt.normalise_span(fmt.parse_span(inp, 0))
        self.assertEqual(
            out,
            f'<span xmlns:ttm="{TTM}" ttm:role="x-bg" xmlns="{TTML}">'
            '<span begin="00:38.631" end="00:39.089">(One</span> '
            '<span begin="00:39.089" end="00:39.563">two</span> '
            '<span begin="00:39.563" end="00:39.789">three)</span></span>',
        )

    def test_rule3_move_space_out_of_span(self) -> None:
        inp = '<span begin="3:11.342" end="3:12.272">horizon </span><span begin="3:12.272" end="3:12.693">dreamer</span>'
        out = fmt.move_spaces_out_of_spans(inp)
        self.assertEqual(
            out,
            '<span begin="3:11.342" end="3:12.272">horizon</span> '
            '<span begin="3:12.272" end="3:12.693">dreamer</span>',
        )

    def test_rule1_keeps_existing_attr_order(self) -> None:
        inp = '<span begin="1.0" end="2.0" ttm:agent="v1">hi</span>'
        out = fmt.normalise_span(fmt.parse_span(inp, 0))
        self.assertEqual(out, f'<span begin="1.0" end="2.0" ttm:agent="v1" xmlns="{TTML}">hi</span>')

    def test_xbg_inner_not_renamespaced(self) -> None:
        inp = ('<span xmlns="http://www.w3.org/ns/ttml" ttm:role="x-bg">'
               '<span xmlns="" begin="00:38.631" end="00:39.089">(One</span></span>')
        out = fmt.normalise_span(fmt.parse_span(inp, 0))
        self.assertIn('xmlns:ttm', out)
        self.assertIn('ttm:role="x-bg"', out)
        self.assertNotIn('<span xmlns=""', out)  # stray empty xmlns removed
        self.assertIn('<span begin="00:38.631" end="00:39.089">(One</span>', out)


class TextBlockTests(unittest.TestCase):
    def test_formats_a_text_element(self) -> None:
        content = ('<span begin="00:06.783" end="00:07.430">hirake</span>'
                   '<span begin="00:07.430" end="00:07.800">goma</span>')
        out = fmt.normalise_text_content(content)
        self.assertIn(f'xmlns="{TTML}"', out)
        self.assertTrue(out.count(f'xmlns="{TTML}"') == 2)

    def test_compressed_detection(self) -> None:
        path = fmt.Path(__file__).resolve().parent.parent / "ttml_formatter.py"
        # A real TTML sample path is checked by is_compressed on content; here we
        # just ensure the helper never crashes on a non-ttml path.
        self.assertFalse(fmt.is_compressed_ttml(path))


class SyllableSpacingTests(unittest.TestCase):
    def test_rule4_rom_and_no_spaces_adds_between_all(self) -> None:
        content = ('<span begin="0" end="1">ashi</span><span begin="1" end="2">moto</span><span begin="2" end="3">ni</span>')
        out = fmt.apply_syllable_spacing(content, "ja-Latn", None)
        self.assertEqual(
            out,
            '<span begin="0" end="1">ashi</span> <span begin="1" end="2">moto</span> <span begin="2" end="3">ni</span>',
        )

    def test_rule4_has_existing_spaces_leaves_alone(self) -> None:
        content = ('<span begin="0" end="1">a b</span><span begin="1" end="2">c</span>')
        out = fmt.apply_syllable_spacing(content, "ja-Latn", None)
        self.assertEqual(out, content)  # internal space -> not touched

    def test_rule4_exception_matches_original_letters(self) -> None:
        content = ('<span begin="0" end="1">ka</span><span begin="1" end="2">ra</span><span begin="2" end="3">wa</span>')
        original = (["ka", "kara", "wa"], [False, True, False])  # ka|kara no gap, kara|wa gap
        out = fmt.apply_syllable_spacing(content, "ja-Latn", original)
        self.assertEqual(
            out,
            '<span begin="0" end="1">ka</span><span begin="1" end="2">ra</span> <span begin="2" end="3">wa</span>',
        )

    def test_rule5_other_lang_follows_original(self) -> None:
        content = ('<span begin="0" end="1">hi</span><span begin="1" end="2">you</span><span begin="2" end="3">de</span>')
        original = (["hi", "you", "de"], [True, False, True])
        out = fmt.apply_syllable_spacing(content, "fr", original)
        self.assertEqual(
            out,
            '<span begin="0" end="1">hi</span> <span begin="1" end="2">you</span><span begin="2" end="3">de</span>',
        )

    def test_letters_only_comparison_ignores_symbols(self) -> None:
        self.assertEqual(fmt._letters("(One)"), "One")
        self.assertEqual(fmt._letters("you're"), "youre")

    def test_relocate_preserves_internal_spaces(self) -> None:
        inp = '<span begin="0" end="1">no you de</span>'
        out = fmt._relocate_span_spaces(inp)
        self.assertEqual(out, '<span begin="0" end="1">no you de</span>')


class LegacyConversionTests(unittest.TestCase):
    def _legacy(self) -> str:
        return (
            '<tt xmlns="http://www.w3.org/ns/ttml" '
            'xmlns:ttm="http://www.w3.org/ns/ttml#metadata">'
            '<head><metadata></metadata></head>'
            '<body dur="3:00"><div><p xml:id="L1" xml:lang="en">'
            '<span begin="0" end="1">Hi</span>'
            '<span ttm:role="x-translation" xml:lang="zh">你好</span>'
            '<span ttm:role="x-transliteration" xml:lang="ja-Latn">hai</span>'
            '</p></div></body></tt>'
        )

    def _clean(self) -> str:
        return (
            '<tt xmlns="http://www.w3.org/ns/ttml" '
            'xmlns:ttm="http://www.w3.org/ns/ttml#metadata">'
            '<head><metadata></metadata></head>'
            '<body dur="3:00"><div><p xml:id="L1" xml:lang="en">'
            '<span begin="0" end="1">Hi</span>'
            '</p></div></body></tt>'
        )

    def test_has_legacy_true(self) -> None:
        self.assertTrue(fmt.has_legacy(self._legacy()))

    def test_has_legacy_false(self) -> None:
        self.assertFalse(fmt.has_legacy(self._clean()))

    def test_convert_legacy_moves_to_itunes(self) -> None:
        import xml.etree.ElementTree as ET
        out = fmt.convert_legacy(self._legacy())
        self.assertNotEqual(out, self._legacy())
        root = ET.fromstring(out)
        # Translation + transliteration now live under head > metadata > iTunesMetadata.
        self.assertTrue([n for n in root.iter() if fmt._local_name(n.tag) == "translations"])
        texts = [n for n in root.iter() if fmt._local_name(n.tag) == "text"]
        self.assertTrue(any(n.attrib.get("for") == "L1" for n in texts))
        # The body no longer carries the x-translation/x-transliteration spans.
        body = next(n for n in root.iter() if fmt._local_name(n.tag) == "body")
        self.assertFalse(any(fmt._role(n) in {"x-translation", "x-transliteration"} for n in body.iter()))

    def test_convert_legacy_noop_when_clean(self) -> None:
        self.assertEqual(fmt.convert_legacy(self._clean()), self._clean())


class EndToEndTests(unittest.TestCase):
    def test_format_ttml_wraps_transliteration(self) -> None:
        raw = (
            '<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata">'
            '<head><metadata>'
            '<iTunesMetadata xmlns="http://music.apple.com/lyric-ttml-internal">'
            '<transliterations><transliteration>'
            '<text for="L1"><span begin="00:01.0" end="00:02.0">da</span><span begin="00:02.0" end="00:03.0">bu</span></text>'
            '</transliteration></transliterations>'
            '</iTunesMetadata></metadata></head>'
            '<body><div><p itunes:key="L1"><span begin="00:01.0" end="00:02.0">大</span><span begin="00:02.0" end="00:03.0">步</span></p></div></body></tt>'
        )
        out = fmt.format_ttml(raw)
        self.assertIn(f'<span begin="00:01.0" end="00:02.0" xmlns="{TTML}">da</span>', out)

    def test_pretty_format_is_valid_xml(self) -> None:
        import xml.etree.ElementTree as ET
        raw = (
            '<tt xmlns="http://www.w3.org/ns/ttml" '
            'xmlns:ttm="http://www.w3.org/ns/ttml#metadata" '
            'xmlns:itunes="http://music.apple.com/lyric-ttml-internal">'
            '<head><metadata><ttm:agent type="person" xml:id="v1"/></metadata></head>'
            '<body dur="3:00"><div><p begin="0" end="1" itunes:key="L1">'
            '<span begin="0" end="1">You</span> <span begin="1" end="2">are</span></p></div></body></tt>'
        )
        out = fmt.pretty_format_ttml(raw)
        # Must parse as valid XML.
        ET.fromstring(out)
        self.assertIn("<space/>", out)
        self.assertIn("\n", out)  # multi-line / indented
        # Inline span is kept on one line.
        self.assertIn('<span begin="0" end="1">You</span>', out)


if __name__ == "__main__":
    unittest.main()
