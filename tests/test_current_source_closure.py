"""Static source identity must not omit missing local imports or run source."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trade_system.migration_boundary import collection_source_files
from trade_system import pipeline_runtime


def write(root, name, text):
    path=root/name
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(text,encoding='utf-8')
    return path


def entry(root, text):
    write(root,'scripts/run_integrated_daily.py',text)


@pytest.mark.parametrize('statement',[
    'import trade_system.missing',
    'from trade_system.missing import value',
    'from trade_system import missing',
    'import collectors.missing',
    'from tools.v2.missing import value',
    'from scripts import missing',
    "import importlib; importlib.import_module('trade_system.missing')",
    'import base', 'from base import logger', 'import config', 'from fetch_all import main',
])
def test_missing_local_module_is_not_skipped(tmp_path,statement):
    entry(tmp_path,statement+'\n')
    write(tmp_path,'trade_system/__init__.py','')
    with pytest.raises(ValueError,match='local collector'):
        collection_source_files(tmp_path,strict_local_imports=True)
    # Existing callers opt in to the stricter identity contract explicitly.
    assert tmp_path/'scripts/run_integrated_daily.py' in collection_source_files(tmp_path)


@pytest.mark.parametrize('package_export',[
    'missing = 7',
    'missing: int = 7',
    'first, missing = (3, 7)',
    'def missing():\n    return 7',
    'class missing:\n    pass',
    'from datetime import date as missing',
    'import json as missing',
])
def test_explicit_package_attribute_is_not_mistaken_for_a_missing_module(tmp_path,package_export):
    entry(tmp_path,'from trade_system import missing\n')
    initializer=write(tmp_path,'trade_system/__init__.py',package_export+'\n')
    assert initializer in collection_source_files(tmp_path,strict_local_imports=True)


@pytest.mark.parametrize('package_export',[
    "__all__=['missing']", 'missing: int',
    'def __getattr__(name):\n    return 7',
    'if False:\n    missing=7',
    'missing=7\ndel missing',
])
def test_ambiguous_or_declaration_only_attribute_does_not_claim_complete_identity(tmp_path,package_export):
    entry(tmp_path,'from trade_system import missing\n')
    write(tmp_path,'trade_system/__init__.py',package_export+'\n')
    with pytest.raises(ValueError,match='unresolved local collector attribute'):
        collection_source_files(tmp_path,strict_local_imports=True)


def test_relative_imports_and_namespace_packages_are_closed_without_execution(tmp_path):
    entry(tmp_path,'from tools.v2 import consumer\nimport scripts.helper\n')
    consumer=write(tmp_path,'tools/v2/consumer.py','from .helper import VALUE\nfrom .. import exported\n')
    helper=write(tmp_path,'tools/v2/helper.py','VALUE=7\n')
    exported=write(tmp_path,'tools/exported.py',"raise AssertionError('source must never execute')\n")
    script=write(tmp_path,'scripts/helper.py','import os\nimport made_up_third_party_package\n')
    paths=collection_source_files(tmp_path,strict_local_imports=True)
    assert {consumer,helper,exported,script} <= paths
    helper.unlink()
    with pytest.raises(ValueError,match='missing local collector import'):
        collection_source_files(tmp_path,strict_local_imports=True)


def test_relative_attribute_must_be_bound_even_when_base_exists(tmp_path):
    entry(tmp_path,'import trade_system.consumer\n')
    write(tmp_path,'trade_system/__init__.py','KNOWN=1\n')
    write(tmp_path,'trade_system/consumer.py','from . import absent\n')
    with pytest.raises(ValueError,match='unresolved local collector attribute'):
        collection_source_files(tmp_path,strict_local_imports=True)


def test_standard_library_and_external_dependency_names_are_not_local_missing_files(tmp_path):
    entry(tmp_path,'import os\nfrom pathlib import Path\nimport pandas\nfrom external_package import Missing\n')
    assert collection_source_files(tmp_path,strict_local_imports=True)=={tmp_path/'scripts/run_integrated_daily.py'}


@pytest.mark.parametrize('statement',[
    "subprocess.run([sys.executable,'scripts/missing.py'])",
    "plan=[('step',[py,'scripts/missing.py'],False)]",
    "command=[python,'-I','-B','collectors/missing.py']",
    "subprocess.Popen(['python.exe','tools/v2/missing.py'])",
    "command=[str(python_path),'scripts/missing.py']",
    "subprocess.run([selected_runtime,'scripts/missing.py'])",
    "import subprocess as sp\nsp.run([selected_runtime,'scripts/missing.py'])",
    "from subprocess import Popen as child\nchild(args=[selected_runtime,'scripts/missing.py'])",
    "subprocess.run(['scripts/missing.py'])",
    "subprocess.run('python scripts/missing.py')",
])
def test_missing_literal_python_command_cannot_disappear_from_closure(tmp_path,statement):
    entry(tmp_path,statement+'\n')
    with pytest.raises(ValueError,match='missing local collector command source'):
        collection_source_files(tmp_path,strict_local_imports=True)


def test_command_input_is_bound_but_documentation_is_not_a_command(tmp_path):
    entry(tmp_path,'"""scripts/missing.py is retired."""\n'
          "note='scripts/another_missing.py'\n"
          "plan=[('step',[py,'scripts/actual.py'],False)]\n")
    actual=write(tmp_path,'scripts/actual.py',"raise AssertionError('never execute')\n")
    assert actual in collection_source_files(tmp_path,strict_local_imports=True)


def test_python_module_command_requires_known_local_module_but_allows_pip(tmp_path):
    entry(tmp_path,"commands=[[py,'-m','pip','check'],[py,'-m','tools.v2.missing']]\n")
    with pytest.raises(ValueError,match='missing local collector import'):
        collection_source_files(tmp_path,strict_local_imports=True)
    module=write(tmp_path,'tools/v2/missing.py','# never execute\n')
    assert module in collection_source_files(tmp_path,strict_local_imports=True)


def test_dynamic_local_fromlist_does_not_claim_closed_imports(tmp_path):
    entry(tmp_path,"__import__('trade_system',fromlist=['absent'])\n")
    write(tmp_path,'trade_system/__init__.py','')
    with pytest.raises(ValueError,match='dynamic local collector import arguments'):
        collection_source_files(tmp_path,strict_local_imports=True)


def test_initialized_package_wins_over_same_named_module(tmp_path):
    entry(tmp_path,'from trade_system import VALUE\n')
    package=write(tmp_path,'trade_system/__init__.py','VALUE=7\n')
    shadow=write(tmp_path,'trade_system.py','import trade_system.missing\n')
    paths=collection_source_files(tmp_path,strict_local_imports=True)
    assert package in paths and shadow not in paths


def test_plain_module_shadowing_namespace_is_not_a_valid_parent_package(tmp_path):
    entry(tmp_path,'import trade_system.consumer\n')
    write(tmp_path,'trade_system.py','VALUE=7\n')
    write(tmp_path,'trade_system/consumer.py','VALUE=7\n')
    with pytest.raises(ValueError,match='package shadowed'):
        collection_source_files(tmp_path,strict_local_imports=True)


def test_active_entrypoint_cannot_be_replaced_by_inert_setup_metadata(tmp_path):
    setup=write(tmp_path,'setup.py','# packaging only\n')
    assert setup in collection_source_files(tmp_path)
    with pytest.raises(ValueError,match='canonical collector entry missing'):
        collection_source_files(tmp_path,strict_local_imports=True)


@pytest.mark.parametrize('statement',[
    'from trade_system import *',
    'from ..trade_system import thing',
])
def test_static_local_ambiguity_refuses_completeness(tmp_path,statement):
    entry(tmp_path,statement+'\n')
    write(tmp_path,'trade_system/__init__.py','thing=1\n')
    with pytest.raises(ValueError,match='(wildcard|escapes)'):
        collection_source_files(tmp_path,strict_local_imports=True)


@pytest.mark.parametrize('missing',['consumer','entrypoint','root_shim','command'])
def test_sealed_runtime_refuses_member_and_manifest_omission_together(tmp_path,monkeypatch,missing):
    contents={
        'scripts/run_integrated_daily.py':'import trade_system.consumer\n',
        'trade_system/config.py':'# config\n','trade_system/schema.py':'# schema\n',
        'trade_system/consumer.py':'# consumer\n','setup.py':'# package metadata\n',
        'pyproject.toml':'# project\n',
    }
    if missing=='root_shim':
        contents['scripts/run_integrated_daily.py']='import base\n'
        contents['base.py']='# old shim\n'
    elif missing=='command':
        contents['scripts/run_integrated_daily.py']="plan=[(name,[python,'scripts/consumer.py'],False)]\n"
        contents['scripts/consumer.py']='# command\n'
    for name,text in contents.items():write(tmp_path,name,text)
    removed={'consumer':'trade_system/consumer.py','entrypoint':'scripts/run_integrated_daily.py',
             'root_shim':'base.py','command':'scripts/consumer.py'}[missing]
    (tmp_path/removed).unlink()
    del contents[removed]
    manifest={'schema':'source_distribution_v1','scope':'source_identity_only_not_deployment_authority',
              'source_commit':'a'*40,'files':{name:hashlib.sha256((tmp_path/name).read_bytes()).hexdigest() for name in contents}}
    write(tmp_path,'source-distribution.json',json.dumps(manifest))
    monkeypatch.setattr(pipeline_runtime,'PROJECT_ROOT',tmp_path)
    monkeypatch.setattr(pipeline_runtime.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=128,stdout='',stderr='no Git'))
    result=pipeline_runtime.runtime_fingerprint()
    assert not result['fingerprint_complete']
    assert 'collector_dependency_closure_unavailable' in result['fingerprint_errors']


def test_namespace_reparse_point_is_rejected_without_path_is_junction(tmp_path,monkeypatch):
    entry(tmp_path,'import tools\n')
    namespace=tmp_path/'tools';namespace.mkdir()
    original=Path.lstat
    monkeypatch.delattr(Path,'is_junction',raising=False)
    def reparse(path):
        value=original(path)
        return SimpleNamespace(st_mode=value.st_mode,st_file_attributes=0x400) if path==namespace else value
    monkeypatch.setattr(Path,'lstat',reparse)
    import stat
    monkeypatch.setattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400,raising=False)
    with pytest.raises(ValueError,match='linked local collector import'):
        collection_source_files(tmp_path,strict_local_imports=True)
