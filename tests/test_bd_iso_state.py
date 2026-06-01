import os
import tempfile
import unittest

from bd_iso_state import (
    BdIsoStateStore,
    DEFAULT_SECTOR_SIZE,
    GRID_PAGE_SIZE,
    STATUS_COPIED,
    STATUS_FAILED,
    STATUS_NOT_TRIED,
    STATUS_VERIFIED,
    STATUS_ZERO_FILLED,
    chunk_page_bounds,
    get_state_path,
    page_count_for_chunks,
)
from bd_iso_ui import BdIsoController
from compare_bd_iso import DEFAULT_CHUNK_SIZE, verify_iso_range
from create_iso_from_bd import copy_iso_range_from_bd, get_copy_progress_path


class BdIsoStateTests(unittest.TestCase):
    def test_hydrate_logs_accepts_legacy_bad_entries(self):
        with tempfile.TemporaryDirectory() as tempdir:
            iso_path = os.path.join(tempdir, "movie.iso")
            with open(iso_path, "wb") as handle:
                handle.write(b"\x00" * (DEFAULT_CHUNK_SIZE * 2))

            with open(get_copy_progress_path(iso_path), "w", encoding="utf-8") as handle:
                handle.write("SOURCE /tmp/disc\n")
                handle.write(f"DEVICE_SIZE {DEFAULT_CHUNK_SIZE * 2}\n")
                handle.write(f"CHUNK_SIZE {DEFAULT_CHUNK_SIZE}\n")
                handle.write(f"CHECKPOINT {DEFAULT_CHUNK_SIZE}\n")
                handle.write(f"BAD {DEFAULT_CHUNK_SIZE}\n")

            store = BdIsoStateStore.load_or_hydrate(
                iso_path,
                source_path="/tmp/disc",
                device_size=DEFAULT_CHUNK_SIZE * 2,
                chunk_size=DEFAULT_CHUNK_SIZE,
                state_path=get_state_path(iso_path),
            )

            self.assertEqual(store.effective_chunk_status(0), STATUS_COPIED)
            self.assertEqual(store.effective_chunk_status(1), STATUS_FAILED)
            self.assertEqual(store.effective_sector_status(1, 0), STATUS_FAILED)
            self.assertEqual(store.effective_sector_status(1, 1), STATUS_NOT_TRIED)

    def test_paging_helpers_cover_full_grid_pages(self):
        self.assertEqual(page_count_for_chunks(GRID_PAGE_SIZE * 2 + 1), 3)
        self.assertEqual(chunk_page_bounds(0, GRID_PAGE_SIZE * 2 + 1), (0, GRID_PAGE_SIZE))
        self.assertEqual(
            chunk_page_bounds(2, GRID_PAGE_SIZE * 2 + 1),
            (GRID_PAGE_SIZE * 2, GRID_PAGE_SIZE * 2 + 1),
        )

    def test_sparse_sector_status_inherits_from_chunk_defaults(self):
        with tempfile.TemporaryDirectory() as tempdir:
            iso_path = os.path.join(tempdir, "movie.iso")
            store = BdIsoStateStore.load_or_hydrate(
                iso_path,
                source_path="/tmp/disc",
                device_size=DEFAULT_CHUNK_SIZE * 2,
                chunk_size=DEFAULT_CHUNK_SIZE,
                state_path=get_state_path(iso_path),
                reset=True,
            )
            store.record_copy_success(0, DEFAULT_CHUNK_SIZE)
            store.record_copy_failure(DEFAULT_CHUNK_SIZE, DEFAULT_SECTOR_SIZE, error="bad sector")

            self.assertEqual(store.effective_sector_status(0, 25), STATUS_COPIED)
            self.assertEqual(store.effective_sector_status(1, 0), STATUS_FAILED)
            self.assertEqual(store.effective_sector_status(1, 10), STATUS_NOT_TRIED)

    def test_single_range_chunk_and_sector_actions_update_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_path = os.path.join(tempdir, "disc.bin")
            iso_path = os.path.join(tempdir, "movie.iso")
            state_path = get_state_path(iso_path)

            source_data = bytearray(b"\x00" * (DEFAULT_CHUNK_SIZE * 2))
            source_data[DEFAULT_SECTOR_SIZE : DEFAULT_SECTOR_SIZE * 2] = b"A" * DEFAULT_SECTOR_SIZE
            source_data[DEFAULT_CHUNK_SIZE : DEFAULT_CHUNK_SIZE + DEFAULT_SECTOR_SIZE] = (
                b"B" * DEFAULT_SECTOR_SIZE
            )

            with open(source_path, "wb") as handle:
                handle.write(source_data)
            with open(iso_path, "wb") as handle:
                handle.write(b"\x00" * (DEFAULT_CHUNK_SIZE * 2))

            copied_sector = copy_iso_range_from_bd(
                source_path,
                iso_path,
                DEFAULT_SECTOR_SIZE,
                DEFAULT_SECTOR_SIZE,
                retries=0,
                retry_delay=0,
                min_read_size=DEFAULT_SECTOR_SIZE,
                state_path=state_path,
                scope="sector",
            )
            self.assertTrue(copied_sector)

            verified_sector = verify_iso_range(
                source_path,
                iso_path,
                DEFAULT_SECTOR_SIZE,
                DEFAULT_SECTOR_SIZE,
                state_path=state_path,
            )
            self.assertTrue(verified_sector)

            copied_chunk = copy_iso_range_from_bd(
                source_path,
                iso_path,
                DEFAULT_CHUNK_SIZE,
                DEFAULT_CHUNK_SIZE,
                retries=0,
                retry_delay=0,
                min_read_size=DEFAULT_SECTOR_SIZE,
                state_path=state_path,
                scope="chunk",
            )
            self.assertTrue(copied_chunk)

            verified_chunk = verify_iso_range(
                source_path,
                iso_path,
                DEFAULT_CHUNK_SIZE,
                DEFAULT_CHUNK_SIZE,
                state_path=state_path,
            )
            self.assertTrue(verified_chunk)

            store = BdIsoStateStore.load(iso_path, state_path=state_path)
            self.assertEqual(store.effective_sector_status(0, 1), STATUS_VERIFIED)
            self.assertEqual(store.effective_chunk_status(1), STATUS_VERIFIED)

    def test_controller_reloads_state_from_worker_events(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_path = os.path.join(tempdir, "disc.bin")
            iso_path = os.path.join(tempdir, "movie.iso")
            with open(source_path, "wb") as handle:
                handle.write(b"\x00" * DEFAULT_CHUNK_SIZE)
            with open(iso_path, "wb") as handle:
                handle.write(b"\x00" * DEFAULT_CHUNK_SIZE)

            state_path = get_state_path(iso_path)
            store = BdIsoStateStore.load_or_hydrate(
                iso_path,
                source_path=source_path,
                device_size=DEFAULT_CHUNK_SIZE,
                chunk_size=DEFAULT_CHUNK_SIZE,
                state_path=state_path,
                reset=True,
            )
            store.record_copy_success(0, DEFAULT_CHUNK_SIZE)

            controller = BdIsoController()
            controller.load_state(source_path, iso_path)
            controller.handle_message(
                {
                    "type": "worker_event",
                    "event": "copy_progress",
                    "current": DEFAULT_CHUNK_SIZE,
                    "total": DEFAULT_CHUNK_SIZE,
                }
            )
            self.assertEqual(controller.store.effective_chunk_status(0), STATUS_COPIED)
            self.assertIn("Copying", controller.last_message)

            store.record_sector_scan(0, [STATUS_VERIFIED] * 10 + [STATUS_FAILED] + [STATUS_VERIFIED] * 1989)
            controller.handle_message(
                {
                    "type": "worker_event",
                    "event": "verify_chunk_failed",
                    "offset": 0,
                    "size": DEFAULT_CHUNK_SIZE,
                    "first_diff_offset": DEFAULT_SECTOR_SIZE * 10,
                    "mismatching_bytes": 1,
                }
            )
            self.assertEqual(controller.store.effective_chunk_status(0), STATUS_FAILED)
            self.assertIn("Mismatch", controller.last_message)

    def test_controller_verify_from_chunk_passes_start_offset(self):
        class RecordingController(BdIsoController):
            def _run_in_thread(self, label, target, *args, **kwargs):
                self.recorded_job = {
                    "label": label,
                    "target": target,
                    "args": args,
                    "kwargs": kwargs,
                }

        with tempfile.TemporaryDirectory() as tempdir:
            source_path = os.path.join(tempdir, "disc.bin")
            iso_path = os.path.join(tempdir, "movie.iso")
            with open(source_path, "wb") as handle:
                handle.write(b"\x00" * (DEFAULT_CHUNK_SIZE * 4))
            with open(iso_path, "wb") as handle:
                handle.write(b"\x00" * (DEFAULT_CHUNK_SIZE * 4))

            controller = RecordingController()
            controller.load_state(source_path, iso_path)
            controller.verify_from_chunk(source_path, iso_path, 2)

            self.assertEqual(controller.recorded_job["label"], "verify_from_here")
            self.assertEqual(
                controller.recorded_job["kwargs"]["start_offset"],
                DEFAULT_CHUNK_SIZE * 2,
            )

    def test_out_of_order_chunk_copy_marks_gap_zero_filled_without_smearing_blue(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_path = os.path.join(tempdir, "disc.bin")
            iso_path = os.path.join(tempdir, "movie.iso")
            state_path = get_state_path(iso_path)

            with open(source_path, "wb") as handle:
                handle.write(b"A" * (DEFAULT_CHUNK_SIZE * 4))
            with open(iso_path, "wb") as handle:
                handle.write(b"\x00" * DEFAULT_CHUNK_SIZE)

            store = BdIsoStateStore.load_or_hydrate(
                iso_path,
                source_path=source_path,
                device_size=DEFAULT_CHUNK_SIZE * 4,
                chunk_size=DEFAULT_CHUNK_SIZE,
                state_path=state_path,
                reset=True,
            )
            store.record_copy_success(0, DEFAULT_CHUNK_SIZE)
            store.save()

            copied = copy_iso_range_from_bd(
                source_path,
                iso_path,
                DEFAULT_CHUNK_SIZE * 3,
                DEFAULT_CHUNK_SIZE,
                retries=0,
                retry_delay=0,
                min_read_size=DEFAULT_SECTOR_SIZE,
                state_path=state_path,
                scope="chunk",
            )
            self.assertTrue(copied)

            store = BdIsoStateStore.load(iso_path, state_path=state_path)
            self.assertEqual(store.effective_chunk_status(0), STATUS_COPIED)
            self.assertEqual(store.effective_chunk_status(1), STATUS_ZERO_FILLED)
            self.assertEqual(store.effective_chunk_status(2), STATUS_ZERO_FILLED)
            self.assertEqual(store.effective_chunk_status(3), STATUS_COPIED)

    def test_out_of_order_verify_marks_only_selected_chunk_verified(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_path = os.path.join(tempdir, "disc.bin")
            iso_path = os.path.join(tempdir, "movie.iso")
            state_path = get_state_path(iso_path)

            with open(source_path, "wb") as handle:
                handle.write(b"Z" * (DEFAULT_CHUNK_SIZE * 4))
            with open(iso_path, "wb") as handle:
                handle.write(b"Z" * (DEFAULT_CHUNK_SIZE * 4))

            store = BdIsoStateStore.load_or_hydrate(
                iso_path,
                source_path=source_path,
                device_size=DEFAULT_CHUNK_SIZE * 4,
                chunk_size=DEFAULT_CHUNK_SIZE,
                state_path=state_path,
                reset=True,
            )
            store.record_copy_success(0, DEFAULT_CHUNK_SIZE)
            store.save()

            verified = verify_iso_range(
                source_path,
                iso_path,
                DEFAULT_CHUNK_SIZE * 3,
                DEFAULT_CHUNK_SIZE,
                state_path=state_path,
            )
            self.assertTrue(verified)

            store = BdIsoStateStore.load(iso_path, state_path=state_path)
            self.assertEqual(store.effective_chunk_status(0), STATUS_COPIED)
            self.assertEqual(store.effective_chunk_status(1), STATUS_NOT_TRIED)
            self.assertEqual(store.effective_chunk_status(2), STATUS_NOT_TRIED)
            self.assertEqual(store.effective_chunk_status(3), STATUS_VERIFIED)

    def test_prepare_resume_from_chunk_zero_fills_prior_failed_and_untried(self):
        with tempfile.TemporaryDirectory() as tempdir:
            iso_path = os.path.join(tempdir, "movie.iso")
            state_path = get_state_path(iso_path)
            store = BdIsoStateStore.load_or_hydrate(
                iso_path,
                source_path="/tmp/disc",
                device_size=DEFAULT_CHUNK_SIZE * 5,
                chunk_size=DEFAULT_CHUNK_SIZE,
                state_path=state_path,
                reset=True,
            )
            store.record_copy_success(0, DEFAULT_CHUNK_SIZE)
            store.record_copy_failure(DEFAULT_CHUNK_SIZE * 2, DEFAULT_SECTOR_SIZE)
            store.record_copy_success(DEFAULT_CHUNK_SIZE * 4, DEFAULT_CHUNK_SIZE)

            resume_offset = store.prepare_resume_from_chunk(3)

            self.assertEqual(resume_offset, DEFAULT_CHUNK_SIZE * 3)
            self.assertEqual(store.effective_chunk_status(0), STATUS_COPIED)
            self.assertEqual(store.effective_chunk_status(1), STATUS_ZERO_FILLED)
            self.assertEqual(store.effective_chunk_status(2), STATUS_ZERO_FILLED)
            self.assertEqual(store.effective_chunk_status(3), STATUS_NOT_TRIED)
            self.assertEqual(store.effective_chunk_status(4), STATUS_COPIED)
            self.assertEqual(store.state["copy_checkpoint"], DEFAULT_CHUNK_SIZE * 3)

    def test_prepare_resume_from_chunk_preserves_later_checkpoint_completed_chunks(self):
        with tempfile.TemporaryDirectory() as tempdir:
            iso_path = os.path.join(tempdir, "movie.iso")
            state_path = get_state_path(iso_path)
            store = BdIsoStateStore.load_or_hydrate(
                iso_path,
                source_path="/tmp/disc",
                device_size=DEFAULT_CHUNK_SIZE * 5,
                chunk_size=DEFAULT_CHUNK_SIZE,
                state_path=state_path,
                reset=True,
            )
            store.set_copy_checkpoint(DEFAULT_CHUNK_SIZE * 5)

            resume_offset = store.prepare_resume_from_chunk(2)

            self.assertEqual(resume_offset, DEFAULT_CHUNK_SIZE * 2)
            self.assertEqual(store.state["copy_checkpoint"], DEFAULT_CHUNK_SIZE * 2)
            self.assertEqual(store.effective_chunk_status(0), STATUS_COPIED)
            self.assertEqual(store.effective_chunk_status(1), STATUS_COPIED)
            self.assertEqual(store.effective_chunk_status(2), STATUS_NOT_TRIED)
            self.assertEqual(store.effective_chunk_status(3), STATUS_COPIED)
            self.assertEqual(store.effective_chunk_status(4), STATUS_COPIED)
            self.assertEqual(
                store.get_chunk_record(3)["status"],
                STATUS_COPIED,
            )
            self.assertEqual(
                store.get_chunk_record(4)["status"],
                STATUS_COPIED,
            )

    def test_prepare_resume_from_chunk_skips_later_failed_chunks(self):
        with tempfile.TemporaryDirectory() as tempdir:
            iso_path = os.path.join(tempdir, "movie.iso")
            state_path = get_state_path(iso_path)
            store = BdIsoStateStore.load_or_hydrate(
                iso_path,
                source_path="/tmp/disc",
                device_size=DEFAULT_CHUNK_SIZE * 5,
                chunk_size=DEFAULT_CHUNK_SIZE,
                state_path=state_path,
                reset=True,
            )
            store.record_copy_failure(DEFAULT_CHUNK_SIZE, DEFAULT_SECTOR_SIZE)
            store.record_copy_failure(DEFAULT_CHUNK_SIZE * 3, DEFAULT_SECTOR_SIZE)

            resume_offset = store.prepare_resume_from_chunk(1)

            self.assertEqual(resume_offset, DEFAULT_CHUNK_SIZE)
            self.assertEqual(store.effective_chunk_status(0), STATUS_ZERO_FILLED)
            self.assertEqual(store.effective_chunk_status(1), STATUS_NOT_TRIED)
            self.assertEqual(store.effective_chunk_status(2), STATUS_NOT_TRIED)
            self.assertEqual(store.effective_chunk_status(3), STATUS_ZERO_FILLED)
            self.assertEqual(store.effective_chunk_status(4), STATUS_NOT_TRIED)

    def test_prepare_retry_from_chunk_retries_zero_filled_range(self):
        with tempfile.TemporaryDirectory() as tempdir:
            iso_path = os.path.join(tempdir, "movie.iso")
            state_path = get_state_path(iso_path)
            store = BdIsoStateStore.load_or_hydrate(
                iso_path,
                source_path="/tmp/disc",
                device_size=DEFAULT_CHUNK_SIZE * 6,
                chunk_size=DEFAULT_CHUNK_SIZE,
                state_path=state_path,
                reset=True,
            )
            store.set_copy_checkpoint(DEFAULT_CHUNK_SIZE * 6)
            for chunk_index in (2, 3):
                record = store.get_chunk_record(chunk_index, create=True)
                record["status"] = STATUS_ZERO_FILLED
            store.record_copy_failure(DEFAULT_CHUNK_SIZE * 4, DEFAULT_SECTOR_SIZE)

            resume_offset, stop_offset = store.prepare_retry_from_chunk(2, max_chunks=3)

            self.assertEqual(resume_offset, DEFAULT_CHUNK_SIZE * 2)
            self.assertEqual(stop_offset, DEFAULT_CHUNK_SIZE * 5)
            self.assertEqual(store.state["copy_checkpoint"], DEFAULT_CHUNK_SIZE * 2)
            self.assertEqual(store.effective_chunk_status(0), STATUS_COPIED)
            self.assertEqual(store.effective_chunk_status(1), STATUS_COPIED)
            self.assertEqual(store.effective_chunk_status(2), STATUS_NOT_TRIED)
            self.assertEqual(store.effective_chunk_status(3), STATUS_NOT_TRIED)
            self.assertEqual(store.effective_chunk_status(4), STATUS_NOT_TRIED)
            self.assertEqual(store.effective_chunk_status(5), STATUS_COPIED)
            self.assertEqual(store.get_chunk_record(5)["status"], STATUS_COPIED)


if __name__ == "__main__":
    unittest.main()
