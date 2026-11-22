"""Build the conservative Google/Chicago-inspection merge described in merge_plan.md.

Run: python -u build_initial_merge.py
Raw sources are read only. Large outputs are ignored under data/processed/initial_exact/.
Accepted means every documented source-evidence check passed, not independently
verified ownership or proven opening/closing dates. No fuzzy links are accepted.
"""
import argparse
import csv
import hashlib
import json
import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'data' / 'processed' / 'initial_exact'
STAGING = ROOT / 'data' / 'staging' / 'initial_exact'
REPORTS = ROOT / 'data' / 'reports'
CHICAGO = ZoneInfo('America/Chicago')
SCORED_RESULTS = {'Pass', 'Pass w/ Conditions', 'Fail'}
TERMINAL_RESULTS = {'Out of Business', 'Business Not Located'}
MAX_DISTANCE_M = 25.0
MAX_INSPECTION_AGE_DAYS = 365
MIN_REVIEW_DATE = date(2010, 1, 1)
WORDS = dict(zip('NORTH SOUTH EAST WEST NORTHEAST NORTHWEST SOUTHEAST SOUTHWEST STREET AVENUE BOULEVARD ROAD DRIVE PLACE COURT PARKWAY TERRACE LANE HIGHWAY SQUARE'.split(),
                 'N S E W NE NW SE SW ST AVE BLVD RD DR PL CT PKWY TER LN HWY SQ'.split()))
SUFFIXES = {'ST','AVE','BLVD','RD','DR','PL','CT','PKWY','TER','LN','HWY','SQ','WAY'}

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))

def digest(value):
    return hashlib.sha256(canonical(value).encode()).digest()

def normalize_name(value):
    value = unicodedata.normalize('NFKD', value or '')
    value = ''.join(c for c in value if not unicodedata.combining(c)).upper()
    value = value.replace('&', ' AND ').replace("'", '').replace('\u2019', '')
    return ' '.join(re.sub(r'[^A-Z0-9]+', ' ', value).split())

def normalize_street_words(value):
    value = re.sub(r'[.,]', ' ', value.upper())
    return ' '.join(WORDS.get(w,w) for w in value.split())

def parse_street(value):
    """Require a single explicit street, retaining units and house suffixes.

    Ranges and unfamiliar layouts are pending; no guess at their first number.
    """
    raw = (value or '').strip().upper()
    unit = ''
    unit_match = re.search(r'(?:\b(SUITE|STE|UNIT|APT|APARTMENT|FLOOR|FL)\.?\s*|#\s*)([A-Z0-9-]+)\s*$',raw)
    if unit_match:
        kind = 'FLOOR' if unit_match[1] in {'FLOOR','FL'} else 'UNIT'
        unit = kind + ':' + unit_match[2]
        raw = raw[:unit_match.start()].rstrip(' ,')
    base = normalize_street_words(raw)
    if not re.fullmatch(r'\d+[A-Z]? [A-Z0-9 ]+',base):
        return {'street':base, 'unit':unit, 'status':'unparsed_or_range'}
    if base.split()[-1] not in SUFFIXES:
        return {'street':base, 'unit':unit, 'status':'unrecognized_street_suffix'}
    return {'street':base, 'unit':unit, 'status':'parsed'}

def parse_google_address(record):
    address = record.get('address') or ''
    m = re.fullmatch(r'(.*),\s*([^,]+),\s*([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?(?:,\s*(?:USA|United States))?',address.strip(),re.I)
    if not m:
        return {'street':'','unit':'','city':'','state':'','zip':'','status':'unparsed_locality'}
    prefix,city,state,zip_code=m.groups()
    business_prefix=(record.get('name') or '')+', '
    if prefix.startswith(business_prefix): prefix=prefix[len(business_prefix):]
    # Additional building prefixes are explicitly pending, never discarded silently.
    street=parse_street(prefix)
    if ',' in prefix and street['status']=='parsed': street['status']='unparsed_building_or_unit_layout'
    return {**street,'city':city.strip().upper(),'state':state.upper(),'zip':zip_code}

def loose_street(value):
    """Blocking only; never sufficient to accept a link."""
    raw=re.sub(r'\b(?:SUITE|STE|UNIT|APT|APARTMENT|FLOOR|FL)\b.*$|#.*$', '', (value or '').upper())
    return normalize_street_words(raw)

def google_block(record):
    value=record.get('address') or ''
    prefix=(record.get('name') or '')+', '
    if value.startswith(prefix): value=value[len(prefix):]
    parts=[p.strip() for p in value.split(',')]
    return loose_street(next((p for p in parts if re.match(r'^\d',p)),parts[0]))

def range_description(value):
    m=re.match(r'^(\d+)\s*-\s*(\d+)\s+(.+)$',value)
    if not m: return None
    start,end=int(m[1]),int(m[2])
    # E.g. 1200-08: expand the abbreviated end, conservatively blocking the span.
    if end<start and len(m[2])<len(m[1]):
        end=int(m[1][:-len(m[2])]+m[2])
        if end<start: end+=10**len(m[2])
    return (start,end,m[3]) if start<=end else None

def overlaps_range(street, ranges):
    m=re.fullmatch(r'(\d+)[A-Z]? (.+)',street)
    return bool(m and any(lo<=int(m[1])<=hi for lo,hi in ranges.get(m[2],())))

def core_category(categories):
    cats={normalize_name(c).lower() for c in categories or []}
    return any(c=='restaurant' or c.endswith(' restaurant') or c in
               {'bar and grill','diner','sandwich shop','bistro','creperie'} for c in cats)

def valid_coords(lat,lon):
    return isinstance(lat,(int,float)) and isinstance(lon,(int,float)) and math.isfinite(lat) and math.isfinite(lon) and 41.64<=lat<=42.03 and -87.94<=lon<=-87.52

def distance_m(lat1,lon1,lat2,lon2):
    a,b,c,d=map(math.radians,(lat1,lon1,lat2,lon2))
    h=math.sin((c-a)/2)**2+math.cos(a)*math.cos(c)*math.sin((d-b)/2)**2
    return 6371000*2*math.asin(min(1,math.sqrt(h)))

def evaluate_candidate(place,history,address_licenses,google_ids,range_overlap=False):
    """Return all rejection reasons and evidence; test the entire license history."""
    reasons=[]
    p=place['_address']
    expected=normalize_name(place.get('name'))
    if p['status']!='parsed': reasons.append('google_address_not_strictly_parsed')
    if (p['city'],p['state'])!=('CHICAGO','IL'): reasons.append('google_locality_not_exact')
    if not expected: reasons.append('missing_google_name')
    if len(address_licenses)!=1 or address_licenses & {'','0'}: reasons.append('multiple_or_invalid_licenses_at_address')
    if google_ids!={place['gmap_id']}: reasons.append('competing_google_place_at_address')
    if range_overlap: reasons.append('overlapping_house_number_range')
    if not valid_coords(place.get('latitude'),place.get('longitude')): reasons.append('google_coordinates_missing_or_outside_screen')
    distances=[]
    for r in history:
        if r['address_status']!='parsed': reasons.append('inspection_address_not_strictly_parsed')
        if (r['street'],r['unit'])!=(p['street'],p['unit']): reasons.append('license_address_or_unit_changes')
        if (r['city'],r['state'],r['zip'])!=('CHICAGO','IL',p['zip']): reasons.append('inspection_locality_or_zip_conflict')
        if r['facility']!='RESTAURANT': reasons.append('license_has_noncore_facility_history')
        if normalize_name(r['dba'])!=expected: reasons.append('dba_name_not_exact_throughout_history')
        if r['aka'] and normalize_name(r['aka'])!=expected: reasons.append('aka_name_conflicts')
        if not valid_coords(r['latitude'],r['longitude']): reasons.append('inspection_coordinates_missing_or_outside_screen')
        elif valid_coords(place.get('latitude'),place.get('longitude')):
            distances.append(distance_m(place['latitude'],place['longitude'],r['latitude'],r['longitude']))
    if distances and max(distances)>MAX_DISTANCE_M: reasons.append('coordinate_distance_exceeds_25m')
    scored=sorted(r['date'] for r in history if r['result'] in SCORED_RESULTS)
    first,last=(scored[0],scored[-1]) if scored else (None,None)
    if not scored: reasons.append('no_scored_inspections')
    if last and any(r['result'] in TERMINAL_RESULTS and r['date']<last for r in history):
        reasons.append('terminal_status_precedes_later_scored_inspection')
    return sorted(set(reasons)),{'distance_min_m':min(distances) if distances else None,
        'distance_max_m':max(distances) if distances else None,'observed_first_scored_date':first,
        'observed_last_scored_date':last,'inspection_events':len(history)}

def next_month(month):
    return date(month.year+(month.month==12),1 if month.month==12 else month.month+1,1)

def choose_prior_inspection(history,month,first,last,max_age_days=MAX_INSPECTION_AGE_DAYS):
    """Do not skip an ambiguous/non-scored latest event to find a nicer outcome."""
    if not first or not last or month<=first or next_month(month)>last:
        return None,'month_not_bracketed_by_scored_observations'
    before=[r for r in history if r['date']<month]
    if not before: return None,'no_prior_inspection'
    latest=max(r['date'] for r in before)
    candidates=[r for r in before if r['date']==latest]
    if len(candidates)!=1: return None,'latest_inspection_same_day_tie'
    chosen=candidates[0]
    if chosen['result'] not in SCORED_RESULTS: return None,'latest_prior_event_not_scored'
    if (month-latest).days>max_age_days: return None,'prior_inspection_older_than_365_days'
    return chosen,'included'

class ReviewDeduper:
    def __init__(self):
        self.records=set()
        self.identities={}
        self.conflicts=set()

    def add(self,record):
        fingerprint=digest(record)
        if fingerprint in self.records: return None,None
        self.records.add(fingerprint)
        identity=None
        if record.get('user_id') not in (None,''):
            identity=digest([str(record['user_id']),record.get('gmap_id'),record.get('time')])
            old=self.identities.get(identity)
            if old is not None and old!=fingerprint: self.conflicts.add(identity)
            self.identities[identity]=fingerprint
        return fingerprint,identity

def review_time(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value): return None,None
    try:
        dt=datetime.fromtimestamp(value/1000,timezone.utc)
        return dt,dt.astimezone(CHICAGO).date()
    except (ValueError,OverflowError,OSError): return None,None

class ParquetSink:
    def __init__(self,path,schema,batch_size=25000):
        self.path=path
        self.schema=schema
        self.rows=[]
        self.batch_size=batch_size
        self.count=0
        self.writer=pq.ParquetWriter(path,schema,compression='zstd')

    def add(self,row):
        self.rows.append(row)
        if len(self.rows)>=self.batch_size: self.flush()

    def flush(self):
        if self.rows:
            self.writer.write_table(pa.Table.from_pylist(self.rows,schema=self.schema))
            self.count+=len(self.rows)
            self.rows.clear()

    def close(self):
        self.flush()
        self.writer.close()

def schema(strings=(),ints=(),floats=(),bools=(),dates=()):
    return pa.schema([(k,pa.string()) for k in strings]+[(k,pa.int64()) for k in ints]+
                     [(k,pa.float64()) for k in floats]+[(k,pa.bool_()) for k in bools]+[(k,pa.date32()) for k in dates])

PLACE_SCHEMA=schema(strings=('source','source_place_id','place_name','raw_address','street','unit','city','geographic_state','zip','geography_status',
    'restaurant_scope','scope_reason','match_status','match_reasons','restaurant_id','business_episode_id','license','categories_json','operating_status','metadata_json'),
    ints=('snapshot_num_reviews',),floats=('latitude','longitude','snapshot_avg_rating'))
REVIEW_SCHEMA=schema(strings=('review_id','identity_key','source','source_place_id','restaurant_id','business_episode_id','user_id','reviewer_name','text',
    'pics_json','response_json','raw_time_json','date_status','eligibility_reason'),ints=('time_ms',),floats=('rating',),
    bools=('rating_valid','has_text','user_id_present','identity_conflict','has_accepted_link','eligible_for_monthly_candidate'),dates=('review_date_chicago',))
REVIEW_SCHEMA=REVIEW_SCHEMA.append(pa.field('review_time_utc',pa.timestamp('ms',tz='UTC')))
INSPECTION_SCHEMA=schema(strings=('inspection_id','license','dba_name','aka_name','raw_address','street','unit','city','state','zip','facility_type','risk','inspection_type',
    'result','violations','source_place_id','restaurant_id','business_episode_id','match_status'),floats=('latitude','longitude'),dates=('inspection_date',))
MONTH_SCHEMA=schema(strings=('restaurant_id','business_episode_id','source','source_place_id','place_name','street','unit','zip','license','inspection_id','inspection_result',
    'inspection_type','inspection_risk','match_method'),ints=('review_count','text_count','low_rating_count','inspection_age_days'),
    floats=('mean_rating','low_rating_share','distance_max_m'),dates=('month','inspection_date','observed_first_scored_date','observed_last_scored_date'))

def write_json(path,value):
    path.write_text(json.dumps(value,indent=2,sort_keys=True,default=lambda x:x.isoformat() if isinstance(x,(date,datetime)) else str(x))+'\n',encoding='utf-8')

def read_jsonl(path,audit):
    h=hashlib.sha256()
    rows=0
    with path.open('rb') as f:
        for line in f:
            h.update(line)
            r=json.loads(line)
            if not isinstance(r,dict): raise ValueError(f'Nonobject in {path.name}: {rows+1}')
            rows+=1
            yield r
    audit[path.name]={'bytes':path.stat().st_size,'sha256':h.hexdigest(),'rows':rows}

def float_or_none(value):
    try: return float(value) if value not in ('',None) else None
    except (ValueError,TypeError): return None

def run(match_only=False):
    started=time.monotonic()
    for path in (OUT,STAGING,REPORTS): path.mkdir(parents=True,exist_ok=True)
    audit={'status':'building','version':'initial_exact_v1','sources':{},'parameters':{
        'max_coordinate_distance_m':MAX_DISTANCE_M,'maximum_prior_inspection_age_days':MAX_INSPECTION_AGE_DAYS,
        'earliest_review_date_for_monthly':MIN_REVIEW_DATE.isoformat(),'timezone':'America/Chicago',
        'monthly_window':'Entire month strictly after first scored observation and ending no later than last scored observation',
        'acceptance':'Exact name, street, unit, ZIP; one place and license at location; whole-license stable history; all coordinates within threshold',
        'ownership_verification':'Source corroboration only; no independent ownership or opening/closing-date claim',
        'geography':'Chicago/IL address labels plus broad coordinate sanity rectangle; no municipal boundary polygon',
        'threshold_status':'Conservative fixed policies, not statistically calibrated confidence guarantees'},
        'notes':['Candidate-reason counts overlap. Primary cohort has no fuzzy links.',
                 'Observed dates bound primary analysis conservatively; they are not proven business validity dates.',
                 'Review deduplication stores fingerprints across the full scoped stream; review payloads are batched.',
                 'All source dates remain in scoped tables; only eligible observations enter restaurant_month.',
                 'Monthly rows require observed reviews; zero-review months are not synthesized.',
                 'eligible_for_monthly_candidate flags review eligibility before month-level checks; join monthly_eligibility_audit for final inclusion.',
                 'Whole-history identity screening and bracketing use later observations for retrospective cohort selection; this is not a prospective training cohort.']}
    write_json(REPORTS/'initial_merge_audit.json',audit)
    metadata,hashes,conflicts={}, {}, set()
    google_addresses=defaultdict(set)
    ranges=defaultdict(set)
    duplicate_rows=0
    for r in read_jsonl(ROOT/'meta-Illinois.json',audit['sources']):
        gid=r.get('gmap_id')
        if not isinstance(gid,str) or not gid: raise ValueError('Metadata has missing/non-string ID')
        h=digest(r)
        if gid in hashes:
            duplicate_rows+=1
            if hashes[gid]!=h: conflicts.add(gid)
        else: metadata[gid],hashes[gid]=r,h
        if re.search(r',\s*Chicago,\s*IL\b',r.get('address') or '',re.I):
            block=google_block(r)
            google_addresses[block].add(gid)
            parsed=parse_google_address(r)
            if parsed['status']=='parsed': google_addresses[parsed['street']].add(gid)
            span=range_description(block)
            if span: ranges[span[2]].add(span[:2])
    del hashes
    places={}
    scope_counts=Counter()
    for gid,r in metadata.items():
        r['_address']=parse_google_address(r)
        is_chicago=bool(re.search(r',\s*Chicago,\s*IL\b',r.get('address') or '',re.I))
        core=core_category(r.get('category'))
        expansion=any(normalize_name(v) in {'CAFE','CAFETERIA'} for v in r.get('category') or [])
        scope='pending' if gid in conflicts else 'core' if core else 'expanded' if expansion else 'excluded'
        r['_scope']=scope
        r['_is_chicago']=is_chicago
        scope_counts[('chicago_' if is_chicago else 'other_')+scope]+=1
        if is_chicago and scope=='core': places[gid]=r
    audit['metadata']={'unique_places':len(metadata),'duplicate_id_extra_rows':duplicate_rows,'conflicting_ids':len(conflicts),
                       'scope_counts':scope_counts,'core_chicago_places':len(places)}
    print('Scoped Google restaurants:',len(places),flush=True)

    histories=defaultdict(list)
    address_licenses=defaultdict(set)
    inspection_ids=set()
    inspection_scope=Counter()
    scope_sink=ParquetSink(OUT/'inspection_scope_audit.parquet',schema(strings=('inspection_id','license','city','state','facility_type','scope','reason')))
    with (ROOT/'Food_Inspections_20261103.csv').open(encoding='utf-8-sig',newline='') as f:
        for raw in csv.DictReader(f):
            iid=raw['Inspection ID']
            if iid in inspection_ids: raise ValueError(f'Duplicate inspection ID: {iid}')
            inspection_ids.add(iid)
            addr=parse_street(raw['Address'])
            r={'inspection_id':iid,'license':raw['License #'],'dba':raw['DBA Name'],'aka':raw['AKA Name'],'raw_address':raw['Address'],
               'street':addr['street'],'unit':addr['unit'],'address_status':addr['status'],'city':raw['City'].strip().upper(),
               'state':raw['State'].strip().upper(),'zip':raw['Zip'].strip(),'facility':raw['Facility Type'].strip().upper(),
               'date':datetime.strptime(raw['Inspection Date'],'%m/%d/%Y').date(),'result':raw['Results'],'type':raw['Inspection Type'],
               'risk':raw['Risk'],'latitude':float_or_none(raw['Latitude']),'longitude':float_or_none(raw['Longitude'])}
            histories[r['license']].append(r)
            chi=(r['city'],r['state'])==('CHICAGO','IL')
            if chi:
                block=loose_street(raw['Address'])
                address_licenses[block].add(r['license'])
                if addr['status']=='parsed': address_licenses[addr['street']].add(r['license'])
                span=range_description(block)
                if span: ranges[span[2]].add(span[:2])
            reason='core_restaurant' if chi and r['facility']=='RESTAURANT' else 'non_chicago_or_uncertain_locality' if not chi else 'facility_requires_review' if not r['facility'] or 'REST' in r['facility'] else 'nonrestaurant_facility'
            inspection_scope[reason]+=1
            scope_sink.add({'inspection_id':iid,'license':r['license'],'city':r['city'],'state':r['state'],
                            'facility_type':raw['Facility Type'],'scope':'core' if reason=='core_restaurant' else 'pending' if reason=='facility_requires_review' else 'excluded','reason':reason})
    scope_sink.close()
    with (ROOT/'Food_Inspections_20261103.csv').open('rb') as f:
        audit['sources']['Food_Inspections_20261103.csv']={'rows':len(inspection_ids),'bytes':f.seek(0,2)}
        f.seek(0)
        audit['sources']['Food_Inspections_20261103.csv']['sha256']=hashlib.file_digest(f,'sha256').hexdigest()
    audit['inspections']={'scope_counts':inspection_scope,'unique_inspection_ids':len(inspection_ids)}
    del inspection_ids

    crosswalk=[]
    accepted={}
    place_reasons={}
    reason_counts=Counter()
    for gid,p in sorted(places.items()):
        street=p['_address']['street'] if p['_address']['status']=='parsed' else google_block(p)
        candidates=address_licenses.get(street,set())
        all_reasons=set()
        for lic in sorted(candidates) or ['']:
            h=histories.get(lic,[]) if lic else []
            if h:
                reasons,evidence=evaluate_candidate(p,h,candidates,google_addresses.get(street,set()),overlaps_range(street,ranges))
            else:
                reasons=['no_same_address_license_candidate']
                evidence={'distance_min_m':None,'distance_max_m':None,'observed_first_scored_date':None,'observed_last_scored_date':None,'inspection_events':0}
            all_reasons.update(reasons)
            exact_here=any(normalize_name(p['name']) in {normalize_name(v['dba']),normalize_name(v['aka'])} and v['street']==street for v in h)
            decision='accepted_exact' if not reasons else 'unmatched' if not h else 'pending' if exact_here else 'rejected'
            restaurant_id='google:'+gid if decision=='accepted_exact' else None
            episode_id='episode:'+hashlib.sha256((gid+'|'+lic+'|'+street).encode()).hexdigest()[:24] if restaurant_id else None
            row={'source':'google','source_place_id':gid,'restaurant_id':restaurant_id,'business_episode_id':episode_id,'license':lic,
                 'google_name':p['name'],'google_address':p.get('address'),'normalized_name':normalize_name(p['name']),
                 'street':street,'unit':p['_address']['unit'],'zip':p['_address']['zip'],
                 'inspection_dba_names_json':canonical(sorted({v['dba'] for v in h})),
                 'inspection_aka_names_json':canonical(sorted({v['aka'] for v in h})),
                 'inspection_addresses_json':canonical(sorted({v['raw_address'] for v in h})),
                 'inspection_ids_json':canonical(sorted(v['inspection_id'] for v in h)),
                 'google_latitude':p.get('latitude'),'google_longitude':p.get('longitude'),
                 'candidate_license_count':len(candidates),'google_places_at_address':len(google_addresses.get(street,set())),
                 'name_similarity':1.0 if exact_here else None,'method':'exact_full_history_25m_v1',
                 'decision':decision,'reasons':';'.join(reasons),'business_valid_from':None,'business_valid_to':None,
                 'date_evidence_note':'Observed scored-inspection span only; not proven operating/ownership dates',**evidence}
            crosswalk.append(row)
            if not reasons:
                assert gid not in accepted
                accepted[gid]=row
        place_reasons[gid]=';'.join(sorted(all_reasons))
        reason_counts.update(all_reasons)
    license_to_place={v['license']:g for g,v in accepted.items()}
    assert len(license_to_place)==len(accepted),'Competing accepted source places claim a license'
    with (OUT/'restaurant_inspection_crosswalk.csv').open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(crosswalk[0]))
        writer.writeheader()
        writer.writerows(crosswalk)
    with (REPORTS/'initial_merge_accepted_links.csv').open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(crosswalk[0]))
        writer.writeheader()
        writer.writerows(accepted.values())
    audit['matching']={'candidate_rows':len(crosswalk),'accepted_places':len(accepted),'unaccepted_core_places':len(places)-len(accepted),
        'candidate_decisions':Counter(v['decision'] for v in crosswalk),'place_reason_flags':reason_counts,
        'accepted_inspection_events':sum(v['inspection_events'] for v in accepted.values())}
    print('Exact accepted links:',len(accepted),'of',len(places),flush=True)
    write_json(REPORTS/'initial_merge_audit.json',audit)

    place_sink=ParquetSink(OUT/'chicago_restaurants.parquet',PLACE_SCHEMA)
    scope_sink=ParquetSink(OUT/'place_scope_audit.parquet',PLACE_SCHEMA.remove(PLACE_SCHEMA.get_field_index('metadata_json')))
    for gid,p in metadata.items():
        a=p['_address']
        link=accepted.get(gid,{})
        row={'source':'google','source_place_id':gid,'place_name':p['name'],'raw_address':p.get('address'),
             'street':a['street'],'unit':a['unit'],'city':a['city'],'geographic_state':a['state'],'zip':a['zip'],
             'geography_status':'chicago_address_parsed' if p['_is_chicago'] and a['status']=='parsed' else 'chicago_address_pending_parse' if p['_is_chicago'] else 'outside_or_unknown',
             'restaurant_scope':p['_scope'],'scope_reason':'category_rule_v1' if gid not in conflicts else 'conflicting_metadata_id',
             'match_status':'accepted_exact' if link else 'pending_or_unmatched' if gid in places else 'not_in_core',
             'match_reasons':place_reasons.get(gid,''),'restaurant_id':link.get('restaurant_id'),'business_episode_id':link.get('business_episode_id'),
             'license':link.get('license'),'categories_json':canonical(p.get('category')),'operating_status':p.get('state'),
             'metadata_json':canonical({k:v for k,v in p.items() if not k.startswith('_')}),
             'snapshot_num_reviews':p.get('num_of_reviews'),'latitude':p.get('latitude'),'longitude':p.get('longitude'),'snapshot_avg_rating':p.get('avg_rating')}
        scope_sink.add(row)
        if gid in places: place_sink.add(row)
    place_sink.close()
    scope_sink.close()

    inspection_sink=ParquetSink(OUT/'chicago_restaurant_inspections.parquet',INSPECTION_SCHEMA)
    with (ROOT/'Food_Inspections_20261103.csv').open(encoding='utf-8-sig',newline='') as f:
        for r in csv.DictReader(f):
            if (r['City'].strip().upper(),r['State'].strip().upper(),r['Facility Type'].strip().upper())!=('CHICAGO','IL','RESTAURANT'): continue
            gid=license_to_place.get(r['License #'])
            link=accepted.get(gid,{})
            addr=parse_street(r['Address'])
            inspection_sink.add({'inspection_id':r['Inspection ID'],'license':r['License #'],'dba_name':r['DBA Name'],'aka_name':r['AKA Name'],
                'raw_address':r['Address'],'street':addr['street'],'unit':addr['unit'],'city':r['City'],'state':r['State'],'zip':r['Zip'],
                'facility_type':r['Facility Type'],'risk':r['Risk'],'inspection_type':r['Inspection Type'],'result':r['Results'],'violations':r['Violations'],
                'source_place_id':gid,'restaurant_id':link.get('restaurant_id'),'business_episode_id':link.get('business_episode_id'),
                'match_status':'accepted_exact' if link else 'unmatched','latitude':float_or_none(r['Latitude']),'longitude':float_or_none(r['Longitude']),
                'inspection_date':datetime.strptime(r['Inspection Date'],'%m/%d/%Y').date()})
    inspection_sink.close()

    yelp_counts=Counter()
    for r in read_jsonl(ROOT/'yelp_academic_dataset_business.json',audit['sources']):
        is_chicago=(str(r.get('city','')).strip().upper(),str(r.get('state','')).strip().upper())==('CHICAGO','IL')
        is_restaurant='restaurants' in {s.strip().lower() for s in (r.get('categories') or '').split(',')}
        yelp_counts['eligible_chicago_restaurants']+=is_chicago and is_restaurant
        yelp_counts['coordinates_inside_broad_chicago_rectangle']+=valid_coords(r.get('latitude'),r.get('longitude'))
    audit['yelp']={**yelp_counts,'dependent_files_scanned':False,'reason':'No Chicago restaurant cohort; primary source is Google'}
    if yelp_counts['eligible_chicago_restaurants'] or yelp_counts['coordinates_inside_broad_chicago_rectangle']:
        raise ValueError('Yelp coverage changed; resolve geographic candidates before using the Google-only pipeline')
    if match_only:
        audit['status']='matching_only'
        write_json(REPORTS/'initial_merge_audit.json',audit)
        return

    # Discard statewide metadata payloads before streaming the large review file.
    core_ids=set(places)
    place_names={g:p['name'] for g,p in places.items()}
    del metadata, places, crosswalk, google_addresses, address_licenses
    deduper=ReviewDeduper()
    review_counts=Counter()
    stage_sink=ParquetSink(STAGING/'deduplicated_reviews.parquet',REVIEW_SCHEMA)
    all_time_min,all_time_max=None,None
    for record in read_jsonl(ROOT/'review-Illinois.json',audit['sources']):
        review_counts['input_rows']+=1
        gid=record.get('gmap_id')
        if gid not in core_ids:
            review_counts['outside_core_rows']+=1
            continue
        review_counts['scoped_raw_rows']+=1
        fingerprint,identity=deduper.add(record)
        if fingerprint is None:
            review_counts['exact_duplicate_extra_rows']+=1
            continue
        dt,day=review_time(record.get('time'))
        date_status='invalid_timestamp' if day is None else 'before_analysis_coverage' if day<MIN_REVIEW_DATE else 'valid'
        rating=record.get('rating')
        rating_valid=not isinstance(rating,bool) and isinstance(rating,(int,float)) and rating in (1,2,3,4,5)
        text=record.get('text')
        has_text=isinstance(text,str) and bool(text.strip())
        link=accepted.get(gid,{})
        # A source-future time cannot enter the primary table; no timestamp is rewritten.
        if day and day>date(2026,11,22): date_status='after_analysis_date'
        if dt:
            all_time_min=min(all_time_min or dt,dt)
            all_time_max=max(all_time_max or dt,dt)
        stage_sink.add({'review_id':fingerprint.hex(),'identity_key':identity.hex() if identity else None,'source':'google','source_place_id':gid,
            'restaurant_id':link.get('restaurant_id'),'business_episode_id':link.get('business_episode_id'),
            'user_id':str(record['user_id']) if record.get('user_id') not in (None,'') else None,'reviewer_name':record.get('name'),
            'text':text,'pics_json':canonical(record.get('pics')),'response_json':canonical(record.get('resp')),
            'raw_time_json':canonical(record.get('time')),'date_status':date_status,'eligibility_reason':None,
            'time_ms':int(record['time']) if dt else None,'rating':float(rating) if isinstance(rating,(int,float)) and math.isfinite(rating) else None,
            'rating_valid':rating_valid,'has_text':has_text,'user_id_present':record.get('user_id') not in (None,''),
            'identity_conflict':False,'has_accepted_link':bool(link),'eligible_for_monthly_candidate':False,'review_date_chicago':day,'review_time_utc':dt})
        if review_counts['scoped_raw_rows']%250000==0: print('Scoped review rows:',review_counts['scoped_raw_rows'],flush=True)
    stage_sink.close()
    review_counts['deduplicated_scoped_rows']=stage_sink.count
    conflicts_hex={h.hex() for h in deduper.conflicts}
    audit['reviews']={'counts':review_counts,'conflicting_identity_groups':len(conflicts_hex),'time_min_utc':all_time_min,'time_max_utc':all_time_max}
    del deduper
    print('Review source scanned; applying conflict and temporal exclusions',flush=True)
    write_json(REPORTS/'initial_merge_audit.json',audit)

    monthly=defaultdict(lambda:{'count':0,'rating_sum':0.0,'low':0,'text':0})
    review_sink=ParquetSink(OUT/'chicago_restaurant_reviews.parquet',REVIEW_SCHEMA)
    eligibility=Counter()
    diagnostic_flags=Counter()
    reviews_per_place=Counter()
    monthly_candidate_reviews=0
    for batch in pq.ParquetFile(STAGING/'deduplicated_reviews.parquet').iter_batches(batch_size=25000):
        for row in batch.to_pylist():
            gid=row['source_place_id']
            reviews_per_place[gid]+=1
            conflict=row['identity_key'] in conflicts_hex
            row['identity_conflict']=conflict
            diagnostic_flags['identity_conflict_rows']+=conflict
            diagnostic_flags['missing_user_rows']+=not row['user_id_present']
            diagnostic_flags['invalid_rating_rows']+=not row['rating_valid']
            diagnostic_flags['invalid_or_excluded_date_rows']+=row['date_status']!='valid'
            diagnostic_flags['without_text_rows']+=not row['has_text']
            diagnostic_flags['accepted_link_rows']+=row['has_accepted_link']
            reason='eligible'
            if not row['has_accepted_link']: reason='no_accepted_business_link'
            elif conflict: reason='conflicting_review_identity'
            elif not row['user_id_present']: reason='missing_user_id'
            elif not row['rating_valid']: reason='invalid_or_missing_rating'
            elif row['date_status']!='valid': reason=row['date_status']
            if reason=='eligible':
                link=accepted[gid]
                day=row['review_date_chicago']
                if not link['observed_first_scored_date']<=day<=link['observed_last_scored_date']:
                    reason='outside_observed_scored_inspection_span'
            row['eligibility_reason']=reason
            row['eligible_for_monthly_candidate']=reason=='eligible'
            eligibility[reason]+=1
            if reason=='eligible':
                month=row['review_date_chicago'].replace(day=1)
                agg=monthly[(gid,month)]
                agg['count']+=1
                agg['rating_sum']+=row['rating']
                agg['low']+=row['rating']<=2
                agg['text']+=row['has_text']
                monthly_candidate_reviews+=1
            review_sink.add(row)
    review_sink.close()
    audit['reviews'].update(eligibility_first_exclusion=eligibility,overlapping_diagnostic_flags=diagnostic_flags,
        places_with_reviews=len(reviews_per_place),matched_places_with_reviews=sum(g in accepted for g in reviews_per_place),
        unmatched_places_with_reviews=sum(g not in accepted for g in reviews_per_place))

    month_sink=ParquetSink(OUT/'restaurant_month.parquet',MONTH_SCHEMA)
    month_counts=Counter()
    final_places=set()
    final_review_count=0
    month_audit=[]
    for (gid,month),agg in sorted(monthly.items()):
        link=accepted[gid]
        chosen,reason=choose_prior_inspection(histories[link['license']],month,link['observed_first_scored_date'],link['observed_last_scored_date'])
        month_counts[reason]+=1
        month_audit.append({'source_place_id':gid,'restaurant_id':link['restaurant_id'],'month':month,'decision':reason,'review_count':agg['count']})
        if chosen is None: continue
        final_places.add(gid)
        final_review_count+=agg['count']
        month_sink.add({'restaurant_id':link['restaurant_id'],'business_episode_id':link['business_episode_id'],'source':'google',
            'source_place_id':gid,'place_name':place_names[gid],'street':link['street'],'unit':link['unit'],'zip':link['zip'],'license':link['license'],
            'month':month,'review_count':agg['count'],'mean_rating':agg['rating_sum']/agg['count'],'low_rating_count':agg['low'],
            'low_rating_share':agg['low']/agg['count'],'text_count':agg['text'],'inspection_id':chosen['inspection_id'],
            'inspection_date':chosen['date'],'inspection_result':chosen['result'],'inspection_type':chosen['type'],'inspection_risk':chosen['risk'],
            'inspection_age_days':(month-chosen['date']).days,'match_method':link['method'],'distance_max_m':link['distance_max_m'],
            'observed_first_scored_date':link['observed_first_scored_date'],'observed_last_scored_date':link['observed_last_scored_date']})
    month_sink.close()
    with (OUT/'monthly_eligibility_audit.csv').open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=('source_place_id','restaurant_id','month','decision','review_count'))
        writer.writeheader()
        writer.writerows(month_audit)
    # A small, spreadsheet-friendly copy of the primary merged dataset.
    import pyarrow.csv as arrow_csv
    arrow_csv.write_csv(pq.read_table(OUT/'restaurant_month.parquet'),OUT/'restaurant_month.csv')
    audit['monthly']={'candidate_months':len(monthly),'decisions':month_counts,'included_months':month_sink.count,
        'included_restaurants':len(final_places),'included_reviews':final_review_count,'candidate_reviews':monthly_candidate_reviews,
        'accepted_restaurants_without_primary_months':len(accepted)-len(final_places)}
    audit['coverage']={'matched_places':len(accepted),'unmatched_places':len(core_ids)-len(accepted),
        'matched_inspections':sum(v['inspection_events'] for v in accepted.values()),
        'unmatched_inspections':inspection_sink.count-sum(v['inspection_events'] for v in accepted.values()),
        'matched_reviews':diagnostic_flags['accepted_link_rows'],'unmatched_reviews':review_sink.count-diagnostic_flags['accepted_link_rows']}

    # Acceptance checks on persisted output, including foreign keys and join grain.
    checks={}
    places_out=pq.read_table(OUT/'chicago_restaurants.parquet',columns=['source_place_id']).column(0).to_pylist()
    inspection_out=pq.read_table(OUT/'chicago_restaurant_inspections.parquet',columns=['inspection_id','source_place_id']).to_pydict()
    months=pq.read_table(OUT/'restaurant_month.parquet').to_pylist()
    checks['place_keys_unique']=len(places_out)==len(set(places_out))==len(core_ids)
    checks['inspection_keys_unique']=len(inspection_out['inspection_id'])==len(set(inspection_out['inspection_id']))==inspection_scope['core_restaurant']
    checks['accepted_links_one_to_one']=len(accepted)==len(license_to_place)
    checks['review_input_reconciles']=review_counts['input_rows']==review_counts['outside_core_rows']+review_counts['scoped_raw_rows']
    checks['review_dedup_reconciles']=review_counts['scoped_raw_rows']==review_sink.count+review_counts['exact_duplicate_extra_rows']
    checks['review_exclusions_reconcile']=sum(eligibility.values())==review_sink.count
    checks['monthly_keys_unique']=len(months)==len({(r['restaurant_id'],r['business_episode_id'],r['source'],r['month']) for r in months})
    checks['monthly_reviews_reconcile']=sum(r['review_count'] for r in months)==final_review_count
    checks['all_inspections_strictly_prior']=all(r['inspection_date']<r['month'] for r in months)
    checks['all_months_bracketed']=all(r['observed_first_scored_date']<r['month'] and next_month(r['month'])<=r['observed_last_scored_date'] for r in months)
    checks['all_monthly_links_accepted']=all(r['source_place_id'] in accepted for r in months)
    checks['coordinate_threshold_respected']=all(r['distance_max_m']<=MAX_DISTANCE_M for r in months)
    checks['no_missing_or_unscored_inspection_outcomes']=all(r['inspection_result'] in SCORED_RESULTS for r in months)
    checks['all_input_sizes_unchanged']=all((ROOT/k).stat().st_size==v['bytes'] for k,v in audit['sources'].items())
    review_keys=set()
    persisted_review_count=0
    foreign_keys_valid=True
    conflict_flags_valid=True
    candidate_flags_valid=True
    for batch in pq.ParquetFile(OUT/'chicago_restaurant_reviews.parquet').iter_batches(batch_size=25000,
            columns=['review_id','source_place_id','identity_key','identity_conflict','eligible_for_monthly_candidate','rating_valid','user_id_present','date_status']):
        for r in batch.to_pylist():
            persisted_review_count+=1
            review_keys.add(r['review_id'])
            foreign_keys_valid &= r['source_place_id'] in core_ids
            conflict_flags_valid &= r['identity_conflict']==(r['identity_key'] in conflicts_hex)
            if r['eligible_for_monthly_candidate']:
                candidate_flags_valid &= r['source_place_id'] in accepted and not r['identity_conflict'] and r['rating_valid'] and r['user_id_present'] and r['date_status']=='valid'
    checks['persisted_review_keys_unique']=len(review_keys)==persisted_review_count==review_sink.count
    checks['persisted_review_foreign_keys_valid']=foreign_keys_valid
    checks['all_conflicting_review_variants_flagged']=conflict_flags_valid
    checks['persisted_review_candidate_flags_valid']=candidate_flags_valid
    del review_keys
    if not all(checks.values()): raise AssertionError(checks)
    audit['acceptance_checks']=checks
    audit['outputs']={path.name:{'bytes':path.stat().st_size,'rows':pq.ParquetFile(path).metadata.num_rows if path.suffix=='.parquet' else None}
                      for path in OUT.iterdir() if path.is_file()}
    audit['status']='complete'
    audit['runtime_seconds']=round(time.monotonic()-started,2)
    write_json(OUT/'filter_audit.json',audit)
    write_json(REPORTS/'initial_merge_audit.json',audit)
    # Remove only this pipeline's completed temporary file.
    (STAGING/'deduplicated_reviews.parquet').unlink()
    print('COMPLETE:',audit['monthly'],flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--match-only',action='store_true',help='Build scope and exact crosswalk outputs without scanning reviews')
    run(parser.parse_args().match_only)
