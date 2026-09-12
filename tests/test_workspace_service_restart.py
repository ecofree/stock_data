"""Local lifecycle only: stale metadata never establishes port ownership."""
import json
import socket
from pathlib import Path

import pytest

from trade_system.file_lock import FileLock, FileLockBusy
from trade_system.v2 import research_product_server as server


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def test_stale_control_replaced_only_by_owner_and_restart_works(tmp_path,monkeypatch):
    port=free_port();control=tmp_path/f'service-{port}.json'
    control.write_text('{"csrf":"stale"}',encoding='utf-8')
    tokens=[]
    def run(instance):
        value=json.loads(control.read_text(encoding='utf-8'))
        assert value['port']==port and value['csrf']!='stale'
        tokens.append(value['csrf'])
        with pytest.raises(FileLockBusy):
            with FileLock(tmp_path/f'service-{port}.guard'): pass
    monkeypatch.setattr(server.ThreadingHTTPServer,'serve_forever',run)
    for _ in range(2):
        server.serve(Path.cwd(),tmp_path,port)
        assert not control.exists()
        assert (tmp_path/f'service-{port}.guard').exists()
    assert tokens[0]!=tokens[1]


def test_occupied_port_leaves_control_untouched(tmp_path):
    with socket.socket() as sock:
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        sock.bind(('127.0.0.1',0));sock.listen()
        port=sock.getsockname()[1];control=tmp_path/f'service-{port}.json'
        control.write_text('original',encoding='utf-8')
        with pytest.raises(OSError):server.serve(Path.cwd(),tmp_path,port)
        assert control.read_text(encoding='utf-8')=='original'


def test_failed_control_publication_releases_port_and_guard(tmp_path,monkeypatch):
    port=free_port();control=tmp_path/f'service-{port}.json'
    control.write_text('original',encoding='utf-8')
    def fail(*args):raise OSError('synthetic disk failure')
    monkeypatch.setattr(server.product,'write_pointer',fail)
    with pytest.raises(OSError,match='synthetic'):
        server.serve(Path.cwd(),tmp_path,port)
    assert control.read_text(encoding='utf-8')=='original'
    with FileLock(tmp_path/f'service-{port}.guard'):pass
    with socket.socket() as sock:sock.bind(('127.0.0.1',port))
