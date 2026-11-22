"""Safety checks for exact linkage, cross-batch duplicates, and time alignment."""
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from build_initial_merge import (
    ReviewDeduper, choose_prior_inspection, evaluate_candidate, normalize_name,
    overlaps_range, parse_google_address, parse_street, range_description, review_time,
)


def fixture():
    place={'gmap_id':'g1','name':'Example Pizza','address':'Example Pizza, 123 N Main St, Chicago, IL 60601',
           'latitude':41.88,'longitude':-87.63}
    place['_address']=parse_google_address(place)
    event={'inspection_id':'i1','license':'l1','dba':'EXAMPLE PIZZA','aka':'Example Pizza','street':'123 N MAIN ST',
           'unit':'','address_status':'parsed','city':'CHICAGO','state':'IL','zip':'60601','facility':'RESTAURANT',
           'latitude':41.88,'longitude':-87.63,'date':date(2015,1,15),'result':'Pass'}
    later={**event,'inspection_id':'i2','date':date(2017,1,15)}
    return place,[event,later]


def test_exact_stable_evidence_accepts_and_retains_observed_dates():
    p,h=fixture()
    reasons,evidence=evaluate_candidate(p,h,{'l1'},{'g1'})
    assert reasons==[]
    assert evidence['distance_max_m']==0
    assert evidence['observed_first_scored_date']==date(2015,1,15)


@pytest.mark.parametrize(('change','reason'),[
    ({'street':'125 N MAIN ST'},'license_address_or_unit_changes'),
    ({'unit':'UNIT:2'},'license_address_or_unit_changes'),
    ({'zip':'60602'},'inspection_locality_or_zip_conflict'),
    ({'city':'EVANSTON'},'inspection_locality_or_zip_conflict'),
    ({'dba':'Different Operator'},'dba_name_not_exact_throughout_history'),
    ({'aka':'Different Operator'},'aka_name_conflicts'),
    ({'facility':'GROCERY STORE'},'license_has_noncore_facility_history'),
    ({'latitude':None},'inspection_coordinates_missing_or_outside_screen'),
    ({'latitude':41.89},'coordinate_distance_exceeds_25m'),
    ({'address_status':'unparsed_or_range'},'inspection_address_not_strictly_parsed'),
])
def test_one_conflicting_historical_event_blocks_entire_license(change,reason):
    p,h=fixture()
    h[0].update(change)
    reasons,_=evaluate_candidate(p,h,{'l1'},{'g1'})
    assert reason in reasons


def test_occupancy_competition_and_house_ranges_block_acceptance():
    p,h=fixture()
    reasons,_=evaluate_candidate(p,h,{'l1','l2'},{'g1','g2'},True)
    assert {'multiple_or_invalid_licenses_at_address','competing_google_place_at_address','overlapping_house_number_range'}<=set(reasons)


def test_closure_before_later_scored_event_is_not_a_stable_episode():
    p,h=fixture()
    h.insert(1,{**h[0],'inspection_id':'closed','date':date(2016,1,1),'result':'Out of Business'})
    reasons,_=evaluate_candidate(p,h,{'l1'},{'g1'})
    assert 'terminal_status_precedes_later_scored_inspection' in reasons


def test_address_normalization_preserves_units_house_suffixes_and_uncertainty():
    assert parse_street('123 North Main Street Suite 4')=={'street':'123 N MAIN ST','unit':'UNIT:4','status':'parsed'}
    assert parse_street('123A N MAIN ST #4')['street']=='123A N MAIN ST'
    assert parse_street('123 N MAIN ST FLOOR 4')['unit']=='FLOOR:4'
    assert parse_street('123-125 N MAIN ST')['status']!='parsed'
    assert range_description('1200-08 N MAIN ST')==(1200,1208,'N MAIN ST')
    assert overlaps_range('1204 N MAIN ST',{'N MAIN ST':{(1200,1208)}})
    assert not overlaps_range('1209 N MAIN ST',{'N MAIN ST':{(1200,1208)}})
    p={'name':'Example','address':'Example, Big Building, 123 N Main St, Chicago, IL 60601'}
    assert parse_google_address(p)['status']!='parsed'
    assert normalize_name('Example, Inc.')!=normalize_name('Example')


def test_global_dedup_retains_both_conflicting_variants_for_quarantine():
    dedup=ReviewDeduper()
    a={'gmap_id':'g1','user_id':'u1','time':1514779200000,'rating':5,'text':'Good'}
    fingerprint,identity=dedup.add(a)
    # The state persists between writer batches, so a distant duplicate is removed.
    dedup.add({**a,'user_id':'other'})
    assert dedup.add(deepcopy(a))==(None,None)
    other_fingerprint,other_identity=dedup.add({**a,'rating':1})
    assert fingerprint!=other_fingerprint
    assert identity==other_identity
    assert identity in dedup.conflicts


def test_missing_users_are_not_arbitrarily_grouped_into_an_identity():
    dedup=ReviewDeduper()
    for rating in (1,5):
        _,identity=dedup.add({'gmap_id':'g1','user_id':None,'time':1514779200000,'rating':rating})
        assert identity is None
    assert not dedup.conflicts


def test_review_utc_date_is_converted_before_calendar_month_assignment():
    timestamp=datetime(2018,1,1,4,tzinfo=timezone.utc).timestamp()*1000
    utc,local=review_time(timestamp)
    assert utc.date()==date(2018,1,1)
    assert local==date(2017,12,31)
    assert review_time(True)==(None,None)
    assert review_time(float('nan'))==(None,None)


def test_monthly_join_strictly_precedes_month_and_never_selects_same_day():
    _,h=fixture()
    same_day={**h[0],'inspection_id':'same_day','date':date(2015,2,1),'result':'Fail'}
    chosen,reason=choose_prior_inspection(h+[same_day],date(2015,2,1),date(2015,1,15),date(2017,1,15))
    assert reason=='included'
    assert chosen['inspection_id']=='i1'


def test_same_day_latest_ties_are_excluded_even_with_identical_results():
    _,h=fixture()
    h.append({**h[0],'inspection_id':'tie'})
    chosen,reason=choose_prior_inspection(h,date(2015,2,1),date(2015,1,15),date(2017,1,15))
    assert chosen is None
    assert reason=='latest_inspection_same_day_tie'


def test_do_not_skip_latest_no_entry_to_recover_an_earlier_pass():
    _,h=fixture()
    h.append({**h[0],'inspection_id':'no_entry','date':date(2015,1,25),'result':'No Entry'})
    chosen,reason=choose_prior_inspection(h,date(2015,2,1),date(2015,1,15),date(2017,1,15))
    assert chosen is None
    assert reason=='latest_prior_event_not_scored'


@pytest.mark.parametrize(('month','reason'),[
    (date(2015,1,1),'month_not_bracketed_by_scored_observations'),
    (date(2017,1,1),'month_not_bracketed_by_scored_observations'),
    (date(2016,2,1),'prior_inspection_older_than_365_days'),
])
def test_unknown_boundaries_and_stale_evidence_are_excluded(month,reason):
    _,h=fixture()
    assert choose_prior_inspection(h,month,date(2015,1,15),date(2017,1,15))==(None,reason)
