"""Build one private portable candidate from explicit pinned frozen inputs.

No defaults point at production. Input plan and source export must be separately
pinned. Every output is fresh; failed attempts remain for inspection.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.v2.offline_package import safe, member, digest, seal_package, verify_package, write_new
from tools.v2.offline_home import read, validate_targets
from trade_system.v2 import recovery_bundle as recovery, capacity_acceptance as capacity
from trade_system.v2.domain import identity
from trade_system.v2.isolated_workspace import initialize, write_json
from trade_system.v2.offline_binding import verify_bindings, verify_observation_fixture
from tools.v2.verify_offline_recovery import verify as verify_business


def export_source(source, files, destination):
    source, destination = safe(source), safe(destination)
    if not files or destination.exists():
        raise ValueError('explicit source files and new export destination required')
    for name, sha in files.items():
        path = member(source,name)
        if ((path.name.startswith('.env') and path.name!='.env.example')
                or path.suffix.lower() in ('.pem','.key','.pfx','.duckdb','.sqlite','.zip','.whl')
                or digest(path)!=sha):
            raise ValueError('source export identity or scope differs')
    destination.mkdir(parents=True)
    for name in files:
        target = member(destination,name)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(member(source,name),target)
    if {name:digest(member(destination,name)) for name in files} != files:
        raise ValueError('source export changed during copy')


def pinned(record):
    path = safe(record['path'])
    if digest(path)!=record['sha256']:
        raise ValueError('explicit input pin differs')
    return path


def stage_workspace(source, destination):
    """Copy exactly the recovery owner's authority set, never portable caches."""
    source, destination = safe(source), safe(destination)
    if destination.exists() or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('new separate workspace staging required')
    files = recovery.inventory(source)
    if not files:
        raise ValueError('nonempty authority workspace required')
    destination.mkdir(parents=True)
    for name in files:
        target = member(destination,name)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(member(source,name),target)
    if recovery.inventory(destination)!=files or recovery.inventory(source)!=files:
        raise ValueError('workspace authority bytes changed during staging')
    return files


def home_scripts(package):
    (package/'RUN-RESTORE.ps1').write_text('''param(
 [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ManifestSha256,
 [Parameter(Mandatory=$true)][string]$NewDestination,
 [string]$IntakeStartedAt
)
$ErrorActionPreference='Stop'
$arguments=@('-I','-B',(Join-Path $PSScriptRoot 'code/tools/v2/offline_package.py'),'--package',$PSScriptRoot,'--manifest-sha256',$ManifestSha256,'--restore-to',$NewDestination)
if ($IntakeStartedAt) { $arguments += @('--intake-started-at',$IntakeStartedAt) }
& (Join-Path $PSScriptRoot 'bootstrap/python.exe') @arguments
if ($LASTEXITCODE -ne 0) { throw 'Recovery failed. Preserve this destination and logs; use a new destination for retry.' }
''',encoding='utf-8')
    (package/'RUN-HOME.ps1').write_text('''param(
 [Parameter(Mandatory=$true)][string]$ManifestSha256,
 [Parameter(Mandatory=$true)][string]$NewDestination,
 [Parameter(Mandatory=$true)][string]$IntakeStartedAt
)
$ErrorActionPreference='Stop'
$arguments=@('-I','-B',(Join-Path $PSScriptRoot 'code/tools/v2/offline_home.py'),'--run-package',$PSScriptRoot,'--manifest-sha256',$ManifestSha256,'--destination',$NewDestination,'--output',($NewDestination+'.home-attempt'),'--intake-started-at',$IntakeStartedAt)
& (Join-Path $PSScriptRoot 'bootstrap/python.exe') @arguments
if ($LASTEXITCODE -ne 0) { throw 'Home recovery failed. Preserve the attempt and destination directories.' }
''',encoding='utf-8')
    (package/'COLLECT-ACCEPTANCE.ps1').write_text('''param(
 [Parameter(Mandatory=$true)][string]$ManifestSha256,
 [Parameter(Mandatory=$true)][string]$Destination,
 [Parameter(Mandatory=$true)][string]$NewOutput,
 [string]$HumanConfirmation
)
$ErrorActionPreference='Stop'
$arguments=@('-I','-B',(Join-Path $PSScriptRoot 'code/tools/v2/offline_home.py'),'--manifest-sha256',$ManifestSha256,'--destination',$Destination,'--output',$NewOutput)
if ($HumanConfirmation) { $arguments += @('--human-confirmation',$HumanConfirmation) }
& (Join-Path $PSScriptRoot 'bootstrap/python.exe') @arguments
if ($LASTEXITCODE -ne 0) { throw 'Collection failed. Preserve evidence; never send databases or credentials.' }
''',encoding='utf-8')
    instructions='''StockData 私密离线恢复候选包 · 家庭电脑一页操作

范围：历史行情、原始人工记录、冻结研究材料和只读页面。SYNTHETIC页仅演示观察闭环。
不包含实时采集、退役训练/预测、独立QLib完整研究环境或真实账户；没有恢复运营授权。
候选支持 Windows 10/11 x64、NTFS本地固定盘。具体系统版本需实际运行确认；无需安装Python或联网。
本包含未脱敏历史库与旧人工原件，只通过自己的私有介质/通道传输，严禁公开上传。

1. 开始取回/复制之前，在PowerShell记录 $intake=[DateTimeOffset]::UtcNow.ToString('o')。
   将整个包复制到本机短路径，另经可信渠道核对本轮manifest SHA。pin不要只信同介质可改副本。
2. 空间：恢复目标盘额外剩余至少 包清单bytes×2.5 + 5GiB；同盘还须另容纳包本身与压缩件。
   另加migration verification临时预算（最多16GiB，实际数字见独立pin）。
   使用现有父目录下全新目标，例如 C:\\StockData-Recovered-01。失败目录保留，重试另选新名。
3. 断网后在包目录运行：
   .\\RUN-HOME.ps1 -ManifestSha256 <本轮独立SHA> -NewDestination C:\\StockData-Recovered-01 -IntakeStartedAt $intake
   不需要API Key。若系统执行策略阻止，保留错误并联系交付人，不永久修改系统策略。
4. 成功后打开目标 evidence/index.html、historical-market.html、workbench.html。
   核对中文、真实历史日期/资格缺口与SYNTHETIC区别、只读/C3不适用、研究材料可用。
   原始人工记录存放于 rehearsal/restores/offline/raw_facts/original-workspace；保持私密与原字节。
5. 先运行 .\\COLLECT-ACCEPTANCE.ps1 -ManifestSha256 <SHA> -Destination <目标> -NewOutput <全新回执目录>。
   其中human-checklist.json仅供填写：六项均由本人实际核对后改true，confirmed_at填确认时UTC ISO时间，
   operator_alias填1至48位英文数字/下划线/连字符代号。不要加入姓名、账号、路径或其他字段。
   再以新回执目录运行收集，追加 -HumanConfirmation <填好的human-checklist.json>。
6. 私下返回收集目录中的脱敏回执和限定日志，不发送整库/人工原件/凭据。
   4小时RTO从取回/复制开始至技术通过且人工确认可查看与研究材料可用，技术时间单独记录。
   RPO目标为最多1个交易日，不能换算成86400秒；暂停历史快照只能验证零文件/多重集差异。
   本机重跑和机器代号不同不等于离机通过，仍须核对真实家庭机/介质来源并由人签署。
'''
    (package/'HOME-STEPS.txt').write_text(instructions,encoding='utf-8')
    (package/'README-离机验收.txt').write_text(instructions,encoding='utf-8')


def build(plan_path, plan_sha256):
    started = time.monotonic()
    plan_path = pinned({'path':str(plan_path),'sha256':plan_sha256})
    plan = read(plan_path)
    if plan.get('schema')!='offline_candidate_build_plan_v1':
        raise ValueError('explicit offline candidate build plan required')
    out, source = safe(plan['output']), safe(plan['source_root'])
    old = safe(plan['previous_package']['path'])
    if out.exists() or any(out.is_relative_to(p) or p.is_relative_to(out) for p in (source,old)):
        raise ValueError('new disjoint candidate output required')
    old_manifest = verify_package(old,plan['previous_package']['manifest_sha256'])
    source_pin = pinned(plan['source_manifest'])
    source_manifest = read(source_pin)
    if source_manifest.get('schema')!='offline_source_export_v1':
        raise ValueError('explicit tracked source export required')
    files = source_manifest['files']
    tracked = set(subprocess.check_output(['git','-C',str(source),'ls-files','-z','--cached'],
                  env={**os.environ,'GIT_OPTIONAL_LOCKS':'0'}).decode('utf-8').split('\0'))
    dirty = subprocess.check_output(['git','-C',str(source),'status','--porcelain','--untracked-files=no'],
                  env={**os.environ,'GIT_OPTIONAL_LOCKS':'0'})
    if dirty.strip() or not set(files)<=tracked:
        raise ValueError('clean tracked source bytes and explicit export membership required')
    migration_path = pinned(plan['migration_receipt'])
    market = pinned(plan['migrated_database'])
    migration = read(migration_path)
    raw_name = plan['raw_database_member']
    if not raw_name.startswith('rehearsal/bundles/initial/databases/'):
        raise ValueError('explicit original archive database member required')
    raw = member(old,raw_name)
    if (digest(raw)!=migration['source']['sha256'] or digest(market)!=migration['candidate']['sha256']
            or migration.get('status')!='passed'):
        raise ValueError('migration does not bind declared raw and migrated inputs')
    observation = plan['observation']
    fixture_path = pinned(observation['receipt'])
    fixture = read(fixture_path)
    workspace, pages = safe(observation['workspace']), safe(observation['pages'])
    if any(out.is_relative_to(p) or p.is_relative_to(out) for p in (workspace,pages)):
        raise ValueError('candidate output overlaps observation input')
    if (safe(fixture['workspace'])!=workspace or safe(fixture['pages'])!=pages
            or fixture.get('fixture_only') is not True):
        raise ValueError('explicit synthetic fixture paths required')
    verify_observation_fixture(source,workspace,fixture_path)
    targets = pinned(plan['targets'])
    validate_targets(read(targets))
    retained = sum(v['bytes'] for n,v in old_manifest['files'].items()
                   if n.startswith('rehearsal/bundles/initial/'))
    required = 6*(retained+market.stat().st_size) + 5*1024**3
    if shutil.disk_usage(out.parent).free < required:
        raise ValueError('insufficient candidate staging, archive and evidence capacity')
    out.mkdir()
    write_new(out/'build-started.json',{'plan_sha256':plan_sha256,'started_at':datetime.now(timezone.utc).isoformat(),
        'required_free_bytes':required,'scope':'private_historical_candidate_no_activation'})
    lab = initialize(out/'lab')
    inputs = lab/'inputs'
    inputs.mkdir()
    export_source(source,files,inputs/'code')
    shutil.copytree(old/'environment',inputs/'environment')
    old_bundle = old/'rehearsal/bundles/initial'
    shutil.copytree(old_bundle/'databases',inputs/'databases')
    migrated_name = 'databases/migrated-market.duckdb'
    shutil.copyfile(market,inputs/migrated_name)
    stage_workspace(workspace,inputs/'workspace')
    shutil.copytree(old_bundle/'raw_facts',inputs/'raw_facts/original-facts')
    shutil.copytree(old_bundle/'workspace',inputs/'raw_facts/original-workspace')
    facts = inputs/'raw_facts'
    shutil.copyfile(migration_path,facts/'migration-receipt.json')
    (facts/'observation-fixture').mkdir()
    shutil.copyfile(fixture_path,facts/'observation-fixture/fixture-receipt.json')
    shutil.copytree(pages,facts/'observation-fixture/pages')
    for name,sha in fixture['artifacts'].items():
        normalized = name.replace('\\','/')
        if digest(member(facts/'observation-fixture',normalized))!=sha:
            raise ValueError('frozen synthetic page artifact differs')
    raw_relative = raw_name.removeprefix('rehearsal/bundles/initial/')
    catalog = old_manifest['catalog']
    old_catalog = member(old,'rehearsal/'+catalog)
    if read(old_catalog/'completed.json')['source_sha256']!=migration['source']['sha256']:
        raise ValueError('old capacity catalog binds a different raw database')
    shutil.copyfile(old_catalog/'completed.json',facts/'original-capacity-completed.json')
    binding = {'schema':'offline_database_bindings_v1','execution_ready':False,
        'raw_archive_database':{'path':raw_relative,'sha256':migration['source']['sha256']},
        'migrated_market_database':{'path':migrated_name,'sha256':migration['candidate']['sha256']},
        'migration_receipt':{'path':'raw_facts/migration-receipt.json','sha256':digest(migration_path)},
        'qualification_sha256':identity(migration['qualification']),
        'synthetic_observation_receipt':{'path':'raw_facts/observation-fixture/fixture-receipt.json','sha256':digest(fixture_path)},
        'observation_workspace_files':recovery.inventory(inputs/'workspace'), 'projection':plan['projection']}
    write_json(facts/'offline-bindings.json',binding)
    verify_bindings(inputs,binding)
    capture_plan = {'components':{name:'inputs/'+name for name in recovery.COMPONENTS},
                    'credential_custody_reference':'credentials excluded; live entitlements remain external'}
    bundle = recovery.capture(lab,capture_plan,'bundles/initial')
    restored = recovery.restore(lab,'bundles/initial','restores/build-check')
    archive = member(lab,catalog)
    archive.mkdir(parents=True)
    for path in old_catalog.iterdir():
        if path.name!='completed.json':
            shutil.copyfile(path,archive/path.name)
    capacity.archive_table(lab,'inputs/'+raw_relative,'multi_source_kline','restores/build-check',catalog)
    business = out/'business-evidence'; business.mkdir()
    verify_business(lab,'restores/build-check',catalog,business/'business-verification.json',
                    bindings='raw_facts/offline-bindings.json')
    kit = out/'portable-package'; kit.mkdir()
    for name in ('code','environment'):
        shutil.copytree(inputs/name,kit/name)
    shutil.copytree(old/'bootstrap',kit/'bootstrap')
    (kit/'rehearsal').mkdir()
    shutil.copyfile(lab/'rehearsal.json',kit/'rehearsal/rehearsal.json')
    shutil.copytree(lab/'bundles/initial',kit/'rehearsal/bundles/initial')
    shutil.copytree(archive,member(kit,'rehearsal/'+catalog))
    shutil.copyfile(source_pin,kit/'source-export.json')
    shutil.copyfile(targets,kit/'RECOVERY-TARGETS.json')
    home_scripts(kit)
    # Recheck every source/input after the expensive capture/verification stages.
    for name,sha in files.items():
        if digest(member(source,name))!=sha:
            raise ValueError('final source changed during candidate build')
    for record in (plan['migration_receipt'],plan['migrated_database'],observation['receipt'],plan['targets'],plan['source_manifest']):
        pinned(record)
    if recovery.inventory(workspace)!=binding['observation_workspace_files']:
        raise ValueError('observation workspace changed during build')
    verify_package(old,plan['previous_package']['manifest_sha256'])
    pin = seal_package(kit,base_sha=old_manifest['base_sha256'],jsonpath_name=old_manifest['jsonpath_name'],
        jsonpath_sha=old_manifest['jsonpath_sha256'],catalog=catalog,candidate=True)
    manifest = verify_package(kit,pin)
    compressed = out/'portable-candidate.zip'
    with zipfile.ZipFile(compressed,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as archive:
        for path in sorted(kit.rglob('*')):
            if path.is_file():
                archive.write(path,'portable-package/'+path.relative_to(kit).as_posix())
    with zipfile.ZipFile(compressed) as archive:
        if archive.testzip() is not None:
            raise ValueError('transfer archive CRC failed')
    verify_package(kit,pin)
    package_bytes = sum(v['bytes'] for v in manifest['files'].values())
    verification_spill = min(16*1024**3,max(256*1024**2,raw.stat().st_size*4))
    result = {'schema':'offline_candidate_build_receipt_v1','package':str(kit),'manifest_sha256':pin,
        'source_export_sha256':digest(source_pin),'previous_manifest_sha256':plan['previous_package']['manifest_sha256'],
        'bundle_id':bundle['receipt_id'],'restore_id':restored['receipt_id'],
        'archive_sha256':digest(compressed),'archive_bytes':compressed.stat().st_size,
        'package_bytes':package_bytes,'files':len(manifest['files']),
        'required_recovery_free_bytes':int(package_bytes*2.5+5*1024**3+verification_spill),
        'verification_temp_budget_bytes':verification_spill,
        'elapsed_seconds':time.monotonic()-started,'offsite_accepted':False,'execution_ready':False,
        'production_activated':False,'real_accounts':'not_applicable_current_scope'}
    write_new(out/'package-pin.json',result)
    delivery = ('StockData 家庭离机交接 · 私密历史候选包\n'
        '候选范围/步骤见包内HOME-STEPS.txt；不含实时采集、退役模型或完整QLib环境。\n'
        f'解压后包目录：portable-package；归档：portable-candidate.zip（{result["archive_bytes"]} bytes）。\n'
        f'包材料：{package_bytes} bytes；目标盘额外可用空间至少 {result["required_recovery_free_bytes"]} bytes。\n'
        f'清单SHA：{pin}\n压缩件SHA：{result["archive_sha256"]}\n'
        '独立可信渠道保管package-pin.json和本交接说明；不要把同介质指纹当作来源认证。\n'
        '在取回/复制开始前记UTC时间；RUN-HOME使用全新目标，家庭操作按包内一页步骤。\n'
        '4小时RTO含取回/复制、技术核验及本人确认可查看/研究材料可用。\n'
        'RPO最多1个交易日；暂停历史快照不证明持续备份达标。\n'
        '成功/失败现场均保留；只私下返回限定脱敏回执；另一台机器实际执行与独立审核仍待完成。\n')
    (out/'HOME-DELIVERY.txt').write_text(delivery,encoding='utf-8')
    print(json.dumps(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',required=True,type=Path)
    parser.add_argument('--plan-sha256',required=True)
    args = parser.parse_args()
    build(args.plan,args.plan_sha256)


if __name__ == '__main__':
    main()
