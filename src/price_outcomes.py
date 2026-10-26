"""Disjoint terminal-price categories; exact paths do not have positive probability."""
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from scipy.special import ndtr

VERSION='terminal-regimes-v1'
BAND=.0025
KEYS=('down','middle','up')
LABELS={'down':'Down','middle':'Sideways','up':'Up'}
REPORT_PATH=Path(__file__).parent/'data/outcome_validation.json'


def baseline_probabilities(variance):
    if variance<=0:return np.array([0.,1.,0.])
    scale=math.sqrt(variance)
    lower=float(ndtr((math.log1p(-BAND)+.5*variance)/scale))
    upper=float(ndtr((math.log1p(BAND)+.5*variance)/scale))
    return np.array([lower,upper-lower,1-upper])


def percentage_units(probabilities):
    """Largest-remainder rounding: tenths of one percent total exactly 1,000."""
    p=np.asarray(probabilities,dtype=float)
    if len(p)!=3 or not np.isfinite(p).all() or (p<0).any() or p.sum()<=0:raise ValueError('Invalid category probabilities')
    scaled=p/p.sum()*1000;units=np.floor(scaled).astype(int)
    for index in np.argsort(-(scaled-units),kind='stable')[:1000-int(units.sum())]:units[index]+=1
    return units.tolist()


def summarize(samples,hour,reference):
    if not np.isfinite(samples[:hour+1]).all() or (samples[:hour+1]<=0).any():raise ValueError('Invalid simulated prices')
    end=samples[hour].astype(float);lower=reference*(1-BAND);upper=reference*(1+BAND)
    masks=(end<lower,(end>=lower)&(end<=upper),end>upper)
    counts=np.array([int(mask.sum()) for mask in masks]);p=counts/len(end);units=percentage_units(p)
    groups=[]
    for key,mask,count,prob,display in zip(KEYS,masks,counts,p,units):
        # Each trace is a pointwise conditional median, not an actual sampled path.
        curve=np.median(samples[:hour+1,mask],axis=1).astype(float).tolist() if count else []
        groups.append({'key':key,'label':LABELS[key],'probability':float(prob),'percentage_units':display,
                       'sample_count':int(count),'curve':curve,'endpoint':curve[-1] if curve else None})
    largest=max(units);leaders=[g['key'] for g in groups if g['percentage_units']==largest]
    ordered=np.sort(p)[::-1];gap=float(ordered[0]-ordered[1])
    noise=1.96*math.sqrt(max(0,float(ordered[0]+ordered[1]-gap*gap))/len(end))
    return {'hours':hour,'groups':groups,'reference_price':reference,'lower_boundary':lower,'upper_boundary':upper,
            'band_percent':BAND*100,'percentage_total':sum(units)/10,'most_likely':leaders,
            'near_tie':bool(gap<=max(.02,noise)),'sample_size':len(end),
            'event':'price at horizon end, not touching a target or following an exact line',
            'curve_meaning':'Pointwise median among paths assigned by their terminal category; not a coherent or promised price path'}


def validation(data_sha256):
    if not REPORT_PATH.exists():return {'version':VERSION,'available':False,'reason':'Terminal-category validation unavailable'}
    report=json.loads(REPORT_PATH.read_text())
    return {**report,'current_archive_matches':report.get('data_sha256')==data_sha256,
            'applies_to_live_news_overlay':False,'applies_to_arbitrary_intraday_origins':False}
