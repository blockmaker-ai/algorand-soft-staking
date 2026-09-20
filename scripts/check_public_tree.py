"""Check publishable files for credentials and accidental operator artifacts.

This is a supplementary guard, not a replacement for reviewing a public diff.
It prints file names/rule names only, never the matched content.
"""
from pathlib import Path
import re
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
paths=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=root).decode().split('\0')
rules={
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'GitHub credential': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'JWT credential': re.compile(r'\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b'),
    'deployed Supabase project': re.compile(r'https://[a-z0-9]{20}\.supabase\.co'),
    'personal filesystem path': re.compile(r'/(?:Users|Volumes)/[^\s"\']+'),
}
failures=[]
for name in sorted(set(paths)):
    if not name: continue
    path=root/name
    if not path.is_file(): continue
    if (path.name.startswith('.env') and name!='.env.example') or ('evidence' in path.parts and path.suffix in ('.json','.gz')):
        failures.append((name,'operator file'))
    try: text=path.read_text()
    except UnicodeDecodeError: continue
    for label,pattern in rules.items():
        if pattern.search(text): failures.append((name,label))
for name,label in failures:
    print(f'{name}: {label}',file=sys.stderr)
if failures: raise SystemExit(1)
print('Public source guard passed: no matching credentials or operator artifacts.')
