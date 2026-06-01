import json
import math
import os

STATE_SUFFIX = ".state.json"
STATE_VERSION = 1
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
DEFAULT_SECTOR_SIZE = 2048
GRID_COLUMNS = 50
GRID_ROWS = 40
GRID_PAGE_SIZE = GRID_COLUMNS * GRID_ROWS

STATUS_NOT_TRIED = "not_tried"
STATUS_ZERO_FILLED = "zero_filled"
STATUS_COPIED = "copied"
STATUS_FAILED = "failed"
STATUS_VERIFIED = "verified"

VALID_STATUSES = {
    STATUS_NOT_TRIED,
    STATUS_ZERO_FILLED,
    STATUS_COPIED,
    STATUS_FAILED,
    STATUS_VERIFIED,
}


def get_state_path(iso_path):
    return f"{iso_path}{STATE_SUFFIX}"


def ceil_div(value, divisor):
    if divisor <= 0:
        raise ValueError("divisor must be greater than 0")
    return -(-value // divisor)


def chunk_count_for_size(size, chunk_size):
    if size <= 0:
        return 0
    return ceil_div(size, chunk_size)


def chunk_index_for_offset(offset, chunk_size):
    if offset < 0:
        raise ValueError("offset cannot be negative")
    return offset // chunk_size


def chunk_offset_for_index(chunk_index, chunk_size):
    if chunk_index < 0:
        raise ValueError("chunk_index cannot be negative")
    return chunk_index * chunk_size


def page_count_for_chunks(chunk_count, page_size=GRID_PAGE_SIZE):
    if chunk_count <= 0:
        return 0
    return ceil_div(chunk_count, page_size)


def chunk_page_bounds(page_index, chunk_count, page_size=GRID_PAGE_SIZE):
    if page_index < 0:
        raise ValueError("page_index cannot be negative")
    start = page_index * page_size
    end = min(start + page_size, chunk_count)
    return start, end


def sector_count_for_bytes(size, sector_size=DEFAULT_SECTOR_SIZE):
    if size <= 0:
        return 0
    return ceil_div(size, sector_size)


def chunk_status_to_sector_default(status):
    if status == STATUS_ZERO_FILLED:
        return STATUS_ZERO_FILLED
    if status == STATUS_COPIED:
        return STATUS_COPIED
    if status == STATUS_VERIFIED:
        return STATUS_VERIFIED
    return STATUS_NOT_TRIED


class BdIsoStateStore:
    def __init__(self, iso_path, state_path=None):
        self.iso_path = iso_path
        self.state_path = state_path or get_state_path(iso_path)
        self.state = self._default_state()

    @classmethod
    def load(cls, iso_path, state_path=None):
        store = cls(iso_path, state_path=state_path)
        with open(store.state_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        store.state = store._normalize_state(data)
        return store

    @classmethod
    def load_or_hydrate(
        cls,
        iso_path,
        *,
        source_path=None,
        device_size=None,
        chunk_size=DEFAULT_CHUNK_SIZE,
        sector_size=DEFAULT_SECTOR_SIZE,
        state_path=None,
        reset=False,
    ):
        store = cls(iso_path, state_path=state_path)
        if os.path.exists(store.state_path) and not reset:
            loaded = cls.load(iso_path, state_path=store.state_path)
            if source_path is not None or device_size is not None:
                loaded.ensure_metadata(
                    source_path=source_path,
                    device_size=device_size,
                    chunk_size=chunk_size,
                    sector_size=sector_size,
                )
                loaded.save()
            return loaded

        store.ensure_metadata(
            source_path=source_path,
            device_size=device_size,
            chunk_size=chunk_size,
            sector_size=sector_size,
        )
        store.hydrate_from_logs()
        store.save()
        return store

    def _default_state(self):
        return {
            "version": STATE_VERSION,
            "iso_path": self.iso_path,
            "source_path": None,
            "device_size": None,
            "chunk_size": DEFAULT_CHUNK_SIZE,
            "sector_size": DEFAULT_SECTOR_SIZE,
            "copy_checkpoint": 0,
            "verify_checkpoint": 0,
            "chunks": {},
        }

    def _normalize_state(self, data):
        state = self._default_state()
        state.update(data)
        state["iso_path"] = self.iso_path
        state["copy_checkpoint"] = int(state.get("copy_checkpoint", 0) or 0)
        state["verify_checkpoint"] = int(state.get("verify_checkpoint", 0) or 0)
        state["chunk_size"] = int(state.get("chunk_size", DEFAULT_CHUNK_SIZE))
        state["sector_size"] = int(state.get("sector_size", DEFAULT_SECTOR_SIZE))
        state["device_size"] = (
            None if state.get("device_size") is None else int(state["device_size"])
        )
        chunks = {}
        for key, value in (state.get("chunks") or {}).items():
            chunk_index = str(int(key))
            chunk = self._normalize_chunk(value)
            chunks[chunk_index] = chunk
        state["chunks"] = chunks
        return state

    def _normalize_chunk(self, chunk):
        normalized = {
            "status": chunk.get("status", STATUS_NOT_TRIED),
            "retry_count": int(chunk.get("retry_count", 0) or 0),
            "verify_count": int(chunk.get("verify_count", 0) or 0),
            "last_error": chunk.get("last_error"),
            "sector_scan_complete": bool(chunk.get("sector_scan_complete", False)),
            "sectors": {},
        }
        if normalized["status"] not in VALID_STATUSES:
            normalized["status"] = STATUS_NOT_TRIED

        for key, value in (chunk.get("sectors") or {}).items():
            sector_index = str(int(key))
            status = value.get("status", STATUS_NOT_TRIED)
            if status not in VALID_STATUSES:
                status = STATUS_NOT_TRIED
            normalized["sectors"][sector_index] = {
                "status": status,
                "last_error": value.get("last_error"),
            }
        return normalized

    @property
    def chunk_size(self):
        return self.state["chunk_size"]

    @property
    def sector_size(self):
        return self.state["sector_size"]

    @property
    def device_size(self):
        return self.state["device_size"] or 0

    @property
    def chunk_count(self):
        return chunk_count_for_size(self.device_size, self.chunk_size)

    def chunk_size_for_index(self, chunk_index):
        if self.device_size <= 0:
            return self.chunk_size
        chunk_offset = chunk_offset_for_index(chunk_index, self.chunk_size)
        return min(self.chunk_size, max(self.device_size - chunk_offset, 0))

    def sector_count_for_chunk(self, chunk_index):
        return sector_count_for_bytes(
            self.chunk_size_for_index(chunk_index),
            sector_size=self.sector_size,
        )

    def ensure_metadata(
        self,
        *,
        source_path=None,
        device_size=None,
        chunk_size=None,
        sector_size=None,
    ):
        if source_path is not None:
            self.state["source_path"] = os.path.realpath(source_path)
        if device_size is not None:
            self.state["device_size"] = int(device_size)
        if chunk_size is not None:
            self.state["chunk_size"] = int(chunk_size)
        if sector_size is not None:
            self.state["sector_size"] = int(sector_size)
        if self.state["device_size"] is None and os.path.exists(self.iso_path):
            self.state["device_size"] = os.path.getsize(self.iso_path)

    def reset(self):
        chunk_size = self.chunk_size
        sector_size = self.sector_size
        source_path = self.state["source_path"]
        device_size = self.state["device_size"]
        self.state = self._default_state()
        self.ensure_metadata(
            source_path=source_path,
            device_size=device_size,
            chunk_size=chunk_size,
            sector_size=sector_size,
        )

    def save(self):
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        temp_path = f"{self.state_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(self.state, handle, indent=2, sort_keys=True)
        os.replace(temp_path, self.state_path)

    def hydrate_from_logs(self):
        self._hydrate_copy_progress()
        self._hydrate_verify_progress()

    def _hydrate_copy_progress(self):
        from create_iso_from_bd import get_copy_progress_path, parse_copy_progress

        progress_path = get_copy_progress_path(self.iso_path)
        if not os.path.exists(progress_path):
            return

        progress = parse_copy_progress(progress_path)
        self.ensure_metadata(
            source_path=progress.source_path,
            device_size=progress.device_size,
            chunk_size=progress.chunk_size,
        )
        self.state["copy_checkpoint"] = max(
            self.state["copy_checkpoint"],
            int(progress.checkpoint),
        )
        for offset, size in progress.bad_regions:
            self.record_copy_failure(offset, size, persist=False)

    def _hydrate_verify_progress(self):
        from compare_bd_iso import parse_progress_file

        checkpoint, mismatches = parse_progress_file(self.iso_path)
        self.state["verify_checkpoint"] = max(
            self.state["verify_checkpoint"],
            int(checkpoint),
        )
        for offset in mismatches:
            self.record_verify_mismatch(offset, self.chunk_size, persist=False)

    def _chunk_key(self, chunk_index):
        return str(int(chunk_index))

    def get_chunk_record(self, chunk_index, create=False):
        chunk_key = self._chunk_key(chunk_index)
        record = self.state["chunks"].get(chunk_key)
        if record is None and create:
            record = {
                "status": STATUS_NOT_TRIED,
                "retry_count": 0,
                "verify_count": 0,
                "last_error": None,
                "sector_scan_complete": False,
                "sectors": {},
            }
            self.state["chunks"][chunk_key] = record
        return record

    def clear_chunk_record(self, chunk_index):
        self.state["chunks"].pop(self._chunk_key(chunk_index), None)

    def _materialize_chunk_status(self, chunk_index, status):
        record = self.get_chunk_record(chunk_index)
        if record is not None:
            return
        record = self.get_chunk_record(chunk_index, create=True)
        record["status"] = status
        record["last_error"] = None
        record["sector_scan_complete"] = False
        record["sectors"] = {}

    def _mark_chunk_not_tried(self, chunk_index):
        record = self.get_chunk_record(chunk_index, create=True)
        record["status"] = STATUS_NOT_TRIED
        record["last_error"] = None
        record["sector_scan_complete"] = False
        record["sectors"] = {}

    def _mark_chunk_zero_filled(self, chunk_index):
        record = self.get_chunk_record(chunk_index, create=True)
        record["status"] = STATUS_ZERO_FILLED
        record["last_error"] = None
        record["sector_scan_complete"] = False
        record["sectors"] = {}

    def prepare_resume_from_chunk(self, chunk_index, *, persist=True):
        if chunk_index < 0 or chunk_index >= self.chunk_count:
            raise ValueError("chunk_index is outside the known disc size")

        for prior_index in range(chunk_index):
            status = self.effective_chunk_status(prior_index)
            if status in {STATUS_COPIED, STATUS_VERIFIED, STATUS_ZERO_FILLED}:
                continue
            self._mark_chunk_zero_filled(prior_index)

        self.clear_chunk_record(chunk_index)
        for later_index in range(chunk_index + 1, self.chunk_count):
            status = self.effective_chunk_status(later_index)
            if status in {STATUS_COPIED, STATUS_VERIFIED, STATUS_ZERO_FILLED}:
                self._materialize_chunk_status(later_index, status)
                continue
            if status == STATUS_FAILED:
                self._mark_chunk_zero_filled(later_index)

        resume_offset = chunk_offset_for_index(chunk_index, self.chunk_size)
        self.state["copy_checkpoint"] = resume_offset
        self.state["verify_checkpoint"] = min(
            self.state["verify_checkpoint"],
            resume_offset,
        )

        if persist:
            self.save()
        return resume_offset

    def prepare_retry_from_chunk(self, chunk_index, *, max_chunks=None, persist=True):
        if chunk_index < 0 or chunk_index >= self.chunk_count:
            raise ValueError("chunk_index is outside the known disc size")
        if max_chunks is not None and max_chunks <= 0:
            raise ValueError("max_chunks must be greater than 0")

        end_chunk = self.chunk_count
        if max_chunks is not None:
            end_chunk = min(self.chunk_count, chunk_index + int(max_chunks))

        for prior_index in range(chunk_index):
            status = self.effective_chunk_status(prior_index)
            if status in {STATUS_COPIED, STATUS_VERIFIED, STATUS_ZERO_FILLED}:
                continue
            self._mark_chunk_zero_filled(prior_index)

        for retry_index in range(chunk_index, end_chunk):
            status = self.effective_chunk_status(retry_index)
            if status in {STATUS_COPIED, STATUS_VERIFIED}:
                self._materialize_chunk_status(retry_index, status)
                continue
            self._mark_chunk_not_tried(retry_index)

        for later_index in range(end_chunk, self.chunk_count):
            status = self.effective_chunk_status(later_index)
            if status in {
                STATUS_COPIED,
                STATUS_VERIFIED,
                STATUS_ZERO_FILLED,
                STATUS_FAILED,
            }:
                self._materialize_chunk_status(later_index, status)

        resume_offset = chunk_offset_for_index(chunk_index, self.chunk_size)
        self.state["copy_checkpoint"] = resume_offset
        self.state["verify_checkpoint"] = min(
            self.state["verify_checkpoint"],
            resume_offset,
        )

        if persist:
            self.save()
        return resume_offset, chunk_offset_for_index(end_chunk, self.chunk_size)

    def should_skip_copy_chunk(self, chunk_index):
        return self.effective_chunk_status(chunk_index) in {
            STATUS_ZERO_FILLED,
            STATUS_COPIED,
            STATUS_VERIFIED,
        }

    def effective_chunk_status(self, chunk_index):
        record = self.get_chunk_record(chunk_index)
        if record is not None and record.get("status") in VALID_STATUSES:
            return record["status"]

        chunk_end = chunk_offset_for_index(chunk_index + 1, self.chunk_size)
        if chunk_end <= self.state["verify_checkpoint"]:
            return STATUS_VERIFIED
        if chunk_end <= self.state["copy_checkpoint"]:
            return STATUS_COPIED
        return STATUS_NOT_TRIED

    def effective_sector_status(self, chunk_index, sector_index):
        record = self.get_chunk_record(chunk_index)
        if record is not None:
            sector = record.get("sectors", {}).get(str(int(sector_index)))
            if sector is not None and sector.get("status") in VALID_STATUSES:
                return sector["status"]
        return chunk_status_to_sector_default(self.effective_chunk_status(chunk_index))

    def set_copy_checkpoint(self, offset, *, start_offset=None, persist=True):
        frontier = self.state["copy_checkpoint"]
        offset = int(offset)
        if start_offset is None:
            self.state["copy_checkpoint"] = max(frontier, offset)
        else:
            start_offset = int(start_offset)
            if start_offset > frontier:
                self._mark_zero_filled_gap(frontier, start_offset)
            if start_offset == frontier:
                self.state["copy_checkpoint"] = offset
                self._advance_copy_checkpoint()
        if persist:
            self.save()

    def set_verify_checkpoint(self, offset, *, start_offset=None, persist=True):
        frontier = self.state["verify_checkpoint"]
        offset = int(offset)
        if start_offset is None:
            self.state["verify_checkpoint"] = max(frontier, offset)
        elif int(start_offset) == frontier:
            self.state["verify_checkpoint"] = offset
            self._advance_verify_checkpoint()
        if persist:
            self.save()

    def record_copy_success(self, offset, size, *, persist=True):
        chunk_index = chunk_index_for_offset(offset, self.chunk_size)
        record = self.get_chunk_record(chunk_index)
        if record is None and offset > self.state["copy_checkpoint"]:
            record = self.get_chunk_record(chunk_index, create=True)
        if record is not None:
            record["status"] = STATUS_COPIED
            record["last_error"] = None
            record["sector_scan_complete"] = False
            record["sectors"] = {}
        self.set_copy_checkpoint(offset + size, start_offset=offset, persist=False)
        if persist:
            self.save()

    def record_copy_failure(self, offset, size, *, error=None, persist=True):
        chunk_index = chunk_index_for_offset(offset, self.chunk_size)
        record = self.get_chunk_record(chunk_index, create=True)
        record["status"] = STATUS_FAILED
        record["last_error"] = error
        record["sector_scan_complete"] = False
        for sector_index in self._sector_indexes_for_range(offset, size):
            record["sectors"][str(sector_index)] = {
                "status": STATUS_FAILED,
                "last_error": error,
            }
        if persist:
            self.save()

    def record_verify_success(self, offset, size, *, persist=True):
        chunk_index = chunk_index_for_offset(offset, self.chunk_size)
        record = self.get_chunk_record(chunk_index)
        if record is None and offset > self.state["verify_checkpoint"]:
            record = self.get_chunk_record(chunk_index, create=True)
        if record is not None:
            record["status"] = STATUS_VERIFIED
            record["last_error"] = None
            record["sector_scan_complete"] = False
            record["sectors"] = {}
            record["verify_count"] += 1
        self.set_verify_checkpoint(offset + size, start_offset=offset, persist=False)
        if persist:
            self.save()

    def record_verify_mismatch(self, offset, size, *, error=None, persist=True):
        chunk_index = chunk_index_for_offset(offset, self.chunk_size)
        record = self.get_chunk_record(chunk_index, create=True)
        record["status"] = STATUS_FAILED
        record["last_error"] = error
        record["verify_count"] += 1
        if not record.get("sector_scan_complete"):
            record["sectors"] = record.get("sectors", {})
        if persist:
            self.save()

    def record_chunk_retry(self, chunk_index, *, persist=True):
        record = self.get_chunk_record(chunk_index, create=True)
        record["status"] = STATUS_COPIED
        record["retry_count"] += 1
        record["last_error"] = None
        record["sector_scan_complete"] = False
        record["sectors"] = {}
        if persist:
            self.save()

    def record_sector_retry(self, chunk_index, sector_index, *, error=None, persist=True):
        record = self.get_chunk_record(chunk_index, create=True)
        record["retry_count"] += 1
        record["last_error"] = error
        record["sectors"][str(sector_index)] = {
            "status": STATUS_COPIED if error is None else STATUS_FAILED,
            "last_error": error,
        }
        self._refresh_chunk_status(chunk_index, record)
        if persist:
            self.save()

    def record_sector_verify(
        self,
        chunk_index,
        sector_index,
        *,
        verified,
        error=None,
        persist=True,
    ):
        record = self.get_chunk_record(chunk_index, create=True)
        record["verify_count"] += 1
        record["last_error"] = error
        record["sectors"][str(sector_index)] = {
            "status": STATUS_VERIFIED if verified else STATUS_FAILED,
            "last_error": error,
        }
        self._refresh_chunk_status(chunk_index, record)
        if persist:
            self.save()

    def record_sector_scan(self, chunk_index, sector_statuses, *, persist=True):
        record = self.get_chunk_record(chunk_index, create=True)
        record["verify_count"] += 1
        record["sector_scan_complete"] = True
        record["sectors"] = {
            str(index): {"status": status, "last_error": None}
            for index, status in enumerate(sector_statuses)
        }
        self._refresh_chunk_status(chunk_index, record)
        if persist:
            self.save()

    def _refresh_chunk_status(self, chunk_index, record):
        sector_statuses = [value["status"] for value in record.get("sectors", {}).values()]
        if any(status == STATUS_FAILED for status in sector_statuses):
            record["status"] = STATUS_FAILED
            return

        sector_total = self.sector_count_for_chunk(chunk_index)
        if (
            record.get("sector_scan_complete")
            and len(record.get("sectors", {})) >= sector_total
            and sector_total > 0
        ):
            if all(status == STATUS_VERIFIED for status in sector_statuses):
                record["status"] = STATUS_VERIFIED
                record["last_error"] = None
                return
            if all(
                status in {STATUS_COPIED, STATUS_VERIFIED, STATUS_ZERO_FILLED}
                for status in sector_statuses
            ):
                record["status"] = STATUS_COPIED
                record["last_error"] = None
                return

        if record["status"] not in VALID_STATUSES:
            record["status"] = STATUS_NOT_TRIED

    def _mark_zero_filled_gap(self, start_offset, end_offset):
        if end_offset <= start_offset:
            return

        start_chunk = chunk_index_for_offset(start_offset, self.chunk_size)
        end_chunk = chunk_index_for_offset(end_offset - 1, self.chunk_size)
        for chunk_index in range(start_chunk, end_chunk + 1):
            record = self.get_chunk_record(chunk_index)
            if record is None:
                record = self.get_chunk_record(chunk_index, create=True)
                record["status"] = STATUS_ZERO_FILLED
                record["last_error"] = None
                record["sector_scan_complete"] = False
                record["sectors"] = {}
                continue
            if record["status"] == STATUS_NOT_TRIED:
                record["status"] = STATUS_ZERO_FILLED
                record["last_error"] = None

    def _advance_copy_checkpoint(self):
        frontier = self.state["copy_checkpoint"]
        while frontier < self.device_size:
            chunk_index = chunk_index_for_offset(frontier, self.chunk_size)
            record = self.get_chunk_record(chunk_index)
            if record is None or record.get("status") not in {
                STATUS_COPIED,
                STATUS_VERIFIED,
            }:
                break
            frontier = chunk_offset_for_index(chunk_index, self.chunk_size) + self.chunk_size_for_index(chunk_index)
        self.state["copy_checkpoint"] = frontier

    def _advance_verify_checkpoint(self):
        frontier = self.state["verify_checkpoint"]
        while frontier < self.device_size:
            chunk_index = chunk_index_for_offset(frontier, self.chunk_size)
            record = self.get_chunk_record(chunk_index)
            if record is None or record.get("status") != STATUS_VERIFIED:
                break
            frontier = chunk_offset_for_index(chunk_index, self.chunk_size) + self.chunk_size_for_index(chunk_index)
        self.state["verify_checkpoint"] = frontier

    def _sector_indexes_for_range(self, offset, size):
        chunk_index = chunk_index_for_offset(offset, self.chunk_size)
        chunk_offset = chunk_offset_for_index(chunk_index, self.chunk_size)
        relative_start = max(offset - chunk_offset, 0)
        relative_end = min(relative_start + size, self.chunk_size_for_index(chunk_index))
        start_sector = relative_start // self.sector_size
        end_sector = ceil_div(relative_end, self.sector_size)
        return range(start_sector, end_sector)

    def chunk_info(self, chunk_index):
        record = self.get_chunk_record(chunk_index)
        if record is None:
            record = {
                "status": self.effective_chunk_status(chunk_index),
                "retry_count": 0,
                "verify_count": 0,
                "last_error": None,
                "sector_scan_complete": False,
                "sectors": {},
            }
        return {
            "chunk_index": chunk_index,
            "offset": chunk_offset_for_index(chunk_index, self.chunk_size),
            "size": self.chunk_size_for_index(chunk_index),
            "status": self.effective_chunk_status(chunk_index),
            "retry_count": record.get("retry_count", 0),
            "verify_count": record.get("verify_count", 0),
            "last_error": record.get("last_error"),
            "sector_scan_complete": record.get("sector_scan_complete", False),
            "sector_count": self.sector_count_for_chunk(chunk_index),
        }

    def summarize_counts(self):
        counts = {
            STATUS_NOT_TRIED: 0,
            STATUS_ZERO_FILLED: 0,
            STATUS_COPIED: 0,
            STATUS_FAILED: 0,
            STATUS_VERIFIED: 0,
        }
        for chunk_index in range(self.chunk_count):
            counts[self.effective_chunk_status(chunk_index)] += 1
        return counts
