"""
Test verify-from-here on actual optical drive (Windows).

Usage:  python tests/test_verify_windows.py [drive_letter] [iso_path]

Defaults to drive E: and D:/Workspace/bd_iso/output/img.iso.
Runs compare_bd_to_iso from chunk 0 to verify the BD handle survives
sustained sequential reads. Also tests resolve_source_path correctness.
"""
import os
import sys
import time

# Ensure the parent (project root) is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import threading
import queue


def test_resolve_source_path():
    """Verify resolve_source_path returns device paths, not filesystem paths."""
    from bd_utils import resolve_source_path

    tests = [
        ("E:",     r"\\.\E:"),
        ("E" + chr(92), r"\\.\E:"),   # E:\
        ("E",      r"\\.\E:"),
        (chr(92)*2 + "." + chr(92) + "E:", r"\\.\E:"),  # \\.\E:
    ]
    for input_val, expected in tests:
        result = resolve_source_path(input_val)
        status = "OK" if result == expected else "FAIL"
        print(f"  resolve_source_path({input_val!r}) -> {result!r}  [{status}]")
        assert result == expected, f"Expected {expected!r}, got {result!r}"
    print("  resolve_source_path: all OK\n")


def test_normalize_bd_path():
    """Verify normalize_bd_path handles all input formats."""
    from compare_bd_iso import normalize_bd_path

    tests = [
        ("E:",     r"\\.\E:"),
        ("E" + chr(92), r"\\.\E:"),    # E:\
        ("E",      r"\\.\E:"),
        (chr(92)*2 + "." + chr(92) + "E:", r"\\.\E:"),  # \\.\E:
    ]
    for input_val, expected in tests:
        result = normalize_bd_path(input_val)
        status = "OK" if result == expected else "FAIL"
        print(f"  normalize_bd_path({input_val!r}) -> {result!r}  [{status}]")
        assert result == expected, f"Expected {expected!r}, got {result!r}"
    print("  normalize_bd_path: all OK\n")


def test_verify_from_here(drive, iso_path, chunk_index=0):
    """Run compare_bd_to_iso exactly as the GUI does for Verify From Here."""
    from bd_iso_state import BdIsoStateStore, chunk_offset_for_index
    from bd_utils import resolve_source_path
    from compare_bd_iso import compare_bd_to_iso

    print(f"Drive: {drive}")
    print(f"ISO:   {iso_path}")
    state_path = iso_path + ".state.json"
    print(f"State: {state_path}")
    print()

    # Step 1: Load state (like GUI's load_state)
    if os.path.exists(state_path):
        store = BdIsoStateStore.load(iso_path, state_path=state_path)
        chunk_size = store.chunk_size
        device_size = store.device_size
        print(f"Loaded state: chunk_size={chunk_size}, device_size={device_size}")
    else:
        chunk_size = 4 * 1024 * 1024
        print(f"No state file; using default chunk_size={chunk_size}")

    # Step 2: Resolve source path (like GUI's resolve_active_source_path)
    resolved = resolve_source_path(drive)
    print(f"Resolved source: {resolved!r}")

    # Step 3: Compute offset
    offset = chunk_offset_for_index(chunk_index, chunk_size)
    print(f"Start offset: {offset} (chunk {chunk_index})")

    # Step 4: Verify in a background thread (like GUI's _run_in_thread)
    event_queue = queue.Queue()
    cancel_event = threading.Event()

    def observer(event, **payload):
        event_queue.put({"event": event, **payload})

    errors = []

    def runner():
        try:
            result = compare_bd_to_iso(
                resolved, iso_path,
                chunk_size=chunk_size,
                start_offset=offset,
                state_path=state_path,
                observer=observer,
                cancel_event=cancel_event,
            )
            event_queue.put({"type": "job_finished", "success": result})
        except Exception as exc:
            event_queue.put({"type": "job_error", "error": str(exc)})

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    start = time.monotonic()

    last_event = None
    while t.is_alive():
        try:
            msg = event_queue.get(timeout=1)
            etype = msg.get("event") or msg.get("type", "")
            if etype != last_event:
                if etype == "verify_progress":
                    pass  # too noisy
                elif etype == "verify_chunk_verified":
                    pass  # too noisy
                else:
                    print(f"  [{etime}] {msg}")
                last_event = etype
            if etype == "job_error":
                errors.append(msg["error"])
                print(f"  ERROR: {msg['error']}")
        except queue.Empty:
            pass

    # Drain remaining
    while True:
        try:
            msg = event_queue.get_nowait()
            if msg.get("type") == "job_error":
                errors.append(msg["error"])
            print(f"  Final: {msg}")
        except queue.Empty:
            break

    t.join(timeout=5)
    elapsed = time.monotonic() - start

    if errors:
        print(f"\nFAILED with {len(errors)} error(s) after {elapsed:.1f}s:")
        for e in errors:
            print(f"  {e}")
        return False
    else:
        print(f"\nPASSED in {elapsed:.1f}s")
        return True


if __name__ == "__main__":
    drive = sys.argv[1] if len(sys.argv) > 1 else "E:"
    iso_path = sys.argv[2] if len(sys.argv) > 2 else "D:/Workspace/bd_iso/output/img.iso"

    print("=" * 60)
    print("Testing path resolution")
    print("=" * 60)
    test_resolve_source_path()
    test_normalize_bd_path()

    print("=" * 60)
    print("Testing verify-from-here (chunk 0)")
    print("=" * 60)
    ok = test_verify_from_here(drive, iso_path)
    sys.exit(0 if ok else 1)
