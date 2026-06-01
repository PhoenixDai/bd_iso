import os
import sys

from bd_utils import open_bd_drive
from compare_bd_iso import DEFAULT_CHUNK_SIZE, MISMATCH_PREFIX, format_bytes, normalize_bd_path


def parse_mismatch_offsets(progress_file_path):
    offsets = []
    seen = set()

    with open(progress_file_path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue

            offset = None
            if line.startswith(f"{MISMATCH_PREFIX} "):
                value = line[len(MISMATCH_PREFIX) + 1 :].strip()
                offset = int(value, 0)
            elif line[0].isdigit():
                # Backward compatibility with the original one-offset-per-line format.
                offset = int(line, 0)

            if offset is None or offset in seen:
                continue

            seen.add(offset)
            offsets.append(offset)

    return sorted(offsets)


def repair_iso_from_bd(
    drive,
    iso_path,
    progress_file_path,
    chunk_size=DEFAULT_CHUNK_SIZE,
    create_backup=False,
):
    if chunk_size <= 0:
        print("Error: chunk_size must be greater than 0.")
        return False

    if not os.path.exists(progress_file_path):
        print(f"Error: Progress file not found at {progress_file_path}")
        return False

    if not os.path.exists(iso_path):
        print(f"Error: ISO file not found at {iso_path}")
        return False

    try:
        iso_size = os.path.getsize(iso_path)
    except OSError as exc:
        print(f"Error getting ISO size: {exc}")
        return False

    try:
        mismatch_offsets = parse_mismatch_offsets(progress_file_path)
    except Exception as exc:
        print(f"Error reading mismatch log: {exc}")
        return False

    if not mismatch_offsets:
        print("No mismatch offsets found in the progress file.")
        return False

    bd_path = normalize_bd_path(drive)
    print(f"Repair source: {bd_path}")
    print(f"Target ISO: {iso_path}")
    print(f"Mismatch log: {progress_file_path}")
    print(f"Unique mismatching chunks: {len(mismatch_offsets)}")
    print(f"Chunk Size: {format_bytes(chunk_size)}")

    if create_backup:
        print("Backup mode: enabled")

    repaired = 0

    try:
        with open_bd_drive(bd_path) as bd_file, open(iso_path, "r+b") as iso_file:
            for offset in mismatch_offsets:
                if offset < 0 or offset >= iso_size:
                    print(
                        f"[skip] Offset {offset} (0x{offset:X}) is outside the ISO size."
                    )
                    continue

                bytes_to_copy = min(chunk_size, iso_size - offset)

                bd_file.seek(offset)
                good_chunk = bd_file.read(bytes_to_copy)
                if len(good_chunk) != bytes_to_copy:
                    print(
                        f"[fail] Could not read full chunk from BD at {offset} "
                        f"(0x{offset:X}); expected {bytes_to_copy}, got {len(good_chunk)}."
                    )
                    return False

                if create_backup:
                    backup_path = f"{iso_path}.bak.{offset}"
                    iso_file.seek(offset)
                    original_chunk = iso_file.read(bytes_to_copy)
                    with open(backup_path, "wb") as backup_file:
                        backup_file.write(original_chunk)

                iso_file.seek(offset)
                iso_file.write(good_chunk)
                iso_file.flush()

                repaired += 1
                print(
                    f"[ok] Repaired chunk at {offset} (0x{offset:X}) "
                    f"size {format_bytes(bytes_to_copy)}"
                )

    except PermissionError:
        print("Error: Permission denied while accessing the BD drive or ISO file.")
        return False
    except Exception as exc:
        print(f"Error during repair: {exc}")
        return False

    print(f"Repair complete. Updated {repaired} chunks.")
    print("Run compare_bd_iso.py again to verify the repaired ISO.")
    return repaired > 0


if __name__ == "__main__":
    if len(sys.argv) < 4 or len(sys.argv) > 6:
        print(
            "Usage: python repair_iso_from_bd.py <Drive> <Path_to_ISO> "
            "<Path_to_Progress_File> [chunk_size_bytes] [--backup]"
        )
        print(
            "Example: python repair_iso_from_bd.py /dev/cdrom "
            "/media/qidai/MP600/t2/diskimage.iso "
            "/media/qidai/MP600/t2/diskimage.iso.txt"
        )
        sys.exit(1)

    drive_arg = sys.argv[1]
    iso_arg = sys.argv[2]
    progress_arg = sys.argv[3]
    chunk_size_arg = DEFAULT_CHUNK_SIZE
    backup_arg = False

    for arg in sys.argv[4:]:
        if arg == "--backup":
            backup_arg = True
        else:
            try:
                chunk_size_arg = int(arg, 0)
            except ValueError:
                print(
                    "Error: chunk_size must be an integer "
                    "(for example 4194304 or 0x400000)."
                )
                sys.exit(1)

    if repair_iso_from_bd(
        drive_arg,
        iso_arg,
        progress_arg,
        chunk_size=chunk_size_arg,
        create_backup=backup_arg,
    ):
        sys.exit(0)
    sys.exit(1)
