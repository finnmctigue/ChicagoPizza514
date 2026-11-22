"""Export two linked, cleaned CSV tables from the already verified exact merge.

Run: python prepare_final_data.py
This preserves all 252 accepted businesses and their deduplicated reviews.
Review date/analysis eligibility is explicit; the narrower 249-business monthly
sample can be recovered using included_in_primary_month. No raw join is repeated.
"""
import hashlib
import json
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'data' / 'processed' / 'initial_exact'
FINAL = ROOT / 'data' / 'final data'
REPORTS = ROOT / 'data' / 'reports'

RESTAURANT_FIELDS = {
    'restaurant_id':'Primary key; stable Google-based restaurant identifier.',
    'gmap_id':'Original Google place identifier; string, not numeric.',
    'business_episode_id':'Identifier for the accepted stable observed business history; not proof of ownership dates.',
    'license':'Chicago inspection license identifier, stored as text.',
    'place_name':'Google business name.',
    'raw_address':'Original Google address, including its business-name prefix.',
    'street':'Normalized street address including house number/suffix.',
    'unit':'Normalized suite/unit/floor; blank means not supplied, not a verified absence of a unit.',
    'city':'Parsed city (CHICAGO for this cohort).',
    'geographic_state':'Parsed geographic state (IL), not Google operating status.',
    'zip':'Five-digit postal code, retained as text.',
    'latitude':'Google latitude in decimal degrees.',
    'longitude':'Google longitude in decimal degrees.',
    'categories_json':'Google category list serialized as JSON; categories overlap.',
    'operating_status':'Undated Google opening/closure status; not current verified status.',
    'price':'Optional undated Google price category.',
    'business_description':'Optional Google business description.',
    'hours_json':'Optional Google opening-hours list serialized as JSON; blank if missing.',
    'google_maps_url':'Place URL in the original Google metadata.',
    'snapshot_avg_rating':'Undated Google metadata average rating; not recomputed from the export.',
    'snapshot_num_reviews':'Undated Google metadata review count; may differ from exported rows.',
    'match_method':'Exact matching rule version used to accept this restaurant.',
    'match_distance_max_m':'Largest Google-to-inspection coordinate distance over the accepted history, in meters.',
    'observed_first_scored_date':'First observed Pass/Conditional/Fail inspection, not an opening date.',
    'observed_last_scored_date':'Last observed Pass/Conditional/Fail inspection, not a closing date.',
    'inspection_first_date':'First inspection event of any result for this accepted history.',
    'inspection_last_date':'Last inspection event of any result for this accepted history.',
    'inspection_count':'Number of unique inspection events across the entire accepted history.',
    'inspection_pass_count':'Events with result Pass.',
    'inspection_conditional_count':'Events with result Pass w/ Conditions.',
    'inspection_fail_count':'Events with result Fail.',
    'inspection_no_entry_count':'Events with result No Entry; not treated as passes or failures.',
    'inspection_not_ready_count':'Events with result Not Ready; not treated as passes or failures.',
    'inspection_out_of_business_count':'Events with result Out of Business.',
    'inspection_business_not_located_count':'Events with result Business Not Located.',
    'inspection_missing_violations_count':'Events without violation text; absence does not establish no violations.',
    'scored_inspection_count':'Pass + Pass w/ Conditions + Fail event count.',
    'inspection_failure_share':'Fail / scored_inspection_count over the full inspection history; descriptive, not historical predictor.',
    'review_count':'All deduplicated review records for the accepted place, including flagged dates.',
    'reviews_with_text':'Review records with nonblank text.',
    'valid_date_review_count':'Review records with date_status == valid; includes records outside the observed inspection span.',
    'monthly_candidate_review_count':'Review records passing the pre-month checks.',
    'primary_month_review_count':'Review records actually represented in the earlier primary monthly dataset.',
    'mean_export_rating':'Mean valid rating across the restaurant\'s exported reviews, irrespective of date eligibility.',
    'median_export_rating':'Median valid rating across exported reviews.',
    'first_valid_review_date':'Earliest valid Chicago-local review date; not a proven start of source coverage.',
    'last_valid_review_date':'Latest valid Chicago-local review date; not a proven end of business activity.',
    'primary_month_count':'Number of rows for this restaurant in restaurant_month.parquet; can be zero.',
}
REVIEW_FIELDS = {
    'review_id':'Primary key; SHA-256 fingerprint of the original full review record.',
    'restaurant_id':'Foreign key to cleaned_restaurants.csv; many reviews to one restaurant.',
    'gmap_id':'Original Google place identifier.',
    'business_episode_id':'Same observed-history identifier as the restaurant table.',
    'source':'Review platform, google for every row.',
    'user_id':'Original reviewer identifier, retained as text; unrelated to Yelp IDs.',
    'rating':'Google rating on the 1-5 scale; valid ratings without text are retained.',
    'text':'Original review text; blank means unavailable/blank, not neutral sentiment.',
    'has_text':'True if text is nonblank after whitespace checking.',
    'time_ms':'Original numeric Unix timestamp in milliseconds.',
    'raw_time_json':'Unmodified source timestamp encoded as JSON.',
    'review_time_utc':'Parsed timezone-aware UTC timestamp.',
    'review_date_chicago':'Local calendar date in America/Chicago.',
    'review_month':'First day of the Chicago calendar month, even when the date is flagged.',
    'review_year':'Chicago calendar year for valid dates only; blank for flagged dates.',
    'date_status':'valid, before_analysis_coverage, invalid_timestamp, or after_analysis_date.',
    'date_valid_for_analysis':'True exactly when date_status is valid.',
    'rating_valid':'Whether the source rating is a valid 1-5 value.',
    'user_id_present':'Whether a source user ID is available.',
    'identity_conflict':'Whether distinct full records share user/place/time; all variants are flagged.',
    'eligible_for_monthly_candidate':'Passes business-link, identity, user, rating, date, and observed-span checks before month-level checks.',
    'candidate_eligibility_reason':'First pre-month exclusion reason, or eligible.',
    'included_in_primary_month':'True only for reviews represented in the earlier 13,271-row primary monthly table.',
    'primary_analysis_status':'included or the first review/month-level exclusion reason.',
    'prior_inspection_id':'Inspection linked by the primary monthly table; blank unless included_in_primary_month.',
    'prior_inspection_date':'Date of that inspection, strictly before review_month; blank otherwise.',
    'prior_inspection_result':'Pass / Pass w/ Conditions / Fail from the linked event; blank otherwise.',
    'prior_inspection_age_days':'Age of the linked inspection at the beginning of the review month, not review day.',
}

def iso_range(series):
    s=pd.to_datetime(series,errors='coerce').dropna()
    return [s.min().date().isoformat(),s.max().date().isoformat()] if len(s) else [None,None]

def main():
    audit=json.loads((REPORTS/'initial_merge_audit.json').read_text())
    verified=json.loads((REPORTS/'initial_merge_verification.json').read_text())
    assert audit['status']=='complete' and all(audit['acceptance_checks'].values())
    assert verified['status']=='passed'
    FINAL.mkdir(parents=True,exist_ok=True)
    restaurants=pd.read_parquet(SOURCE/'chicago_restaurants.parquet')
    restaurants=restaurants.loc[restaurants['match_status'].eq('accepted_exact')].copy()
    restaurants=restaurants.rename(columns={'source_place_id':'gmap_id'})
    metadata=restaurants['metadata_json'].map(json.loads)
    for field,original in [('price','price'),('business_description','description'),('google_maps_url','url')]:
        restaurants[field]=metadata.map(lambda r:r.get(original))
    restaurants['hours_json']=metadata.map(lambda r:json.dumps(r['hours'],ensure_ascii=False) if r.get('hours') else None)
    links=pd.read_csv(REPORTS/'initial_merge_accepted_links.csv',dtype={'source_place_id':'string','license':'string'})
    links=links.rename(columns={'source_place_id':'gmap_id','method':'match_method','distance_max_m':'match_distance_max_m'})
    restaurants=restaurants.merge(links[['gmap_id','match_method','match_distance_max_m','observed_first_scored_date','observed_last_scored_date']],
                                  on='gmap_id',how='left',validate='one_to_one')

    inspections=pd.read_parquet(SOURCE/'chicago_restaurant_inspections.parquet')
    inspections=inspections.loc[inspections['match_status'].eq('accepted_exact')].copy()
    assert inspections['inspection_id'].is_unique
    inspection_summary=inspections.groupby('restaurant_id').agg(inspection_count=('inspection_id','size'),
        inspection_first_date=('inspection_date','min'),inspection_last_date=('inspection_date','max'))
    result_columns={'Pass':'inspection_pass_count','Pass w/ Conditions':'inspection_conditional_count','Fail':'inspection_fail_count',
        'No Entry':'inspection_no_entry_count','Not Ready':'inspection_not_ready_count','Out of Business':'inspection_out_of_business_count',
        'Business Not Located':'inspection_business_not_located_count'}
    results=pd.crosstab(inspections['restaurant_id'],inspections['result']).reindex(columns=list(result_columns),fill_value=0).rename(columns=result_columns)
    inspection_summary=inspection_summary.join(results)
    inspections['missing_violations']=inspections['violations'].fillna('').str.strip().eq('')
    inspection_summary['inspection_missing_violations_count']=inspections.groupby('restaurant_id')['missing_violations'].sum()
    inspection_summary['scored_inspection_count']=inspection_summary[['inspection_pass_count','inspection_conditional_count','inspection_fail_count']].sum(axis=1)
    inspection_summary['inspection_failure_share']=inspection_summary['inspection_fail_count']/inspection_summary['scored_inspection_count'].replace(0,float('nan'))
    restaurants=restaurants.merge(inspection_summary,on='restaurant_id',how='left',validate='one_to_one')

    columns=['review_id','restaurant_id','source_place_id','business_episode_id','source','user_id','rating','text','has_text','time_ms','raw_time_json',
        'review_time_utc','review_date_chicago','date_status','rating_valid','user_id_present','identity_conflict','eligible_for_monthly_candidate','eligibility_reason']
    reviews=ds.dataset(SOURCE/'chicago_restaurant_reviews.parquet',format='parquet').to_table(
        columns=columns,filter=ds.field('has_accepted_link')==True).to_pandas()
    reviews=reviews.rename(columns={'source_place_id':'gmap_id','eligibility_reason':'candidate_eligibility_reason'})
    local_date=pd.to_datetime(reviews['review_date_chicago'])
    reviews['review_month']=local_date.dt.to_period('M').dt.to_timestamp()
    reviews['date_valid_for_analysis']=reviews['date_status'].eq('valid')
    reviews['review_year']=local_date.dt.year.where(reviews['date_valid_for_analysis']).astype('Int64')
    months=pd.read_parquet(SOURCE/'restaurant_month.parquet')
    months['month']=pd.to_datetime(months['month'])
    primary=months[['source_place_id','month','inspection_id','inspection_date','inspection_result','inspection_age_days']].rename(columns={
        'source_place_id':'gmap_id','month':'review_month','inspection_id':'prior_inspection_id','inspection_date':'prior_inspection_date',
        'inspection_result':'prior_inspection_result','inspection_age_days':'prior_inspection_age_days'})
    reviews=reviews.merge(primary,on=['gmap_id','review_month'],how='left',validate='many_to_one')
    reviews['included_in_primary_month']=reviews['eligible_for_monthly_candidate'] & reviews['prior_inspection_id'].notna()
    month_decisions=pd.read_csv(SOURCE/'monthly_eligibility_audit.csv',dtype={'source_place_id':'string'})
    month_decisions['month']=pd.to_datetime(month_decisions['month'])
    month_decisions=month_decisions.rename(columns={'source_place_id':'gmap_id','month':'review_month','decision':'month_decision'})
    reviews=reviews.merge(month_decisions[['gmap_id','review_month','month_decision']],on=['gmap_id','review_month'],how='left',validate='many_to_one')
    assert reviews.loc[reviews['eligible_for_monthly_candidate'],'month_decision'].notna().all()
    reviews['primary_analysis_status']=reviews['candidate_eligibility_reason'].where(~reviews['eligible_for_monthly_candidate'],reviews['month_decision'])
    for col in ['prior_inspection_id','prior_inspection_date','prior_inspection_result','prior_inspection_age_days']:
        reviews.loc[~reviews['included_in_primary_month'],col]=None
    reviews['prior_inspection_age_days']=reviews['prior_inspection_age_days'].astype('Int64')
    review_summary=reviews.groupby('restaurant_id').agg(review_count=('review_id','size'),reviews_with_text=('has_text','sum'),
        valid_date_review_count=('date_valid_for_analysis','sum'),monthly_candidate_review_count=('eligible_for_monthly_candidate','sum'),
        primary_month_review_count=('included_in_primary_month','sum'),mean_export_rating=('rating','mean'),median_export_rating=('rating','median'))
    valid_reviews=reviews.loc[reviews['date_valid_for_analysis']]
    dates=valid_reviews.groupby('restaurant_id').agg(first_valid_review_date=('review_date_chicago','min'),last_valid_review_date=('review_date_chicago','max'))
    review_summary=review_summary.join(dates)
    review_summary['primary_month_count']=months.groupby('restaurant_id').size().reindex(review_summary.index,fill_value=0)
    restaurants=restaurants.merge(review_summary,on='restaurant_id',how='left',validate='one_to_one')
    restaurants=restaurants[list(RESTAURANT_FIELDS)].sort_values(['place_name','restaurant_id']).reset_index(drop=True)
    reviews=reviews[list(REVIEW_FIELDS)].sort_values(['restaurant_id','review_time_utc','review_id']).reset_index(drop=True)

    # Reconcile keys, counts, and temporal joins before publishing the two tables.
    assert restaurants['restaurant_id'].is_unique and restaurants['gmap_id'].is_unique
    assert reviews['review_id'].is_unique
    assert set(reviews['restaurant_id'])==set(restaurants['restaurant_id'])
    assert len(restaurants)==audit['coverage']['matched_places']
    assert len(reviews)==audit['coverage']['matched_reviews']
    assert restaurants['review_count'].sum()==len(reviews)
    assert restaurants['inspection_count'].sum()==audit['coverage']['matched_inspections']
    assert restaurants[list(result_columns.values())].sum(axis=1).eq(restaurants['inspection_count']).all()
    assert reviews['included_in_primary_month'].sum()==audit['monthly']['included_reviews']
    assert reviews['rating_valid'].all() and reviews['user_id_present'].all() and not reviews['identity_conflict'].any()
    included=reviews.loc[reviews['included_in_primary_month']]
    assert pd.to_datetime(included['prior_inspection_date']).lt(included['review_month']).all()
    assert reviews.loc[~reviews['included_in_primary_month'],'prior_inspection_id'].isna().all()
    expected_counts=months.set_index(['source_place_id','month'])['review_count'].sort_index()
    actual_counts=included.groupby(['gmap_id','review_month']).size().sort_index()
    assert actual_counts.index.equals(expected_counts.index) and actual_counts.eq(expected_counts).all()
    restaurants.to_csv(FINAL/'cleaned_restaurants.csv',index=False,encoding='utf-8')
    reviews.to_csv(FINAL/'reviews.csv',index=False,encoding='utf-8')
    summary={'restaurants':len(restaurants),'restaurant_columns':len(restaurants.columns),'reviews':len(reviews),'review_columns':len(reviews.columns),
        'inspection_events_summarized':int(restaurants['inspection_count'].sum()),'reviews_with_text':int(reviews['has_text'].sum()),
        'date_valid_reviews':int(reviews['date_valid_for_analysis'].sum()),'flagged_date_reviews':int((~reviews['date_valid_for_analysis']).sum()),
        'primary_reviews':len(included),'primary_restaurants':included['restaurant_id'].nunique(),'primary_months':len(months),
        'all_review_date_range_chicago':iso_range(reviews['review_date_chicago']),'valid_review_date_range_chicago':iso_range(valid_reviews['review_date_chicago']),
        'inspection_date_range':iso_range(inspections['inspection_date']),'review_mean_rating':float(reviews['rating'].mean()),
        'review_median_rating':float(reviews['rating'].median()),'restaurant_median_review_count':float(restaurants['review_count'].median()),
        'inspection_failure_share':float(restaurants['inspection_fail_count'].sum()/restaurants['scored_inspection_count'].sum()),
        'validation':'Unique keys, exact restaurant/review coverage, many-to-one merges, inspection counts, and every primary monthly review count reconcile.'}
    for filename in ['cleaned_restaurants.csv','reviews.csv']:
        with (FINAL/filename).open('rb') as f:
            summary[filename]={'bytes':(FINAL/filename).stat().st_size,'sha256':hashlib.file_digest(f,'sha256').hexdigest()}
    (REPORTS/'final_data_build_audit.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    write_description(summary,restaurants,reviews,audit)
    print(json.dumps(summary,indent=2))

def write_description(s,restaurants,reviews,audit):
    profile=json.loads((REPORTS/'current_dataset_profile.json').read_text())
    raw_inventory=[('Google metadata','meta-Illinois.json',profile['google_metadata']['rows']),
        ('Google reviews','review-Illinois.json',profile['google_reviews']['rows']),
        ('Chicago inspections','Food_Inspections_20261103.csv',profile['inspections']['rows'])]
    for kind in ['business','review','tip','checkin','user']:
        raw_inventory.append(('Yelp '+kind,'yelp_academic_dataset_'+kind+'.json',profile['yelp_'+kind]['rows']))
    lines=['CHICAGOPIZZA514: DATASET DESCRIPTION','Prepared for the local November 22, 2026 analysis snapshot.','',
        '1. PURPOSE AND FINAL DATASETS',
        'The project studies historical customer reviews and food-inspection records for Chicago restaurants. The current scope is all restaurants, not only pizza restaurants.',
        f"cleaned_restaurants.csv: {s['restaurants']:,} rows and {s['restaurant_columns']} attributes. One row per accepted restaurant, combining Google place metadata with aggregate inspection history and review summaries.",
        f"reviews.csv: {s['reviews']:,} rows and {s['review_columns']} attributes. One deduplicated Google review per row. Every review belongs to one of the {s['restaurants']} restaurants; every restaurant has reviews.",
        'Join reviews.restaurant_id to cleaned_restaurants.restaurant_id (many-to-one). gmap_id is also preserved. Do not join raw review rows to raw inspection events without a date-specific design.',
        f"The restaurant table summarizes {s['inspection_events_summarized']:,} inspection events. It is not an inspection-event table; detailed events and original violation text remain under data/processed/initial_exact/.",
        f"These files preserve the full accepted-business cohort. The narrower earlier monthly analysis contains {s['primary_restaurants']} restaurants, {s['primary_months']:,} months and {s['primary_reviews']:,} reviews. Select included_in_primary_month == True to recover its review cohort.",
        '', '2. DATA SOURCES AND COLLECTION',
        'Google metadata and reviews: the identified public source is the UC San Diego McAuley Lab Google Local Data (2021) release, originating from Google Maps. Its published Illinois row counts match the local files exactly. [1]',
        'The publisher describes a US review and place-metadata collection through September 2021, including ratings, optional text/images and business attributes. The local files are JSON Lines. Publicly contributed reviews are observational records, not a probability sample. [1]',
        'Local acquisition provenance is limited: no original download command, transfer log, or independently verified upstream checksum is present. The source identification is supported by matching filenames, schemas and published counts; a particular download date or scraping procedure is not established. This task performed no new Google collection.',
        'Chicago inspections: the identified source is the City of Chicago Food Inspections dataset, ID 4ijn-s7e5. The local CSV is Food_Inspections_20261103.csv. Its filename suggests a November 3, 2026 snapshot but does not prove the acquisition date. [2]',
        'The City describes inspections conducted by the Chicago Department of Public Health Food Protection Program, recorded in a database and reviewed by a licensed environmental health practitioner. Findings describe conditions at inspection time. The source also notes a July 2018 procedure change and possible duplicates. [2]',
        'Yelp: the repository also contains five files named yelp_academic_dataset_*.json (business, review, tip, checkin and user), consistent with the Yelp Open Dataset format. They were inventoried locally but are not used in these final files. The original local acquisition date is unknown; the provider documentation page was access-restricted during this task. [3]',
        'Local processing: existing raw files were profiled; Chicago restaurant candidates were selected; stable exact business links were validated; reviews were streamed and deduplicated; date-aware monthly eligibility was computed. The final two CSVs were exported from those verified processed tables. Source SHA-256 hashes and counts are recorded in data/reports/initial_merge_audit.json; final file hashes are in data/reports/final_data_build_audit.json.',
        '', '3. INVENTORY AND OBSERVED DURATION']
    lines += [f'{name}: {rows:,} raw rows; {filename}.' for name,filename,rows in raw_inventory]
    lines += [
        'Raw Google metadata has 178,204 unique gmap_id values; 1,001 repeated rows were identical. It has no explicit snapshot date for average ratings, review counts, hours, price or operating status.',
        f"Raw inspections: {profile['inspections']['date_min']} through {profile['inspections']['date_max']}. Final accepted inspection histories: {s['inspection_date_range'][0]} through {s['inspection_date_range'][1]}.",
        f"Raw statewide Google review timestamps: {profile['google_reviews']['time_min_utc']} through {profile['google_reviews']['time_max_utc']} (UTC; the early bound includes implausible dates).",
        f"Final valid review dates: {s['valid_review_date_range_chicago'][0]} through {s['valid_review_date_range_chicago'][1]} in America/Chicago. All retained timestamps, including flagged dates, span {s['all_review_date_range_chicago'][0]} through {s['all_review_date_range_chicago'][1]} locally.",
        f"{s['flagged_date_reviews']:,} review dates are flagged rather than rewritten; these remain usable for date-independent ratings but not time-dependent analysis. Source coverage in 2021 is partial. Missing review months are not proven zero-activity periods.",
        'Local Yelp review records span '+profile['yelp_review']['date_min']+' through '+profile['yelp_review']['date_max']+' (timezone not established). Check-in rows encode timestamp lists, not individual visits. User yelping_since describes account creation, not the date of every profile attribute.',
        '', '4. DATA PREPARATION',
        'a. Scope: Chicago/IL address labels and the documented restaurant-category rules select 8,317 Google places. Inspection scope requires normalized Chicago/IL and Facility Type = Restaurant, retaining 215,578 events. Cafe-only and uncertain categories remain outside the core. Geographic screening is not an official city-boundary polygon test.',
        'b. Exact matching: normalize case, accents, punctuation, street direction/suffix and units. Require exact restaurant names against DBA and nonblank AKA histories; exact street/unit/ZIP; a unique place and license at the address; consistent full-license restaurant history; and Google-to-inspection coordinates no more than 25 meters apart. Competing operators, unresolved ranges/units and inconsistent histories are excluded.',
        'c. The strict rules accepted 252 businesses. These are verifiable agreements in the supplied records, not independent proof of ownership or business opening/closing dates. Observed inspection bounds are explicitly named as observations.',
        'd. Stream Google reviews by selected gmap_id. Across the entire Chicago restaurant core, 2,828,225 raw review rows became 2,789,275 distinct full records after removing 38,950 exact repeats. Generate stable SHA-256 review IDs and flag all variants of conflicting user/place/time identities. The final accepted cohort contains no such identity conflicts and has valid ratings and user IDs.',
        'e. Preserve source timestamps, convert milliseconds to UTC, and derive Chicago-local calendar dates. Keep date-status flags. Pre-2010 dates outside inspection coverage and invalid/future dates cannot enter the temporal analysis. Ratings without text are retained; missing text is not imputed or labeled neutral.',
        'f. The temporal subset requires reviews within the observed scored-inspection span; months wholly bracketed by scored observations; and a unique, scored prior inspection strictly before the month, at most 365 days old. Same-day ties and latest non-scored events exclude the month. Pass, conditional pass and fail remain distinct.',
        'g. Final restaurant rows aggregate inspection outcomes over their complete accepted history. Inspection failure share is Fail / (Pass + Pass w/ Conditions + Fail); other statuses are not treated as passes. Review summaries use the final exported review rows. Optional metadata and absent text remain missing; identifiers remain strings.',
        'h. Retain 125,254 accepted-business reviews with their eligibility flags; do not conflate them with the 103,049-review monthly subset. Source reviewer display names, picture payloads and business response payloads are available in the processed source table but omitted from the final review file because this descriptive analysis does not require them.',
        'i. Check unique primary keys, referential integrity, exact row-count reconciliation, many-to-one join validation, and agreement with every primary monthly review count. The previous pipeline also passed 23 focused tests and an independent source/aggregate verification.',
        '', '5. ATTRIBUTES IN THE ORIGINAL SOURCES',
        'Google metadata: gmap_id, name, address, latitude, longitude, category, description, avg_rating, num_of_reviews, price, hours, MISC, state (operating status), relative_results and url.',
        'Google reviews: user_id, name (reviewer, not business), time (milliseconds), rating, text, pics, resp and gmap_id.',
        'Inspections: Inspection ID, DBA Name, AKA Name, License #, Facility Type, Risk, Address, City, State, Zip, Inspection Date, Inspection Type, Results, Violations, Latitude, Longitude and Location.',
        'Unused Yelp files: business identifiers/location/categories/stars/attributes/hours; review identifiers, user/business IDs, stars/text/date/votes; tips with text/date/compliments; check-in timestamp lists; user account dates, counts, average stars, friends, fans, elite and compliments.',
        '', '6. FINAL ATTRIBUTE DICTIONARY: cleaned_restaurants.csv']
    lines += [f'{key}: {value}' for key,value in RESTAURANT_FIELDS.items()]
    lines += ['', '7. FINAL ATTRIBUTE DICTIONARY: reviews.csv']
    lines += [f'{key}: {value}' for key,value in REVIEW_FIELDS.items()]
    lines += ['', '8. SHORT DESCRIPTIVE SUMMARY',
        f"Restaurants: {s['restaurants']:,}; reviews: {s['reviews']:,}; reviews with text: {s['reviews_with_text']:,} ({s['reviews_with_text']/s['reviews']:.1%}); valid-date reviews: {s['date_valid_reviews']:,}.",
        f"Review-weighted mean rating: {s['review_mean_rating']:.3f}/5; median rating: {s['review_median_rating']:.1f}/5. Median reviews per restaurant: {s['restaurant_median_review_count']:.1f}.",
        f"Accepted inspection events: {s['inspection_events_summarized']:,}; failure share among scored outcomes: {s['inspection_failure_share']:.2%}. This is an event-level historical description, not a current failure probability.",
        'Open descriptive_analysis.ipynb in this directory and run all cells for numeric summaries, missing-value tables, frequency/cross-tabulation tables, valid-date yearly summaries, charts and an interpretation report. All statistics are computed from the two final CSVs.',
        '', '9. BENEFITS AND LIMITATIONS',
        'Benefits: two clearly defined table grains; exact source-supported identity links; stable identifiers; review text and ratings; inspection outcome histories; separate missingness/date flags; reproducible filters and checks. The sample supports descriptive customer-experience analysis and carefully aligned historical comparisons.',
        'Limits: only 252 of 8,317 candidate Google restaurants passed the strict rules, so this is not a census or representative random sample. Stable names, addresses, licenses and clean geocodes are favored. Chains and high-review-volume restaurants may dominate review-weighted summaries.',
        'Reviews are self-selected contributions; missing text prevents sentiment measurement for many records. Ratings are not direct measures of food safety. Language, sarcastic text, reviewer behavior and unobserved collection effects can bias text analysis; this script does not assign sentiment labels.',
        'Google review coverage ends in 2021, whereas inspection histories extend into 2026. Lifetime inspection aggregates and undated metadata must not be used as if they were known at a historical prediction date. Whole-history screening and bracketing use later observations to choose a retrospective cohort and can introduce survival/selection bias.',
        'Inspection types have different sampling mechanisms, repeat inspections are not independent restaurant observations, blank violations do not imply no violations, and violation-code meanings change across years. Source labels and coordinates can be wrong even when they agree. The 25-meter/365-day thresholds are conservative policies, not calibrated guarantees.',
        'Dates indicate observed records, not operating intervals. Google metadata review counts and exported counts need not agree. No causal effects or prediction accuracy are established by these descriptive statistics. Yelp contributes no final observations: there were zero eligible Chicago restaurants and zero records in the broad Chicago coordinate screen.',
        '', '10. METHODS ADAPTED FROM THE LOCAL COURSE REPOSITORIES',
        'datamining/module2/[6] Introduction to EDA (Complete).ipynb: zero-based notebook cell indices 10-11 (missingness/info), 27 (describe), 32/34 (histograms/boxplots), 41/44 (frequency counts), 55 (scatterplot), 58 (groupby/aggregation), 62 (crosstab), 64/66 (correlation).',
        'ISA591/Class Notebooks/[7] ISA591 EDA Worksheet Part2 (Complete).ipynb: cells 6-7, 11, 23 and 39 (missing values, fractions, mean/median and descriptive tables).',
        'The analysis uses these descriptive pandas/Matplotlib methods, with transparent boolean filtering for date/rating eligibility. It does not introduce a predictive model or silently apply the course examples\' imputation choices to these data.',
        '', '11. SOURCES AND REPRODUCTION',
        '[1] UCSD McAuley Lab, Google Local Data (2021): https://mcauleylab.ucsd.edu/public_datasets/gdrive/googlelocal/ ; dataset/attribute reference: https://cseweb.ucsd.edu/~jmcauley/datasets.html . Consulted November 22, 2026.',
        '[2] City of Chicago Food Inspections (4ijn-s7e5): https://data.cityofchicago.org/Health-Human-Services/Food-Inspections/4ijn-s7e5 ; official City-published catalog description: https://catalog.data.gov/dataset/food-inspections . Consulted November 22, 2026. The live portal is newer than the local CSV; reported counts come from the local snapshot.',
        '[3] Yelp Open Dataset documentation reference: https://www.yelp.com/dataset/documentation/main . Retrieval returned HTTP 403; Yelp descriptions and counts here are based on the local files, not a newly verified download.',
        'From the ChicagoPizza514 repository: python prepare_final_data.py',
        'Then: python "data/final data/descriptive_analysis.py"',
        'Read CSV identifiers as strings. The analysis notebook demonstrates correct loading and validation. Empty CSV fields mean missing/not applicable; do not replace them universally with zero.',
        'Final data CSVs and generated analysis outputs are ignored by Git; code and this description can be versioned. No source data was recollected during export.']
    (FINAL/'dataset_description.txt').write_text('\n\n'.join(lines)+'\n',encoding='utf-8')

if __name__=='__main__':
    main()
