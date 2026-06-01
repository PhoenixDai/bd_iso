import os
import tempfile
import unittest

from bd_iso_state import (
    BdIsoStateStore,
    STATUS_NOT_TRIED,
    STATUS_VERIFIED,
    get_state_path,
)
from compare_bd_iso import compare_bd_to_iso, parse_start_offset_arg


class CompareBdIsoArgParsingTests(unittest.TestCase):
    def test_parse_start_offset_arg_accepts_decimal_and_hex(self):
        self.assertEqual(parse_start_offset_arg("1048576"), 1048576)
        self.assertEqual(parse_start_offset_arg("0x100000"), 0x100000)

    def test_parse_start_offset_arg_accepts_resume_aliases(self):
        self.assertIsNone(parse_start_offset_arg("resume"))
        self.assertIsNone(parse_start_offset_arg("--resume"))
        self.assertIsNone(parse_start_offset_arg(" Resume "))

    def test_parse_start_offset_arg_rejects_invalid_text(self):
        with self.assertRaises(ValueError):
            parse_start_offset_arg("later")


class CompareBdIsoStateTests(unittest.TestCase):
    def test_verify_from_offset_does_not_mark_earlier_chunks_verified(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_path = os.path.join(tempdir, "disc.bin")
            iso_path = os.path.join(tempdir, "movie.iso")
            state_path = get_state_path(iso_path)
            chunk_size = 4

            data = b"aaaabbbbccccdddd"
            with open(source_path, "wb") as handle:
                handle.write(data)
            with open(iso_path, "wb") as handle:
                handle.write(data)

            verified = compare_bd_to_iso(
                source_path,
                iso_path,
                chunk_size=chunk_size,
                start_offset=chunk_size * 2,
                state_path=state_path,
                sector_size=1,
            )

            self.assertTrue(verified)
            store = BdIsoStateStore.load(iso_path, state_path=state_path)
            self.assertEqual(store.effective_chunk_status(0), STATUS_NOT_TRIED)
            self.assertEqual(store.effective_chunk_status(1), STATUS_NOT_TRIED)
            self.assertEqual(store.effective_chunk_status(2), STATUS_VERIFIED)
            self.assertEqual(store.effective_chunk_status(3), STATUS_VERIFIED)


if __name__ == "__main__":
    unittest.main()
