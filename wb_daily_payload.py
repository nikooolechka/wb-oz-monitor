"""WB daily search report validation and six-calendar-month history."""
import calendar
import datetime as dt
import math
import hashlib
import hmac
import time

MSK=dt.timezone(dt.timedelta(hours=3))
def today_msk(now=None):
    return (now or dt.datetime.now(MSK)).astimezone(MSK).date()
def yesterday(now=None):
    return today_msk(now)-dt.timedelta(days=1)
def cutoff_date(now=None):
    date=today_msk(now);year,month0=divmod(date.year*12+date.month-1-6,12)
    return dt.date(year,month0+1,min(date.day,calendar.monthrange(year,month0+1)[1]))
def verify_signature(timestamp,signature,body,key,now_epoch=None):
    try:
        if abs((time.time() if now_epoch is None else now_epoch)-int(timestamp))>300:return False
        expected=hmac.new(key.encode(),timestamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature,expected)
    except (ValueError,TypeError,AttributeError):return False
def validate_payload(payload,now=None):
    assert isinstance(payload,dict) and payload.get('marketplace')=='wb','Wrong marketplace'
    assert payload.get('schema_version')==1 and payload.get('status')=='ok','Incomplete WB report'
    date=dt.date.fromisoformat(payload['report_date'])
    assert date==yesterday(now),'WB collection must cover yesterday in Moscow'
    stamp=dt.datetime.fromisoformat(payload['collected_at'])
    assert stamp.tzinfo is not None,'Collection timestamp must include timezone'
    current=(now or dt.datetime.now(MSK)).astimezone(MSK)
    assert -300<(current-stamp).total_seconds()<86400,'Collection timestamp out of range'
    source=payload.get('source',{})
    assert source.get('metric')=='avgPosition','Unexpected position metric'
    selection=payload.get('selection',{})
    assert selection=={'topOrderBy':'openCard','orderBy':{'field':'openCard','mode':'desc'},'limit':5,'includeSubstitutedSKUs':True,'includeSearchTexts':True},'Unexpected report selection'
    products=payload.get('products');assert isinstance(products,list) and products,'Empty catalog'
    assert len(products)<=1000,'Catalog too large'
    articles=set();ids=set()
    for product in products:
        article=product.get('article');sku=product.get('nm_id')
        assert isinstance(article,str) and article and article!='makeup_30','Invalid WB article'
        assert type(sku) is int and sku>0 and sku not in ids and article not in articles,'Duplicate product'
        articles.add(article);ids.add(sku)
        queries=product.get('queries');assert isinstance(queries,list) and len(queries)<=5,'Invalid phrase count'
        assert product.get('status')==('ok' if queries else 'no_queries'),'Incorrect product status'
        texts=set()
        for order,query in enumerate(queries,1):
            text=query.get('text');position=query.get('position')
            assert isinstance(text,str) and text.strip() and len(text)<=5000 and text not in texts,'Invalid phrase'
            texts.add(text)
            assert query.get('rank_in_report')==order,'Phrase order not preserved'
            assert position is None or (type(position) in [int,float] and math.isfinite(position) and position>=1),'Invalid position'
    assert payload.get('products_count')==len(products),'Product count mismatch'
    assert payload.get('queries_count')==sum(len(p['queries']) for p in products),'Query count mismatch'
    return payload
def update_history(history,payload,now=None):
    validate_payload(payload,now)
    assert isinstance(history,dict),'Corrupt WB history'
    cutoff=cutoff_date(now)
    kept={}
    for date,data in history.items():
        parsed=dt.date.fromisoformat(date)
        if parsed>=cutoff:kept[date]=data
    kept[payload['report_date']]=payload
    return kept
