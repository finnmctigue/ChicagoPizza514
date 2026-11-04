# Plan for merging Google places, reviews, and food inspections

Analyzed November 4, 2026. This plan follows the cautions in
[instructions.txt](instructions.txt). Counts come from a full scan of the three
local files, reproduced by `python analyze_merge_inputs.py` and saved in
[merge_input_profile.json](merge_input_profile.json). Candidate matches have not
been approved, and no merged dataset has been produced.

## What the data supports

| Dataset | Observed size and coverage | Merge implication |
| --- | --- | --- |
| Metadata | 179,205 rows; 178,204 distinct `gmap_id` values; 192.6 MB | The 1,001 extra rows are identical full-record duplicates and can be removed. There is no metadata snapshot date. |
| Reviews | 23,096,838 rows; 7.16 GB; every review's `gmap_id` exists in metadata, and every metadata place has at least one review row | Reviews link directly to deduplicated metadata using `gmap_id`. Stream the file rather than loading it all at once. |
| Inspections | 317,961 rows; 356.2 MB; all `Inspection ID` values unique; January 4, 2010 through October 29, 2026 | These are repeated inspections, not unique businesses. There is no direct Google identifier. |

Sizes use decimal units. The initial cohort is Chicago places with at least one
category containing "pizza", consistent with this repository's project. It has
705 unique places and 319,340 review rows. This is a category-based starting
cohort, not a complete census of pizzerias. Inspect ambiguous categories and
name-based candidates separately before expanding it.

### Quality and coverage findings

- All 705 pizza places have review rows. Removing 4,570 exact duplicate copies
  leaves 314,770 distinct full review records. There are also nine repeated
  `(user_id, gmap_id, time)` keys whose full contents differ; these need review
  before choosing a final deduplication rule.
- Pizza reviews end on August 17, 2021, in UTC. The statewide review file ends
  September 8, 2021. The main pizza/inspection analysis must therefore be
  historical; 2022-2026 inspections cannot describe conditions preceding these
  reviews.
- There are 49 distinct pizza review records before 2010, including one dated
  December 31, 1990. Treat the early timestamp as suspicious pending validation;
  do not silently repair it. Reviews before inspection coverage cannot receive
  a preceding inspection from this file.
- After exact deduplication, exported review counts equal metadata
  `num_of_reviews` for 496 pizza places and are lower for 209. Do not assume the
  exported reviews reconstruct every metadata rating or its observation date.
- Statewide, 9,984,559 review rows (43.2%) have no nonempty text. There are
  134,508 rows without a rating and the same number without a user ID; their
  overlap has not been checked. Validate each field independently. Keep valid
  ratings without text for rating analysis; text is required only for sentiment.
- Metadata marks 121 pizza places "Permanently closed" and has no state value
  for 193. Retain historical businesses. A state such as "Closed - Opens 11AM"
  describes opening hours, not permanent closure.
- Inspections have 859 missing/zero license values and 1,064 missing coordinate
  pairs. Normalizing city case and whitespace selects 317,328 Chicago/IL rows;
  misspelled cities and missing addresses in metadata require separate checks.
- In the Chicago inspection subset, 319 valid licenses occur at multiple
  normalized addresses, and 1,078 license/address groups have multiple normalized
  DBA names. These are review flags: spelling, aliases, and ownership changes
  must be distinguished rather than assumed equivalent.

## Recommended workflow

### 1. Build a place table and stream its reviews

Preserve source files and their original values. Keep IDs as strings, including
`gmap_id`, `user_id`, and `License #`; keep ZIP codes as strings too. Deduplicate
identical metadata records before any review join. Rename metadata `name` to
`place_name` and review `name` to `reviewer_name`: the review field identifies a
person and must never be used for restaurant matching. Keep metadata `state`
separate from inspection `State` (business status versus geographic state).

Parse addresses into house number/range, street direction/name/type, unit,
city, state, and ZIP. Preserve raw values. Handle building labels, units, ranges,
and suffixes such as `4202A` explicitly. Use the normalized base street for
candidate generation while retaining units to distinguish tenants. Address-city
selection in this profile is not a Chicago boundary test; assess missing or
misspelled cities with coordinates rather than silently dropping them.

Build a set of selected `gmap_id` values and read `review-Illinois.json` one line
or chunk at a time. Retain only the relevant records in processed outputs. Parse
`time` as Unix milliseconds in UTC, then convert to America/Chicago before
assigning local dates or months. Chicago local dates are appropriate for linking
to inspections, which contain dates without times. Profile dates above are UTC.

Remove exact duplicate full records. Flag the nine conflicting identity keys;
do not resolve them by arbitrarily keeping the first or latest timestamp, since
their timestamps are identical. Generate a stable internal review identifier
from the retained content and retain source provenance. Exclude unresolved
conflicts from the primary analysis while reporting their count.

### 2. Resolve inspection business identities before attaching history

Preserve every `Inspection ID` and its original DBA/AKA names, license, address,
date, facility type, inspection type, result, and coordinates. Build candidate
business histories from license + normalized address + name/alias evidence.
Do not collapse all inspections sharing a license, address, or current name.

Record first and last observed inspection dates, but do not treat them as proven
opening/closing dates. Split or flag histories when names or locations change.
Assign internal business/episode IDs only after reviewing the evidence. Keep
restaurant-like facility records as the main pool while reviewing missing or
inconsistent facility labels instead of requiring exactly `Restaurant`.

### 3. Match Google places to inspection business histories in stages

The improved preliminary address parser removes units for blocking and handles
building labels. Its counts supersede the simpler initial pass in
`instructions.txt`; that historical note does not need to be overwritten.

| Preliminary evidence among 705 pizza places | Places | Interpretation |
| --- | ---: | --- |
| Exact normalized base-street candidate | 599 | Address evidence only. |
| Exact street plus name matching DBA or AKA | 287 | Stronger candidates; not confirmed links. |
| Exact street/name candidates associated with one valid license | 172 | First group to validate; still requires conflict and date checks. |
| Street candidates include multiple licenses | 433 | Shared premises and successive operators make address-only joins unsafe. |
| No exact street, but an exact-name candidate within 100 meters | 40 | Additional candidates for address/range/entrance review. |

For the 287 street/name candidates, 282 have a nearest matching coordinate
within 100 meters and five do not. Sixteen have an exact-name candidate whose
license/address group contains multiple normalized DBA names. These groups can
overlap; do not subtract them to invent an accepted-match count.

Validate the strongest group first: address, DBA/AKA name, coordinate consistency,
unit/tenant evidence, and compatibility with the reviewed historical business.
Next compare fuzzy names at the same address. Finally, consider nearby candidates
for discrepant addresses, requiring supporting name and address evidence.
The 100-meter radius is a candidate-search setting, not an acceptance threshold.
Calibrate name scores and distance limits against manually reviewed examples.

Review all ambiguous links, multiple licenses, changed names/locations, shared
buildings, and spatial disagreements. Check samples of apparently clear matches
and unmatched places as well. Record competing candidates and rejection reasons.
Leave unresolved places unmatched. Never match a chain solely by its name or
nearest location, or equate a same-address successor with its predecessor.

Examples needing care include multiple operators at 945 W Randolph St, the food
court at 500 W Madison St, and Navy Pier at 600 E Grand Ave. Pepperino Pizzeria
at 1501 W Madison St has exact-name candidates under two licenses; both require
historical validation rather than choosing a license arbitrarily.

### 4. Keep the joins auditable and control their row counts

Use separate tables with explicit grains:

| Table | One row per | Main key/link |
| --- | --- | --- |
| `places` | Deduplicated Google place | `gmap_id` |
| `reviews` | Retained review record | Internal review ID; `gmap_id` foreign key |
| `inspections` | Source inspection | `Inspection ID`; reviewed business/episode ID where established |
| `place_inspection_links` | Reviewed Google-place/business-episode link | `gmap_id`, business/episode ID |
| `restaurant_month` | Place and local calendar month | `gmap_id`, month |

The link table should retain license(s), raw and normalized names/addresses,
name score, distance, match method, candidate ambiguity, approved/rejected/pending
status, temporal evidence, and review notes. Record validated date restrictions
when available; flag unknown episode boundaries instead of inventing them.

Require the reviews-to-places join to be many-to-one. Attach inspections through
approved links and their reviewed history; check every inspection's name,
location, and applicable period. Resolve inspections claimed by multiple places
before aggregation. Keep unmatched places and their reviews visible.

Never directly join raw reviews and raw inspections on a place identifier:
R reviews and I inspections would produce R x I rows and inflate counts.
Aggregate each source separately to the chosen grain before combining features.

### 5. Build a historical analysis table

Use one place per Chicago local calendar month as the default. For each month,
calculate review count, mean valid rating, low-rating share, text-review count,
and optional sentiment from eligible reviews in that month. Keep months with no
reviews distinguishable from months with low ratings: count zero, rating null.

Attach the most recent approved inspection strictly before the month begins,
plus its date, type, result, and age in days. Retain tied inspection dates as
explicit ambiguity or a defined day-level summary rather than an arbitrary
winner. Report stale inspection history and test a justified maximum lag.
Months without prior inspection evidence remain missing, not a presumed pass.

Start with the actual date overlap: January 2010 through August 2021. August
2021 is partial and availability differs by restaurant; do not assume every
place was observed continuously. Use 2016-2020 as a complete-calendar-year
sensitivity window, not proof of complete collection; it contains 286,658
distinct pizza review records before unresolved-conflict exclusions.

Keep `Pass`, `Pass w/ Conditions`, and `Fail` distinct. Any failure-rate metric
must define its eligible outcomes explicitly and report other statuses separately.
`No Entry`, `Not Ready`, `Out of Business`, and `Business Not Located` are not
passes or automatic failures. Keep routine inspections, complaints, and
re-inspections distinguishable to avoid treating their sampling as equivalent.

Use dated review-derived ratings for historical comparisons. Metadata
`avg_rating`, `num_of_reviews`, hours, categories, and status are undated snapshot
attributes; do not treat them as known historical values. Retain post-2021
inspections separately for later questions. If predicting future inspections,
define the prediction date and features separately to prevent future-data leakage.

### 6. Validate before analysis

- Confirm unique place, review, and inspection keys after the chosen cleanup;
  require review foreign keys to exist and ratings to be 1-5 for rating metrics.
- Reconcile 319,340 scoped review rows to 314,770 exact-distinct records, then
  account separately for unresolved conflicts, invalid dates, and date filters.
- Publish accepted, rejected, pending, and unmatched counts, methods, and
  coverage by place and month. Candidate counts above are not acceptance rates.
- Verify that each retained inspection contributes once at its intended grain,
  that matched dates obey episode restrictions, and that each temporal feature
  precedes its analysis month. Check that no join inflates source row counts.
- Compare matched versus unmatched places using review volume, neighborhood,
  status, and ratings to assess selection bias. Compare conclusions using only
  the strongest reviewed links versus all accepted links.
- Save processed large tables in an ignored data/output directory, preferably
  partitioned Parquet when implementing the pipeline. Version code, this plan,
  aggregate diagnostics, and business-match review decisions separately.

## Implementation order

1. Clean the 705-place cohort and stream its review subset.
2. Produce a candidate-review sheet for the 172 single-license street/name
   candidates, including the spatial and historical flags above.
3. Approve or reject business histories, then handle fuzzy and spatial candidates.
4. Build and validate the historical restaurant-month table.
5. Run coverage and matching-sensitivity checks before rating/inspection analysis.

The profiling script supplies diagnostics only. It does not approve matches,
resolve business episodes, or create the final analysis tables.
