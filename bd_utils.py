import os
import random
import stat
import subprocess
from glob import glob


OPTICAL_ALIAS_PATHS = (
    "/dev/cdrom",
    "/dev/dvd",
    "/dev/bluray",
    "/dev/bd",
)
TOUCH_READ_SIZE = 64 * 1024
TOUCH_MAX_CANDIDATE_FILES = 512


def is_optical_device_path(device_path):
    if not device_path:
        return False
    if os.name == "nt":
        stripped = device_path.strip()
        if stripped.startswith("\\\\.\\"):
            return True
    normalized = os.path.realpath(device_path)
    base_name = os.path.basename(normalized)
    return (
        device_path in OPTICAL_ALIAS_PATHS
        or normalized in OPTICAL_ALIAS_PATHS
        or base_name.startswith("sr")
        or base_name.startswith("scd")
    )


def list_optical_drive_candidates():
    """Return a list of likely optical drive device paths for the current OS."""
    if os.name == "nt":
        import ctypes

        DRIVE_CDROM = 5
        candidates = []
        seen = set()
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter_index in range(26):
            if bitmask & (1 << letter_index):
                letter = chr(ord("A") + letter_index)
                root = f"{letter}:\\"
                if ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_CDROM:
                    device_path = f"\\\\.\\{letter}:"
                    if device_path not in seen:
                        candidates.append(device_path)
                        seen.add(device_path)
        return candidates

    candidates = []
    seen = set()

    for alias_path in OPTICAL_ALIAS_PATHS:
        if os.path.exists(alias_path):
            resolved = os.path.realpath(alias_path)
            if resolved not in seen:
                candidates.append(resolved)
                seen.add(resolved)

    for sysfs_path in sorted(glob("/sys/class/block/sr*")) + sorted(glob("/sys/class/block/scd*")):
        device_name = os.path.basename(sysfs_path)
        dev_path = os.path.join("/dev", device_name)
        resolved = os.path.realpath(dev_path)
        if os.path.exists(resolved) and resolved not in seen:
            candidates.append(resolved)
            seen.add(resolved)

    for dev_path in sorted(glob("/dev/sr*")) + sorted(glob("/dev/scd*")):
        resolved = os.path.realpath(dev_path)
        if os.path.exists(resolved) and resolved not in seen:
            candidates.append(resolved)
            seen.add(resolved)

    return candidates


def get_block_device_size(device_path):
    """
    Returns the size of a block device in bytes.

    On Windows, uses CreateFileW + DeviceIoControl (IOCTL_DISK_GET_LENGTH_INFO).
    On Linux, uses fcntl.ioctl (BLKGETSIZE64) with a sysfs fallback.

    For tests and dry runs, regular files are also supported and their file
    size is returned directly.
    """
    try:
        mode = os.stat(device_path).st_mode
    except OSError as exc:
        raise OSError(f"Could not stat source path {device_path!r}: {exc}") from exc

    if stat.S_ISREG(mode):
        return os.path.getsize(device_path)

    if os.name == "nt":
        import ctypes

        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80
        IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C

        handle = ctypes.windll.kernel32.CreateFileW(
            device_path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )

        if handle in (-1, 0xFFFFFFFF, 18446744073709551615):
            err = ctypes.GetLastError()
            if err == 5:
                raise PermissionError(
                    "Access is denied. Please run as Administrator "
                    "to query block device size."
                )
            raise OSError(f"Error opening device (WinError {err}).")

        try:
            length_info = ctypes.c_uint64(0)
            bytes_returned = ctypes.c_uint32(0)
            success = ctypes.windll.kernel32.DeviceIoControl(
                handle,
                IOCTL_DISK_GET_LENGTH_INFO,
                None,
                0,
                ctypes.byref(length_info),
                ctypes.sizeof(length_info),
                ctypes.byref(bytes_returned),
                None,
            )
            if not success:
                raise ctypes.WinError()
            size = length_info.value
            if size > 0:
                return size
            raise OSError("DeviceIoControl returned disk length 0.")
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    last_error = None

    try:
        import fcntl
        import struct

        blkgetsize64 = 0x80081272
        with open(device_path, "rb", buffering=0) as handle:
            packed_size = fcntl.ioctl(handle.fileno(), blkgetsize64, b"\x00" * 8)
        size = struct.unpack("Q", packed_size)[0]
        if size > 0:
            return size
    except Exception as exc:
        last_error = exc

    block_name = os.path.basename(os.path.realpath(device_path))
    sysfs_size_path = os.path.join("/sys/class/block", block_name, "size")

    try:
        with open(sysfs_size_path, "r", encoding="utf-8") as handle:
            sectors = int(handle.read().strip(), 0)
        size = sectors * 512
        if size > 0:
            return size
    except Exception as exc:
        if last_error is None:
            last_error = exc

    if stat.S_ISBLK(mode):
        raise OSError(
            f"Could not determine block device size for {device_path!r}: {last_error}"
        )

    return os.path.getsize(device_path)


def auto_detect_optical_drive(preferred_path=None, expected_size=None):
    candidate_paths = []
    seen = set()
    is_windows = os.name == "nt"

    def add_candidate(path):
        if not path:
            return
        if is_windows and path.startswith("\\\\.\\"):
            # Windows device paths don't work with os.path.realpath / os.path.exists
            resolved = path
            if resolved in seen:
                return
        else:
            resolved = os.path.realpath(path)
            if resolved in seen or not os.path.exists(resolved):
                return
        seen.add(resolved)
        candidate_paths.append(resolved)

    if preferred_path:
        if is_windows and preferred_path.startswith("\\\\.\\"):
            add_candidate(preferred_path)
        elif os.path.exists(preferred_path):
            add_candidate(preferred_path)

    for candidate in list_optical_drive_candidates():
        add_candidate(candidate)

    if expected_size is not None:
        for candidate in candidate_paths:
            try:
                if get_block_device_size(candidate) == expected_size:
                    return candidate
            except Exception:
                continue

    if candidate_paths:
        return candidate_paths[0]

    raise OSError("No optical drive device could be auto-detected.")


def resolve_source_path(source_path, expected_size=None):
    if os.name == "nt":
        # If a specific existing path is given (e.g. a regular file), use it
        if source_path and os.path.exists(source_path):
            return os.path.realpath(source_path)
        # If it already looks like a Windows device path, use it directly
        if source_path and source_path.startswith("\\\\.\\"):
            return source_path
        # Try parsing as a drive letter (D:, D:\, D) — convert to \\.\X:
        if source_path:
            try:
                drive_root = _windows_drive_root(source_path)
                return f"\\\\.\\{drive_root[0]}:"
            except OSError:
                pass
        # Auto-detect: enumerate CD-ROM drives
        return auto_detect_optical_drive(
            preferred_path=source_path or None,
            expected_size=expected_size,
        )

    if source_path and os.path.exists(source_path):
        return os.path.realpath(source_path)

    if is_optical_device_path(source_path) or not source_path:
        return auto_detect_optical_drive(
            preferred_path=source_path,
            expected_size=expected_size,
        )

    return os.path.realpath(source_path)


def run_eject_command(device_path, *, close_tray=False):
    if os.name == "nt":
        try:
            _windows_drive_root(device_path)
        except OSError:
            raise OSError(
                f"Cannot determine a Windows drive from {device_path!r}. "
                "Pass a drive letter like D:, D:\\, or \\\\.\\D:."
            )
        else:
            return _run_eject_windows(device_path, close_tray=close_tray)

    resolved = resolve_source_path(device_path)
    command = ["eject"]
    if close_tray:
        command.append("-t")
    command.append(resolved)

    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise OSError("The `eject` command was not found.") from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        if details:
            raise OSError(details) from exc
        raise OSError(f"`{' '.join(command)}` failed with exit code {exc.returncode}.") from exc

    return completed.stdout.strip() or completed.stderr.strip()


def eject_disc(device_path):
    run_eject_command(device_path, close_tray=False)
    return True


def close_tray(device_path):
    run_eject_command(device_path, close_tray=True)
    return True


def _windows_drive_root(device_path):
    """Extract a drive-letter root from a Windows device path.

    ``\\\\.\\D:``  →  ``D:\\``
    ``D:``         →  ``D:\\``
    ``D``          →  ``D:\\``
    ``D:\\``       →  ``D:\\``
    """
    stripped = device_path.strip()
    # Remove the \\\\.\\ prefix if present
    if stripped.startswith("\\\\.\\"):
        stripped = stripped[4:]
    # Extract the drive letter
    drive_letter = stripped.lstrip("\\").lstrip("/").rstrip(":\\/").strip(":")
    if len(drive_letter) == 1 and drive_letter.isalpha():
        return f"{drive_letter.upper()}:\\"
    raise OSError(f"Could not determine drive letter from {device_path!r}.")


def _run_eject_windows(device_path, *, close_tray=False):
    """Eject or load an optical drive tray on Windows via DeviceIoControl."""
    import ctypes

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
    IOCTL_STORAGE_LOAD_MEDIA = 0x002D480C

    # Resolve to \\\\.\\X: format if given a bare drive letter
    try:
        drive_root = _windows_drive_root(device_path)
    except OSError:
        drive_root = None

    if drive_root and not device_path.startswith("\\\\.\\"):
        device_path = f"\\\\.\\{drive_root[0]}:"

    # Eject / load require write access to the device
    handle = ctypes.windll.kernel32.CreateFileW(
        device_path,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,  # No file attributes for device handles
        None,
    )

    if handle in (-1, 0xFFFFFFFF, 18446744073709551615):
        err = ctypes.GetLastError()
        if err == 5:
            raise PermissionError(
                "Access is denied. Please run as Administrator to eject the disc."
            )
        if err == 21:
            raise OSError("The device is not ready. No disc in the drive?")
        raise OSError(f"Error opening device (WinError {err}).")

    try:
        ioctl = IOCTL_STORAGE_LOAD_MEDIA if close_tray else IOCTL_STORAGE_EJECT_MEDIA
        bytes_returned = ctypes.c_uint32(0)
        success = ctypes.windll.kernel32.DeviceIoControl(
            handle,
            ioctl,
            None,
            0,
            None,
            0,
            ctypes.byref(bytes_returned),
            None,
        )
        if not success:
            err = ctypes.GetLastError()
            if err == 1167:  # ERROR_NOT_READY
                raise OSError("The device is not ready. No disc in the drive?")
            if err == 87:  # ERROR_INVALID_PARAMETER
                action = "load" if close_tray else "eject"
                raise OSError(
                    f"Unable to {action} the tray. "
                    "The drive may not support software tray control."
                )
            raise ctypes.WinError(err)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)

    action = "Closed tray for" if close_tray else "Ejected"
    return f"{action} {device_path}."


def _decode_mount_field(value):
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def find_mount_points_for_source(source_path):
    if os.name == "nt":
        try:
            return [_windows_drive_root(source_path)]
        except OSError:
            pass

    resolved_source = os.path.realpath(source_path)
    mount_points = []

    try:
        with open("/proc/mounts", "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return mount_points

    for raw_line in lines:
        parts = raw_line.split()
        if len(parts) < 2:
            continue
        mounted_source = os.path.realpath(_decode_mount_field(parts[0]))
        mount_point = _decode_mount_field(parts[1])
        if mounted_source == resolved_source and os.path.isdir(mount_point):
            mount_points.append(mount_point)

    return mount_points


def mount_source_device(source_path):
    if os.name == "nt":
        try:
            _windows_drive_root(source_path)
        except OSError:
            pass
        else:
            import ctypes

            drive_root = _windows_drive_root(source_path)
            # Check whether the volume is ready (has media).
            # GetDriveTypeW returns 2 (DRIVE_REMOVABLE) or 5 (DRIVE_CDROM)
            # for optical drives.
            result = ctypes.windll.kernel32.GetDriveTypeW(drive_root)
            if result not in (2, 5):  # DRIVE_REMOVABLE or DRIVE_CDROM
                raise OSError(
                    f"Cannot access {drive_root}. "
                    f"The drive may not exist or is not an optical / removable drive."
                )

            # Verify there is actually media by attempting to stat the root
            try:
                os.stat(drive_root)
            except OSError as exc:
                raise OSError(
                    f"No disc detected in {drive_root}. "
                    "Insert a disc and try again."
                ) from exc

            return [drive_root]

    resolved = os.path.realpath(source_path)
    commands = (
        ["udisksctl", "mount", "-b", resolved],
        ["mount", resolved],
    )
    errors = []

    for command in commands:
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            errors.append(f"`{command[0]}` was not found")
            continue
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or exc.stdout or "").strip()
            if details:
                errors.append(details)
            else:
                errors.append(
                    f"`{' '.join(command)}` failed with exit code {exc.returncode}"
                )
            continue

        mount_points = find_mount_points_for_source(resolved)
        if mount_points:
            return mount_points
        errors.append(f"`{' '.join(command)}` completed but {resolved} is still not mounted")

    details = "; ".join(errors) if errors else "no mount command succeeded"
    raise OSError(f"Could not mount {resolved}: {details}")


def _read_random_small_file(mount_point, *, rng=random, max_candidates=TOUCH_MAX_CANDIDATE_FILES):
    candidates = []

    for root, dirs, files in os.walk(mount_point):
        dirs[:] = sorted(dirs)
        for file_name in sorted(files):
            path = os.path.join(root, file_name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size <= 0:
                continue
            candidates.append(path)
            if len(candidates) >= max_candidates:
                break
        if len(candidates) >= max_candidates:
            break

    if not candidates:
        raise OSError(f"No readable files found under mounted disc {mount_point!r}.")

    path = rng.choice(candidates)
    with open(path, "rb") as handle:
        data = handle.read(TOUCH_READ_SIZE)
    if not data:
        raise OSError(f"Read no data from {path!r}.")
    return path, len(data)


def touch_disc(device_path, *, expected_size=None, rng=random):
    resolved = resolve_source_path(device_path, expected_size=expected_size)
    mount_points = find_mount_points_for_source(resolved)
    if not mount_points:
        mount_points = mount_source_device(resolved)

    errors = []
    for mount_point in mount_points:
        try:
            path, byte_count = _read_random_small_file(mount_point, rng=rng)
            return f"Read {byte_count} bytes from {path}"
        except OSError as exc:
            errors.append(str(exc))

    details = "; ".join(errors) if errors else "no readable file found"
    raise OSError(f"Could not touch mounted disc {resolved}: {details}")


def open_bd_drive(bd_path):
    """
    Opens a BD drive for reading on Windows or Unix systems.

    Args:
        bd_path (str): The device path of the BD drive.
                      On Windows: r'\\\\.\\D:' format
                      On Unix: '/dev/sr0' format

    Returns:
        file: A file object opened in binary read mode.

    Raises:
        PermissionError: If access is denied (Windows).
        OSError: If the drive cannot be opened.
    """
    if os.name == "nt":
        import ctypes
        import msvcrt

        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80

        handle = ctypes.windll.kernel32.CreateFileW(
            bd_path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )

        if handle == -1 or handle == 0xFFFFFFFF or handle == 18446744073709551615:
            err = ctypes.GetLastError()
            if err == 5:
                raise PermissionError("Access is denied. Please run as Administrator.")
            raise OSError(f"Error opening drive (WinError {err}).")

        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        return os.fdopen(fd, "rb")

    bd_path = resolve_source_path(bd_path)
    return open(bd_path, "rb")
