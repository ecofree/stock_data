import importlib
import sys
from pathlib import Path


def reload_config(monkeypatch, tmp_path, env=None, dotenv_text="", dotenv_example_text=""):
    monkeypatch.chdir(tmp_path)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(dotenv_text, encoding="utf-8")
    if dotenv_example_text:
        (tmp_path / ".env.example").write_text(dotenv_example_text, encoding="utf-8")
    for key in [
        "KPL_API_KEY", "KPL_API_BASE", "KPL_DB_PATH", "KPL_REQUEST_DELAY", "KPL_ENV_FILE",
        "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    ]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KPL_ENV_FILE", str(dotenv_path))
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    # Re-import the package config against the test environment.
    sys.modules.pop("trade_system.config", None)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    return importlib.import_module("trade_system.config")


def test_config_prefers_environment_over_dotenv(monkeypatch, tmp_path):
    cfg = reload_config(
        monkeypatch,
        tmp_path,
        env={"KPL_API_KEY": "env-key", "KPL_API_BASE": "https://example.test/api"},
        dotenv_text="KPL_API_KEY=dotenv-key\nKPL_API_BASE=https://dotenv.test/api\n",
    )
    assert cfg.API_KEY == "env-key"
    assert cfg.API_BASE == "https://example.test/api"


def test_config_reads_dotenv_when_environment_missing(monkeypatch, tmp_path):
    cfg = reload_config(
        monkeypatch,
        tmp_path,
        dotenv_text="KPL_API_KEY=dotenv-key\nKPL_REQUEST_DELAY=0.15\n",
    )
    assert cfg.API_KEY == "dotenv-key"
    assert cfg.REQUEST_DELAY == 0.15


def test_config_does_not_read_env_example_as_active_config(monkeypatch, tmp_path):
    cfg = reload_config(
        monkeypatch,
        tmp_path,
        dotenv_example_text="KPL_API_KEY=example-key\nKPL_API_BASE=https://example-fallback.test/api\n",
    )
    assert cfg.API_KEY == ""
    assert cfg.API_BASE == "https://www.kpl-api.cn/api"


def test_config_prefers_dotenv_over_env_example(monkeypatch, tmp_path):
    cfg = reload_config(
        monkeypatch,
        tmp_path,
        dotenv_text="KPL_API_KEY=dotenv-key\n",
        dotenv_example_text="KPL_API_KEY=example-key\n",
    )
    assert cfg.API_KEY == "dotenv-key"


def test_config_rejects_placeholder_api_key(monkeypatch, tmp_path):
    cfg = reload_config(
        monkeypatch,
        tmp_path,
        dotenv_example_text="KPL_API_KEY=replace-with-local-key\n",
    )
    assert cfg.API_KEY == ""


def test_config_reads_deepseek_standard_key_and_legacy_alias(monkeypatch, tmp_path):
    cfg = reload_config(
        monkeypatch,
        tmp_path,
        dotenv_text="deepseek-v4-flash=legacy-token\nDEEPSEEK_MODEL=deepseek-v4-flash\n",
    )
    assert cfg.DEEPSEEK_API_KEY == "legacy-token"
    assert cfg.DEEPSEEK_MODEL == "deepseek-v4-flash"


def test_explicit_runtime_root_routes_all_mutable_state_outside_release(tmp_path, monkeypatch):
    import json
    import os
    import subprocess
    root = tmp_path/'runtime'; root.mkdir()
    monkeypatch.setenv('STOCKDATA_RUNTIME_ROOT', str(root))
    code = '''import json
from trade_system import config
from trade_system.host_limiter import SharedHostLimiter
from trade_system.eastmoney_clist_guard import PRIMARY_CLIST_GUARD, DELAY_CLIST_GUARD, TRENDS_GUARD
from trade_system import resilient_sources
print(json.dumps(dict(db=config.DB_PATH, logs=config.LOG_DIR, spill=config.DUCKDB_TEMP_DIR,
    shared=str(SharedHostLimiter().db_path), cache=resilient_sources.DB_PATH,
    guards=[str(g.path) for g in (PRIMARY_CLIST_GUARD,DELAY_CLIST_GUARD,TRENDS_GUARD)])))
'''
    env = {k:v for k,v in os.environ.items() if not k.startswith(('KPL_', 'EASTMONEY_'))}
    result = subprocess.run([sys.executable, '-B', '-c', code],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.splitlines()[-1])
    assert Path(data['db']) == root/'market/kpl_data.duckdb'
    assert Path(data['logs']) == root/'logs'
    assert Path(data['spill']) == root/'tmp/duckdb_spill'
    assert Path(data['shared']) == Path(data['cache']) == root/'state/acquisition/resilient.db'
    assert all(Path(p).parent == root/'state/acquisition' for p in data['guards'])


def test_runtime_root_never_implicitly_loads_development_credentials(tmp_path, monkeypatch):
    import os
    import subprocess
    (tmp_path/'.env').write_text('KPL_API_KEY=synthetic-do-not-read\n', encoding='utf-8')
    root = tmp_path/'runtime'; root.mkdir()
    monkeypatch.setenv('STOCKDATA_RUNTIME_ROOT', str(root))
    env = {k:v for k,v in os.environ.items() if not k.startswith(('KPL_', 'DEEPSEEK_'))}
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, '-B', '-c',
        "from trade_system.config import API_KEY; assert API_KEY == ''"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_invalid_runtime_root_is_rejected_before_any_state_creation(tmp_path, monkeypatch):
    import os
    import subprocess
    source = Path(__file__).resolve().parents[1]
    for bad in ('relative-runtime', str(source)):
        env = dict(os.environ, STOCKDATA_RUNTIME_ROOT=bad)
        result = subprocess.run([sys.executable, '-B', '-c', 'import trade_system.config'],
            cwd=source, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode != 0 and 'runtime root' in result.stderr.lower()
    assert not (source/'relative-runtime').exists()
