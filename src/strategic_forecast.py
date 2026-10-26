"""Evaluated daily-volatility baseline plus separately labeled risk scenarios."""
import math
from datetime import timedelta

import numpy as np
from scipy.special import ndtri

from src.simulation import closed_form_barrier_touch_prob
from src.strategic import YEAR_HOURS, integrated_variance


def calculate(snapshot, request, now, rows, strategy, news):
    from src.forecast import trading_hours
    sigma=math.sqrt(strategy['short_variance'])
    model=strategy['model']
    delay=trading_hours(snapshot.timestamp,now)/YEAR_HOURS
    nodes,weights=np.polynomial.hermite.hermgauss(32)
    current=snapshot.price*np.exp(-.5*sigma*sigma*delay+sigma*math.sqrt(2*delay)*nodes)
    def probability(variance,years):
        values=[]
        for price in current:
            reached=price>=request.target_price if request.direction=='long' else price<=request.target_price
            values.append(1. if reached else closed_form_barrier_touch_prob(float(price),request.target_price,
                          math.sqrt(variance/years) if years>0 else sigma,years))
        return float(np.dot(weights,values)/math.sqrt(math.pi))
    projection=[];risk_projection=[];total_variance=0.;stress_variance=0.;probabilities={};stress_probabilities={}
    news_scale=min(2.25,max(1.,news['vol_multiplier']))
    fundamental_scale=strategy['fundamental_scale']
    jumps=min(65.,max(0.,news['jump_intensity']))
    stressed=news_scale>1 or jumps>0 or fundamental_scale>1
    n=6000;paths=np.full(n,math.log(snapshot.price));touched=np.zeros(n,dtype=bool)
    if delay:
        paths+=-.5*sigma*sigma*delay+sigma*math.sqrt(delay)*np.random.default_rng(20261026).standard_normal(n)
    log_target=math.log(request.target_price) if request is not None else None
    samples=np.empty((721,n),dtype=np.float32)
    endpoints={r['hours'] for r in rows};active_hours=0.
    for hour in range(721):
        if hour:
            hours=trading_hours(now+timedelta(hours=hour-1),now+timedelta(hours=hour));dt=hours/YEAR_HOURS
            previous_years=active_hours/YEAR_HOURS;active_hours+=hours;years=active_hours/YEAR_HOURS
            step_variance=integrated_variance(strategy,years,model)-integrated_variance(strategy,previous_years,model)
            total_variance+=step_variance
            if dt:
                # Temporary headline risk is not extrapolated unchanged for a month.
                news_decay=2**(-(hour-.5)/6)
                fundamental_decay=2**(-(active_hours/23)/5)
                scale=(1+(news_scale-1)*news_decay)*(1+(fundamental_scale-1)*fundamental_decay)
                variance=step_variance*scale*scale;stress_variance+=variance
                rate=jumps*news_decay
                rng=np.random.default_rng(20261025+hour);jump_rng=np.random.default_rng(20271025+hour)
                previous=paths.copy();counts=jump_rng.poisson(rate*dt,n)
                paths+=-.5*variance-rate*(math.exp(.5*.025**2)-1)*dt+math.sqrt(variance)*rng.standard_normal(n)+np.sqrt(counts)*.025*jump_rng.standard_normal(n)
                if request is not None:
                    a=log_target-previous if request.direction=='long' else previous-log_target
                    b=log_target-paths if request.direction=='long' else paths-log_target
                    crossed=(a<=0)|(b<=0)
                    bridge=np.exp(np.minimum(0,-2*np.maximum(a,0)*np.maximum(b,0)/max(variance,1e-15)))
                    touched|=crossed|(rng.random(n)<bridge)
        elif request is not None:
            touched|=paths>=log_target if request.direction=='long' else paths<=log_target
        samples[hour]=np.exp(paths)

        variance=total_variance+sigma*sigma*delay
        median=snapshot.price*math.exp(-.5*variance)
        projection.append({'hours':hour,'low':median*math.exp(float(ndtri(.1))*math.sqrt(variance)),
                           'median':median,'high':median*math.exp(float(ndtri(.9))*math.sqrt(variance))})
        if stressed:
            low,mid,high=np.quantile(np.exp(paths),[.1,.5,.9])
            risk_projection.append({'hours':hour,'low':float(low),'median':float(mid),'high':float(high)})
        if hour in endpoints:
            years=active_hours/YEAR_HOURS
            if request is not None:
                probabilities[hour]=probability(total_variance,years)
                stress_probabilities[hour]=float(np.mean(touched)) if stressed else probabilities[hour]
    for row in rows:
        if request is not None:
            row['probability']=probabilities[row['hours']]
            row['stress_probability']=stress_probabilities[row['hours']]
        row['sampling_error_pp']=0.  # Central probability is analytical, not Monte Carlo.
        row['estimation_method']='analytical zero-drift daily-volatility baseline'
        row['integrated_variance']=integrated_variance(strategy,row['trading_hours']/YEAR_HOURS,model)
    long_term=[]
    for label,hour in [('1 week',168),('1 month',720)]:
        point=projection[hour];risk=risk_projection[hour] if stressed else point
        long_term.append({'horizon':label,'reference_price':snapshot.price,
                          'central_range':point,'stress_range':risk,
                          'central_downside_percent':(point['low']/snapshot.price-1)*100,
                          'stress_downside_percent':(risk['low']/snapshot.price-1)*100})
    from src.price_outcomes import summarize
    outcomes={str(h):summarize(samples,h,snapshot.price) for h in sorted(endpoints)}
    return {'outcomes':outcomes,'projection':projection,'long_term':long_term,'stress_active':stressed,'annual_volatility':sigma,
            'risk_assumptions':{'headline_half_life_calendar_hours':6,'fundamental_half_life_trading_days':5,
                                'news_volatility_scale':news_scale,'news_jump_intensity_annual':jumps,
                                'fundamental_volatility_scale':fundamental_scale,
                                'validated':False,'simulation_paths':n}}
