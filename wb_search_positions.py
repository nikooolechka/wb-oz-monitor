"""Collect exactly yesterday's first five WB search queries for every catalog item."""
import datetime as dt
import json
import hashlib
import hmac
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from wb_daily_payload import MSK,yesterday,validate_payload

API='https://seller-analytics-api.wildberries.ru/api/v2/search-report/product/search-texts'
CONTENT='https://content-api.wildberries.ru/content/v2/get/cards/list'
SELECTION={'topOrderBy':'openCard','orderBy':{'field':'openCard','mode':'desc'},'limit':5,'includeSubstitutedSKUs':True,'includeSearchTexts':True}
class ApiError(RuntimeError):
    pass
def request(url,body=None,headers=None):
    data=None if body is None else json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    req=urllib.request.Request(url,data=data,headers={'User-Agent':'ASFarm-WB-DailySearch/1.0','Content-Type':'application/json',**(headers or {})})
    try:
        with urllib.request.urlopen(req,timeout=45) as response:return json.load(response)
    except urllib.error.HTTPError as error:
        retry=error.headers.get('X-Ratelimit-Retry') or error.headers.get('Retry-After') or 'not provided'
        raise ApiError('HTTP '+str(error.code)+'; retry-after='+str(retry)+'; no repeated WB request in this run') from None
def catalog(token):
    body={'settings':{'cursor':{'limit':100},'filter':{'withPhoto':-1}}}
    result={}
    for page in range(10):
        response=request(CONTENT,body,{'Authorization':token})
        cards=response.get('cards');cursor=response.get('cursor')
        if not isinstance(cards,list) or not isinstance(cursor,dict):raise ApiError('Invalid Content API response')
        for card in cards:
            article=card['vendorCode']
            if article=='makeup_30':continue
            if article in result:raise ApiError('Duplicate vendorCode in WB catalog')
            result[article]={'nmID':int(card['nmID']),'name':card.get('title','')}
        if len(cards)<100:break
        body['settings']['cursor']={'limit':100,'updatedAt':cursor['updatedAt'],'nmID':cursor['nmID']}
        time.sleep(1)
    else:raise ApiError('Catalog pagination did not finish')
    if not result:raise ApiError('WB catalog is empty')
    return result
def build_payload(catalog_rows,response,report_date,collected_at,selection=None):
    selection=selection or SELECTION
    items=response.get('data',{}).get('items')
    if not isinstance(items,list):raise ApiError('Missing report items; previous report retained')
    by_id={int(x['nmID']):article for article,x in catalog_rows.items()}
    grouped={article:[] for article in catalog_rows}
    for item in items:
        sku=item.get('nmId')
        if sku not in by_id:raise ApiError('Report contains a product outside requested catalog')
        article=by_id[sku]
        if item.get('vendorCode') and item['vendorCode']!=article:raise ApiError('Report article/SKU mismatch')
        queries=grouped[article]
        position=item.get('avgPosition',{}).get('current')
        if position==0:position=None
        queries.append({'rank_in_report':len(queries)+1,'text':item['text'],'position':position,'open_card':item.get('openCard',{}).get('current')})
    products=[{'article':a,'nm_id':int(c['nmID']),'title':c.get('name',''),'status':'ok' if grouped[a] else 'no_queries','queries':grouped[a]} for a,c in catalog_rows.items()]
    payload={'schema_version':1,'marketplace':'wb','status':'ok','report_date':report_date,'collected_at':collected_at,
        'source':{'report':'Поисковые запросы: ваши товары','metric':'avgPosition','metric_label':'Средняя позиция в поиске','api':'WB Seller Analytics API'},
        'selection':selection,'products_count':len(products),'queries_count':sum(len(x['queries']) for x in products),'products':products}
    validate_payload(payload)
    return payload
def main():
    token=os.environ['WB_TOKEN'].strip();push_url=os.environ['WB_POSITIONS_PUSH_URL'].strip();key=os.environ['WB_POSITIONS_PUSH_KEY'].strip()
    report_date=yesterday().isoformat()
    latest_url=push_url.rsplit('/',1)[0]+'/wb-positions.json'
    rows=catalog(token)
    print('WB catalog verified: '+str(len(rows))+' products')
    latest=None
    try:
        latest=request(latest_url+'?check='+str(time.time_ns()))
        validate_payload(latest)
    except (ApiError,OSError,ValueError,AssertionError):pass
    else:
        # This also repairs an incomplete publication from the same saved response.
        # Authentication or transport errors here must never trigger another WB call.
        publish(latest,push_url,key)
        print('Already collected report '+report_date+'; authenticated publication and archive validated; WB report API was not called');return
    if len(rows)>50:raise ApiError('WB report supports at most 50 items per request; explicit batch design required')
    body={'currentPeriod':{'start':report_date,'end':report_date},'nmIds':[int(x['nmID']) for x in rows.values()],**SELECTION}
    response=request(API,body,{'Authorization':token})
    payload=build_payload(rows,response,report_date,dt.datetime.now(MSK).isoformat())
    destination=Path(os.environ.get('WB_REPORT_OUTPUT','wb-report-result.json'));destination.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    # Publication retries reuse this response, never repeat the rate-limited WB call.
    for attempt in range(3):
        try:
            publish(payload,push_url,key)
            break
        except (ApiError,OSError,AssertionError):
            if attempt==2:raise
            time.sleep(5)
    print('Published '+report_date+': '+str(payload['products_count'])+' products, '+str(payload['queries_count'])+' phrases; readback matched')
def publish(payload,push_url,key):
    validate_payload(payload)
    stamp=str(int(time.time()))
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    signature=hmac.new(key.encode(),stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
    receipt=request(push_url,payload,{'X-WB-Timestamp':stamp,'X-WB-Signature':signature})
    assert receipt.get('ok') and receipt.get('report_date')==payload['report_date'],'Push receipt mismatch'
    latest_url=push_url.rsplit('/',1)[0]+'/wb-positions.json'
    actual=request(latest_url+'?verify='+str(time.time_ns()))
    assert actual==payload,'Published WB report readback mismatch'
    history=request(push_url.rsplit('/',1)[0]+'/wb-history.json?verify='+str(time.time_ns()))
    assert history.get(payload['report_date'])==payload,'Published WB history mismatch'
if __name__=='__main__':
    try:main()
    except Exception as error:
        print(type(error).__name__+': '+str(error),file=sys.stderr);sys.exit(1)
