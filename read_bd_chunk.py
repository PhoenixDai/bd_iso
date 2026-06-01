import sys
import os
from bd_utils import open_bd_drive

def read_chunk(drive, offset, size, output_file=None):
    if os.name == 'nt':
        drive_letter = drive.upper().strip(':').strip('\\').strip('/')
        bd_path = f'\\\\.\\{drive_letter}:'
    else:
        bd_path = drive

    print(f"Opening drive {bd_path}...")
    try:
        with open_bd_drive(bd_path) as f:
            print(f"Seeking to offset {offset} (0x{offset:X})...")
            f.seek(offset)
            
            print(f"Reading {size} bytes...")
            data = f.read(size)
            
            if not data:
                print("Error: Reached end of drive or no data read.")
                return
            
            print(f"Successfully read {len(data)} bytes.")
            
            if output_file:
                with open(output_file, 'wb') as out:
                    out.write(data)
                print(f"Saved chunk to {output_file}")
            else:
                print("\nData Hex Dump (first 256 bytes):")
                dump = data[:256]
                for i in range(0, len(dump), 16):
                    chunk = dump[i:i+16]
                    hex_str = ' '.join(f'{b:02X}' for b in chunk)
                    ascii_str = ''.join(chr(b) if 32 <= b <= 126 else '.' for b in chunk)
                    print(f"{i:04X}  {hex_str:<48}  {ascii_str}")
                
                if len(data) > 256:
                    print(f"... ({len(data) - 256} more bytes not shown)")
                
                return data
                
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    if len(sys.argv) < 4:
        print("Usage: python read_bd_chunk.py <Drive> <Offset> <Size> [Output_File]")
        print("Example (Windows): python read_bd_chunk.py E 1048576 4096 chunk.bin")
        print("Example (Linux): python read_bd_chunk.py /dev/sr0 0x100000 0x1000")
        print("Note: Offset and Size can be hex (0x...) or integer")
        sys.exit(1)
        
    drive_input = sys.argv[1]
    
    def parse_int(val):
        if val.lower().startswith('0x'):
            return int(val, 16)
        return int(val)
        
    try:
        offset_val = parse_int(sys.argv[2])
        size_val = parse_int(sys.argv[3])
    except ValueError:
        print("Error: Offset and Size must be integers or hex values (e.g., 1024 or 0x400).")
        sys.exit(1)
        
    out_file = sys.argv[4] if len(sys.argv) > 4 else None
    
    read_chunk(drive_input, offset_val, size_val, out_file)