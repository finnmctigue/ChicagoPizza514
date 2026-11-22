"""Descriptive analysis of the cleaned Chicago restaurant and review datasets.

Run from any directory:
    python "data/final data/descriptive_analysis.py"

Dataset introduction
--------------------
cleaned_restaurants.csv has one row per accepted Google/Chicago-inspection
restaurant. reviews.csv has one row per deduplicated review for those restaurants.
Inspection histories are aggregated at restaurant level; reviews remain separate
to prevent reviews-times-inspections joins. restaurant_id links the two tables.

Preparation already performed by build_initial_merge.py / prepare_final_data.py:
Chicago/restaurant filtering; identical metadata/review deduplication; exact
names/address/unit/ZIP matching with stable histories and <=25m coordinates;
UTC and Chicago-local date parsing; explicit missingness/conflict/date flags;
temporal checks for the optional prior-inspection subset; and one-to-one or
many-to-one validated merges. Flagged timestamps remain available for audit.
Ratings without text are retained. We do not fill unknown values with zeros.

Course methods used (zero-based notebook cell indices):
* datamining/module2/[6] Introduction to EDA (Complete).ipynb:
  10-11 isna/info, 27 describe, 32/34 hist/boxplot, 41/44 value_counts,
  55 scatterplot, 58 groupby/agg, 62 crosstab, 64 correlation.
* ISA591/Class Notebooks/[7] ISA591 EDA Worksheet Part2 (Complete).ipynb:
  6-7, 11, 23, 39: missing counts/fractions, mean/median and numeric summaries.

This script uses only pandas, numpy, Matplotlib, and the Python standard library.
It writes reproducible tables, figures, and descriptive_analysis_report.txt into
analysis_outputs/. It fits no predictive model and makes no causal claims.
"""
import argparse
import io
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')  # Save standalone figures without opening a window.
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

BOOL_COLUMNS = ['has_text','date_valid_for_analysis','rating_valid','user_id_present','identity_conflict',
                'eligible_for_monthly_candidate','included_in_primary_month']
INSPECTION_RESULTS = {
    'Pass':'inspection_pass_count','Pass w/ Conditions':'inspection_conditional_count','Fail':'inspection_fail_count',
    'No Entry':'inspection_no_entry_count','Not Ready':'inspection_not_ready_count',
    'Out of Business':'inspection_out_of_business_count','Business Not Located':'inspection_business_not_located_count',
}

def load_data(directory):
    # Load all columns as strings first so IDs/ZIPs never become floating point.
    # Literal review text such as "NA" is preserved; only empty fields are missing.
    restaurants=pd.read_csv(directory/'cleaned_restaurants.csv',dtype='string',keep_default_na=False).replace('',pd.NA)
    reviews=pd.read_csv(directory/'reviews.csv',dtype='string',keep_default_na=False).replace('',pd.NA)
    restaurant_numeric=[c for c in restaurants if c.endswith('_count') or c.startswith('snapshot_') or c in
        ['latitude','longitude','match_distance_max_m','inspection_failure_share','reviews_with_text','mean_export_rating','median_export_rating']]
    for c in restaurant_numeric: restaurants[c]=pd.to_numeric(restaurants[c],errors='raise')
    for c in ['rating','time_ms','review_year','prior_inspection_age_days']:
        reviews[c]=pd.to_numeric(reviews[c],errors='raise')
    for c in BOOL_COLUMNS:
        assert reviews[c].isin(['True','False']).all(),f'Unexpected boolean: {c}'
        reviews[c]=reviews[c].map({'True':True,'False':False}).astype('boolean')
    for c in restaurants.columns:
        if c.endswith('_date'): restaurants[c]=pd.to_datetime(restaurants[c],errors='raise')
    for c in ['review_date_chicago','review_month','prior_inspection_date']:
        reviews[c]=pd.to_datetime(reviews[c],errors='raise')
    # ISO timestamps may have zero or three fractional-second digits.
    reviews['review_time_utc']=pd.to_datetime(reviews['review_time_utc'],format='ISO8601',utc=True,errors='raise')
    reviews.loc[reviews['text'].fillna('').str.strip().eq(''),'text']=pd.NA
    return restaurants,reviews

def validate(restaurants,reviews):
    assert restaurants['restaurant_id'].is_unique and reviews['review_id'].is_unique
    assert restaurants['gmap_id'].is_unique
    assert set(reviews['restaurant_id'])==set(restaurants['restaurant_id'])
    assert restaurants['city'].eq('CHICAGO').all() and restaurants['geographic_state'].eq('IL').all()
    mapped=reviews[['restaurant_id','gmap_id']].merge(restaurants[['restaurant_id','gmap_id']],on='restaurant_id',validate='many_to_one',suffixes=('_review','_restaurant'))
    assert mapped['gmap_id_review'].eq(mapped['gmap_id_restaurant']).all()
    count=reviews.groupby('restaurant_id').size().sort_index()
    assert count.eq(restaurants.set_index('restaurant_id')['review_count'].sort_index()).all()
    assert restaurants[list(INSPECTION_RESULTS.values())].sum(axis=1).eq(restaurants['inspection_count']).all()
    assert reviews['date_valid_for_analysis'].eq(reviews['date_status'].eq('valid')).all()
    assert reviews['has_text'].eq(reviews['text'].fillna('').str.strip().ne('')).all()
    assert reviews['rating_valid'].all() and reviews['rating'].between(1,5).all()
    assert not reviews['identity_conflict'].any()
    included=reviews.loc[reviews['included_in_primary_month']]
    assert included['prior_inspection_date'].lt(included['review_month']).all()
    assert included['prior_inspection_age_days'].between(1,365).all()
    assert included['date_valid_for_analysis'].all()
    assert included['prior_inspection_result'].isin(['Pass','Pass w/ Conditions','Fail']).all()
    assert reviews.loc[~reviews['included_in_primary_month'],'prior_inspection_id'].isna().all()
    assert len(included)==restaurants['primary_month_review_count'].sum()

def missing_table(frame):
    # Same missing-count and fraction method as ISA591 EDA Part2, cell 11.
    return pd.DataFrame({'missing_count':frame.isna().sum(),'missing_fraction':frame.isna().mean(),
                         'dtype':frame.dtypes.astype(str)}).sort_values('missing_fraction',ascending=False)

def date_range(series):
    return [series.min().date().isoformat(),series.max().date().isoformat()] if series.notna().any() else [None,None]

def analyze(directory):
    output=directory/'analysis_outputs'
    output.mkdir(parents=True,exist_ok=True)
    restaurants,reviews=load_data(directory)
    validate(restaurants,reviews)
    # Explicitly separate date-independent ratings from eligible date analysis.
    rated=reviews.loc[reviews['rating_valid']].copy()
    dated=rated.loc[rated['date_valid_for_analysis']].copy()
    dated['year']=dated['review_date_chicago'].dt.year
    textual=rated.loc[rated['has_text']].copy()
    textual['text_character_count']=textual['text'].str.len()
    primary=reviews.loc[reviews['included_in_primary_month']]

    restaurant_summary=restaurants.select_dtypes(include='number').describe().T
    review_summary=rated[['rating']].describe().T
    text_summary=textual[['text_character_count']].describe().T
    restaurant_missing=missing_table(restaurants)
    review_missing=missing_table(reviews)
    rating_counts=rated['rating'].value_counts().sort_index().rename('review_count').to_frame()
    rating_counts['share']=rating_counts['review_count']/len(rated)
    yearly=dated.groupby('year').agg(review_count=('review_id','size'),restaurant_count=('restaurant_id','nunique'),
                                   mean_rating=('rating','mean'),median_rating=('rating','median'),text_count=('has_text','sum'))
    primary_status=reviews['primary_analysis_status'].value_counts(dropna=False).rename('review_count').to_frame()
    inspection_counts=pd.Series({label:int(restaurants[col].sum()) for label,col in INSPECTION_RESULTS.items()},name='event_count').to_frame()
    inspection_counts['share_of_all_events']=inspection_counts['event_count']/restaurants['inspection_count'].sum()
    # Course crosstab method: categorical rating levels versus text availability.
    text_by_rating=pd.crosstab(rated['rating'],rated['has_text'],normalize='index').rename(columns={False:'without_text_share',True:'with_text_share'})
    categories=restaurants['categories_json'].dropna().map(json.loads).explode().value_counts().rename('restaurant_count').to_frame()
    # These correlations use review-era summaries only, not later inspection outcomes.
    review_features=restaurants[['review_count','mean_export_rating']].copy()
    review_features['text_share']=restaurants['reviews_with_text']/restaurants['review_count']
    correlations=review_features.corr()
    outputs={'restaurant_numeric_summary':restaurant_summary,'review_rating_summary':review_summary,'review_text_length_summary':text_summary,
        'restaurant_missingness':restaurant_missing,'review_missingness':review_missing,'rating_distribution':rating_counts,
        'reviews_by_year':yearly,'primary_analysis_status':primary_status,'inspection_outcomes':inspection_counts,
        'text_availability_by_rating':text_by_rating,'restaurant_categories':categories,'review_summary_correlations':correlations}
    for name,frame in outputs.items(): frame.to_csv(output/(name+'.csv'))
    info=io.StringIO()
    restaurants.info(buf=info)
    info.write('\nREVIEW TABLE\n')
    reviews.info(buf=info)

    plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False})
    fig,ax=plt.subplots(2,2,figsize=(12,9),layout='constrained')
    ax[0,0].hist(restaurants['review_count'],bins=25,color='#237f89')
    ax[0,0].set(title='Review volume by restaurant',xlabel='Deduplicated reviews',ylabel='Restaurants')
    ax[0,1].boxplot(restaurants['review_count'].to_numpy(dtype=float),vert=False)
    ax[0,1].set(title='Review-volume outliers',xlabel='Deduplicated reviews',yticks=[])
    ax[1,0].hist(restaurants['mean_export_rating'],bins=20,color='#617bad')
    ax[1,0].set(title='Mean exported rating by restaurant',xlabel='Mean rating (1–5)',ylabel='Restaurants')
    ax[1,1].scatter(restaurants['review_count'],restaurants['mean_export_rating'],s=20,alpha=.65,color='#397758')
    ax[1,1].set(xscale='log',title='Review volume and mean rating',xlabel='Review count (log scale)',ylabel='Mean exported rating')
    fig.savefig(output/'restaurant_profiles.png',dpi=160)
    plt.close(fig)
    fig,ax=plt.subplots(2,2,figsize=(12,9),layout='constrained')
    ax[0,0].bar(rating_counts.index.astype(int),rating_counts['review_count'],color='#617bad')
    ax[0,0].set(title='Google rating distribution',xlabel='Stars',ylabel='Reviews',xticks=[1,2,3,4,5])
    ax[0,1].bar(yearly.index,yearly['review_count'],color='#237f89')
    ax[0,1].set(title='Review counts by Chicago-local year\nValid dates only; boundary years are partial',xlabel='Year',ylabel='Reviews')
    # Long texts are retained; log scaling makes their distribution readable.
    ax[1,0].hist(np.log1p(textual['text_character_count'].to_numpy(dtype=float)),bins=30,color='#ac7948')
    ax[1,0].set(title='Length of available review text',xlabel='log(1 + character count)',ylabel='Reviews with text')
    ax[1,1].bar(['Text available','Text absent'],[len(textual),len(rated)-len(textual)],color=['#397758','#adb8b8'])
    ax[1,1].set(title='Text availability among valid ratings',ylabel='Reviews')
    fig.savefig(output/'review_profiles.png',dpi=160)
    plt.close(fig)
    fig,ax=plt.subplots(1,2,figsize=(14,5),layout='constrained')
    inspection_counts['event_count'].sort_values().plot.barh(ax=ax[0],color='#ac7948')
    ax[0].set(title='All accepted inspection-history outcomes',xlabel='Inspection events (not restaurants)',ylabel='')
    shown=restaurant_missing.loc[restaurant_missing['missing_count'].gt(0)].head(8)
    if len(shown): (shown['missing_fraction']*100).sort_values().plot.barh(ax=ax[1],color='#617bad')
    ax[1].set(title='Restaurant fields with missing values',xlabel='Percent of restaurants',ylabel='')
    fig.savefig(output/'inspection_and_missingness_profiles.png',dpi=160)
    plt.close(fig)

    summary={'restaurants':len(restaurants),'reviews':len(reviews),'restaurant_attributes':len(restaurants.columns),'review_attributes':len(reviews.columns),
        'inspection_events':int(restaurants['inspection_count'].sum()),'valid_date_reviews':len(dated),'flagged_date_reviews':len(rated)-len(dated),
        'reviews_with_text':len(textual),'missing_text_fraction':1-len(textual)/len(rated),'review_weighted_mean_rating':float(rated['rating'].mean()),
        'restaurant_weighted_mean_rating':float(restaurants['mean_export_rating'].mean()),'median_rating':float(rated['rating'].median()),
        'five_star_share':float(rated['rating'].eq(5).mean()),'low_rating_share':float(rated['rating'].le(2).mean()),
        'median_reviews_per_restaurant':float(restaurants['review_count'].median()),'min_reviews_per_restaurant':int(restaurants['review_count'].min()),
        'max_reviews_per_restaurant':int(restaurants['review_count'].max()),'valid_review_dates':date_range(dated['review_date_chicago']),
        'inspection_dates':[restaurants['inspection_first_date'].min().date().isoformat(),restaurants['inspection_last_date'].max().date().isoformat()],
        'primary_reviews':len(primary),'primary_restaurants':primary['restaurant_id'].nunique(),
        'primary_months':len(primary[['restaurant_id','review_month']].drop_duplicates()),
        'failure_share_among_scored_inspections':float(restaurants['inspection_fail_count'].sum()/restaurants['scored_inspection_count'].sum()),
        'validation':'passed: primary/foreign keys, counts, review flags, and strictly prior inspection dates'}
    (output/'analysis_summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    report=[__doc__,'\nCOMPUTED DATASET SUMMARY\n',json.dumps(summary,indent=2),
        '\nDATA PREPARATION AND DENOMINATORS\n',
        'These final files contain the full exact-match business cohort. The review table retains flagged dates and records outside the inspection observation span, with explicit eligibility flags. Date-independent rating statistics use valid ratings, yearly statistics use valid local dates, and prior-inspection comparisons must use included_in_primary_month. Missing text is not a zero rating or neutral sentiment.',
        'Restaurant inspection counters summarize all accepted historical events. The failure share denominator includes only Pass, Pass w/ Conditions and Fail. Those histories extend beyond the review era, so their aggregates are not used here as contemporaneous rating predictors. Averages over reviews and averages over restaurants have different weights.',
        '\nNUMERIC RESTAURANT SUMMARY\n',restaurant_summary.to_string(),
        '\nRATING SUMMARY\n',review_summary.to_string(),'\nTEXT LENGTH SUMMARY (nonblank text only)\n',text_summary.to_string(),
        '\nRATING FREQUENCIES\n',rating_counts.to_string(),'\nREVIEW YEARLY PROFILE (valid dates only)\n',yearly.to_string(),
        '\nINSPECTION EVENT OUTCOMES\n',inspection_counts.to_string(),'\nPRIMARY ANALYSIS ELIGIBILITY\n',primary_status.to_string(),
        '\nRESTAURANT MISSING VALUES\n',restaurant_missing.to_string(),'\nREVIEW MISSING VALUES\n',review_missing.to_string(),
        'Missing prior_inspection_* values are structural for reviews excluded from the primary monthly analysis. Blank unit fields mean no unit was supplied. Empty optional metadata is not imputed. A parseable pre-2010 timestamp is retained but excluded from date-dependent analysis because it predates inspection coverage; it is not necessarily an erroneous timestamp.',
        '\nTEXT AVAILABILITY BY RATING (row proportions)\n',text_by_rating.to_string(),
        '\nTOP 15 CATEGORIES (multiple categories per restaurant)\n',categories.head(15).to_string(),
        '\nREVIEW-SUMMARY CORRELATIONS\n',correlations.to_string(),
        '\nINTERPRETATION\n',
        f"The review-weighted mean is {summary['review_weighted_mean_rating']:.3f}; the equal-restaurant mean is {summary['restaurant_weighted_mean_rating']:.3f}. Five-star ratings account for {summary['five_star_share']:.1%}, while ratings of 1 or 2 account for {summary['low_rating_share']:.1%}.",
        f"The median restaurant has {summary['median_reviews_per_restaurant']:.0f} reviews, compared with a maximum of {summary['max_reviews_per_restaurant']:,}. Review-level estimates give heavily reviewed restaurants more influence; the histogram and boxplot show that imbalance.",
        f"Text is unavailable for {summary['missing_text_fraction']:.1%} of valid rating records. A text or sentiment analysis therefore describes a smaller, potentially different subset. The script describes text length only and does not infer sentiment.",
        f"There are {summary['flagged_date_reviews']:,} retained records with dates excluded from temporal analysis. The yearly chart uses only valid dates, and the first/last observed years are not complete observation periods. The data do not establish activity in months without reviews.",
        'The final 252 businesses are a conservative subset of 8,317 Google Chicago restaurant candidates. Exact-match selection favors stable names, addresses, licenses and complete location evidence. Whole-history verification uses later observations; this is a retrospective cohort rather than a prospective prediction sample.',
        'Outcome counts are event-level, not the percentage of restaurants that are unsafe. Inspection types, repeated visits, changing rules and unequal follow-up affect comparisons. Correlations in the review-summary table are descriptive and do not demonstrate causality or significance. All source/collection details and column definitions are in dataset_description.txt.',
        '\nDATA TYPES AND NONNULL COUNTS\n',info.getvalue(),
        '\nGENERATED FIGURES\n','restaurant_profiles.png; review_profiles.png; inspection_and_missingness_profiles.png.',
        '\nMETHOD TRACEABILITY\n','The notebook paths and cell indices are documented in the script header and dataset_description.txt. Summary tables use describe, isna, value_counts, groupby/agg, crosstab and corr; visualizations use histograms, boxplots, scatterplots and frequency bars.']
    (output/'descriptive_analysis_report.txt').write_text('\n\n'.join(report)+'\n',encoding='utf-8')
    print(json.dumps(summary,indent=2))
    print('Saved report, tables, and figures to',output)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Describe the two cleaned final CSV datasets.')
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parent)
    analyze(parser.parse_args().data_dir)
