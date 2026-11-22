# Plan: Chicago restaurant reviews and inspections

Updated November 18, 2026. The target is **all Chicago restaurants**, not only pizza restaurants. This is a plan for preparing the data; candidate business matches are not yet verified.

Implementation update, November 22, 2026: `build_initial_merge.py` implements a
deliberately narrower first pass using exact, corroborated links only. See
`README.md` for its acceptance and temporal policies and
`data/reports/initial_merge_audit.json` for build status and counts. Its outputs
are under `data/processed/initial_exact/`; fuzzy and unresolved candidates remain
outside the primary merged table. Acceptance verifies agreement within the source
records, not independent ownership or business opening/closing dates.

## 1. Source inventory and coverage

All Google/Yelp JSON files are JSON Lines (one object per line), read with `lines=True`. Inspections are CSV. A `head(5)` display does not restrict the underlying read or establish geographic coverage.

| File | Purpose | Identifier/filter |
| --- | --- | --- |
| `meta-Illinois.json` | Identify Google restaurants and their locations | Select `gmap_id` using address and category |
| `review-Illinois.json` | Dated Google ratings and review text | Keep selected `gmap_id` values |
| `Food_Inspections_20261103.csv` | Inspection events and business histories | Preserve `Inspection ID`; use license, address, DBA/AKA names to establish identities |
| `yelp_academic_dataset_business.json` | Determine Yelp geographic/restaurant coverage | Select `business_id` using city, state, categories |
| `yelp_academic_dataset_review.json` | Yelp reviews, if eligible businesses exist | Filter selected `business_id`; preserve `review_id` |
| `yelp_academic_dataset_tip.json` | Optional tips; not rated reviews | Filter selected `business_id`; keep separately |
| `yelp_academic_dataset_checkin.json` | Optional activity; not reviews | Filter selected `business_id`; `date` is a list encoded as text |
| `yelp_academic_dataset_user.json` | Optional reviewer attributes | Filter user IDs from eligible Yelp reviews; not needed for the core merge |

### Yelp cannot currently supply a Chicago restaurant cohort

A full scan of the uploaded Yelp business file found **150,346 businesses**, **2,145 in Illinois**, and **zero** with normalized city `CHICAGO`, state `IL`, and an exact `Restaurants` token in the comma-separated categories.

The one Chicago/IL record is North Avenue Collective, a retail shop whose coordinates also conflict with Chicago. Another record has city `chicago` but state `FL`. City alone is insufficient. Most of the Illinois coverage is around places such as Belleville, Edwardsville, and Fairview Heights.

Use **Google reviews plus Chicago inspections** for the primary dataset. Record the Yelp coverage result, and skip scanning its dependent review/tip/check-in/user files when the eligible business ID set is empty. Do not substitute statewide Illinois businesses for Chicago restaurants. Before final exclusions, inspect Yelp records with coordinates near Chicago but inconsistent locality fields for possible recoverable location errors; require restaurant evidence as well. If eligible businesses emerge or new data arrives, apply the Yelp workflow described below.

### Existing profile: useful baseline, not the new cohort

`merge_input_profile.json` reports 179,205 Google metadata rows, 178,204 unique places, 23,096,838 reviews, and 317,961 inspections. Normalized Chicago/IL inspections total 317,328 before restaurant filtering. Inspection dates span January 4, 2010 through October 29, 2026; statewide Google review timestamps end September 8, 2021.

The old 705-place cohort, review deduplication counts, and match-candidate rates in `analyze_merge_inputs.py` are pizza-specific. Recompute them for all Chicago restaurants; do not reuse them as accepted-match counts. Preserve the identity/address cautions in `instructions.txt`, but broaden its pizza-only scope.

## 2. Define Chicago restaurants before extracting reviews

### Google metadata

1. Process the complete metadata file, not a 1,000-row preview. Remove exact duplicates and investigate conflicting records sharing `gmap_id` before creating a unique place table.
2. Parse `address` into street, unit, city, geographic state, and ZIP, preserving raw values. Google's metadata `state` is business status, not Illinois; never use it as the geographic state filter.
3. Select parsed Chicago, IL addresses. Queue missing/misspelled localities and coordinate/address conflicts for review. Exclude suburbs; ZIP codes or proximity alone do not establish city membership. If a municipal boundary polygon is added, record its source and use it to resolve uncertain coordinates. Until then, describe selection as address-based, not a verified city-boundary test.
4. Evaluate every item in the `category` list using case/whitespace normalization. Include exact `Restaurant` and categories ending in `restaurant`, plus exact `Bar & grill`, `Diner`, `Sandwich shop`, `Bistro`, and `Cr?perie`.
5. Keep cafe/cafeteria-only businesses as a separately labeled expansion cohort pending meal-service review. Do not automatically include standalone bars, bakeries, coffee shops, groceries, caterers, dessert shops, or generic `Food`/`Food service` entries. A qualifying restaurant category alongside one of these still qualifies. Explicitly exclude `Restaurant supply store`; a loose substring search would include it incorrectly.
6. Save `geography_status`, `restaurant_scope` (core/expanded/pending/excluded), and inclusion/exclusion reasons. Profile all categories and review borderline cases before freezing the rules. Do not require pizza in the name or categories.
7. Retain historical closed restaurants. Current closure status and undated categories are snapshot evidence, not proven historical operating periods.

### Inspections

1. Normalize `City` and `State`; select Chicago/IL. Review missing/misspelled localities using address and coordinate evidence instead of silently guessing.
2. Start with normalized `Facility Type == RESTAURANT`. Profile all labels and explicitly map additional restaurant variants. Retain missing/inconsistent facility labels in a candidate pool; admit them only with documented restaurant evidence.
3. Exclude school, daycare, hospital, grocery-only, and other nonrestaurant facilities from the core scope. A restaurant inside a mixed-use building requires tenant evidence; a shared address alone cannot override an exclusion.
4. Keep every eligible inspection event, including restaurants without reviews or Google matches. Preserve type, date, result, violations, and coordinates.
5. Keep `Pass`, `Pass w/ Conditions`, and `Fail` distinct. `No Entry`, `Not Ready`, `Out of Business`, and `Business Not Located` are separate statuses, not automatic passes/failures. Distinguish routine inspections, complaints, and re-inspections.

## 3. Stream reviews for selected businesses

Build the selected Chicago restaurant `gmap_id` set first. Stream the 7.16 GB Google review file with `pd.read_json(..., lines=True, chunksize=100_000, convert_dates=False)` or a line-by-line reader. Filter each chunk and write retained records incrementally to Parquet. Do not accumulate all statewide chunks in memory.

- Preserve IDs as strings. Rename Google review `name` to `reviewer_name` and metadata `name` to `place_name`; reviewer names must not be business-match keys.
- Convert Google `time` with `pd.to_datetime(..., unit="ms", utc=True, errors="coerce")`, then convert to `America/Chicago` for local dates. Preserve raw timestamps.
- Validate ratings, text, dates, and user IDs independently. Keep valid ratings without text for rating analysis; require text only for sentiment analysis. Flag missing values rather than silently filling them.
- Remove identical full-record duplicates across the entire retained subset, including across chunk boundaries. Generate a stable retained-record ID. Flag conflicting `(user_id, gmap_id, time)` records instead of choosing an arbitrary copy; keep missing-user records separately identifiable.
- Investigate implausible early timestamps, including the 1990 dates in the earlier profile. Exclude unresolved dates from date-dependent analysis without rewriting them to guessed dates.
- Record counts before/after each filter and deduplication step.

If Yelp has eligible businesses after geography review or a future data update, stream reviews by `business_id` and validate `review_id` uniqueness. Parse review dates, verifying timezone conventions before alignment. Keep tips and check-ins separate from rated reviews. Only read the large user file if reviewer attributes are required, filtering to selected reviewers. User snapshot statistics must not become historical predictors without a valid observation date. Google and Yelp user IDs are unrelated.

## 4. Establish restaurant identities before joining histories

Google `gmap_id`, Yelp `business_id`, and inspection `License #` are different ID systems. Build a crosswalk rather than directly merging them.

1. Normalize names, punctuation, whitespace, street directions, and street suffixes while preserving raw values. Parse address ranges, letter suffixes, business/building prefixes, and units. Use base addresses to generate candidates; retain units to distinguish tenants.
2. Build inspection business histories using license + address + DBA/AKA evidence. A license can move and an address can have successive operators. Flag name/location changes; assign business episode IDs only when evidence supports a coherent identity. First/last observed inspections are not proven opening/closing dates.
3. Generate same-address candidates, comparing Google names against both DBA and AKA names. Validate unique exact address/name candidates first, with coordinate and unit checks.
4. Review fuzzy-name matches at the same address. For address discrepancies, search nearby coordinates (100 meters as an initial candidate radius), requiring supporting name/address evidence. Calibrate thresholds using reviewed examples; proximity alone is insufficient.
5. Record accepted, rejected, pending, and unmatched decisions. Review multiple licenses, shared buildings, chains, changed operators, and spatial conflicts. Sample apparently clear matches as well. Leave unsupported links unmatched.
6. Apply historical restrictions where supported. Unknown episode boundaries stay unknown; uncertain events must not be assigned to a different operator.

Crosswalk fields: `source`, `source_place_id`, `restaurant_id`, `business_episode_id`, license, raw/normalized names and addresses, similarity score, distance, candidate count, method, decision, notes, and evidenced validity dates. Multiple historical licenses may belong to a restaurant only after validation.

## 5. Separate scoped data from the matched analysis subset

Keep raw files unchanged. Write derived outputs under `data/processed/` and aggregate diagnostics under `data/reports/`. Ensure large derived files are ignored by Git before generating them; existing ignore rules do not cover all Yelp or derived files.

| Proposed output | Grain and purpose |
| --- | --- |
| `chicago_restaurants.parquet` | One source place per `(source, source_place_id)`, with scope and match status |
| `chicago_restaurant_reviews.parquet` | One retained review, including unmatched places and eligibility flags |
| `chicago_restaurant_inspections.parquet` | One `Inspection ID`, including restaurants without reviews |
| `restaurant_inspection_crosswalk.csv` | One candidate source-place/business-episode pair with evidence and decision |
| `restaurant_month.parquet` | One accepted restaurant episode, review source, and Chicago calendar month |
| `filter_audit.json` | Counts, exclusions, conflicts, duplicates, date ranges, and coverage by source |

Reviews join to places many-to-one. Inspections attach only through accepted links and their historical restrictions. Resolve inspections claimed by multiple source places before assigning analysis identities. Preserve unmatched cohorts to show coverage losses.

Never join every raw review to every inspection for a restaurant: this creates reviews ? inspections rows. For the default comparison table, aggregate reviews by restaurant episode/source/month: review count, mean valid rating, low-rating share, and text count. Then attach the latest accepted inspection strictly before the month begins, retaining result, type, date, and age. Resolve same-day ties with a documented day-level summary or flag ambiguity; do not choose arbitrarily.

No prior inspection means missing evidence, not a pass. Zero-review months have null ratings, not zero ratings. Observation gaps are not proven zero-activity periods. Recompute the full restaurant cohort's date overlap and report partial years and per-restaurant coverage. Retain all scoped dates in source tables, but use valid overlapping coverage for comparisons. Google coverage ends in 2021 in the earlier profile, so later inspections cannot represent conditions preceding those reviews. Flag stale inspection histories and evaluate a justified maximum lag.

If the modeling objective becomes predicting inspection outcomes, use one row per inspection with reviews strictly before its inspection date and a predefined lookback window. Undated metadata ratings, review counts, hours, and status must not be historical predictors. Define eligible outcomes explicitly for failure-rate calculations.

## 6. Implementation order and acceptance checks

1. Profile full metadata, Yelp businesses, and inspections. Save category/location counts and the Yelp coverage limitation.
2. Freeze geography and restaurant rules; export scoped places and inspection candidates with excluded/pending reasons.
3. Stream selected Google reviews, validate, and deduplicate across chunks. Skip dependent Yelp scans when its eligible business set is empty.
4. Generate and review crosswalk candidates; apply accepted historical identities. Exclude unresolved links from the primary matched analysis.
5. Build the monthly comparison table and coverage report.
6. Update `pizza.ipynb` to preview processed tables and show audit summaries. Keep sample previews clearly distinguished from full processing.

Acceptance checks:

- Place and inspection keys are unique at their declared grains; review deduplication spans chunk boundaries.
- Every retained review references an eligible Chicago restaurant; every matched inspection belongs to the intended business episode.
- Counts reconcile from input through retained/excluded/duplicate/pending records using a documented filter order. Label overlapping diagnostic flags separately.
- Joins preserve expected row counts; aliases and competing links cannot multiply records.
- Temporal features precede cutoffs; unresolved dates and episode boundaries cannot enter the primary analysis.
- Report matched/unmatched restaurant, review, and inspection counts separately, with date coverage and exclusions.
- Compare matched versus unmatched coverage and core versus expanded restaurant definitions. Do not present the matched subset as a complete Chicago census.

### Notebook implementation

`pizza.ipynb` now reads the completed initial merged table and audit summaries.
It displays explicitly limited Parquet previews of scoped places, reviews, and
inspections; it no longer loads the raw statewide review file. Full processing is
performed by `build_initial_merge.py`, with validation in `test_initial_merge.py`
and an independent source/aggregate check in `verify_initial_merge.py`.
