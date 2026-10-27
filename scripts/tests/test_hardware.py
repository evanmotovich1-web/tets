import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import hardware


class HardwareTests(unittest.TestCase):
    def evidence(self,model,translated,arch='x86_64'):
        values={'hw.model':model,'hw.memsize':'8589934592','machdep.cpu.brand_string':'Intel Core i5',
                'sysctl.proc_translated':'1'if translated else None}
        with patch.object(hardware.platform,'system',return_value='Darwin'), \
             patch.object(hardware.platform,'machine',return_value=arch), \
             patch.object(hardware.platform,'mac_ver',return_value=('12.7.6','','')), \
             patch.object(hardware,'command',side_effect=lambda args:values.get(args[-1])):
            return hardware.machine_evidence()

    def test_rosetta_never_counts_as_physical_2017_even_if_a_model_name_is_supplied(self):
        result=self.evidence('MacBookPro14,1',True)
        self.assertFalse(result['physical_2017_model_confirmed'])
        self.assertFalse(result['native_intel_mac'])
        self.assertTrue(result['rosetta_translated'])

    def test_native_2017_intel_pro_is_identified_with_real_ram_and_os_fields(self):
        result=self.evidence('MacBookPro14,2',False)
        self.assertTrue(result['physical_2017_model_confirmed'])
        self.assertTrue(result['macos_minimum_met'])
        self.assertEqual(result['memory_bytes'],8589934592)

    def test_shared_air_model_does_not_establish_exact_year(self):
        result=self.evidence('MacBookAir7,2',False)
        self.assertTrue(result['shared_2015_2017_air_family'])
        self.assertFalse(result['physical_2017_model_confirmed'])

    def test_apple_silicon_is_not_identified_as_native_intel(self):
        result=self.evidence('MacBookAir10,1',False,'arm64')
        self.assertFalse(result['native_intel_mac'])
        self.assertFalse(result['physical_2017_model_confirmed'])


if __name__=='__main__':unittest.main()
