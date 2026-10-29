"""Choose Monterey-compatible wheels even when verifying Intel on a newer Mac."""
import os
import platform
from pathlib import Path
import re
import struct
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


def _format_packed_version(packed):
    major = (packed >> 16) & 0xffff
    minor = (packed >> 8) & 0xff
    patch = packed & 0xff
    return f'{major}.{minor}' if patch == 0 else f'{major}.{minor}.{patch}'


def macho_minimum_versions(path, target_cputype=0x01000007):
    """Pure-Python Mach-O load-command fallback when /usr/bin/otool (Xcode CLT) is unavailable."""
    try:
        with Path(path).open('rb') as handle:
            head = handle.read(65536)
            if len(head) < 32:
                return []
            slice_data = head
            magic_be = struct.unpack('>I', head[:4])[0]
            if magic_be == 0xcafebabe:
                nfat = struct.unpack('>I', head[4:8])[0]
                for idx in range(min(nfat, 16)):
                    base = 8 + idx * 20
                    if base + 20 > len(head):
                        break
                    cputype, _sub, offset, size, _align = struct.unpack('>IIIII', head[base:base + 20])
                    if cputype == target_cputype:
                        handle.seek(offset)
                        slice_data = handle.read(min(size, 65536))
                        break
            if len(slice_data) < 32 or struct.unpack('<I', slice_data[:4])[0] != 0xfeedfacf:
                return []
            _magic, _cpu, _sub, _ft, ncmds, _sz, _fl, _res = struct.unpack('<IIIIIIII', slice_data[:32])
            pos = 32
            versions = []
            for _ in range(min(ncmds, 256)):
                if pos + 8 > len(slice_data):
                    break
                cmd, cmdsize = struct.unpack('<II', slice_data[pos:pos + 8])
                if cmdsize < 8:
                    break
                if cmd in (0x32, 0x80000032) and pos + 24 <= len(slice_data):
                    _plat, minos, _sdk, _ntools = struct.unpack('<IIII', slice_data[pos + 8:pos + 24])
                    versions.append(_format_packed_version(minos))
                elif cmd == 0x24 and pos + 16 <= len(slice_data):
                    ver, _sdk = struct.unpack('<II', slice_data[pos + 8:pos + 16])
                    versions.append(_format_packed_version(ver))
                pos += cmdsize
            return versions
    except OSError:
        return []


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
        versions=minimum_versions(result.stdout) if result.returncode == 0 else []
        if not versions:
            versions=macho_minimum_versions(path)
        record=dict(path=str(path),minimum_macos=versions);records.append(record)
        def newer(version):
            parts=list(map(int,version.split('.')))
            return tuple((parts+[0,0,0])[:3])>(12,0,0)
        if not versions or any(newer(version) for version in versions):failures.append(record)
    report=dict(state='passed' if not failures else 'failed',target_macos='12.0',unique_native_files=len(records),
                incompatible_or_unmeasured=failures,binaries=records,physical_monterey_execution=False)
    return report
