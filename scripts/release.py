"""Source inventory for review/publication; never walk installed state."""
from pathlib import Path
import re

BLOCKED={'.git','.state','.venv','node_modules','__pycache__','.pytest_cache','dist'}
SECRET=re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bAKIA[A-Z0-9]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bsk-proj-[A-Za-z0-9_-]{40,}\b')


def allowed(name):
    p=Path(name)
    return not p.is_absolute() and not any(s in BLOCKED or s=='..' for s in p.parts) and p.name not in ('credentials.json','secrets.json','tokens.json') and '.env' not in p.name


def source_files(root):
    import os
    root=Path(root).resolve();result={}
    roots={'README.md','AGENTS.md','LICENSE','AGPL-NOTICE.md','.gitignore','SOURCE-PROVENANCE.json','install.sh','install-2017-mac.sh','tets'}
    paths=[root/n for n in roots]
    for folder in ('scripts','desk'):
        for current,dirs,names in os.walk(root/folder,followlinks=False):
            dirs[:]=[d for d in dirs if d not in BLOCKED and not d.startswith('.')]
            paths.extend(Path(current)/n for n in names if not n.startswith('.'))
    for path in paths:
        if not path.is_file(): continue
        name=path.relative_to(root).as_posix()
        if path.suffix not in ('','.md','.py','.mjs','.json','.jsonl','.sh','.lock','.in','.html') and name!='.gitignore':continue
        if not allowed(name) or path.is_symlink() or SECRET.search(path.read_bytes()):raise ValueError('Unsafe source file: '+name)
        result[name]=path.read_bytes()
    return result
