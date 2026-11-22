"""Independently reconcile accepted links and monthly aggregates against source data."""
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime

import pyarrow.dataset as ds
import pyarrow.parquet as pq

from build_initial_merge import (
    ROOT, OUT, REPORTS, MAX_DISTANCE_M, MAX_INSPECTION_AGE_DAYS, SCORED_RESULTS,
    normalize_name, parse_street, parse_google_address, google_block, loose_street,
    valid_coords, distance_m, next_month, canonical,
)


def verify():
    audit=json.loads((REPORTS/'initial_merge_audit.json').read_text())
    assert audit['status']=='complete'
    assert all(audit['acceptance_checks'].values())
    with (REPORTS/'initial_merge_accepted_links.csv').open(encoding='utf-8',newline='') as f:
        links={r['source_place_id']:r for r in csv.DictReader(f)}
    by_street={r['street']:r for r in links.values()}
    by_license={r['license']:r for r in links.values()}
    assert len(links)==len(by_street)==len(by_license)==audit['matching']['accepted_places']
    google={}
    for line in (ROOT/'meta-Illinois.json').open(encoding='utf-8'):
        r=json.loads(line)
        gid=r['gmap_id']
        if gid in links:
            if gid in google: assert canonical(google[gid])==canonical(r)
            google[gid]=r
            link=links[gid]
            address=parse_google_address(r)
            assert address['status']=='parsed'
            assert (address['street'],address['unit'],address['zip'])==(link['street'],link['unit'],link['zip'])
            assert (address['city'],address['state'])==('CHICAGO','IL')
            assert normalize_name(r['name'])==link['normalized_name']
        # Broader than the build's label-based occupancy block: locality mistakes
        # among coordinate-nearby Google records cannot hide a competing place.
        if valid_coords(r.get('latitude'),r.get('longitude')):
            street=google_block(r)
            if street in by_street:
                assert gid==by_street[street]['source_place_id'],('competing_nearby_google_place',gid,street)
    assert set(google)==set(links)

    histories=defaultdict(list)
    by_id={}
    with (ROOT/'Food_Inspections_20261103.csv').open(encoding='utf-8-sig',newline='') as f:
        for r in csv.DictReader(f):
            street=loose_street(r['Address'])
            # Check every city/state label, not only the scoped Chicago rows.
            if street in by_street:
                assert r['License #']==by_street[street]['license'],('competing_inspection_license',r['Inspection ID'])
            if r['License #'] not in by_license: continue
            link=by_license[r['License #']]
            g=google[link['source_place_id']]
            parsed=parse_street(r['Address'])
            assert parsed['status']=='parsed'
            assert (parsed['street'],parsed['unit'])==(link['street'],link['unit'])
            assert (r['City'].strip().upper(),r['State'].strip().upper(),r['Zip'].strip())==('CHICAGO','IL',link['zip'])
            assert r['Facility Type'].strip().upper()=='RESTAURANT'
            assert normalize_name(r['DBA Name'])==normalize_name(g['name'])
            assert not r['AKA Name'] or normalize_name(r['AKA Name'])==normalize_name(g['name'])
            lat,lon=float(r['Latitude']),float(r['Longitude'])
            assert valid_coords(lat,lon)
            assert distance_m(g['latitude'],g['longitude'],lat,lon)<=MAX_DISTANCE_M
            r['_date']=datetime.strptime(r['Inspection Date'],'%m/%d/%Y').date()
            histories[r['License #']].append(r)
            by_id[r['Inspection ID']]=r
    for license_id,history in histories.items():
        link=by_license[license_id]
        assert {r['Inspection ID'] for r in history}==set(json.loads(link['inspection_ids_json']))
        scored=[r['_date'] for r in history if r['Results'] in SCORED_RESULTS]
        assert min(scored).isoformat()==link['observed_first_scored_date']
        assert max(scored).isoformat()==link['observed_last_scored_date']

    scoped=pq.read_table(OUT/'chicago_restaurant_inspections.parquet',columns=['inspection_id','source_place_id']).to_pylist()
    assert {r['inspection_id'] for r in scoped if r['source_place_id']}==set(by_id)
    months=pq.read_table(OUT/'restaurant_month.parquet').to_pylist()
    monthly={(r['source_place_id'],r['month']):r for r in months}
    assert len(monthly)==len(months)
    for r in months:
        raw=by_id[r['inspection_id']]
        assert raw['License #']==r['license']
        assert raw['Results']==r['inspection_result']
        assert raw['Inspection Type']==r['inspection_type']
        assert raw['_date']==r['inspection_date']<r['month']
        eligible_dates=[v['_date'] for v in histories[r['license']] if v['_date']<r['month']]
        assert max(eligible_dates)==r['inspection_date']
        assert eligible_dates.count(r['inspection_date'])==1
        assert (r['month']-r['inspection_date']).days==r['inspection_age_days']<=MAX_INSPECTION_AGE_DAYS
        assert r['observed_first_scored_date']<r['month']
        assert next_month(r['month'])<=r['observed_last_scored_date']

    # Recompute the actual merged aggregates from persisted review observations.
    recomputed=defaultdict(lambda:{'count':0,'sum':0.,'low':0,'text':0})
    dataset=ds.dataset(OUT/'chicago_restaurant_reviews.parquet',format='parquet')
    for batch in dataset.to_batches(columns=['source_place_id','review_date_chicago','rating','has_text','identity_conflict','date_status','user_id_present'],
                                    filter=ds.field('eligible_for_monthly_candidate')==True,batch_size=25000):
        for r in batch.to_pylist():
            key=(r['source_place_id'],r['review_date_chicago'].replace(day=1))
            if key not in monthly: continue
            assert not r['identity_conflict'] and r['date_status']=='valid' and r['user_id_present']
            a=recomputed[key]
            a['count']+=1
            a['sum']+=r['rating']
            a['low']+=r['rating']<=2
            a['text']+=r['has_text']
    assert set(recomputed)==set(monthly)
    for key,a in recomputed.items():
        r=monthly[key]
        assert a['count']==r['review_count']
        assert a['low']==r['low_rating_count']
        assert a['text']==r['text_count']
        assert math.isclose(a['sum']/a['count'],r['mean_rating'],abs_tol=1e-12)
        assert math.isclose(a['low']/a['count'],r['low_rating_share'],abs_tol=1e-12)
    report={'status':'passed','accepted_links_checked':len(links),'source_inspections_checked':len(by_id),
            'monthly_rows_recomputed':len(months),'primary_reviews_recomputed':sum(a['count'] for a in recomputed.values()),
            'first_primary_month':min((r['month'] for r in months),default=None).isoformat() if months else None,
            'last_primary_month':max((r['month'] for r in months),default=None).isoformat() if months else None,
            'primary_month_rows_by_year':dict(sorted(Counter(str(r['month'].year) for r in months).items())),
            'coverage_note':'Months contain observed exported reviews only; 2021 source coverage ends in early September and is partial.',
            'extra_checks':['No competing inspection license at accepted addresses across all city/state labels',
                            'No competing coordinate-nearby Google place, regardless of locality label',
                            'Every linked inspection reconciles to its raw event and complete license history',
                            'Every monthly rating/count/text aggregate recomputed from persisted reviews',
                            'Every prior inspection is unique and latest strictly before its month']}
    (REPORTS/'initial_merge_verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    verify()
