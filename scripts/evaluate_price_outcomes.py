"""Terminal close-category validation; separate from target-touch diagnostics."""
import hashlib,json,math,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from src.strategic import DATA_PATH,parameters,integrated_variance
from src.price_outcomes import VERSION,BAND,KEYS,REPORT_PATH,baseline_probabilities


def metrics(rows,prior):
    p=np.array([r['p'] for r in rows]);y=np.array([r['y'] for r in rows]);actual=np.eye(3)[y]
    predicted=np.argmax(p,axis=1);confidence=p.max(axis=1);bins=[]
    for lower,upper in [(0,.4),(.4,.5),(.5,.6),(.6,.8),(.8,1.000001)]:
        mask=(confidence>=lower)&(confidence<upper)
        if mask.any():bins.append({'count':int(mask.sum()),'mean_top_share':float(confidence[mask].mean()),'observed_top_category_fraction':float((predicted[mask]==y[mask]).mean())})
    return {'origins':len(rows),'multiclass_brier':float(np.mean(np.sum((p-actual)**2,axis=1))),
            'frequency_benchmark_brier':float(np.mean(np.sum((prior-actual)**2,axis=1))),
            'largest_category_accuracy':float(np.mean(predicted==y)),
            'largest_category_ece':float(sum(b['count']*abs(b['mean_top_share']-b['observed_top_category_fraction']) for b in bins)/len(rows)),
            'categories':{key:{'mean_predicted':float(p[:,i].mean()),'observed_fraction':float((y==i).mean())} for i,key in enumerate(KEYS)},'bins':bins}


def evaluate(data,fingerprint):
    bars=data['bars'];results={}
    for horizon,label in [(5,'1 week'),(21,'1 month')]:
        development=[];heldout=[]
        for i in range(504,len(bars)-horizon,horizon):
            origin=bars[i];future=bars[i+horizon]
            if origin['close']<=0 or future['close']<=0:continue
            state=parameters(bars[:i+1],data['weekly'],origin['date'])
            variance=integrated_variance(state,horizon/252,'baseline');ratio=future['close']/origin['close']
            y=0 if ratio<1-BAND else 2 if ratio>1+BAND else 1
            row={'p':baseline_probabilities(variance).tolist(),'y':y}
            if future['date']<'2019-01-01':development.append(row)
            elif origin['date']>='2019-01-01':heldout.append(row)
        prior=np.bincount([r['y'] for r in development],minlength=3)/len(development)
        results[label]=metrics(heldout,prior)
    return {'version':VERSION,'data_sha256':fingerprint,'instrument':'TVC:USOIL','data_last_bar':bars[-1]['date'],
            'band_percent':BAND*100,'horizons':results,'method':'Completed daily-origin terminal close returns in non-overlapping 5/21-session windows. Pre-2019 frequency benchmark; evaluation from 2019. Multiclass Brier is on a 0–2 scale, lower is better.',
            'limitations':['This is not the earlier target-touch test or a technical trading-rule backtest.',
              'Daily-origin baseline results do not validate the live news/supply stress overlay or hourly/intraday-origin categories.',
              '5/21-session windows differ from exact live calendar horizons and exceptional holiday schedules.',
              'Provider history can be revised; category ranks are not trade instructions or a guarantee of future accuracy.']}


if __name__=='__main__':
    data=json.loads(DATA_PATH.read_text());report=evaluate(data,hashlib.sha256(DATA_PATH.read_bytes()).hexdigest())
    REPORT_PATH.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    for h,v in report['horizons'].items():print(h,v)
