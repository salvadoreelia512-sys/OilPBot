"""Frozen development selection; later non-overlapping OHLC touch evaluation.
No tuning on the held-out period. All targets and features are fixed at each origin.
"""
import hashlib,json,math,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from datetime import date
import numpy as np
from src.strategic import DATA_PATH,REPORT_PATH,SPEC_VERSION,parameters,integrated_variance
from src.simulation import closed_form_barrier_touch_prob

MODELS=('baseline','regime','regime_fundamentals')
SPLIT='2019-01-01'


def score(rows, model):
    p=np.array([r['p'][model] for r in rows]);y=np.array([r['y'] for r in rows])
    bins=[]
    for lower in np.arange(0,1,.1):
        indices=(p>=lower)&(p<(lower+.1) if lower<.9 else p<=1)
        if indices.any():bins.append({'lower':round(float(lower),1),'count':int(indices.sum()),'predicted':float(p[indices].mean()),'observed':float(y[indices].mean())})
    return {'cases':len(rows),'origins':len({r['origin'] for r in rows}),
            'brier':float(np.mean((p-y)**2)),
            'ece':float(sum(b['count']*abs(b['predicted']-b['observed']) for b in bins)/len(rows)), 'reliability_bins':bins}


def evaluate(data, data_sha256=None):
    bars=data['bars'];groups={5:[],21:[]};excluded={5:0,21:0}
    # One origin per horizon length; subsequent origin starts when labels mature.
    for horizon in groups:
        for i in range(504,len(bars)-horizon,horizon):
            origin=bars[i];future=bars[i+1:i+horizon+1]
            if origin['close']<=0 or any(b['low']<=0 for b in future):
                excluded[horizon]+=1;continue
            state=parameters(bars[:i+1],data['weekly'],origin['date'])
            t=horizon/252;sigmas={m:math.sqrt(integrated_variance(state,t,m)/t) for m in MODELS}
            for direction,sign in [('long',1),('short',-1)]:
                for z in (.5,1.,1.5):
                    # Same target for every candidate, fixed using baseline volatility.
                    target=origin['close']*math.exp(sign*z*sigmas['baseline']*math.sqrt(t))
                    outcome=int(max(b['high'] for b in future)>=target) if sign>0 else int(min(b['low'] for b in future)<=target)
                    groups[horizon].append({'origin':origin['date'],'ends':future[-1]['date'],'direction':direction,'z':z,'y':outcome,
                        'p':{m:closed_form_barrier_touch_prob(origin['close'],target,sigmas[m],t) for m in MODELS}})
    development={h:[r for r in rows if r['ends']<SPLIT] for h,rows in groups.items()}
    heldout={h:[r for r in rows if r['origin']>=SPLIT] for h,rows in groups.items()}
    # Predeclared family and equal horizon weight. The held-out sample is not consulted.
    selected=min(MODELS,key=lambda m:sum(score(rows,m)['brier'] for rows in development.values())/2)
    report={};passes=[]
    for h,rows in heldout.items():
        metrics={m:score(rows,m) for m in MODELS}
        for m in MODELS:
            metrics[m]['direction_brier']={direction:score([r for r in rows if r['direction']==direction],m)['brier'] for direction in ('long','short')}
        frequencies={(direction,z):np.mean([r['y'] for r in development[h] if r['direction']==direction and r['z']==z]) for direction in ('long','short') for z in (.5,1.,1.5)}
        frequency_brier=float(np.mean([(frequencies[(r['direction'],r['z'])]-r['y'])**2 for r in rows]))
        origins=sorted({r['origin'] for r in rows})
        differences=np.array([np.mean([(r['p']['baseline']-r['y'])**2-(r['p'][selected]-r['y'])**2 for r in rows if r['origin']==o]) for o in origins])
        # Six-origin blocks retain some serial dependence; barriers cluster by origin.
        rng=np.random.default_rng(20261026);samples=[];n=len(differences)
        for _ in range(2000):
            starts=rng.integers(0,n,size=math.ceil(n/6));indices=np.concatenate([(s+np.arange(6))%n for s in starts])[:n]
            samples.append(float(differences[indices].mean()))
        interval=np.quantile(samples,[.025,.975]).tolist()
        passed=bool(n>=50 and selected!='baseline' and interval[0]>0 and metrics[selected]['ece']<=metrics['baseline']['ece']+.015)
        passes.append(passed)
        report['1 week' if h==5 else '1 month']={'trading_days':h,'first_origin':origins[0],'last_origin':origins[-1],
              'models':metrics,'selected_minus_baseline_brier_improvement_95pct':interval,'passes_upgrade_gate':passed,
              'excluded_nonpositive_price_windows':excluded[h],
              'development_frequency_benchmark_brier':frequency_brier,
              'baseline_beats_frequency_benchmark':metrics['baseline']['brier']<frequency_brier}
    return {'version':SPEC_VERSION,'data_sha256':data_sha256 or hashlib.sha256(DATA_PATH.read_bytes()).hexdigest(),
            'development_end_exclusive':SPLIT,'heldout_start':SPLIT,'selected_on_development':selected,
            'selected_model':selected if all(passes) else 'baseline','promoted':bool(all(passes)),
            'horizons':report,'directional_edge_validated':False,'exact_tvc_touch_validated':False,'daily_tvc_touch_evaluated':True,'instrument':'TVC:USOIL',
            'method':'Daily TVC:USOIL OHLC target touches; non-overlapping 5/21-session windows; three fixed standardized barriers per direction. Training inputs strictly known at each origin. EIA seven-day availability lag. Selection before 2019, evaluation from 2019; six-origin block bootstrap clusters all barriers.',
            'limitations':data['limitations']+['Target selection uses baseline volatility; results do not cover arbitrary targets or hourly horizons.',
                'Cross-horizon outcomes overlap and are not independent. Block intervals do not remove all regime dependence.',
                'No news feature has historical out-of-sample validation. Daily-origin and 5/21-session results do not calibrate arbitrary live origins or exact calendar horizons. No guaranteed future accuracy.'],
            'development_brier':{str(h):{m:score(rows,m)['brier'] for m in MODELS} for h,rows in development.items()}}


if __name__=='__main__':
    report=evaluate(json.loads(DATA_PATH.read_text()))
    REPORT_PATH.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    print({k:report[k] for k in ('selected_on_development','selected_model','promoted')})
    for h,v in report['horizons'].items():print(h,{m:{k:metrics[k] for k in ('origins','cases','brier','ece')} for m,metrics in v['models'].items()},'CI',v['selected_minus_baseline_brier_improvement_95pct'])
