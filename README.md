# bd_iso

Create, verify, and repair ISO images from Blu-ray / DVD / CD discs.

Cross-platform (Linux and Windows). Resilient to read errors with automatic retries and adaptive read-size reduction.

## Features

- **Resilient reads** — automatic retries with shrinking read size on failure
- **Resumable** — interrupted copies can continue from the last checkpoint
- **Verify** — bit-by-bit comparison of ISO against the source disc, with per-sector mismatch detail
- **Repair** — re-read mismatched chunks from the disc into an existing ISO
- **Tkinter GUI** — visual progress grid with per-chunk and per-sector inspection and retry
- **Cross-platform** — Linux (`/dev/sr0`) and Windows (`E:` / `\\.\E:`)
- **Auto-detect** — finds optical drives automatically on both platforms

## Requirements

- Python 3.10+
- Linux: `eject` command (for tray control), `udisksctl` or `mount` (for touch disc)
- Windows: no additional dependencies; MCI and Win32 APIs are used directly

## Quick start

```bash
# Copy a disc to ISO
python create_iso_from_bd.py /dev/cdrom disc.iso --verify

# Windows
python create_iso_from_bd.py \\.\E: disc.iso --verify

# Resume an interrupted copy
python create_iso_from_bd.py /dev/cdrom disc.iso --resume --verify

# Verify an existing ISO against the disc
python compare_bd_iso.py /dev/cdrom disc.iso
python compare_bd_iso.py E: disc.iso          # Windows

# Repair mismatched chunks
python repair_iso_from_bd.py /dev/cdrom disc.iso disc.iso.txt

# Launch the GUI
python bd_iso_ui.py
```

## CLI tools

| Script | Purpose |
|---|---|
| `create_iso_from_bd.py` | Copy a disc to an ISO file |
| `compare_bd_iso.py` | Bit-by-bit verification of ISO against disc |
| `repair_iso_from_bd.py` | Re-read mismatched chunks from disc into ISO |
| `read_bd_chunk.py` | Read and hex-dump a raw chunk from a disc |
| `bd_iso_ui.py` | Tkinter GUI with progress grid and per-sector controls |

## How it works

The copy engine reads the source disc in 4 MiB chunks. When a chunk fails, it splits into progressively smaller reads (down to 2 KiB — one optical sector) to isolate unreadable regions. Bad regions are logged and can be retried later.

Progress is saved to sidecar files:
- `.copy.txt` — copy checkpoint and bad-region log
- `.txt` — verify checkpoint and mismatch log
- `.state.json` — structured state used by the GUI

## GUI

```
python bd_iso_ui.py
```

- **Grid view** — each cell is a 4 MiB chunk, color-coded by status
- **Per-chunk window** — click any cell to see per-sector detail, retry individual sectors
- **Buttons** — Start Copy, Resume, Restart, Verify From Here, Refresh, Stop, Eject, Close Tray, Touch Disc
- **Auto-detect** — Find Drive button scans for optical drives

## Options

### create_iso_from_bd.py

| Option | Description |
|---|---|
| `--resume` | Continue from the last checkpoint |
| `--restart` | Overwrite the ISO and start fresh |
| `--chunk-size N` | Bytes per read chunk (default: 4 MiB) |
| `--retries N` | Retries per failing read before splitting (default: 3) |
| `--retry-delay N` | Seconds between retries (default: 1.0) |
| `--min-read-size N` | Smallest fallback read size (default: 2048) |
| `--verify` | Run verification after copy completes |
