"""Choose Monterey-compatible wheels even when verifying Intel on a newer Mac."""
import os
import platform
from pathlib import Path
import re
import subprocess


def wheel_args():
    if platform.system() == 'Darwin' and platform.machine() == 'x86_64':
        os.environ['MACOSX_DEPLOYMENT_TARGET'] = '12.0'
        # Reinstall also repairs a same-version wheel previously selected for a
        # newer host OS; a version-only audit would leave that wheel in place.
        return ['--python-platform', 'x86_64-apple-darwin', '--reinstall']
    return []


def minimum_versions(load_commands):
    versions = []
    for block in load_commands.split('Load command'):
        if 'LC_BUILD_VERSION' in block:
            match = re.search(r'\bminos\s+([0-9.]+)', block)
        elif 'LC_VERSION_MIN_MACOSX' in block:
            match = re.search(r'\bversion\s+([0-9.]+)', block)
        else:
            continue
        if match: versions.append(match.group(1))
    return versions


def native_audit(root):
    if platform.system() != 'Darwin' or platform.machine() != 'x86_64':
        return dict(state='not_applicable_to_this_architecture', target_macos='12.0')
    root = Path(root); paths = set()
    environments = [root/'desk/backtest/.venv', root/'desk/runtime/.state/shadow/.venv', root/'desk/runtime/.state/paper/.venv']
    for environment in environments:
        python = (environment/'bin/python').resolve(); paths.add(python)
        for directory in [environment, python.parent.parent/'lib']:
            for suffix in ('*.so', '*.dylib'):
                paths.update(p.resolve() for p in directory.rglob(suffix) if p.is_file())
    for relative in ['desk/runtime/.state/tools/node/bin/node','desk/runtime/.state/tools/uv/uv']:
        path=root/relative
        if path.exists():paths.add(path.resolve())
    if len(paths)>2000:raise ValueError('Native compatibility audit exceeds file budget')
    records=[]; failures=[]
    for path in sorted(paths):
        result=subprocess.run(['/usr/bin/otool','-arch','x86_64','-l',str(path)],capture_output=True,text=True,timeout=15)
        versions=minimum_versions(result.stdout)
        record=dict(path=str(path),minimum_macos=versions);records.append(record)
        def newer(version):
            parts=list(map(int,version.split('.')))
            return tuple((parts+[0,0,0])[:3])>(12,0,0)
        if result.returncode or not versions or any(newer(version) for version in versions):failures.append(record)
    report=dict(state='passed' if not failures else 'failed',target_macos='12.0',unique_native_files=len(records),
                incompatible_or_unmeasured=failures,binaries=records,physical_monterey_execution=False)
    return report
