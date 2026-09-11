"""Source-bound full audit of an existing isolated paper DB, no ledger writes."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.domain import canonical,file_hash,identity
from trade_system.v2.paper_storage import load_paper
from trade_system.v2.storage import Store


def run(db,output,budget):
    db=Path(db).resolve(strict=True);output=Path(output).resolve()
    if output.exists() or db.parent==output or db.parent in output.parents:
        raise ValueError('separate new evidence directory required')
    output.mkdir(parents=True)
    root=Path(__file__).resolve().parents[2]
    paths=['trade_system/v2/paper_storage.py','trade_system/v2/paper_ledger.py','trade_system/v2/storage.py',
           'trade_system/v2/domain.py','tools/v2/probe_recovery_budget.py']
    before={p:file_hash(root/p) for p in paths};db_before=file_hash(db)
    started={'source_files':before,'database_sha256':db_before,'budget_seconds':budget,
             'scope':'existing_isolated_ledger_full_audit_no_new_events'}
    (output/'started.json').write_text(canonical(started),encoding='utf-8')
    results=[]
    with Store(db) as s:
        accounts=s.con.execute('SELECT account_id,last_seq,last_hash FROM paper_account ORDER BY account_id').fetchall()
        for account,seq,expected in accounts:
            t=time.monotonic();s.command_deadline=t+budget
            try:
                book=load_paper(s,account,full_replay=True)
                results.append({'account_id':account,'events':seq,'elapsed_seconds':time.monotonic()-t,
                                'state_matches':identity(book.state)==expected,'status':'verified'})
            except TimeoutError:
                results.append({'account_id':account,'events':seq,'elapsed_seconds':time.monotonic()-t,
                                'status':'budget_exceeded_not_verified'})
            finally:s.command_deadline=None
    unchanged=before=={p:file_hash(root/p) for p in paths} and db_before==file_hash(db)
    result={**started,'accounts':results,'source_and_database_unchanged':unchanged,'execution_ready':False}
    (output/'result.json').write_text(canonical(result),encoding='utf-8')
    if not unchanged:raise ValueError('evidence input changed')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',required=True);p.add_argument('--output',required=True)
    p.add_argument('--budget-seconds',type=float,default=30)
    a=p.parse_args();print(json.dumps(run(a.db,a.output,a.budget_seconds)))
