"""Bounded top-level MP4 atom inspection for optional original-file reuse."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_sources import MAX_MP4_ATOMS, read_mp4_faststart


def atom(kind, data=b"", *, extended=False):
    size = len(data) + (16 if extended else 8)
    return ((1).to_bytes(4, "big") + kind + size.to_bytes(8, "big") if extended
            else size.to_bytes(4, "big") + kind) + data


class MP4FaststartTests(unittest.TestCase):
    def inspect(self, data):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "source.mp4")
            path.write_bytes(data)
            return read_mp4_faststart(path)

    def test_order_extended_sizes_and_eof_sized_media(self):
        prefix = atom(b"ftyp", b"isom") + atom(b"moov", b"index", extended=True)
        self.assertTrue(self.inspect(prefix + atom(b"mdat", b"media")))
        self.assertTrue(self.inspect(prefix + b"\0\0\0\0mdatmedia"))
        self.assertFalse(self.inspect(atom(b"mdat", b"media") + atom(b"moov", b"index")))

    def test_payload_text_cannot_masquerade_as_moov(self):
        self.assertFalse(self.inspect(atom(b"free", b"moov") + atom(b"mdat", b"media")))
        self.assertTrue(self.inspect(atom(b"free", b"mdat") + atom(b"moov") + atom(b"mdat")))

    def test_malformed_truncated_duplicate_and_excessive_headers_disable_preservation(self):
        for data in (b"", b"short", b"\0\0\0\x04moov", b"\0\0\0\x20moov",
                     b"\0\0\0\x01moov123", atom(b"moov"), atom(b"moov") * 2 + atom(b"mdat"),
                     atom(b"free") * MAX_MP4_ATOMS + atom(b"moov") + atom(b"mdat")):
            with self.subTest(data=data[:32]):
                self.assertFalse(self.inspect(data))
        self.assertFalse(read_mp4_faststart("missing-source.mp4"))

    def test_atom_payloads_are_skipped_without_reading_them(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "source.mp4")
            path.write_bytes(atom(b"free", b"x" * 65536) + atom(b"moov") + atom(b"mdat", b"y" * 65536))
            with path.open("rb") as stream, patch("media_sources.open", return_value=stream), \
                    patch.object(stream, "read", wraps=stream.read) as read:
                self.assertTrue(read_mp4_faststart(path))
                self.assertTrue(all(call.args == (8,) for call in read.call_args_list))
                self.assertEqual(read.call_count, 3)


if __name__ == "__main__":
    unittest.main()
