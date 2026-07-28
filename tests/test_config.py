import importlib
import sys


def reload_config(monkeypatch, tmp_path, env=None, dotenv_text="", dotenv_example_text=""):
    monkeypatch.chdir(tmp_path)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(dotenv_text, encoding="utf-8")
    if dotenv_example_text:
        (tmp_path / ".env.example").write_text(dotenv_example_text, encoding="utf-8")
    for key in ["KPL_API_KEY", "KPL_API_BASE", "KPL_DB_PATH", "KPL_REQUEST_DELAY", "KPL_ENV_FILE"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KPL_ENV_FILE", str(dotenv_path))
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("config", None)
    sys.path.insert(0, "D:/accio/stock_data")
    return importlib.import_module("config")


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
    assert cfg.API_BASE == "https://kpl.liuhepc.cn/api"


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
