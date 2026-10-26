"""Manual price-range outlook independent of a target-touch trading request."""
from datetime import timedelta

from fastapi import HTTPException

from src.price_outcomes import VERSION, validation
from src.strategic import context
from src.strategic_forecast import calculate


def build(snapshot,reference,now):
    from src.forecast import quote_payload,trading_hours,HORIZONS
    from src.intelligence import news_context
    quote=quote_payload(snapshot,now)
    if not quote['usable']:raise HTTPException(status_code=503,detail='The source quote is too old for a new outlook.')
    strategy=context(now)
    if strategy['data_age_days']>10:raise HTTPException(status_code=503,detail='Daily model history is out of date; public-data refresh is pending.')
    news=news_context(now)
    rows=[{'label':label,'hours':hours,'trading_hours':trading_hours(now,now+timedelta(hours=hours)),
           'ends_at_utc':(now+timedelta(hours=hours)).isoformat()} for label,hours in HORIZONS+[('1 month',720)]]
    analysis=calculate(snapshot,None,now,rows,strategy,news)
    for row in rows:analysis['outcomes'][str(row['hours'])].update({'label':row['label'],'ends_at_utc':row['ends_at_utc'],'trading_hours':row['trading_hours']})
    return {'quote':quote,'reference_price':reference,'outcomes':analysis['outcomes'],
            'model':{'version':VERSION,'as_of_utc':now.isoformat(),'data_sha256':strategy['data_sha256'],
                     'last_daily_bar':strategy['last_bar'],'simulation_paths':6000,
                     'basis':'Completed TVC daily volatility and current source quote; temporary headline/supply risk assumptions with deterministic shared simulation draws.',
                     'risk_assumptions':analysis['risk_assumptions'],
                     'probability_scope':'Terminal-price categories, not target touches or exact path probabilities. News/supply risk assumptions remain unvalidated.'},
            'validation':validation(strategy['data_sha256']),
            'news':{'coverage':news['coverage'],'sources':news['sources'],'headlines':news['headlines']},
            'data_status':strategy['data_status'],'fundamentals':strategy['fundamentals']}
