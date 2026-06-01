import argparse
import os
import sys
import time
from dataclasses import dataclass, field

from bd_iso_state import BdIsoStateStore, STATUS_ZERO_FILLED, chunk_offset_for_index
from bd_utils import (
    get_block_device_size,
    is_optical_device_path,
    open_bd_drive,
    resolve_source_path,
)
from compare_bd_iso import DEFAULT_CHUNK_SIZE, compare_bd_to_iso, format_bytes


COPY_PROGRESS_SUFFIX = ".copy.txt"
SOURCE_PREFIX = "SOURCE"
DEVICE_SIZE_PREFIX = "DEVICE_SIZE"
CHUNK_SIZE_PREFIX = "CHUNK_SIZE"
CHECKPOINT_PREFIX = "CHECKPOINT"
BAD_PREFIX = "BAD"
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 1.0
DEFAULT_MIN_READ_SIZE = 2048
ZERO_FILL_BUFFER_SIZE = 1024 * 1024


class ProgressFileError(ValueError):
    pass


class ReadFailure(RuntimeError):
    def __init__(self, offset, size, cause):
        self.offset = offset
        self.size = size
        self.cause = cause
        super().__init__(f"Unable to read {size} bytes at offset {offset}: {cause}")


@dataclass
class CopyProgress:
    source_path: str | None = None
    device_size: int | None = None
    chunk_size: int | None = None
    checkpoint: int = 0
    bad_regions: list[tuple[int, int]] = field(default_factory=list)


def get_copy_progress_path(iso_path):
    return f"{iso_path}{COPY_PROGRESS_SUFFIX}"


def canonicalize_source_path(source_path, expected_size=None):
    return resolve_source_path(source_path, expected_size=expected_size)


def parse_copy_progress(progress_path):
    progress = CopyProgress()

    with open(progress_path, "r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith(f"{SOURCE_PREFIX} "):
                progress.source_path = line[len(SOURCE_PREFIX) + 1 :].strip()
                continue

            parts = line.split()
            prefix = parts[0]

            try:
                if prefix == DEVICE_SIZE_PREFIX and len(parts) == 2:
                    progress.device_size = int(parts[1], 0)
                elif prefix == CHUNK_SIZE_PREFIX and len(parts) == 2:
                    progress.chunk_size = int(parts[1], 0)
                elif prefix == CHECKPOINT_PREFIX and len(parts) == 2:
                    progress.checkpoint = int(parts[1], 0)
                elif prefix == BAD_PREFIX and len(parts) == 2:
                    progress.bad_regions.append((int(parts[1], 0), DEFAULT_MIN_READ_SIZE))
                elif prefix == BAD_PREFIX and len(parts) == 3:
                    progress.bad_regions.append((int(parts[1], 0), int(parts[2], 0)))
                else:
                    raise ProgressFileError(
                        f"Unrecognized progress entry on line {line_number}: {line}"
                    )
            except ValueError as exc:
                raise ProgressFileError(
                    f"Invalid numeric value on line {line_number}: {line}"
                ) from exc

    return progress


def write_copy_metadata(progress_path, source_path, device_size, chunk_size):
    progress = CopyProgress(
        source_path=canonicalize_source_path(source_path),
        device_size=device_size,
        chunk_size=chunk_size,
    )
    write_copy_progress(progress_path, progress)
    return progress


def write_copy_progress(progress_path, progress):
    temp_path = f"{progress_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        handle.write(f"{SOURCE_PREFIX} {progress.source_path}\n")
        handle.write(f"{DEVICE_SIZE_PREFIX} {progress.device_size}\n")
        handle.write(f"{CHUNK_SIZE_PREFIX} {progress.chunk_size}\n")
        handle.write(f"{CHECKPOINT_PREFIX} {progress.checkpoint}\n")
        for offset, size in progress.bad_regions:
            handle.write(f"{BAD_PREFIX} {offset} {size}\n")
    os.replace(temp_path, progress_path)


def save_checkpoint(progress_path, progress, offset):
    progress.checkpoint = offset
    write_copy_progress(progress_path, progress)


def save_bad_region(progress_path, progress, offset, size):
    progress.bad_regions.append((offset, size))
    write_copy_progress(progress_path, progress)


def emit_observer(observer, event, **payload):
    if observer is None:
        return
    observer(event, **payload)


def is_cancelled(cancel_event):
    return cancel_event is not None and cancel_event.is_set()


def validate_resume_state(progress, source_path, device_size, chunk_size, iso_size):
    if progress.source_path is None:
        raise ProgressFileError("Progress file is missing a SOURCE entry.")
    if progress.device_size is None:
        raise ProgressFileError("Progress file is missing a DEVICE_SIZE entry.")
    if progress.chunk_size is None:
        raise ProgressFileError("Progress file is missing a CHUNK_SIZE entry.")
    if progress.checkpoint < 0:
        raise ProgressFileError("Progress file has a negative CHECKPOINT value.")

    canonical_source = canonicalize_source_path(source_path, expected_size=device_size)
    if progress.source_path != canonical_source:
        if not (
            is_optical_device_path(progress.source_path)
            and is_optical_device_path(canonical_source)
        ):
            raise ProgressFileError(
                "Progress file source path does not match the requested source.\n"
                f"Stored: {progress.source_path}\n"
                f"Given:  {canonical_source}"
            )
    if progress.device_size != device_size:
        raise ProgressFileError(
            "Progress file device size does not match the current source.\n"
            f"Stored: {progress.device_size}\n"
            f"Given:  {device_size}"
        )
    if progress.chunk_size != chunk_size:
        raise ProgressFileError(
            "Progress file chunk size does not match the requested chunk size.\n"
            f"Stored: {progress.chunk_size}\n"
            f"Given:  {chunk_size}"
        )
    if iso_size > device_size:
        raise ProgressFileError(
            "Existing ISO is larger than the source device. Use --restart to reset it."
        )

    return min(progress.checkpoint, iso_size)


def read_exact(source_file, offset, size):
    source_file.seek(offset)
    data = source_file.read(size)
    if len(data) != size:
        raise OSError(
            f"Short read at offset {offset}: expected {size} bytes, got {len(data)}"
        )
    return data


def read_range_with_retries(
    source_file,
    offset,
    size,
    retries,
    retry_delay,
    min_read_size,
    cancel_event=None,
):
    if size <= 0:
        return b""

    last_error = None
    for attempt in range(retries + 1):
        if is_cancelled(cancel_event):
            raise KeyboardInterrupt()
        try:
            return read_exact(source_file, offset, size)
        except Exception as exc:
            last_error = exc
            if attempt < retries and retry_delay > 0:
                time.sleep(retry_delay)

    if size <= min_read_size:
        raise ReadFailure(offset, size, last_error)

    smaller_size = max(min_read_size, size // 2)
    if smaller_size >= size:
        smaller_size = min_read_size

    chunks = []
    current_offset = offset
    end_offset = offset + size

    while current_offset < end_offset:
        part_size = min(smaller_size, end_offset - current_offset)
        chunks.append(
            read_range_with_retries(
                source_file,
                current_offset,
                part_size,
                retries=retries,
                retry_delay=retry_delay,
                min_read_size=min_read_size,
                cancel_event=cancel_event,
            )
        )
        current_offset += part_size

    return b"".join(chunks)


def print_copy_progress(current, total, start_time, current_chunk_size, start_offset=0):
    elapsed = max(time.monotonic() - start_time, 1e-9)
    processed_bytes = max(current - start_offset, 0)
    speed_mb_s = (processed_bytes / (1024**2)) / elapsed
    percent = current / total if total > 0 else 1
    percent = max(0, min(percent, 1))
    bar_length = 40
    filled = int(bar_length * percent)
    bar = "#" * filled + "-" * (bar_length - filled)
    current_mb = current / (1024**2)
    total_mb = total / (1024**2)
    sys.stdout.write(
        f"\r[{bar}] {format_bytes(current)}/{format_bytes(total)} "
        f"({percent * 100:.2f}%) | "
        f"Speed: {speed_mb_s:.2f} MB/s"
    )
    sys.stdout.flush()


def copy_range_between_handles(
    source_file,
    iso_file,
    offset,
    size,
    *,
    retries,
    retry_delay,
    min_read_size,
    observer=None,
    cancel_event=None,
):
    if is_cancelled(cancel_event):
        raise KeyboardInterrupt()

    emit_observer(observer, "copy_chunk_start", offset=offset, size=size)
    chunk = read_range_with_retries(
        source_file,
        offset,
        size,
        retries=retries,
        retry_delay=retry_delay,
        min_read_size=min_read_size,
        cancel_event=cancel_event,
    )
    iso_file.seek(offset)
    iso_file.write(chunk)
    iso_file.flush()
    emit_observer(observer, "copy_chunk_success", offset=offset, size=len(chunk))
    return len(chunk)


def zero_fill_marked_chunks_before(iso_file, state_store, chunk_index):
    zero_buffer = b"\x00" * min(ZERO_FILL_BUFFER_SIZE, state_store.chunk_size)
    for prior_index in range(chunk_index):
        if state_store.effective_chunk_status(prior_index) != STATUS_ZERO_FILLED:
            continue

        remaining = state_store.chunk_size_for_index(prior_index)
        iso_file.seek(chunk_offset_for_index(prior_index, state_store.chunk_size))
        while remaining > 0:
            write_size = min(len(zero_buffer), remaining)
            iso_file.write(zero_buffer[:write_size])
            remaining -= write_size
    iso_file.flush()


def zero_fill_chunk(iso_file, state_store, chunk_index):
    zero_buffer = b"\x00" * min(ZERO_FILL_BUFFER_SIZE, state_store.chunk_size)
    remaining = state_store.chunk_size_for_index(chunk_index)
    iso_file.seek(chunk_offset_for_index(chunk_index, state_store.chunk_size))
    while remaining > 0:
        write_size = min(len(zero_buffer), remaining)
        iso_file.write(zero_buffer[:write_size])
        remaining -= write_size


def copy_iso_range_from_bd(
    source_path,
    iso_path,
    offset,
    size,
    *,
    retries=DEFAULT_RETRIES,
    retry_delay=DEFAULT_RETRY_DELAY,
    min_read_size=DEFAULT_MIN_READ_SIZE,
    open_source=open_bd_drive,
    observer=None,
    cancel_event=None,
    state_path=None,
    scope="auto",
):
    canonical_source = canonicalize_source_path(source_path)
    state_store = None
    if state_path is not None:
        device_size = get_block_device_size(canonical_source)
        canonical_source = canonicalize_source_path(
            source_path,
            expected_size=device_size,
        )
        state_store = BdIsoStateStore.load_or_hydrate(
            iso_path,
            source_path=canonical_source,
            device_size=device_size,
            chunk_size=DEFAULT_CHUNK_SIZE,
            state_path=state_path,
        )

    with open_source(canonical_source) as source_file, open(iso_path, "r+b") as iso_file:
        try:
            copied = copy_range_between_handles(
                source_file,
                iso_file,
                offset,
                size,
                retries=retries,
                retry_delay=retry_delay,
                min_read_size=min_read_size,
                observer=observer,
                cancel_event=cancel_event,
            )
        except ReadFailure as exc:
            if state_store is not None:
                state_store.record_copy_failure(
                    exc.offset,
                    exc.size,
                    error=str(exc.cause),
                )
            emit_observer(
                observer,
                "copy_chunk_failed",
                offset=exc.offset,
                size=exc.size,
                error=str(exc.cause),
            )
            return False

    if state_store is not None:
        if scope == "auto":
            scope = "sector" if size <= state_store.sector_size else "chunk"
        chunk_index = offset // state_store.chunk_size
        if scope == "sector":
            sector_index = (offset % state_store.chunk_size) // state_store.sector_size
            state_store.record_sector_retry(
                chunk_index,
                sector_index,
                persist=False,
            )
        else:
            state_store.record_chunk_retry(chunk_index, persist=False)
        state_store.set_copy_checkpoint(
            offset + copied,
            start_offset=offset,
            persist=False,
        )
        state_store.save()
    return True


def create_iso_from_bd(
    source_path,
    iso_path,
    *,
    chunk_size=DEFAULT_CHUNK_SIZE,
    retries=DEFAULT_RETRIES,
    retry_delay=DEFAULT_RETRY_DELAY,
    min_read_size=DEFAULT_MIN_READ_SIZE,
    resume=False,
    restart=False,
    verify=False,
    open_source=open_bd_drive,
    size_func=get_block_device_size,
    compare_func=compare_bd_to_iso,
    observer=None,
    cancel_event=None,
    state_path=None,
    start_offset_override=None,
):
    if os.name == "nt":
        print("Error: create_iso_from_bd.py currently supports Linux only.")
        return False
    if chunk_size <= 0:
        print("Error: chunk_size must be greater than 0.")
        return False
    if retries < 0:
        print("Error: retries cannot be negative.")
        return False
    if retry_delay < 0:
        print("Error: retry_delay cannot be negative.")
        return False
    if min_read_size <= 0:
        print("Error: min_read_size must be greater than 0.")
        return False
    if chunk_size < min_read_size:
        print("Error: chunk_size must be greater than or equal to min_read_size.")
        return False
    if resume and restart:
        print("Error: --resume and --restart cannot be used together.")
        return False
    if start_offset_override is not None and start_offset_override < 0:
        print("Error: start_offset_override cannot be negative.")
        return False

    canonical_source = canonicalize_source_path(source_path)
    progress_path = get_copy_progress_path(iso_path)

    try:
        device_size = size_func(canonical_source)
    except Exception as exc:
        print(f"Error determining source size: {exc}")
        return False
    canonical_source = canonicalize_source_path(source_path, expected_size=device_size)

    iso_exists = os.path.exists(iso_path)
    progress_exists = os.path.exists(progress_path)
    start_offset = 0
    iso_mode = "wb"
    progress = None
    state_store = None

    if state_path is not None:
        state_store = BdIsoStateStore.load_or_hydrate(
            iso_path,
            source_path=canonical_source,
            device_size=device_size,
            chunk_size=chunk_size,
            state_path=state_path,
            reset=restart or (not resume and not iso_exists and not progress_exists),
        )
        state_store.ensure_metadata(
            source_path=canonical_source,
            device_size=device_size,
            chunk_size=chunk_size,
        )
        if restart or (not resume and not iso_exists and not progress_exists):
            state_store.reset()
            state_store.ensure_metadata(
                source_path=canonical_source,
                device_size=device_size,
                chunk_size=chunk_size,
            )
            state_store.save()

    if restart:
        progress = write_copy_metadata(
            progress_path,
            canonical_source,
            device_size,
            chunk_size,
        )
    elif resume:
        if not progress_exists and start_offset_override is None:
            print(f"Error: Copy progress file not found at {progress_path}")
            return False
        if not iso_exists and start_offset_override is None:
            print(
                "Error: Cannot resume because the target ISO does not exist. "
                "Use --restart to begin a fresh copy."
            )
            return False
        if not iso_exists:
            open(iso_path, "wb").close()
            iso_exists = True

        try:
            if progress_exists:
                progress = parse_copy_progress(progress_path)
                start_offset = validate_resume_state(
                    progress,
                    canonical_source,
                    device_size,
                    chunk_size,
                    os.path.getsize(iso_path),
                )
            else:
                progress = write_copy_metadata(
                    progress_path,
                    canonical_source,
                    device_size,
                    chunk_size,
                )
            if start_offset_override is not None:
                if start_offset_override > device_size:
                    print("Error: start_offset_override is beyond the source size.")
                    return False
                start_offset = start_offset_override
            progress.checkpoint = start_offset
            write_copy_progress(progress_path, progress)
        except (OSError, ProgressFileError) as exc:
            print(f"Error: Invalid resume state: {exc}")
            return False

        iso_mode = "r+b"
    else:
        if iso_exists or progress_exists:
            print(
                "Error: Target ISO or copy progress already exists. "
                "Use --resume to continue or --restart to overwrite."
            )
            return False
        progress = write_copy_metadata(
            progress_path,
            canonical_source,
            device_size,
            chunk_size,
        )

    if state_store is not None and start_offset_override is not None:
        state_store.prepare_resume_from_chunk(
            start_offset // chunk_size,
            persist=False,
        )
        state_store.save()

    print(f"Source: {canonical_source}")
    print(f"Target ISO: {iso_path}")
    print(f"Source Size: {format_bytes(device_size)}")
    print(f"Chunk Size: {format_bytes(chunk_size)}")
    print(f"Retries per read: {retries}")
    print(f"Smallest fallback read size: {format_bytes(min_read_size)}")
    if start_offset > 0:
        print(
            f"Resuming from checkpoint {start_offset} "
            f"(0x{start_offset:X}) stored in {progress_path}"
        )
    emit_observer(
        observer,
        "copy_start",
        source_path=canonical_source,
        iso_path=iso_path,
        total=device_size,
        chunk_size=chunk_size,
        start_offset=start_offset,
    )

    try:
        with open_source(canonical_source) as source_file, open(iso_path, iso_mode) as iso_file:
            if start_offset > 0:
                try:
                    current_iso_size = os.path.getsize(iso_path)
                except OSError:
                    current_iso_size = 0
                if current_iso_size < start_offset:
                    iso_file.truncate(start_offset)
                    iso_file.flush()
                if state_store is not None and start_offset_override is not None:
                    zero_fill_marked_chunks_before(
                        iso_file,
                        state_store,
                        start_offset // chunk_size,
                    )

            if start_offset >= device_size:
                iso_file.truncate(device_size)
                iso_file.flush()
                print("ISO copy already complete.")
            else:
                copy_start_time = time.monotonic()
                last_print_time = 0.0
                current_offset = start_offset

                while current_offset < device_size:
                    if is_cancelled(cancel_event):
                        raise KeyboardInterrupt()
                    bytes_to_read = min(chunk_size, device_size - current_offset)
                    current_chunk_index = current_offset // chunk_size

                    if (
                        state_store is not None
                        and state_store.should_skip_copy_chunk(current_chunk_index)
                    ):
                        skipped_status = state_store.effective_chunk_status(current_chunk_index)
                        if skipped_status == STATUS_ZERO_FILLED:
                            zero_fill_chunk(iso_file, state_store, current_chunk_index)
                            iso_file.flush()

                        current_offset += bytes_to_read
                        save_checkpoint(progress_path, progress, current_offset)
                        state_store.set_copy_checkpoint(
                            current_offset,
                            start_offset=current_offset - bytes_to_read,
                            persist=False,
                        )
                        state_store.save()
                        emit_observer(
                            observer,
                            "copy_progress",
                            current=current_offset,
                            total=device_size,
                            skipped=True,
                            chunk_index=current_chunk_index,
                            chunk_status=skipped_status,
                        )
                        now = time.monotonic()
                        if now - last_print_time >= 1 or current_offset >= device_size:
                            print_copy_progress(
                                current_offset,
                                device_size,
                                copy_start_time,
                                bytes_to_read,
                                start_offset=start_offset,
                            )
                            last_print_time = now
                        continue

                    try:
                        copied = copy_range_between_handles(
                            source_file,
                            iso_file,
                            current_offset,
                            bytes_to_read,
                            retries=retries,
                            retry_delay=retry_delay,
                            min_read_size=min_read_size,
                            observer=observer,
                            cancel_event=cancel_event,
                        )
                    except ReadFailure as exc:
                        save_bad_region(progress_path, progress, exc.offset, exc.size)
                        if state_store is not None:
                            state_store.record_copy_failure(
                                exc.offset,
                                exc.size,
                                error=str(exc.cause),
                            )
                        emit_observer(
                            observer,
                            "copy_chunk_failed",
                            offset=exc.offset,
                            size=exc.size,
                            error=str(exc.cause),
                        )
                        print()
                        print(
                            f"Read failed at offset {exc.offset} (0x{exc.offset:X}) "
                            f"for {format_bytes(exc.size)} after retries: {exc.cause}"
                        )
                        print(f"Logged BAD region to {progress_path}")
                        return False

                    current_offset += copied
                    save_checkpoint(progress_path, progress, current_offset)
                    if state_store is not None:
                        state_store.record_copy_success(
                            current_offset - copied,
                            copied,
                            persist=False,
                        )
                        state_store.save()
                    emit_observer(
                        observer,
                        "copy_progress",
                        current=current_offset,
                        total=device_size,
                    )

                    now = time.monotonic()
                    if now - last_print_time >= 1 or current_offset >= device_size:
                        print_copy_progress(
                            current_offset,
                            device_size,
                            copy_start_time,
                            bytes_to_read,
                            start_offset=start_offset,
                        )
                        last_print_time = now

                iso_file.truncate(device_size)
                iso_file.flush()
                print()
                elapsed = max(time.monotonic() - copy_start_time, 1e-9)
                average_speed = (
                    max(device_size - start_offset, 0) / (1024**2)
                ) / elapsed
                print(f"Copy complete in {elapsed:.2f} seconds.")
                print(f"Average speed: {average_speed:.2f} MB/s")
                print(f"Copy progress saved to {progress_path}")
                emit_observer(
                    observer,
                    "copy_complete",
                    total=device_size,
                )

    except PermissionError:
        print("Error: Permission denied while reading the source or writing the ISO.")
        return False
    except KeyboardInterrupt:
        print()
        print(
            "Copy interrupted. Resume later with "
            f"`python create_iso_from_bd.py {source_path} {iso_path} --resume`."
        )
        return False
    except Exception as exc:
        print(f"Error during copy: {exc}")
        return False

    if verify:
        print("Starting verification against the source device...")
        verify_ok = compare_func(
            canonical_source,
            iso_path,
            chunk_size=chunk_size,
            observer=observer,
            cancel_event=cancel_event,
            state_path=state_path,
        )
        if not verify_ok:
            print("Verification failed. See the compare progress log for mismatch details.")
        return verify_ok

    return True


def build_argument_parser():
    parser = argparse.ArgumentParser(
        description="Create or resume an ISO copy directly from a Blu-ray device."
    )
    parser.add_argument("device", help="Linux block device or readable source file")
    parser.add_argument("output_iso", help="Path to the ISO file to create")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume a previously interrupted copy using <iso>.copy.txt",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Overwrite the ISO and start over, replacing existing copy progress",
    )
    parser.add_argument(
        "--chunk-size",
        type=lambda value: int(value, 0),
        default=DEFAULT_CHUNK_SIZE,
        help="Bytes to read per chunk (default: 4 MiB)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="How many times to retry a failing read before splitting it",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=DEFAULT_RETRY_DELAY,
        help="Seconds to wait between retry attempts",
    )
    parser.add_argument(
        "--min-read-size",
        type=lambda value: int(value, 0),
        default=DEFAULT_MIN_READ_SIZE,
        help="Smallest fallback read size in bytes (default: 2048)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run compare_bd_iso.py after the copy completes",
    )
    return parser


def main():
    parser = build_argument_parser()
    args = parser.parse_args()

    success = create_iso_from_bd(
        args.device,
        args.output_iso,
        chunk_size=args.chunk_size,
        retries=args.retries,
        retry_delay=args.retry_delay,
        min_read_size=args.min_read_size,
        resume=args.resume,
        restart=args.restart,
        verify=args.verify,
    )
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
