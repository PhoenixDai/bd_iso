import os
import sys
import time
from dataclasses import dataclass

from bd_iso_state import (
    BdIsoStateStore,
    DEFAULT_SECTOR_SIZE,
    STATUS_FAILED,
    STATUS_VERIFIED,
)
from bd_utils import open_bd_drive, resolve_source_path


PROGRESS_PREFIX = "CHECKPOINT"
MISMATCH_PREFIX = "MISMATCH"
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
DEFAULT_MAX_MISMATCHES = 100


def get_progress_file_path(iso_path):
    return f"{iso_path}.txt"


def parse_progress_file(iso_path):
    progress_file = get_progress_file_path(iso_path)
    checkpoint = 0
    mismatches = []

    if not os.path.exists(progress_file):
        return checkpoint, mismatches

    try:
        with open(progress_file, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue

                if line.startswith(f"{PROGRESS_PREFIX} "):
                    value = line[len(PROGRESS_PREFIX) + 1 :].strip()
                    checkpoint = int(value, 0)
                    continue

                if line.startswith(f"{MISMATCH_PREFIX} "):
                    value = line[len(MISMATCH_PREFIX) + 1 :].strip()
                    mismatches.append(int(value, 0))
                    continue

                # Backward compatibility with the old format, where each line
                # was just a raw offset and the last line doubled as resume data.
                mismatch_offset = int(line, 0)
                mismatches.append(mismatch_offset)
                checkpoint = mismatch_offset
    except Exception as exc:
        print(f"Warning: Could not read progress file: {exc}")

    return checkpoint, mismatches


def append_progress_entry(iso_path, prefix, offset):
    progress_file = get_progress_file_path(iso_path)
    try:
        with open(progress_file, "a", encoding="utf-8") as handle:
            handle.write(f"{prefix} {offset}\n")
    except Exception:
        pass


def save_checkpoint(iso_path, offset):
    append_progress_entry(iso_path, PROGRESS_PREFIX, offset)


def save_mismatch(iso_path, offset):
    append_progress_entry(iso_path, MISMATCH_PREFIX, offset)


def clear_progress(iso_path):
    progress_file = get_progress_file_path(iso_path)
    if os.path.exists(progress_file):
        try:
            os.remove(progress_file)
        except Exception:
            pass


def emit_observer(observer, event, **payload):
    if observer is None:
        return
    observer(event, **payload)


def is_cancelled(cancel_event):
    return cancel_event is not None and cancel_event.is_set()


def format_bytes(num_bytes):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:,.3f} {unit}"
        value /= 1024
    return f"{num_bytes} B"


def print_progress_bar(current, total, speed_mb_s, bar_length=40):
    percent = current / total if total > 0 else 1
    percent = max(0, min(percent, 1))
    filled_length = int(bar_length * percent)
    bar = "#" * filled_length + "-" * (bar_length - filled_length)

    current_mb = current / (1024**2)
    total_mb = total / (1024**2)

    sys.stdout.write(
        f"\r[{bar}] {current_mb:,.2f}MB/{total_mb:,.2f}MB "
        f"({percent * 100:.2f}%) | Speed: {speed_mb_s:.2f} MB/s"
    )
    sys.stdout.flush()


def normalize_bd_path(drive_letter):
    if os.name == "nt":
        stripped = drive_letter.strip()
        # Already a full device path — return as-is
        if stripped.startswith("\\\\.\\"):
            return stripped
        drive_letter = stripped.upper().strip(":").strip("\\").strip("/")
        return f"\\\\.\\{drive_letter}:"
    return resolve_source_path(drive_letter)


def parse_start_offset_arg(raw_value):
    normalized = raw_value.strip()
    if normalized.lower() in {"resume", "--resume"}:
        return None
    return int(normalized, 0)


def find_first_difference_offset(left, right):
    limit = min(len(left), len(right))
    for index in range(limit):
        if left[index] != right[index]:
            return index
    if len(left) != len(right):
        return limit
    return None


def count_mismatching_bytes(left, right):
    limit = min(len(left), len(right))
    mismatch_count = abs(len(left) - len(right))
    for index in range(limit):
        if left[index] != right[index]:
            mismatch_count += 1
    return mismatch_count


@dataclass
class RangeComparison:
    offset: int
    size: int
    matches: bool
    source_bytes: bytes
    iso_bytes: bytes
    first_diff_offset: int | None
    mismatching_bytes: int
    sector_statuses: list[str]


def build_sector_statuses(left, right, sector_size=DEFAULT_SECTOR_SIZE):
    statuses = []
    start = 0
    limit = max(len(left), len(right))
    while start < limit:
        left_sector = left[start : start + sector_size]
        right_sector = right[start : start + sector_size]
        statuses.append(STATUS_VERIFIED if left_sector == right_sector else STATUS_FAILED)
        start += sector_size
    return statuses


def compare_range_between_handles(
    source_file,
    iso_file,
    offset,
    size,
    *,
    sector_size=DEFAULT_SECTOR_SIZE,
    observer=None,
    cancel_event=None,
):
    if is_cancelled(cancel_event):
        raise KeyboardInterrupt()

    source_file.seek(offset)
    iso_file.seek(offset)
    source_bytes = source_file.read(size)
    iso_bytes = iso_file.read(size)
    if len(source_bytes) != size or len(iso_bytes) != size:
        raise OSError(
            f"Short read while comparing offset {offset}: "
            f"source={len(source_bytes)} iso={len(iso_bytes)} expected={size}"
        )

    matches = source_bytes == iso_bytes
    first_diff_in_range = None
    mismatching_bytes = 0
    sector_statuses = []
    if not matches:
        first_diff_in_range = find_first_difference_offset(source_bytes, iso_bytes)
        mismatching_bytes = count_mismatching_bytes(source_bytes, iso_bytes)
        sector_statuses = build_sector_statuses(
            source_bytes,
            iso_bytes,
            sector_size=sector_size,
        )

    result = RangeComparison(
        offset=offset,
        size=size,
        matches=matches,
        source_bytes=source_bytes,
        iso_bytes=iso_bytes,
        first_diff_offset=(
            None if first_diff_in_range is None else offset + first_diff_in_range
        ),
        mismatching_bytes=mismatching_bytes,
        sector_statuses=sector_statuses,
    )
    emit_observer(
        observer,
        "verify_range_complete",
        offset=offset,
        size=size,
        matches=matches,
        first_diff_offset=result.first_diff_offset,
        mismatching_bytes=mismatching_bytes,
    )
    return result


def verify_iso_range(
    drive_letter,
    iso_path,
    offset,
    size,
    *,
    open_source=open_bd_drive,
    sector_size=DEFAULT_SECTOR_SIZE,
    observer=None,
    cancel_event=None,
    state_path=None,
):
    expected_size = None
    try:
        expected_size = os.path.getsize(iso_path)
    except OSError:
        pass
    bd_path = resolve_source_path(normalize_bd_path(drive_letter), expected_size=expected_size)
    state_store = None
    if state_path is not None:
        device_size = os.path.getsize(iso_path)
        state_store = BdIsoStateStore.load_or_hydrate(
            iso_path,
            source_path=bd_path,
            device_size=device_size,
            chunk_size=DEFAULT_CHUNK_SIZE,
            state_path=state_path,
        )

    with open_source(bd_path) as bd_file, open(iso_path, "rb") as iso_file:
        result = compare_range_between_handles(
            bd_file,
            iso_file,
            offset,
            size,
            sector_size=sector_size,
            observer=observer,
            cancel_event=cancel_event,
        )

    if state_store is not None:
        if size <= sector_size:
            sector_index = (offset % state_store.chunk_size) // state_store.sector_size
            chunk_index = offset // state_store.chunk_size
            state_store.record_sector_verify(
                chunk_index,
                sector_index,
                verified=result.matches,
                error=None if result.matches else "Sector mismatch",
            )
        elif result.matches:
            state_store.record_verify_success(offset, size)
        else:
            chunk_index = offset // state_store.chunk_size
            state_store.record_sector_scan(chunk_index, result.sector_statuses)
    return result.matches


def compare_bd_to_iso(
    drive_letter,
    iso_path,
    chunk_size=DEFAULT_CHUNK_SIZE,
    start_offset=None,
    max_mismatches=DEFAULT_MAX_MISMATCHES,
    open_source=open_bd_drive,
    observer=None,
    cancel_event=None,
    state_path=None,
    sector_size=DEFAULT_SECTOR_SIZE,
):
    """
    Compares a BD disc with an ISO file bit by bit.

    Args:
        drive_letter (str): The drive letter of the BD-RE drive (e.g., 'D').
        iso_path (str): The path to the ISO file.
        chunk_size (int): The number of bytes to read at a time. Default is 4MB.
        start_offset (int): The byte offset to start comparing from. If None,
            it tries to read from a progress file.
        max_mismatches (int): Stop after this many chunk mismatches in one run.
    """
    expected_size = None
    if os.path.exists(iso_path):
        try:
            expected_size = os.path.getsize(iso_path)
        except OSError:
            pass
    bd_path = resolve_source_path(normalize_bd_path(drive_letter), expected_size=expected_size)

    if chunk_size <= 0:
        print("Error: chunk_size must be greater than 0.")
        return False

    if max_mismatches <= 0:
        print("Error: max_mismatches must be greater than 0.")
        return False
    if sector_size <= 0:
        print("Error: sector_size must be greater than 0.")
        return False

    if not os.path.exists(iso_path):
        print(f"Error: ISO file not found at {iso_path}")
        return False

    try:
        iso_size = os.path.getsize(iso_path)
    except OSError as exc:
        print(f"Error getting ISO size: {exc}")
        return False

    saved_checkpoint, prior_mismatches = parse_progress_file(iso_path)

    if start_offset is None:
        start_offset = saved_checkpoint
        if start_offset > 0:
            print(
                f"Found saved progress. Auto-resuming from checkpoint "
                f"{start_offset} (0x{start_offset:X})."
            )
    elif start_offset < 0:
        print("Error: start_offset cannot be negative.")
        return False

    if start_offset >= iso_size:
        print(
            f"Error: Start offset ({start_offset}) is greater than or equal to "
            f"ISO size ({iso_size})."
        )
        if start_offset > 0:
            print("You may want to delete the progress file if it is stale.")
        return False

    print(f"Comparing BD drive ({bd_path}) with ISO ({iso_path})")
    print(f"ISO Size: {format_bytes(iso_size)}")
    if start_offset > 0:
        print(
            f"Starting/Resuming from offset: {start_offset} "
            f"(0x{start_offset:X})"
        )
    print(f"Chunk Size: {format_bytes(chunk_size)}")
    if prior_mismatches:
        print(
            f"Existing mismatch log entries: {len(prior_mismatches)} "
            f"(stored in {get_progress_file_path(iso_path)})"
        )
    print("-" * 70)

    bytes_read = start_offset
    last_verified_offset = start_offset
    state_store = None
    if state_path is not None:
        state_store = BdIsoStateStore.load_or_hydrate(
            iso_path,
            source_path=bd_path,
            device_size=iso_size,
            chunk_size=chunk_size,
            state_path=state_path,
        )
        state_store.ensure_metadata(
            source_path=bd_path,
            device_size=iso_size,
            chunk_size=chunk_size,
            sector_size=sector_size,
        )
        state_store.save()
    emit_observer(
        observer,
        "verify_start",
        source_path=bd_path,
        iso_path=iso_path,
        total=iso_size,
        chunk_size=chunk_size,
        start_offset=start_offset,
    )

    try:
        with open_source(bd_path) as bd_file, open(iso_path, "rb") as iso_file:
            if start_offset > 0:
                bd_file.seek(start_offset)
                iso_file.seek(start_offset)

            start_time = time.monotonic()
            last_print_time = start_time
            last_checkpoint_time = start_time
            bytes_processed_this_run = 0
            mismatch_count = 0
            mismatch_offsets = []

            print_progress_bar(bytes_read, iso_size, 0)

            while True:
                if is_cancelled(cancel_event):
                    raise KeyboardInterrupt()
                if mismatch_count >= max_mismatches:
                    print(
                        f"\nReached {max_mismatches} chunk mismatches, "
                        "stopping this run."
                    )
                    break

                iso_chunk = iso_file.read(chunk_size)
                if not iso_chunk:
                    last_verified_offset = bytes_read
                    break

                bd_file.seek(bytes_read)
                iso_file.seek(bytes_read)
                result = compare_range_between_handles(
                    bd_file,
                    iso_file,
                    bytes_read,
                    len(iso_chunk),
                    sector_size=sector_size,
                    observer=observer,
                    cancel_event=cancel_event,
                )

                if not result.matches:
                    mismatch_offset = bytes_read
                    print(
                        f"\n[!] Chunk mismatch at offset {mismatch_offset} "
                        f"(0x{mismatch_offset:X}); first differing byte at "
                        f"{result.first_diff_offset} (0x{result.first_diff_offset:X}); "
                        f"{result.mismatching_bytes} mismatching bytes in chunk"
                    )
                    save_mismatch(iso_path, mismatch_offset)
                    mismatch_offsets.append(mismatch_offset)
                    mismatch_count += 1
                    if state_store is not None:
                        state_store.record_sector_scan(
                            mismatch_offset // chunk_size,
                            result.sector_statuses,
                        )
                    emit_observer(
                        observer,
                        "verify_chunk_failed",
                        offset=mismatch_offset,
                        size=len(iso_chunk),
                        first_diff_offset=result.first_diff_offset,
                        mismatching_bytes=result.mismatching_bytes,
                    )
                else:
                    last_verified_offset = bytes_read + len(iso_chunk)
                    if state_store is not None:
                        state_store.record_verify_success(
                            bytes_read,
                            len(iso_chunk),
                        )
                    emit_observer(
                        observer,
                        "verify_chunk_verified",
                        offset=bytes_read,
                        size=len(iso_chunk),
                    )

                bytes_read += len(result.iso_bytes)
                bytes_processed_this_run += len(result.iso_bytes)

                current_time = time.monotonic()
                if current_time - last_checkpoint_time >= 1:
                    save_checkpoint(iso_path, last_verified_offset)
                    last_checkpoint_time = current_time

                if current_time - last_print_time >= 1:
                    elapsed = current_time - start_time
                    speed = (
                        (bytes_processed_this_run / (1024**2)) / elapsed
                        if elapsed > 0
                        else 0
                    )
                    print_progress_bar(bytes_read, iso_size, speed)
                    last_print_time = current_time
                emit_observer(
                    observer,
                    "verify_progress",
                    current=bytes_read,
                    total=iso_size,
                    last_verified_offset=last_verified_offset,
                    mismatches=mismatch_count,
                )

            elapsed = time.monotonic() - start_time
            average_speed = (
                (bytes_processed_this_run / (1024**2)) / elapsed if elapsed > 0 else 0
            )
            print_progress_bar(bytes_read, iso_size, average_speed)
            print("\n")
            print(f"Run complete. {mismatch_count} mismatches logged this run.")
            print(f"Total time for this run: {elapsed:.2f} seconds")
            print(f"Average speed: {average_speed:.2f} MB/s")
            print(
                f"Last verified matching offset: {last_verified_offset} "
                f"(0x{last_verified_offset:X})"
            )

            if mismatch_offsets:
                preview = ", ".join(
                    f"{offset} (0x{offset:X})" for offset in mismatch_offsets[:5]
                )
                print(f"First mismatch offsets from this run: {preview}")
                if len(mismatch_offsets) > 5:
                    print(
                        f"... plus {len(mismatch_offsets) - 5} more. "
                        f"See {get_progress_file_path(iso_path)}."
                    )

            if mismatch_count == 0:
                print("\nSuccess: BD disc matches ISO file perfectly up to the ISO size!")
                clear_progress(iso_path)
                emit_observer(observer, "verify_complete", success=True)
                return True

            save_checkpoint(iso_path, last_verified_offset)
            print(
                f"Progress and mismatch offsets saved to "
                f"{get_progress_file_path(iso_path)}."
            )
            emit_observer(observer, "verify_complete", success=False)
            return False

    except PermissionError:
        print("\n\nError: Permission denied.")
        print(
            "Please run the command prompt or terminal as Administrator "
            "to access the physical drive directly on Windows."
        )
        return False
    except KeyboardInterrupt:
        print("\n\nComparison interrupted by user.")
        save_checkpoint(iso_path, last_verified_offset)
        print(
            f"Progress saved to {get_progress_file_path(iso_path)}. "
            "You can resume later."
        )
        return False
    except Exception as exc:
        print(f"\n\nAn unexpected error occurred: {exc}")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print(
            "Usage: python compare_bd_iso.py <Drive_Letter> <Path_to_ISO> "
            "[start_offset_bytes|resume]"
        )
        print("Example: python compare_bd_iso.py E C:\\path\\to\\image.iso")
        print("Resume Example: python compare_bd_iso.py E C:\\path\\to\\image.iso 10737418240")
        print("Resume Alias: python compare_bd_iso.py E C:\\path\\to\\image.iso resume")
        sys.exit(1)

    drive = sys.argv[1]
    iso = sys.argv[2]
    start_offset = None

    if len(sys.argv) == 4:
        try:
            start_offset = parse_start_offset_arg(sys.argv[3])
        except ValueError:
            print(
                "Error: start_offset must be an integer "
                "(e.g., 1048576), a hex value (e.g., 0x100000), "
                "or `resume` to use the saved checkpoint."
            )
            sys.exit(1)

    if compare_bd_to_iso(drive, iso, start_offset=start_offset):
        sys.exit(0)
    sys.exit(1)
