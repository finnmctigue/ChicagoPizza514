# ChicagoPizza514

Historical Chicago restaurant reviews and food inspections. The current scope is
all Chicago restaurants, following [merge_plan.md](merge_plan.md).

## Initial exact merge

Run from this directory with Python 3.13, `pyarrow`, `pandas`, and timezone data:

```powershell
python -u build_initial_merge.py
python -m pytest -q -p no:cacheprovider test_initial_merge.py
python verify_initial_merge.py
```

The primary merged dataset is
`data/processed/initial_exact/restaurant_month.parquet`, with an identical
spreadsheet-friendly `restaurant_month.csv`. Each row is one accepted Google
restaurant/business episode/calendar month with observed reviews, linked to the
latest unambiguous inspection strictly before that month. Dates use
`America/Chicago`.

Check that `data/reports/initial_merge_audit.json` has `status: complete` before
using outputs. `--match-only` prepares scope and crosswalk tables without scanning
reviews; it does not produce a completed monthly dataset. Reruns replace this
pipeline's derived files. Raw sources are never modified. Large derived outputs
and temporary files are ignored by Git.

### Exact acceptance policy

Only links passing **all** these checks are accepted:

- Exact normalized Google name and every inspection DBA name; every nonblank AKA
  must agree too. Normalization handles case, accents, punctuation, apostrophes,
  and `&`/`AND`, but does not remove company suffixes or use fuzzy similarity.
- Exact explicitly parsed street, house number/suffix, unit, Chicago/IL locality,
  and five-digit ZIP. Ranges and unresolved building/unit layouts are pending.
- One inspection license and one Google place at the address, including
  nonrestaurant competitors. Overlapping house-number ranges block acceptance.
- The entire license history has the same identity/location and restaurant
  facility type. No terminal status may precede a later scored inspection.
- All coordinates are populated, pass the broad Chicago sanity screen, and lie
  within **25 meters** between Google and every inspection. This corroborates an
  exact match; it never generates a spatial-only match.

Accepted links are verifiable against the supplied source records. These checks
do **not** independently prove ownership, opening/closing dates, or error-free
source data. `business_valid_from` and `business_valid_to` remain empty. The
observed scored-inspection span is separately recorded and is not represented as
a proven operating interval. The 25-meter cutoff is a conservative policy, not a
calibrated probability of a correct link.

### Review and month eligibility

Google reviews are streamed after selecting Chicago restaurant IDs. Full-record
SHA-256 deduplication spans the complete selected stream, including writer batch
boundaries. Conflicting `(user_id, gmap_id, time)` variants are all retained and
flagged; none enter the primary monthly analysis. Missing-user and invalid-rating
records remain in the scoped review table but are ineligible for the primary
dataset. Ratings without text remain eligible for rating aggregates.

Dates are preserved as raw values, UTC timestamps, and Chicago local dates.
Unparseable dates, pre-2010 dates outside inspection coverage, and dates after the
fixed November 22, 2026 audit date are excluded from monthly analysis, not guessed
or rewritten. Missing values are not filled with zeros.

A primary month must be wholly bracketed by scored inspection observations:
its first day must follow the first scored observation, and the first day of the
next month must be no later than the last scored observation. The selected prior
event must be unique on its date, be Pass / Pass w/ Conditions / Fail, and be no
more than **365 days** old. The one-year cap is an explicit initial policy to
avoid carrying old outcomes forward indefinitely; it is not a validated shelf
life for inspection evidence. A recent No Entry event or same-day tie excludes
the month; the pipeline does not skip backward to find a convenient Pass.

The review flag `eligible_for_monthly_candidate` precedes these month-level
checks. Use `monthly_eligibility_audit.csv` for final inclusion/exclusion. Only
months with observed eligible reviews are emitted; absence of a row does not
prove zero review activity. `text_count` counts eligible ratings with text;
`low_rating_share` is the share rated 1 or 2.

Whole-history screening and bracketing use later observations to select a
conservative **retrospective** cohort. Although the joined inspection always
precedes the month, this dataset is not a ready-made prospective prediction
training cohort. Its selection favors stable identities and sufficient coverage.
Undated Google snapshot ratings and operating status are kept in the place table,
not included as monthly predictors.

### Outputs

All large tables are under `data/processed/initial_exact/`:

| File | Grain and purpose |
| --- | --- |
| `restaurant_month.parquet` / `.csv` | Primary merged table: accepted restaurant episode, source, and month |
| `chicago_restaurants.parquet` | One core Chicago Google place, including unmatched places |
| `chicago_restaurant_reviews.parquet` | One deduplicated core review, with IDs and eligibility/conflict flags |
| `chicago_restaurant_inspections.parquet` | One core inspection event, including unmatched events |
| `restaurant_inspection_crosswalk.csv` | One place/license candidate with evidence, source Inspection IDs, decision, and reasons; unmatched places have an empty-license row |
| `place_scope_audit.parquet` | Every deduplicated Google place with geographic/category scope |
| `inspection_scope_audit.parquet` | Every inspection event with its scope decision |
| `monthly_eligibility_audit.csv` | Candidate review months with their first exclusion reason |
| `filter_audit.json` | Source hashes, filter reconciliation, coverage, parameters, and persisted-output checks |

The compact audit and accepted business-link evidence are also available in
`data/reports/initial_merge_audit.json` and
`data/reports/initial_merge_accepted_links.csv`. Exclusion-reason diagnostic flags
can overlap; ordered filter counts are separately labeled and reconcile.

The Yelp business file is rechecked, including a broad coordinate screen. Its
current Chicago restaurant cohort is empty, so dependent Yelp review/tip/check-in/
user files are not scanned or merged. New eligible Yelp coverage causes the build
to stop for a scope update rather than silently ignoring it.

Open `pizza.ipynb` to inspect the completed monthly table, audit counts, and small
source-table previews without loading the raw statewide review file.

## Final restaurant and review files

`data/final data/` contains two linked CSV datasets:

- `cleaned_restaurants.csv`: one row for each of the 252 accepted restaurants,
  combining Google metadata with inspection-history and review summaries.
- `reviews.csv`: 125,254 deduplicated reviews belonging to those restaurants.
  Join on `restaurant_id`. All 252 restaurants have reviews. The flag
  `included_in_primary_month` identifies the narrower 103,049-review temporal
  subset covering 249 restaurants. The 44 pre-2010 reviews remain available for
  date-independent ratings and are explicitly excluded from temporal analysis.

`dataset_description.txt` documents sources, collection/provenance limits,
preparation, every final attribute, date ranges, benefits, and limitations.
Open `descriptive_analysis.ipynb` and run all cells to see the analysis, tables,
and three charts inline. It uses descriptive methods from the local `datamining`
and `ISA591` course notebooks, with exact notebook/cell references, and saves
tables, figures, and a narrative report in `data/final data/analysis_outputs/`.
The original `descriptive_analysis.py` remains available for command-line runs.

```powershell
python prepare_final_data.py
python "data/final data/descriptive_analysis.py"
```

The export uses the verified processed tables and validates keys, counts, and
monthly review membership. Read IDs/ZIPs as strings; the analysis script provides
an example. The final CSVs and generated analysis outputs stay local and are
ignored by Git. Their preparation audit is `data/reports/final_data_build_audit.json`.
