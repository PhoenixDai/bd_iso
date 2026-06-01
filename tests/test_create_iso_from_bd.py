import os
import tempfile
import unittest
from unittest.mock import patch

from bd_iso_state import (
    BdIsoStateStore,
    STATUS_COPIED,
    STATUS_ZERO_FILLED,
    get_state_path,
)
from create_iso_from_bd import (
    BAD_PREFIX,
    CHECKPOINT_PREFIX,
    ProgressFileError,
    create_iso_from_bd,
    get_copy_progress_path,
    parse_copy_progress,
    read_range_with_retries,
    validate_resume_state,
)


class PlannedSource:
    def __init__(self, data, plans=None):
        self._data = data
        self._plans = plans or []
        self._position = 0
        self.read_calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def seek(self, offset, whence=0):
        if whence != 0:
            raise ValueError("Only absolute seeks are supported in tests.")
        self._position = offset

    def read(self, size=-1):
        start = self._position
        end = len(self._data) if size < 0 else min(len(self._data), start + size)
        self.read_calls.append((start, size))

        if self._plans:
            action = self._plans[0]
            if action["match"](start, size):
                self._plans.pop(0)
                result = action["result"]
                if isinstance(result, BaseException):
                    raise result
                if isinstance(result, int):
                    end = min(end, start + result)

        chunk = self._data[start:end]
        self._position = end
        return chunk


def open_planned_source(data, plans=None):
    return lambda _path: PlannedSource(data, plans=list(plans or []))


class CreateIsoFromBdTests(unittest.TestCase):
    def test_parse_copy_progress_and_validate_resume(self):
        with tempfile.TemporaryDirectory() as tempdir:
            iso_path = os.path.join(tempdir, "movie.iso")
            source_path = os.path.join(tempdir, "disc.bin")
            progress_path = get_copy_progress_path(iso_path)

            with open(progress_path, "w", encoding="utf-8") as handle:
                handle.write(f"SOURCE {os.path.realpath(source_path)}\n")
                handle.write("DEVICE_SIZE 64\n")
                handle.write("CHUNK_SIZE 16\n")
                handle.write("CHECKPOINT 48\n")
                handle.write("BAD 32 4\n")

            progress = parse_copy_progress(progress_path)
            self.assertEqual(progress.source_path, os.path.realpath(source_path))
            self.assertEqual(progress.device_size, 64)
            self.assertEqual(progress.chunk_size, 16)
            self.assertEqual(progress.checkpoint, 48)
            self.assertEqual(progress.bad_regions, [(32, 4)])

            resume_offset = validate_resume_state(progress, source_path, 64, 16, 40)
            self.assertEqual(resume_offset, 40)

    def test_validate_resume_accepts_reconnected_optical_drive_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            iso_path = os.path.join(tempdir, "movie.iso")
            source_path = "/dev/sr0"
            progress_path = get_copy_progress_path(iso_path)

            with open(progress_path, "w", encoding="utf-8") as handle:
                handle.write("SOURCE /dev/sr0\n")
                handle.write("DEVICE_SIZE 64\n")
                handle.write("CHUNK_SIZE 16\n")
                handle.write("CHECKPOINT 48\n")

            progress = parse_copy_progress(progress_path)

            with patch("create_iso_from_bd.canonicalize_source_path", return_value="/dev/sr1"):
                resume_offset = validate_resume_state(progress, source_path, 64, 16, 40)
            self.assertEqual(resume_offset, 40)

    def test_parse_copy_progress_rejects_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tempdir:
            progress_path = os.path.join(tempdir, "bad.copy.txt")
            with open(progress_path, "w", encoding="utf-8") as handle:
                handle.write("SOURCE /tmp/disc\n")
                handle.write("BROKEN nope\n")

            with self.assertRaises(ProgressFileError):
                parse_copy_progress(progress_path)

    def test_create_iso_copies_regular_file_source(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_path = os.path.join(tempdir, "disc.bin")
            iso_path = os.path.join(tempdir, "movie.iso")
            data = (b"0123456789abcdef" * 1024) + b"tail"

            with open(source_path, "wb") as handle:
                handle.write(data)

            success = create_iso_from_bd(
                source_path,
                iso_path,
                chunk_size=4096,
                retries=0,
                retry_delay=0,
                min_read_size=2048,
                compare_func=lambda *_args, **_kwargs: True,
            )

            self.assertTrue(success)
            with open(iso_path, "rb") as handle:
                self.assertEqual(handle.read(), data)

            progress = parse_copy_progress(get_copy_progress_path(iso_path))
            self.assertEqual(progress.checkpoint, len(data))

            with open(get_copy_progress_path(iso_path), "r", encoding="utf-8") as handle:
                checkpoint_count = sum(
                    1 for line in handle if line.startswith(f"{CHECKPOINT_PREFIX} ")
                )
            self.assertEqual(checkpoint_count, 1)

    def test_resume_after_interrupted_run_uses_checkpoint(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_path = os.path.join(tempdir, "disc.bin")
            iso_path = os.path.join(tempdir, "movie.iso")
            data = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"

            first_success = create_iso_from_bd(
                source_path,
                iso_path,
                chunk_size=8,
                retries=0,
                retry_delay=0,
                min_read_size=4,
                open_source=open_planned_source(
                    data,
                    plans=[
                        {
                            "match": lambda start, size: start == 8 and size == 8,
                            "result": KeyboardInterrupt(),
                        }
                    ],
                ),
                size_func=lambda _path: len(data),
                compare_func=lambda *_args, **_kwargs: True,
            )

            self.assertFalse(first_success)
            progress = parse_copy_progress(get_copy_progress_path(iso_path))
            self.assertEqual(progress.checkpoint, 8)

            second_success = create_iso_from_bd(
                source_path,
                iso_path,
                chunk_size=8,
                retries=0,
                retry_delay=0,
                min_read_size=4,
                resume=True,
                open_source=open_planned_source(data),
                size_func=lambda _path: len(data),
                compare_func=lambda *_args, **_kwargs: True,
            )

            self.assertTrue(second_success)
            with open(iso_path, "rb") as handle:
                self.assertEqual(handle.read(), data)

    def test_resume_compacts_legacy_checkpoint_log(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_path = os.path.join(tempdir, "disc.bin")
            iso_path = os.path.join(tempdir, "movie.iso")
            progress_path = get_copy_progress_path(iso_path)
            data = b"abcdefgh"

            with open(iso_path, "wb") as handle:
                handle.write(data)
            with open(progress_path, "w", encoding="utf-8") as handle:
                handle.write(f"SOURCE {os.path.realpath(source_path)}\n")
                handle.write("DEVICE_SIZE 8\n")
                handle.write("CHUNK_SIZE 4\n")
                handle.write("CHECKPOINT 0\n")
                handle.write("CHECKPOINT 4\n")
                handle.write("CHECKPOINT 8\n")

            success = create_iso_from_bd(
                source_path,
                iso_path,
                chunk_size=4,
                retries=0,
                retry_delay=0,
                min_read_size=4,
                resume=True,
                open_source=open_planned_source(data),
                size_func=lambda _path: len(data),
                compare_func=lambda *_args, **_kwargs: True,
            )

            self.assertTrue(success)
            with open(progress_path, "r", encoding="utf-8") as handle:
                checkpoint_count = sum(
                    1 for line in handle if line.startswith(f"{CHECKPOINT_PREFIX} ")
                )
            self.assertEqual(checkpoint_count, 1)

    def test_short_read_is_retried(self):
        source = PlannedSource(
            b"abcdefgh",
            plans=[
                {
                    "match": lambda start, size: start == 0 and size == 8,
                    "result": 3,
                }
            ],
        )

        data = read_range_with_retries(
            source,
            0,
            8,
            retries=1,
            retry_delay=0,
            min_read_size=4,
        )

        self.assertEqual(data, b"abcdefgh")
        self.assertEqual(source.read_calls, [(0, 8), (0, 8)])

    def test_unreadable_region_is_logged_and_stops_copy(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_path = os.path.join(tempdir, "disc.bin")
            iso_path = os.path.join(tempdir, "movie.iso")
            data = b"0123456789ABCDEF"

            success = create_iso_from_bd(
                source_path,
                iso_path,
                chunk_size=16,
                retries=0,
                retry_delay=0,
                min_read_size=4,
                open_source=open_planned_source(
                    data,
                    plans=[
                        {
                            "match": lambda start, size: start <= 8 < start + size,
                            "result": OSError("simulated media read failure"),
                        },
                        {
                            "match": lambda start, size: start <= 8 < start + size,
                            "result": OSError("simulated media read failure"),
                        },
                        {
                            "match": lambda start, size: start <= 8 < start + size,
                            "result": OSError("simulated media read failure"),
                        },
                    ],
                ),
                size_func=lambda _path: len(data),
                compare_func=lambda *_args, **_kwargs: True,
            )

            self.assertFalse(success)
            progress = parse_copy_progress(get_copy_progress_path(iso_path))
            self.assertEqual(progress.checkpoint, 0)
            self.assertIn((8, 4), progress.bad_regions)

            with open(get_copy_progress_path(iso_path), "r", encoding="utf-8") as handle:
                self.assertIn(f"{BAD_PREFIX} 8 4", handle.read())

    def test_resume_from_offset_leaves_prior_gap_zero_filled(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_path = os.path.join(tempdir, "disc.bin")
            iso_path = os.path.join(tempdir, "movie.iso")
            state_path = get_state_path(iso_path)
            data = b"AAAABBBBCCCCDDDD"

            with open(source_path, "wb") as handle:
                handle.write(data)
            with open(iso_path, "wb") as handle:
                handle.write(data[:4])

            success = create_iso_from_bd(
                source_path,
                iso_path,
                chunk_size=4,
                retries=0,
                retry_delay=0,
                min_read_size=4,
                resume=True,
                start_offset_override=8,
                state_path=state_path,
                size_func=lambda _path: len(data),
                compare_func=lambda *_args, **_kwargs: True,
            )

            self.assertTrue(success)
            with open(iso_path, "rb") as handle:
                self.assertEqual(handle.read(), b"\x00\x00\x00\x00\x00\x00\x00\x00CCCCDDDD")

            store = BdIsoStateStore.load(iso_path, state_path=state_path)
            self.assertEqual(store.effective_chunk_status(0), STATUS_ZERO_FILLED)
            self.assertEqual(store.effective_chunk_status(1), STATUS_ZERO_FILLED)
            self.assertEqual(store.effective_chunk_status(2), STATUS_COPIED)
            self.assertEqual(store.effective_chunk_status(3), STATUS_COPIED)

            progress = parse_copy_progress(get_copy_progress_path(iso_path))
            self.assertEqual(progress.checkpoint, len(data))

    def test_resume_from_offset_skips_later_zero_filled_failures(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_path = os.path.join(tempdir, "disc.bin")
            iso_path = os.path.join(tempdir, "movie.iso")
            state_path = get_state_path(iso_path)
            data = b"AAAABBBBCCCCDDDDEEEE"

            with open(source_path, "wb") as handle:
                handle.write(data)
            with open(iso_path, "wb") as handle:
                handle.write(b"XXXXYYYYZZZZWWWWQQQQ")

            store = BdIsoStateStore.load_or_hydrate(
                iso_path,
                source_path=source_path,
                device_size=len(data),
                chunk_size=4,
                state_path=state_path,
                reset=True,
            )
            store.record_copy_success(0, 4)
            store.record_copy_failure(12, 4)
            store.save()

            success = create_iso_from_bd(
                source_path,
                iso_path,
                chunk_size=4,
                retries=0,
                retry_delay=0,
                min_read_size=4,
                resume=True,
                start_offset_override=8,
                state_path=state_path,
                size_func=lambda _path: len(data),
                compare_func=lambda *_args, **_kwargs: True,
            )

            self.assertTrue(success)
            with open(iso_path, "rb") as handle:
                self.assertEqual(handle.read(), b"XXXX\x00\x00\x00\x00CCCC\x00\x00\x00\x00EEEE")

            store = BdIsoStateStore.load(iso_path, state_path=state_path)
            self.assertEqual(store.effective_chunk_status(0), STATUS_COPIED)
            self.assertEqual(store.effective_chunk_status(1), STATUS_ZERO_FILLED)
            self.assertEqual(store.effective_chunk_status(2), STATUS_COPIED)
            self.assertEqual(store.effective_chunk_status(3), STATUS_ZERO_FILLED)
            self.assertEqual(store.effective_chunk_status(4), STATUS_COPIED)

    def test_resume_from_offset_skips_later_checkpoint_completed_chunks(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_path = os.path.join(tempdir, "disc.bin")
            iso_path = os.path.join(tempdir, "movie.iso")
            state_path = get_state_path(iso_path)
            data = b"AAAABBBBCCCCDDDDEEEE"
            source = PlannedSource(data)

            with open(source_path, "wb") as handle:
                handle.write(data)
            with open(iso_path, "wb") as handle:
                handle.write(b"AAAABBBBxxxxYYYYZZZZ")
            events = []

            store = BdIsoStateStore.load_or_hydrate(
                iso_path,
                source_path=source_path,
                device_size=len(data),
                chunk_size=4,
                state_path=state_path,
                reset=True,
            )
            store.set_copy_checkpoint(len(data))
            store.save()

            success = create_iso_from_bd(
                source_path,
                iso_path,
                chunk_size=4,
                retries=0,
                retry_delay=0,
                min_read_size=4,
                resume=True,
                start_offset_override=8,
                state_path=state_path,
                open_source=lambda _path: source,
                size_func=lambda _path: len(data),
                compare_func=lambda *_args, **_kwargs: True,
                observer=lambda event, **payload: events.append((event, payload)),
            )

            self.assertTrue(success)
            self.assertEqual(source.read_calls, [(8, 4)])
            with open(iso_path, "rb") as handle:
                self.assertEqual(handle.read(), b"AAAABBBBCCCCYYYYZZZZ")

            store = BdIsoStateStore.load(iso_path, state_path=state_path)
            self.assertEqual(store.effective_chunk_status(2), STATUS_COPIED)
            self.assertEqual(store.effective_chunk_status(3), STATUS_COPIED)
            self.assertEqual(store.effective_chunk_status(4), STATUS_COPIED)
            skipped_progress = [
                payload
                for event, payload in events
                if event == "copy_progress" and payload.get("skipped")
            ]
            self.assertEqual(
                [payload["chunk_index"] for payload in skipped_progress],
                [3, 4],
            )
            self.assertTrue(
                all(payload["chunk_status"] == STATUS_COPIED for payload in skipped_progress)
            )


if __name__ == "__main__":
    unittest.main()
