"""Retain bounded, redacted diagnostics for every isolated recovery stage."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess


def diagnostic(value, env):
    text = value.decode('utf-8', errors='replace') if isinstance(value, bytes) else (value or '')
    for name, secret in env.items():
        if re.search(r'TOKEN|PASSWORD|PASSWD|SECRET|API_KEY|AUTHORIZATION', name, re.I) and len(secret) >= 4:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)([\"\']?(?:api[_-]?key|token|password|secret)[\"\']?\s*[:=]\s*[\"\']?)[^\s\"\',;&]+', r'\1[REDACTED]', text)
    text = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', text)
    return text if len(text) <= 4_000_000 else '[Earlier output truncated]\n' + text[-4_000_000:]


def run_stage(name, command, *, directory, env, cwd, timeout, runner=subprocess.run):
    if not re.fullmatch(r'[a-z][a-z0-9-]*', name): raise ValueError('bounded stage name required')
    started = datetime.now(timezone.utc).isoformat()
    record = {'stage':name,'started_at':started,'timeout_seconds':timeout,'execution_ready':False}

    def retain(stdout, stderr, status, returncode=None):
        record.update(status=status,returncode=returncode,finished_at=datetime.now(timezone.utc).isoformat())
        folder = Path(directory)
        with (folder/(name+'.log')).open('x', encoding='utf-8') as stream:
            stream.write(diagnostic(stdout,env)+'\n'+diagnostic(stderr,env))
        with (folder/(name+'.json')).open('x', encoding='utf-8') as stream:
            json.dump(record,stream,indent=2)

    try:
        result = runner(list(map(str,command)),env=env,cwd=cwd,capture_output=True,
                        text=True,encoding='utf-8',errors='replace',timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        retain(exc.stdout,exc.stderr,'timeout')
        raise
    except OSError as exc:
        retain('',str(exc),'start_failed')
        raise
    retain(result.stdout,result.stderr,'passed' if result.returncode == 0 else 'failed',result.returncode)
    if result.returncode: raise RuntimeError(name+' failed; evidence retained')
    return result.stdout
