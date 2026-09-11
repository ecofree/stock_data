"""Numerical and prefix-invariance checks using the installed official engine."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import numpy as np
import pandas as pd
from trade_system.v2.alpha158_research import compute


def run(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    days=pd.bdate_range('2026-01-01',periods=80)
    n=np.arange(80); close=10+n*.02+np.sin(n)*.1
    frame=pd.DataFrame({'datetime':days,'instrument':'000002','open':close-.1,
        'high':close+.2,'low':close-.2,'close':close,'volume':100+n*3,'turnover':(100+n*3)*close/10})
    a,names=compute(frame,days,output/'a')
    changed=frame.copy();changed.loc[changed.datetime>days[70],'close']+=.05
    b,_=compute(changed,days,output/'b')
    cols=list(names)
    np.testing.assert_allclose(a.loc[a.datetime<=days[70],cols],b.loc[b.datetime<=days[70],cols],rtol=0,atol=0,equal_nan=True)
    last=a.iloc[-1]
    assert len(names)==158 and len(a)==20
    assert abs(last.KMID-(close[-1]-(close[-1]-.1))/(close[-1]-.1))<1e-6
    assert abs(last.VWAP0-1)<1e-6
    assert abs(last.ROC60-close[19]/close[79])<1e-6
    return {'features':158,'rows':20,'prefix_invariance':True,'KMID_VWAP_ROC_checks':True}


if __name__=='__main__':
    import json
    p=argparse.ArgumentParser();p.add_argument('--output',required=True)
    print(json.dumps(run(p.parse_args().output)))
