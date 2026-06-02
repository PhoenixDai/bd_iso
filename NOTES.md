Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process 

./.venv/Scripts/activate

ls /dev/ | grep -E "sr[0-9]|scd|cdrom|dvd"

ddrescue -n -b2048 /dev/cdrom diskimage.iso mapfile
ddrescue -d -r1 -b2048 /dev/cdrom diskimage.iso mapfile

python create_iso_from_bd.py /dev/cdrom diskimage.iso --verify
python create_iso_from_bd.py /dev/cdrom diskimage.iso --resume --verify
