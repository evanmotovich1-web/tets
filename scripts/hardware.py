"""Local hardware/runtime evidence. Never needs broker keys or submits orders."""
from datetime import datetime, timezone
import hashlib
import json
import platform
import resource
import subprocess
import sys
import threading
from urllib.request import urlopen
from mac_compat import native_audit


def command(args,timeout=30):
    try:
        result=subprocess.run(args,capture_output=True,text=True,timeout=timeout,check=True)
        return result.stdout.strip()
    except (OSError,subprocess.SubprocessError):return None


def machine_evidence():
    mac=platform.system()=='Darwin'
    translated=command(['sysctl','-in','sysctl.proc_translated'])=='1' if mac else False
    model=command(['sysctl','-n','hw.model'])if mac else platform.machine()
    memory=command(['sysctl','-n','hw.memsize'])if mac else None
    version=platform.mac_ver()[0]if mac else platform.release()
    native_intel=mac and platform.machine()=='x86_64' and not translated
    exact_2017=model in ('MacBook10,1','MacBookPro14,1','MacBookPro14,2','MacBookPro14,3')
    shared_air=model=='MacBookAir7,2'
    return dict(os=platform.system(),os_version=version,model=model,
        process_architecture=platform.machine(),rosetta_translated=translated,
        memory_bytes=int(memory)if memory and memory.isdigit()else None,
        cpu=command(['sysctl','-n','machdep.cpu.brand_string'])if mac else platform.processor(),
        native_intel_mac=native_intel,
        physical_2017_model_confirmed=bool(native_intel and exact_2017),
        shared_2015_2017_air_family=bool(native_intel and shared_air),
        macos_minimum_met=bool(mac and version and int(version.split('.')[0])>=12),
        note='MacBookAir7,2 identifies both 2015 and 2017 Air; confirm the year with the owner.'if shared_air else
             'Rosetta execution is not a physical Intel hardware or Monterey test.'if translated else None)


def verify(runner):
    hardware=machine_evidence()
    runtimes={}
    for label,python in [('research',runner.ROOT/'desk/backtest/.venv/bin/python'),
                         ('shadow',runner.ROOT/'desk/runtime/.state/shadow/.venv/bin/python'),
                         ('paper',runner.ROOT/'desk/runtime/.state/paper/.venv/bin/python')]:
        raw=command([str(python),'-c','import json,platform,sys;print(json.dumps(dict(architecture=platform.machine(),python=sys.version.split()[0])))'],timeout=120)
        if raw is None:raise ValueError(f'{label} Python runtime failed')
        runtimes[label]=json.loads(raw)
    raw=command(['node','-e','const v8=require("node:v8");console.log(JSON.stringify({architecture:process.arch,version:process.version,heap_limit_bytes:v8.getHeapStatistics().heap_size_limit}))'],timeout=120)
    if raw is None:raise ValueError('Node runtime failed')
    runtimes['node']=json.loads(raw)
    expected='x64'if hardware['process_architecture']=='x86_64'else'arm64'
    if any(r['architecture']!=hardware['process_architecture']for k,r in runtimes.items()if k!='node')or runtimes['node']['architecture']!=expected:
        raise ValueError('Mixed runtime architectures; reinstall from source on this Mac')
    native = native_audit(runner.ROOT)
    runner.shadow.save(runner.STATE/'native-compatibility.json',native)
    if native['state']=='failed':raise ValueError('A native dependency requires newer macOS; see local native-compatibility.json and rerun the installer')
    # Run on this machine's installed interpreter, including the scanner and
    # chart regressions. No dependency on a browser, account, or market hours.
    test_code='''import json,sys,unittest
result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover('scripts/tests'))
print('TETS_TEST_RESULT='+json.dumps(dict(run=result.testsRun,failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped))))
sys.exit(0 if result.wasSuccessful() else 1)
'''
    checks=subprocess.run([sys.executable,'-c',test_code],cwd=runner.ROOT,capture_output=True,text=True,timeout=180)
    runner.STATE.mkdir(parents=True,exist_ok=True)
    (runner.STATE/'hardware-tests.log').write_text(checks.stdout+'\n'+checks.stderr)
    if checks.returncode:raise ValueError('Installed regression checks failed; see local hardware-tests.log')
    counts=json.loads(next(line.split('=',1)[1] for line in checks.stdout.splitlines() if line.startswith('TETS_TEST_RESULT=')))
    runner.self_test()
    self_test=runner.read(runner.STATE/'self-test.json')
    server=runner.open_server(0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        base=f'http://127.0.0.1:{server.server_address[1]}'
        with urlopen(base,timeout=5)as r:web=r.status
        with urlopen(base+'/api/status',timeout=5)as r:
            api=r.status;data=json.load(r)
        if web!=200 or api!=200 or not isinstance(data.get('runner'),dict):raise ValueError('Status server verification failed')
    finally:server.shutdown();server.server_close();thread.join(timeout=5)
    locks=['desk/requirements.lock','desk/package-lock.json','scripts/paper-requirements.lock','scripts/shadow-requirements.lock']
    sources=['scripts/hardware.py','scripts/runner.py','scripts/bootstrap','scripts/env','scripts/install','scripts/shadow.py','scripts/paper.py',
             'scripts/status.html','scripts/company_data.py','scripts/replay_bridge.py','scripts/news.py','scripts/terminal_state.py',
             'scripts/mac_compat.py','install.sh','install-2017-mac.sh','tets']
    peak=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    report=dict(kind='tets_hardware_verification',checked_at=datetime.now(timezone.utc).isoformat(),
        hardware=hardware,runtimes=runtimes,
        software_checks_passed=True,regression_checks=counts,native_compatibility={k:v for k,v in native.items() if k!='binaries'},synthetic_backtest_state=self_test['state'],
        receipt_sha256=self_test['card']['receipt_sha256'],web_http=web,status_api_http=api,
        largest_child_peak_rss_bytes=int(peak if platform.system()=='Darwin'else peak*1024),
        memory_measurement='Largest child peak RSS; not combined process-tree peak or system free RAM',
        lock_sha256={p:hashlib.sha256((runner.ROOT/p).read_bytes()).hexdigest()for p in locks},
        source_sha256={p:hashlib.sha256((runner.ROOT/p).read_bytes()).hexdigest()for p in sources},
        broker_keys_required=False,broker_requests_made=False,orders_submitted=False)
    runner.shadow.save(runner.STATE/'hardware-verification.json',report)
    print(json.dumps(report,indent=2))
    return report
