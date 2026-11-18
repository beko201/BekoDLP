"""Bounded EBML header inspection and strict WebM/Matroska plan verification."""
from pathlib import Path
import tempfile
import unittest

from media_options import OUTPUT_DEFAULTS
from media_plan import MediaSources, SourceStream, build_output_plan
from media_processing import verify_output
from media_sources import MAX_EBML_HEADER_BYTES, parse_ebml_doctype, read_ebml_doctype


ROOT = b"\x1a\x45\xdf\xa3"
WEBM = b"\x42\x82\x84webm"
MATROSKA = b"\x42\x82\x88matroska"


class EbmlDocTypeTests(unittest.TestCase):
    def test_known_doctypes_and_variable_width_sizes(self):
        for data, expected in ((ROOT + b"\x87" + WEBM, "webm"),
                               (ROOT + b"\x8b" + MATROSKA, "matroska"),
                               (ROOT + b"\x40\x07" + WEBM, "webm"),
                               (ROOT + b"\x01\x00\x00\x00\x00\x00\x00\x08"
                                + b"\x42\x82\x40\x04webm", "webm"),
                               (ROOT + b"\x89\x42\x82\x86webm\x00\x00", "webm")):
            with self.subTest(data=data):
                self.assertEqual(parse_ebml_doctype(data), expected)

    def test_doctype_bytes_in_void_or_body_are_not_header_elements(self):
        self.assertEqual(parse_ebml_doctype(ROOT + b"\x94\xec\x87" + WEBM + MATROSKA), "matroska")
        self.assertEqual(parse_ebml_doctype(ROOT + b"\x8b" + MATROSKA + WEBM), "matroska")
        with self.assertRaisesRegex(ValueError, "Missing EBML DocType"):
            parse_ebml_doctype(ROOT + b"\x89\xec\x87" + WEBM)

    def test_invalid_missing_duplicate_and_truncated_headers_fail(self):
        invalid = (b"", b"webm", ROOT, ROOT + b"\x00", ROOT + b"\xff",
                   ROOT + b"\x40", ROOT + b"\x8b" + MATROSKA[:-1],
                   ROOT + b"\x80" + WEBM, ROOT + b"\x87\x42\x82\x85webm",
                   ROOT + b"\x87\x42\x82\xffwebm", ROOT + b"\x83\x42\x82\x80",
                   ROOT + b"\x8e" + WEBM + WEBM,
                   ROOT + b"\x84\x42\x82\x81\xff", ROOT + b"\x81\x00")
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_ebml_doctype(data)

    def test_large_header_is_rejected_before_attempting_to_parse_its_payload(self):
        with self.assertRaisesRegex(ValueError, "inspection limit"):
            parse_ebml_doctype(ROOT + b"\x21\x00\x01")
        self.assertLess(MAX_EBML_HEADER_BYTES, 1024 * 1024)

    def test_reader_uses_header_even_when_extension_claims_webm(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "misnamed.webm")
            path.write_bytes(ROOT + b"\x8b" + MATROSKA)
            self.assertEqual(read_ebml_doctype(path), "matroska")
            path.write_bytes(ROOT + b"\x87" + WEBM[:-1])
            with self.assertRaisesRegex(RuntimeError, "DocType could not be verified"):
                read_ebml_doctype(path)

    def test_shared_ffprobe_names_require_correct_doctype_for_each_plan(self):
        sources = MediaSources((Path("input.mkv"),), (
            SourceStream(0, 0, "video", "vp9"), SourceStream(0, 1, "audio", "opus")), 1)
        probe = {"streams": [{"codec_type": "video", "codec_name": "vp9"},
                             {"codec_type": "audio", "codec_name": "opus"}],
                 "format": {"format_name": "matroska,webm"}}
        for container, expected in (("webm", "webm"), ("mkv", "matroska")):
            plan = build_output_plan({**OUTPUT_DEFAULTS, "container": container}, sources, set())
            verify_output(probe, plan, ebml_doctype=expected)
            for wrong in (None, "", "unknown", "matroska" if expected == "webm" else "webm"):
                with self.subTest(container=container, doctype=wrong), self.assertRaisesRegex(RuntimeError, "container"):
                    verify_output(probe, plan, ebml_doctype=wrong)


if __name__ == "__main__":
    unittest.main()
