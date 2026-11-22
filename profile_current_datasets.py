"""Reproducible, streaming inventory and quality audit. Raw inputs are read only.

Run with Python 3.13+: python -u profile_current_datasets.py
Writes aggregate diagnostics to data/reports/current_dataset_profile.json.
No business links are accepted and no modeling dataset is produced.
"""
import csv
import hashlib
import json
import math
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from analyze_merge_inputs import name_key, street_key, metadata_street, distance_m

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'data' / 'reports' / 'current_dataset_profile.json'
REPORT = {'analysis_date': '2026-11-22', 'notes': [
    'Every raw file is scanned in full; rows count successfully parsed records.',
    'Chicago geography is normalized address/city/state, not a municipal boundary test.',
    'Google core: restaurant or category ending in restaurant, bar & grill, diner, sandwich shop, bistro, creperie; cafe/cafeteria separately expanded.',
    'Google metadata duplicate conflicts are excluded from core pending resolution.',
    'Review full-record deduplication and identity conflicts are measured only for the Chicago Google core cohort using SHA-256 fingerprints.',
    'Yelp review, tip, and user duplicate keys/full records are not tested; business and check-in keys are tested.',
    'Dates are reported in UTC for Google; Yelp timestamps are naive as supplied.',
    'Address/name matches are unverified candidates; units are stripped for candidate generation.',
    'Inspection failure rates are event-level, not restaurant-level or causal.',
]}

def save():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(REPORT, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')

def missing(v):
    return v is None or v == '' or v == [] or v == {}

def sig(r):
    return hashlib.sha256(json.dumps(r, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).digest()

def records(filename, audit):
    audit['rows'] = 0
    audit['malformed_rows'] = 0
    audit['fields'] = []
    fields = set()
    with (ROOT / filename).open(encoding='utf-8-sig') as f:
        for line in f:
            try:
                r = json.loads(line)
                if not isinstance(r, dict):
                    raise ValueError('not an object')
            except (ValueError, TypeError):
                audit['malformed_rows'] += 1
                continue
            audit['rows'] += 1
            fields.update(r)
            if audit['rows'] % 2_000_000 == 0:
                print(filename, audit['rows'], flush=True)
            yield r
    audit['fields'] = sorted(fields)

def distribution(values):
    v = sorted(values)
    if not v:
        return {}
    return {'min': v[0], 'p25': v[int((len(v)-1)*.25)], 'median': statistics.median(v),
            'p75': v[int((len(v)-1)*.75)], 'p90': v[int((len(v)-1)*.9)],
            'p99': v[int((len(v)-1)*.99)], 'max': v[-1], 'mean': statistics.mean(v)}

def core_category(cats):
    cats = {name_key(c).lower() for c in cats or []}
    return any(c == 'restaurant' or c.endswith(' restaurant') or c in
               {'bar grill', 'diner', 'sandwich shop', 'bistro', 'creperie'} for c in cats)

def run():
    started = time.monotonic()
    paths = sorted([*ROOT.glob('*.csv'), *ROOT.glob('meta-*.json'), *ROOT.glob('review-*.json'), *ROOT.glob('yelp*.json')])
    REPORT['files'] = {p.name: {'bytes': p.stat().st_size} for p in paths}
    a = {'missing': Counter(), 'duplicate_id_extra_rows': 0, 'identical_duplicate_extra_rows': 0}
    meta, fingerprints, conflicts = {}, {}, set()
    for r in records('meta-Illinois.json', a):
        for k, v in r.items():
            if missing(v): a['missing'][k] += 1
        gid = r.get('gmap_id')
        fingerprint = sig(r)
        if gid in meta:
            a['duplicate_id_extra_rows'] += 1
            if fingerprints[gid] == fingerprint: a['identical_duplicate_extra_rows'] += 1
            else: conflicts.add(gid)
        else:
            meta[gid], fingerprints[gid] = r, fingerprint
    chicago = {g: r for g, r in meta.items() if re.search(r',\s*Chicago,\s*IL\b', r.get('address') or '', re.I)}
    core = {g: r for g, r in chicago.items() if core_category(r.get('category')) and g not in conflicts}
    expanded = {g for g, r in chicago.items() if g not in core and any(name_key(c) in {'CAFE', 'CAFETERIA'} for c in r.get('category') or [])}
    pizza = {g for g, r in chicago.items() if any('pizza' in c.lower() for c in r.get('category') or [])}
    a.update(unique_ids=len(meta), conflicting_ids=len(conflicts), chicago_places=len(chicago), core_restaurants=len(core),
             cafe_cafeteria_expansion=len(expanded), chicago_pizza_places=len(pizza),
             core_categories=Counter(c for r in core.values() for c in r.get('category') or []),
             chicago_categories=Counter(c for r in chicago.values() for c in r.get('category') or []),
             core_permanently_closed=sum(r.get('state') == 'Permanently closed' for r in core.values()),
             core_missing=Counter(k for r in core.values() for k,v in r.items() if missing(v)),
             core_snapshot_rating_distribution=distribution([r['avg_rating'] for r in core.values() if isinstance(r.get('avg_rating'), (float,int))]),
             core_snapshot_review_count_distribution=distribution([r['num_of_reviews'] for r in core.values() if isinstance(r.get('num_of_reviews'), (float,int))]))
    REPORT['google_metadata'] = a
    del fingerprints
    save()
    print('Metadata complete:', len(core), 'Chicago core restaurants', flush=True)

    a = {'rows': 0, 'missing': Counter(), 'duplicate_ids': 0, 'results': Counter(), 'facility_types': Counter(),
         'cities_states': Counter(), 'years': Counter(), 'chicago_rows': 0, 'core_rows': 0, 'core_results': Counter(),
         'core_types': Counter(), 'core_risks': Counter(), 'core_years': Counter(), 'core_year_results': defaultdict(Counter),
         'core_type_results': defaultdict(Counter), 'core_missing': Counter(), 'core_violation_codes_event_counts': Counter()}
    ids, licenses, addresses = set(), set(), set()
    by_address = defaultdict(list)
    license_names, license_addresses = defaultdict(set), defaultdict(set)
    date_min, date_max = '9999', ''
    with (ROOT / 'Food_Inspections_20261103.csv').open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        a['fields'] = reader.fieldnames
        for r in reader:
            a['rows'] += 1
            a['duplicate_ids'] += r['Inspection ID'] in ids
            ids.add(r['Inspection ID'])
            for k,v in r.items():
                if missing(v) or (k == 'License #' and v == '0'): a['missing'][k] += 1
            d = datetime.strptime(r['Inspection Date'], '%m/%d/%Y').date().isoformat()
            date_min, date_max = min(date_min,d), max(date_max,d)
            a['years'][d[:4]] += 1
            a['results'][r['Results']] += 1
            a['facility_types'][r['Facility Type']] += 1
            city, state = r['City'].strip().upper(), r['State'].strip().upper()
            a['cities_states'][city + '|' + state] += 1
            if (city,state) != ('CHICAGO','IL'): continue
            a['chicago_rows'] += 1
            addr, lic = street_key(r['Address']), r['License #']
            aliases = {name_key(r['DBA Name']), name_key(r['AKA Name'])} - {''}
            coords = (float(r['Latitude']),float(r['Longitude'])) if r['Latitude'] and r['Longitude'] else None
            by_address[addr].append((lic, aliases, coords, d, r['Facility Type'].strip().upper()))
            if lic not in ('','0'):
                license_names[(lic,addr)].add(name_key(r['DBA Name']))
                license_addresses[lic].add(addr)
            if r['Facility Type'].strip().upper() != 'RESTAURANT': continue
            a['core_rows'] += 1
            if lic not in ('','0'): licenses.add(lic)
            addresses.add(addr)
            a['core_results'][r['Results']] += 1
            a['core_types'][r['Inspection Type']] += 1
            a['core_risks'][r['Risk']] += 1
            a['core_years'][d[:4]] += 1
            a['core_year_results'][d[:4]][r['Results']] += 1
            a['core_type_results'][r['Inspection Type']][r['Results']] += 1
            for k,v in r.items():
                if missing(v) or (k == 'License #' and v == '0'): a['core_missing'][k] += 1
            codes = set(re.findall(r'(?:^|\|)\s*(\d+)\.', r['Violations']))
            a['core_violation_codes_event_counts'].update(codes)
    a.update(date_min=date_min,date_max=date_max, core_unique_valid_licenses=len(licenses), core_unique_normalized_addresses=len(addresses),
             chicago_license_address_groups_multiple_names=sum(len(v)>1 for v in license_names.values()),
             chicago_licenses_multiple_addresses=sum(len(v)>1 for v in license_addresses.values()))
    REPORT['inspections'] = a
    match, pizza_match = Counter(), Counter()
    exact_ids, first_dates = set(), {}
    for g,r in core.items():
        candidates = by_address.get(metadata_street(r), [])
        exact = [c for c in candidates if name_key(r['name']) in c[1]]
        flags = {'same_address': bool(candidates), 'same_address_multiple_licenses': len({c[0] for c in candidates})>1,
                 'same_address_exact_name': bool(exact), 'same_address_exact_name_core_facility': any(c[4]=='RESTAURANT' for c in exact),
                 'exact_name_one_valid_license': bool(exact) and len({c[0] for c in exact if c[0] not in ('','0')})==1}
        if exact:
            exact_ids.add(g)
            first_dates[g] = min(c[3] for c in exact)
            distances = [distance_m((r['latitude'],r['longitude']), c[2]) for c in exact if c[2] and r.get('latitude') is not None and r.get('longitude') is not None]
            flags['exact_name_nearest_over_100m'] = bool(distances) and min(distances)>100
        match.update({k:int(v) for k,v in flags.items()})
        if g in pizza: pizza_match.update({k:int(v) for k,v in flags.items()})
    REPORT['inspection_candidates'] = {'core':match, 'pizza_within_core':pizza_match, 'denominator':len(core), 'accepted_links':0}
    del by_address, ids
    save()
    print('Inspections and candidates complete', flush=True)

    a = {'missing':Counter(), 'ratings':Counter(), 'flags':Counter()}
    c = {'raw_rows':0, 'unique_rows':0, 'duplicate_extra_rows':0, 'identity_conflicting_extra_rows':0,
         'unique_missing':Counter(), 'unique_ratings':Counter(), 'unique_years_utc':Counter(), 'flags':Counter()}
    seen, identities, reviewed = set(), {}, set()
    per_place = Counter()
    lo,hi,clo,chi = math.inf, -math.inf, math.inf, -math.inf
    for r in records('review-Illinois.json', a):
        g = r.get('gmap_id')
        reviewed.add(g)
        for k in ('gmap_id','user_id','rating','text','time'):
            if missing(r.get(k)) or (k=='text' and not str(r.get(k) or '').strip()): a['missing'][k] += 1
        a['ratings'][str(r.get('rating'))] += 1
        a['flags']['missing_metadata_rows'] += g not in meta
        a['flags']['chicago_rows'] += g in chicago
        a['flags']['pizza_rows'] += g in pizza
        t = r.get('time')
        valid = isinstance(t,(int,float)) and math.isfinite(t) and 0<=t<=4102444800000
        if valid: lo,hi = min(lo,t),max(hi,t)
        else: a['flags']['invalid_time'] += 1
        if g not in core: continue
        c['raw_rows'] += 1
        fingerprint = sig(r)
        if fingerprint in seen:
            c['duplicate_extra_rows'] += 1
            continue
        seen.add(fingerprint)
        c['unique_rows'] += 1
        identity = sig([r.get('user_id'), g, t])
        if not missing(r.get('user_id')):
            if identity in identities and identities[identity] != fingerprint: c['identity_conflicting_extra_rows'] += 1
            identities[identity] = fingerprint
        per_place[g] += 1
        c['unique_ratings'][str(r.get('rating'))] += 1
        for k in ('user_id','rating','text','time','resp','pics'):
            if missing(r.get(k)) or (k=='text' and not str(r.get(k) or '').strip()): c['unique_missing'][k] += 1
        rating_valid = isinstance(r.get('rating'),(int,float)) and r['rating'] in (1,2,3,4,5)
        c['flags']['valid_rating_rows'] += rating_valid
        c['flags']['valid_rating_with_text_rows'] += rating_valid and bool(str(r.get('text') or '').strip())
        c['flags']['exact_name_candidate_reviews'] += g in exact_ids
        if valid:
            clo,chi = min(clo,t),max(chi,t)
            date = datetime.fromtimestamp(t/1000,timezone.utc).date().isoformat()
            c['unique_years_utc'][date[:4]] += 1
            c['flags']['pre_2010_rows'] += date < '2010-01-01'
            c['flags']['reviews_on_or_after_first_exact_candidate_inspection'] += g in first_dates and date >= first_dates[g]
        else: c['flags']['invalid_time'] += 1
    def stamp(t):
        return datetime.fromtimestamp(t/1000,timezone.utc).isoformat() if math.isfinite(t) else None
    a.update(time_min_utc=stamp(lo),time_max_utc=stamp(hi),unique_place_ids=len(reviewed),
             places_absent_metadata=len(reviewed-meta.keys()),metadata_places_without_reviews=len(meta.keys()-reviewed))
    valid_ratings = {int(k):v for k,v in c['unique_ratings'].items() if k in {'1','2','3','4','5'}}
    c.update(time_min_utc=stamp(clo),time_max_utc=stamp(chi),places_with_reviews=len(per_place),
             places_without_reviews=len(core)-len(per_place),per_place_review_counts=distribution(per_place.values()),
             review_weighted_mean_rating=sum(k*v for k,v in valid_ratings.items())/sum(valid_ratings.values()),
             places_under_10_reviews=sum(v<10 for v in per_place.values()),
             top_1pct_places_share_of_reviews=sum(sorted(per_place.values(),reverse=True)[:max(1,math.ceil(len(per_place)*.01))])/sum(per_place.values()),
             export_vs_metadata=Counter('equal' if per_place[g]==r['num_of_reviews'] else 'below' if per_place[g]<r['num_of_reviews'] else 'above' for g,r in core.items()))
    REPORT['google_reviews'], REPORT['google_core_reviews'] = a,c
    del seen, identities, meta, chicago
    save()
    print('Google reviews complete', flush=True)

    a = {'missing':Counter(), 'states':Counter(), 'illinois_cities':Counter(), 'flags':Counter(), 'chicago_city_records':[], 'near_chicago_records':[]}
    business_ids, eligible, illinois = set(),set(),set()
    for r in records('yelp_academic_dataset_business.json',a):
        bid = r.get('business_id')
        a['flags']['duplicate_business_ids'] += bid in business_ids
        business_ids.add(bid)
        for k,v in r.items():
            if missing(v): a['missing'][k] += 1
        state,city = (r.get('state') or '').strip().upper(), (r.get('city') or '').strip().upper()
        cats = {v.strip().lower() for v in (r.get('categories') or '').split(',')}
        restaurant = 'restaurants' in cats
        a['states'][state] += 1
        a['flags']['restaurant_businesses'] += restaurant
        if state == 'IL':
            illinois.add(bid)
            a['illinois_cities'][city] += 1
            a['flags']['illinois_restaurants'] += restaurant
        if city == 'CHICAGO':
            a['chicago_city_records'].append({k:r.get(k) for k in ('name','city','state','latitude','longitude','categories')})
        if state=='IL' and city=='CHICAGO' and restaurant: eligible.add(bid)
        lat,lon=r.get('latitude'),r.get('longitude')
        if isinstance(lat,(int,float)) and isinstance(lon,(int,float)) and 41.64<=lat<=42.03 and -87.94<=lon<=-87.52:
            a['near_chicago_records'].append({k:r.get(k) for k in ('name','city','state','latitude','longitude','categories')})
    a.update(unique_business_ids=len(business_ids),illinois_businesses=len(illinois),eligible_chicago_restaurants=len(eligible),
             geographic_audit='Broad bounding rectangle, not city boundary; includes O Hare area and nearby suburbs.')
    REPORT['yelp_business'] = a
    save()
    print('Yelp business complete', flush=True)

    for kind in ('review','tip','checkin','user'):
        a = {'missing':Counter(), 'flags':Counter(), 'years':Counter(), 'ratings':Counter()}
        dlo,dhi = '9999',''
        referenced_businesses = set()
        for r in records(f'yelp_academic_dataset_{kind}.json',a):
            for k in {'review':('review_id','business_id','user_id','stars','text','date'),
                      'tip':('business_id','user_id','text','date'), 'checkin':('business_id','date'),
                      'user':('user_id','yelping_since','average_stars','review_count')}[kind]:
                if missing(r.get(k)): a['missing'][k] += 1
            if kind!='user':
                bid=r.get('business_id')
                if kind=='checkin': a['flags']['duplicate_business_ids'] += bid in referenced_businesses
                referenced_businesses.add(bid)
                a['flags']['business_id_absent_from_business_file'] += bid not in business_ids
                a['flags']['illinois_business_rows'] += bid in illinois
                a['flags']['eligible_chicago_restaurant_rows'] += bid in eligible
            if kind=='review':
                a['ratings'][str(r.get('stars'))] += 1
                a['flags']['invalid_stars'] += r.get('stars') not in (1,2,3,4,5)
            dates=(r.get('date') or '').split(', ') if kind=='checkin' else [r.get('yelping_since' if kind=='user' else 'date')]
            for d in dates:
                if not d: continue
                try: datetime.fromisoformat(d)
                except (ValueError,TypeError):
                    a['flags']['invalid_dates'] += 1
                    continue
                dlo,dhi = min(dlo,d),max(dhi,d)
                a['years'][d[:4]] += 1
            if kind=='checkin': a['flags']['checkin_events'] += len(dates) if dates!=[''] else 0
            if kind=='user':
                a['flags']['zero_review_count'] += r.get('review_count')==0
                a['flags']['invalid_average_stars'] += not isinstance(r.get('average_stars'),(int,float)) or not 0<=r['average_stars']<=5
        a.update(date_min=dlo if dlo!='9999' else None,date_max=dhi or None)
        if kind!='user': a['unique_referenced_businesses']=len(referenced_businesses)
        REPORT['yelp_'+kind]=a
        save()
        print('Yelp',kind,'complete:',a['rows'],flush=True)
    REPORT['runtime_seconds']=round(time.monotonic()-started,1)
    REPORT['complete']=True
    save()
    print('COMPLETE:',OUT,flush=True)

if __name__=='__main__':
    run()
