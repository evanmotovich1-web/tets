"""Wheel targeting and native minimum-version parsing for Intel Monterey."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import mac_compat


class MacCompatibilityTests(unittest.TestCase):
    def test_intel_wheels_target_monterey_and_repair_same_version_incompatible_wheels(self):
        with patch.object(mac_compat.platform,'system',return_value='Darwin'), \
             patch.object(mac_compat.platform,'machine',return_value='x86_64'),patch.dict(os.environ,{},clear=True):
            args=mac_compat.wheel_args()
            self.assertEqual(args,['--python-platform','x86_64-apple-darwin','--reinstall'])
            self.assertEqual(os.environ['MACOSX_DEPLOYMENT_TARGET'],'12.0')

    def test_arm_and_linux_do_not_install_intel_wheels(self):
        for system,arch in [('Darwin','arm64'),('Linux','x86_64')]:
            with patch.object(mac_compat.platform,'system',return_value=system),patch.object(mac_compat.platform,'machine',return_value=arch):
                self.assertEqual(mac_compat.wheel_args(),[])

    def test_both_native_macos_load_command_forms_are_read_without_sdk_confusion(self):
        commands='Load command 1\ncmd LC_BUILD_VERSION\nplatform 1\nminos 14.0\nsdk 26.1\nLoad command 2\ncmd LC_VERSION_MIN_MACOSX\nversion 10.15\nsdk 15.0'
        self.assertEqual(mac_compat.minimum_versions(commands),['14.0','10.15'])

    def test_pure_python_macho_parser_reads_minos_without_otool(self):
        import struct, tempfile
        header = struct.pack('<IIIIIIII', 0xfeedfacf, 0x01000007, 3, 2, 2, 40, 0, 0)
        cmd1 = struct.pack('<IIIIII', 0x32, 24, 1, (12 << 16) | (0 << 8) | 0, (14 << 16), 0)
        cmd2 = struct.pack('<IIII', 0x24, 16, (10 << 16) | (15 << 8) | 0, (12 << 16))
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / 'sample.so'
            binary.write_bytes(header + cmd1 + cmd2)
            self.assertEqual(mac_compat.macho_minimum_versions(binary), ['12.0', '10.15'])


if __name__=='__main__':unittest.main()
