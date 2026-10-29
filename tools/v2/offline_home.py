"""Bounded private home-recovery preflight and allowlisted acceptance collection.

Only local new output directories; never starts providers, services or tasks.
Collection is evidence preparation, not independent-machine certification.
"""
import argparse
import ctypes
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.v2.offline_package import safe, digest, member, write_new
from tools.v2.offline_process import diagnostic

CHECKLIST = ('pages_open_offline', 'chinese_readable', 'historical_dates_and_scope_checked',
             'synthetic_demo_distinguished', 'research_materials_usable', 'read_only_confirmed')
ARTIFACTS = ('environment-evidence/verification.json', 'evidence/business-verification.json',
             'evidence/index.html', 'evidence/workbench.html', 'evidence/historical-market.html',
             'evidence/historical-market.json', 'evidence/rebound-readonly-configuration.json',
             'evidence/platform.json')
LOGS = ('environment-rebuild', 'recovery-imports', 'business-restore', 'capacity-recheck')


def read(path):
    path = safe(path)
    if path.stat().st_size > 16_000_000:
        raise ValueError('bounded JSON required')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate JSON key')
            value[key] = item
        return value
    return json.loads(path.read_text('utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def instant(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError('timezone-aware timestamp required')
    return moment.astimezone(timezone.utc)


def validate_targets(value):
    if (value.get('schema') != 'stockdata_recovery_targets_v1'
            or value.get('scope') != 'market_research_observation_v1'
            or value.get('rpo') != {'unit':'trading_session','maximum':1,'seconds_equivalent':None}
            or value.get('rto') != {'maximum_seconds':14400,
                'start':'begin_retrieval_or_copy_for_the_recovery_attempt',
                'end':'technical_verification_complete_and_user_confirms_pages_and_research_materials_usable'}
            or value.get('production_activation_authorized') is not False
            or value.get('real_accounts') != 'not_applicable_current_scope'):
        raise ValueError('explicit user-approved recovery targets required')
    return value


def platform_preflight(destination, package_bytes, *, verification_temp_bytes=0):
    """Candidate Windows 10/11 x64 + NTFS, not a platform certification matrix."""
    target = safe(destination)
    architecture = platform.machine().upper()
    if os.name != 'nt' or architecture not in ('AMD64','X86_64') or struct.calcsize('P') != 8:
        raise ValueError('candidate supports Windows x64 only')
    version = sys.getwindowsversion()
    if version.major != 10 or version.product_type != 1:
        raise ValueError('Windows 10/11 desktop candidate required')
    kernel = ctypes.windll.kernel32
    volume = ctypes.create_unicode_buffer(32768)
    if not kernel.GetVolumePathNameW(str(target.parent), volume, len(volume)):
        raise OSError('target volume cannot be inspected')
    filesystem = ctypes.create_unicode_buffer(256)
    if not kernel.GetVolumeInformationW(volume.value, None, 0, None, None, None, filesystem, len(filesystem)):
        raise OSError('target filesystem cannot be inspected')
    if filesystem.value.upper() != 'NTFS':
        raise ValueError('NTFS local recovery target required')
    if kernel.GetDriveTypeW(volume.value) != 3:
        raise ValueError('fixed local recovery volume required')
    import shutil
    required = int(package_bytes*2.5 + 5*1024**3 + verification_temp_bytes)
    available = shutil.disk_usage(target.parent).free
    if available < required:
        raise ValueError('insufficient offline recovery capacity')
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography') as key:
        machine_guid = winreg.QueryValueEx(key, 'MachineGuid')[0]
    fingerprint = hashlib.sha256(str(machine_guid).encode()).hexdigest()
    return {'system':'Windows', 'architecture':architecture,
        'version':[version.major,version.minor,version.build], 'filesystem':filesystem.value,
        'machine_fingerprint':fingerprint, 'required_free_bytes':required,'observed_free_bytes':available,
        'support_status':'candidate_not_cross_version_certification',
        'independent_machine_status':'pending_external_verification'}


def filesystem_probe(target):
    """Retain a tiny same-directory hardlink/no-overwrite capability probe."""
    probe = safe(target)/'filesystem-probe'
    probe.mkdir()
    source, linked = probe/'source.txt', probe/'linked.txt'
    source.write_text('synthetic filesystem capability probe', encoding='utf-8')
    os.link(source, linked)
    if source.read_bytes() != linked.read_bytes():
        raise ValueError('hardlink capability failed')
    try:
        os.link(source, linked)
    except FileExistsError:
        return {'hardlink':True,'no_overwrite':True,'interruption_recovery':'pending_actual_interruption_test'}
    raise ValueError('filesystem no-overwrite capability failed')


def collect_logs(target, output):
    for name in LOGS:
        path = target/'evidence'/(name+'.log')
        if path.is_file():
            safe(path)
            with path.open('rb') as stream:
                stream.seek(max(0,path.stat().st_size-32000))
                content = diagnostic(stream.read(), dict(os.environ))
            content = content.replace(str(target),'[RECOVERY_DESTINATION]')
            profile = os.environ.get('USERPROFILE')
            if profile:
                content = content.replace(profile,'[USERPROFILE]')
            (output/(name+'.log')).write_text(content, encoding='utf-8')


def run_home(package, manifest_sha256, destination, output, *, intake_started_at):
    from tools.v2.offline_package import restore_package
    package, target, output = safe(package), safe(destination), safe(output)
    if (output.exists() or any(output.is_relative_to(p) or p.is_relative_to(output) for p in (package,target))
            or not intake_started_at or instant(intake_started_at)>datetime.now(timezone.utc)):
        raise ValueError('fresh separate home attempt and actual intake start required')
    started = datetime.now(timezone.utc).isoformat()
    new_target = not target.exists()
    output.mkdir()
    write_new(output/'attempt-started.json',{'package_sha256':manifest_sha256,
        'technical_started_at':started,'intake_started_at':intake_started_at,'offsite_accepted':False})
    try:
        result = restore_package(package,manifest_sha256,target,intake_started_at=intake_started_at)
    except BaseException as exc:
        write_new(output/'home-technical.json',{'status':'failed','error_type':type(exc).__name__,
            'technical_started_at':started,'technical_finished_at':datetime.now(timezone.utc).isoformat(),
            'package_sha256_claimed':manifest_sha256,'offsite_accepted':False,'execution_ready':False})
        if new_target:
            collect_logs(target,output)
        raise
    write_new(output/'home-technical.json',{'status':'passed','package_sha256':manifest_sha256,
        'technical_started_at':started,'technical_finished_at':result['technical_finished_at'],
        'human_confirmation':'pending','offsite_accepted':False,'execution_ready':False})
    return result


def business_summary(target, receipt):
    environment = read(target/'environment-evidence/verification.json')
    business = read(target/'evidence/business-verification.json')
    historical = read(target/'evidence/historical-market.json')
    platform_record = receipt.get('platform',{})
    capacity, roles = business.get('capacity',{}), business.get('database_roles',{})
    rebound, recovery = business.get('rebound_readonly_configuration',{}), business.get('recovery',{})
    if (environment.get('passed') is not True or environment.get('installed_site_packages_copied') is not False
            or environment.get('production_or_global_python_changed') is not False
            or business.get('acceptance_scope') != 'market_research_observation_v1'
            or business.get('execution_ready') is not False
            or business.get('real_accounts') != 'not_applicable_current_scope'
            or capacity.get('complete_multiset_verified') is not True
            or roles.get('preservation_verified') is not True or roles.get('qualification_verified') is not True
            or rebound.get('read_only') is not True or rebound.get('activation_allowed') is not False
            or recovery.get('offsite_accepted') is not False or not isinstance(recovery.get('files'),dict)
            or not recovery['files'] or historical.get('historical_rehearsal') is not True):
        raise ValueError('successful environment, business and historical verification required')
    for key in ('rows','batches'):
        if type(capacity.get(key)) is not int or capacity[key] < 0:
            raise ValueError('bounded numeric capacity evidence required')
    day = historical['market']['trade_date']
    if date.fromisoformat(day).isoformat()!=day or not re.fullmatch('[0-9a-f]{64}',historical['market']['snapshot_id']):
        raise ValueError('historical market identity required')
    allowed_platform = ('machine_fingerprint','system','architecture','version','filesystem','support_status')
    observed = read(target/'evidence/platform.json')
    version = platform_record.get('version')
    if (not re.fullmatch('[0-9a-f]{64}',str(platform_record.get('machine_fingerprint','')))
            or platform_record.get('system')!='Windows' or platform_record.get('architecture') not in ('AMD64','X86_64')
            or platform_record.get('filesystem')!='NTFS'
            or platform_record.get('support_status')!='candidate_not_cross_version_certification'
            or not isinstance(version,list) or len(version)!=3 or any(type(v) is not int or v<0 for v in version)
            or any(observed.get(key)!=platform_record.get(key) for key in allowed_platform)
            or type(receipt.get('elapsed_seconds')) not in (int,float) or receipt['elapsed_seconds']<0):
        raise ValueError('typed pinned platform and technical timing evidence required')
    return {'recovered_files':len(recovery['files']),'archive_rows':capacity['rows'],'archive_batches':capacity['batches'],
            'complete_multiset_verified':True,'migration_preservation_verified':True,'qualification_verified':True,
            'historical_market_date':day,'historical_snapshot_id':historical['market']['snapshot_id'],
            'environment_rebuilt_verified':True,'synthetic_observation_is_separate':True}


def collect(destination, output, manifest_sha256, *, human_confirmation=None):
    target, output = safe(destination), safe(output)
    if output.exists() or output.is_relative_to(target) or target.is_relative_to(output):
        raise ValueError('new separate acceptance collection required')
    if not re.fullmatch('[0-9a-f]{64}', manifest_sha256):
        raise ValueError('manifest SHA required')
    if not (target/'evidence/completed.json').exists():
        if human_confirmation:
            raise ValueError('incomplete recovery cannot accept human usability confirmation')
        output.mkdir()
        result = {'schema':'offline_home_acceptance_collection_v1','status':'technical_incomplete',
            'package_sha256_claimed':manifest_sha256,'package_binding':'not_verified_by_incomplete_collection',
            'offsite_accepted':False,'execution_ready':False,'human_confirmation':'pending',
            'independent_machine_status':'pending_external_verification'}
        write_new(output/'acceptance.json',result)
        collect_logs(target,output)
        return result
    receipt = read(target/'evidence/completed.json')
    if (not re.fullmatch('[0-9a-f]{64}', manifest_sha256)
            or receipt.get('schema') != 'offline_package_local_recovery_v1'
            or receipt.get('package_sha256') != manifest_sha256
            or receipt.get('offsite_accepted') is not False or receipt.get('execution_ready') is not False
            or receipt.get('environment_rebuilt_offline') is not True
            or receipt.get('recovery_imports_verified') is not True):
        raise ValueError('successful pinned technical recovery required')
    targets = validate_targets(receipt['targets'])
    artifacts = receipt['evidence_artifacts']
    if set(artifacts)!=set(ARTIFACTS):
        raise ValueError('incomplete or unapproved collection artifact set')
    for name, sha in artifacts.items():
        if digest(member(target, name)) != sha:
            raise ValueError('technical evidence changed before collection')
    summary = business_summary(target,receipt)
    start, finish = instant(receipt['technical_started_at']), instant(receipt['technical_finished_at'])
    now = datetime.now(timezone.utc)
    if not start <= finish <= now:
        raise ValueError('technical receipt chronology differs')
    checklist = {name:False for name in CHECKLIST}
    human = None
    if human_confirmation:
        human = read(human_confirmation)
        if (set(human) != {*CHECKLIST,'confirmed_at','operator_alias'}
                or not re.fullmatch('[A-Za-z0-9_-]{1,48}', human['operator_alias'])
                or any(human[name] is not True for name in CHECKLIST)
                or not finish <= instant(human['confirmed_at']) <= now):
            raise ValueError('complete explicit human checklist and chronology required')
        checklist = {name:human[name] for name in CHECKLIST}
    intake = receipt.get('intake_started_at')
    if intake and instant(intake) > start:
        raise ValueError('intake cannot start after technical recovery')
    total = (instant(human['confirmed_at'])-instant(intake)).total_seconds() if intake and human else None
    status = ('within_target_measured_not_offsite_certification' if total <= targets['rto']['maximum_seconds']
              else 'exceeded') if total is not None else 'pending_intake_start_and_human_confirmation'
    result = {'schema':'offline_home_acceptance_collection_v1','package_sha256':manifest_sha256,
        'technical_started_at':receipt['technical_started_at'],'technical_finished_at':receipt['technical_finished_at'],
        'technical_elapsed_seconds':receipt['elapsed_seconds'], 'intake_started_at':intake,
        'human_confirmed_at':human['confirmed_at'] if human else None,
        'operator_alias':human['operator_alias'] if human else None,
        'end_to_end_seconds':total,'rto_assessment':status,'rto_target':targets['rto'],
        'rpo':targets['rpo'],'ongoing_rpo_assessment':'not_certified_historical_snapshot',
        'snapshot_requirement':'zero_missing_files_and_zero_business_multiset_difference',
        'machine':{k:receipt.get('platform',{}).get(k) for k in
                   ('machine_fingerprint','system','architecture','version','filesystem','support_status')},
        'artifact_sha256':artifacts,'human_checklist':checklist,'execution_ready':False,
        'business_summary':summary,
        'offsite_accepted':False,'independent_machine_status':'pending_external_verification',
        'real_accounts':'not_applicable_current_scope'}
    output.mkdir()
    write_new(output/'acceptance.json',result)
    write_new(output/'human-checklist.json', {**checklist,'confirmed_at':None,'operator_alias':None})
    collect_logs(target,output)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('--human-confirmation',type=Path)
    parser.add_argument('--run-package',type=Path)
    parser.add_argument('--intake-started-at')
    args = parser.parse_args()
    try:
        if args.run_package:
            run_home(args.run_package,args.manifest_sha256,args.destination,args.output,
                     intake_started_at=args.intake_started_at)
        else:
            collect(args.destination,args.output,args.manifest_sha256,human_confirmation=args.human_confirmation)
        print('Evidence retained; human and independent-machine review remain separate.')
    except Exception as exc:
        print(type(exc).__name__+': operation rejected; retain new attempt/evidence directories.',file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
