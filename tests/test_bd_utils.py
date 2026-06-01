import os
import tempfile
import unittest
from unittest.mock import patch

from bd_utils import mount_source_device, resolve_source_path, run_eject_command, touch_disc


class BdUtilsTests(unittest.TestCase):
    @patch("bd_utils.get_block_device_size")
    @patch("bd_utils.list_optical_drive_candidates")
    @patch("bd_utils.os.path.exists")
    def test_resolve_source_path_auto_detects_reconnected_drive(
        self,
        mock_exists,
        mock_candidates,
        mock_get_size,
    ):
        mock_candidates.return_value = ["/dev/sr1"]

        def fake_exists(path):
            return path == "/dev/sr1"

        def fake_get_size(path):
            if path == "/dev/sr1":
                return 1234
            raise OSError(path)

        mock_exists.side_effect = fake_exists
        mock_get_size.side_effect = fake_get_size

        resolved = resolve_source_path("/dev/sr0", expected_size=1234)
        self.assertEqual(resolved, "/dev/sr1")

    @patch("bd_utils.subprocess.run")
    @patch("bd_utils.resolve_source_path", return_value="/dev/sr1")
    def test_run_eject_command_builds_close_tray_command(self, _mock_resolve, mock_run):
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        run_eject_command("/dev/sr0", close_tray=True)

        mock_run.assert_called_once_with(
            ["eject", "-t", "/dev/sr1"],
            check=True,
            capture_output=True,
            text=True,
        )

    @patch("bd_utils.resolve_source_path")
    @patch("bd_utils.find_mount_points_for_source")
    def test_touch_disc_reads_file_from_mounted_disc(self, mock_mounts, mock_resolve):
        class FirstChoice:
            def choice(self, values):
                return values[0]

        with tempfile.TemporaryDirectory() as tempdir:
            file_path = os.path.join(tempdir, "small.txt")
            with open(file_path, "wb") as handle:
                handle.write(b"hello")

            mock_resolve.return_value = "/dev/sr1"
            mock_mounts.return_value = [tempdir]

            message = touch_disc("/dev/sr1", rng=FirstChoice())

        self.assertIn("Read 5 bytes", message)
        self.assertIn("small.txt", message)

    @patch("bd_utils.mount_source_device", side_effect=OSError("mount failed"))
    @patch("bd_utils.resolve_source_path", return_value="/dev/sr1")
    @patch("bd_utils.find_mount_points_for_source", return_value=[])
    def test_touch_disc_reports_mount_failure(
        self,
        _mock_mounts,
        _mock_resolve,
        mock_mount,
    ):
        with self.assertRaisesRegex(OSError, "mount failed"):
            touch_disc("/dev/sr1")
        mock_mount.assert_called_once_with("/dev/sr1")

    @patch("bd_utils.mount_source_device")
    @patch("bd_utils.resolve_source_path", return_value="/dev/sr1")
    @patch("bd_utils.find_mount_points_for_source", return_value=[])
    def test_touch_disc_mounts_then_reads_file(
        self,
        _mock_mounts,
        _mock_resolve,
        mock_mount,
    ):
        class FirstChoice:
            def choice(self, values):
                return values[0]

        with tempfile.TemporaryDirectory() as tempdir:
            file_path = os.path.join(tempdir, "after_mount.txt")
            with open(file_path, "wb") as handle:
                handle.write(b"mounted")

            mock_mount.return_value = [tempdir]
            message = touch_disc("/dev/sr1", rng=FirstChoice())

        self.assertIn("Read 7 bytes", message)
        self.assertIn("after_mount.txt", message)

    @patch("bd_utils.subprocess.run")
    @patch("bd_utils.find_mount_points_for_source", return_value=["/media/movie"])
    def test_mount_source_device_tries_udisksctl(self, mock_mounts, mock_run):
        mount_points = mount_source_device("/dev/sr1")

        self.assertEqual(mount_points, ["/media/movie"])
        mock_run.assert_called_once_with(
            ["udisksctl", "mount", "-b", "/dev/sr1"],
            check=True,
            capture_output=True,
            text=True,
        )
        mock_mounts.assert_called_once_with("/dev/sr1")


if __name__ == "__main__":
    unittest.main()
