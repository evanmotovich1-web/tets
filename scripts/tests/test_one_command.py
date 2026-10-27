"""One entrypoint, automatic memory profile, and public settings privacy."""
from pathlib import Path
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import runner
import onboarding


class OneCommandTests(unittest.TestCase):
    def test_same_install_command_selects_low_memory_and_respects_override(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'scripts').mkdir();(root/'bin').mkdir()
            (root/'install.sh').write_bytes((runner.ROOT/'install.sh').read_bytes())
            (root/'scripts/install').write_text('printf "%s\\n" "$TETS_PROFILE"\n')
            for name,body in [('uname','echo Darwin'),('sysctl','echo "$TEST_MEMORY"')]:
                path=root/'bin'/name;path.write_text('#!/bin/sh\n'+body+'\n');path.chmod(0o755)
            env={'PATH':str(root/'bin')+':/usr/bin:/bin','TEST_MEMORY':'8589934592'}
            def invoke(extra=None):
                return subprocess.run(['/bin/sh',str(root/'install.sh')],env={**env,**(extra or {})},capture_output=True,text=True,check=True).stdout.strip()
            self.assertEqual(invoke(),'mac2017')
            self.assertEqual(invoke({'TEST_MEMORY':'17179869184'}),'normal')
            self.assertEqual(invoke({'TETS_PROFILE':'normal'}),'normal')

    def test_status_includes_all_saved_setup_fields_without_model_endpoint(self):
        profile=onboarding.defaults();profile['ai'].update(provider='local',model='test-model',consent=False)
        profile['profit_target_usd']='250'
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with patch.object(runner,'STATE',root),patch.object(runner,'CONFIG',root/'config.json'), \
                 patch.object(runner,'ACTIVITY',root/'activity.json'),patch.object(onboarding,'load_profile',return_value=profile):
                data=runner.status()
            self.assertEqual(data['settings']['profit_target_usd'],'250')
            self.assertEqual(data['settings']['ai']['model'],'test-model')
            self.assertIn('liquidity',data['settings']);self.assertIn('capital_usd',data['settings'])
            self.assertNotIn('endpoint',data['settings']['ai'])
            self.assertIsNone(data['activity'])

    def test_default_launch_selects_a_free_port_without_disturbing_existing_listener(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1',0));occupied.listen(1)
            port=occupied.getsockname()[1]
            server=runner.open_server(port,allow_fallback=True)
            try:self.assertNotEqual(server.server_address[1],port)
            finally:server.server_close()
            with self.assertRaises(OSError):runner.open_server(port,allow_fallback=False)


if __name__=='__main__': unittest.main()
