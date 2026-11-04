"""Profile local merge inputs without loading the large review file into memory.

Run: python analyze_merge_inputs.py
Writes aggregate findings to merge_input_profile.json; does not merge source data.
"""

import csv
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def name_key(value):
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().upper()
    return " ".join(re.sub(r"[^A-Z0-9 ]", "", value).split())


STREET_WORDS = dict(zip(
    "NORTH SOUTH EAST WEST STREET AVENUE BOULEVARD ROAD DRIVE PLACE COURT PARKWAY TERRACE".split(),
    "N S E W ST AVE BLVD RD DR PL CT PKWY TER".split(),
))


def street_key(value):
    value = (value or "").upper()
    value = re.sub(r"\b(?:SUITE|STE|UNIT|APT|APARTMENT|FLOOR|FL)\b.*$|#.*$", "", value)
    value = re.sub(r"\b[A-Z]+\b", lambda m: STREET_WORDS.get(m[0], m[0]), value)
    return " ".join(re.sub(r"[^A-Z0-9 -]", "", value).split())


def metadata_street(record):
    address = record.get("address") or ""
    prefix = (record.get("name") or "") + ", "
    if address.startswith(prefix):
        address = address[len(prefix):]
    parts = [part.strip() for part in address.split(",")]
    return street_key(next((part for part in parts if re.match(r"^\d", part)), parts[0]))


def digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def utc_date(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat() if value is not None else None


def distance_m(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(h)))


def profile():
    meta_ids = set()
    signatures = {}
    first_identity = {}
    chicago = {}
    meta_counts = Counter()
    duplicate_differences = Counter()
    meta_missing = Counter()
    meta_keys = Counter()
    for line in (ROOT / "meta-Illinois.json").open(encoding="utf-8"):
        record = json.loads(line)
        meta_counts["rows"] += 1
        meta_keys.update(record.keys())
        for field in ("gmap_id", "name", "address", "latitude", "longitude", "category", "avg_rating", "num_of_reviews"):
            if record.get(field) is None or record.get(field) == "":
                meta_missing[field] += 1
        gid = record.get("gmap_id")
        sig = digest(record)
        identity = tuple(record.get(k) for k in ("name", "address", "latitude", "longitude", "avg_rating", "num_of_reviews"))
        if gid in signatures:
            meta_counts["duplicate_id_extra_rows"] += 1
            meta_counts["duplicate_rows_identical_to_first"] += sig == signatures[gid]
            for field, old, new in zip(("name", "address", "latitude", "longitude", "avg_rating", "num_of_reviews"), first_identity[gid], identity):
                duplicate_differences[field] += old != new
        else:
            signatures[gid] = sig
            first_identity[gid] = identity
        meta_ids.add(gid)
        if re.search(r",\s*Chicago,\s*IL\b", record.get("address") or "", re.I):
            meta_counts["chicago_rows"] += 1
            if gid not in chicago:
                record["_street"] = metadata_street(record)
                chicago[gid] = record
    pizzas = {gid: record for gid, record in chicago.items()
              if any("pizza" in category.lower() for category in record.get("category") or [])}
    meta_counts["unique_ids"] = len(meta_ids)
    meta_counts["chicago_unique_ids"] = len(chicago)
    meta_counts["chicago_pizza_unique_ids_first_metadata_record"] = len(pizzas)
    del signatures, first_identity
    print("Metadata profiled:", dict(meta_counts), flush=True)

    inspection_counts = Counter()
    inspection_missing = Counter()
    inspection_ids = set()
    inspection_results = Counter()
    inspection_types = Counter()
    inspection_years = Counter()
    facilities = Counter()
    cities = Counter()
    by_address = defaultdict(dict)
    license_names = defaultdict(set)
    license_addresses = defaultdict(set)
    inspection_min = inspection_max = None
    with (ROOT / "Food_Inspections_20261103.csv").open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        inspection_columns = reader.fieldnames
        for record in reader:
            inspection_counts["rows"] += 1
            iid = record["Inspection ID"]
            inspection_counts["duplicate_inspection_id_extra_rows"] += iid in inspection_ids
            inspection_ids.add(iid)
            for field in ("License #", "Address", "DBA Name", "AKA Name", "Zip", "Latitude", "Longitude", "Inspection Date", "Facility Type"):
                if not record[field] or (field == "License #" and record[field] == "0"):
                    inspection_missing[field] += 1
            date = datetime.strptime(record["Inspection Date"], "%m/%d/%Y").date().isoformat()
            inspection_min = min(inspection_min or date, date)
            inspection_max = max(inspection_max or date, date)
            inspection_years[date[:4]] += 1
            inspection_results[record["Results"]] += 1
            inspection_types[record["Inspection Type"]] += 1
            facilities[record["Facility Type"]] += 1
            cities[record["City"]] += 1
            if record["City"].strip().upper() != "CHICAGO" or record["State"].strip().upper() != "IL":
                continue
            inspection_counts["chicago_rows"] += 1
            address = street_key(record["Address"])
            license_id = record["License #"]
            aliases = tuple(sorted({name_key(record["DBA Name"]), name_key(record["AKA Name"])} - {""}))
            key = (license_id, aliases)
            business = by_address[address].get(key)
            if business is None:
                coords = None
                if record["Latitude"] and record["Longitude"]:
                    coords = (float(record["Latitude"]), float(record["Longitude"]))
                business = {"license": license_id, "aliases": aliases, "name": record["DBA Name"],
                            "address": record["Address"], "zip": record["Zip"], "coords": coords,
                            "first_date": date, "last_date": date, "rows": 0}
                by_address[address][key] = business
            business["first_date"] = min(business["first_date"], date)
            business["last_date"] = max(business["last_date"], date)
            business["rows"] += 1
            if license_id not in ("", "0"):
                license_names[(license_id, address)].add(name_key(record["DBA Name"]))
                license_addresses[license_id].add(address)
    print("Inspections profiled:", dict(inspection_counts), flush=True)

    match_counts = Counter()
    examples = {"multiple_exact_name_licenses": [], "address_name_disagreement": [], "no_exact_street": []}
    address_match_ids = set()
    exact_name_ids = set()
    unique_license_ids = set()
    spatial_grid = defaultdict(list)
    for address, businesses in by_address.items():
        for business in businesses.values():
            if business["coords"]:
                lat, lon = business["coords"]
                spatial_grid[(math.floor(lat/.002), math.floor(lon/.002))].append(business)
    for gid, record in pizzas.items():
        candidates = list(by_address.get(record["_street"], {}).values())
        exact = [c for c in candidates if name_key(record["name"]) in c["aliases"]]
        if candidates:
            address_match_ids.add(gid)
            match_counts["exact_street_candidates"] += 1
            match_counts["street_has_multiple_licenses"] += len({c["license"] for c in candidates}) > 1
        if exact:
            exact_name_ids.add(gid)
            match_counts["exact_street_and_name_candidates"] += 1
            licenses = {c["license"] for c in exact if c["license"] not in ("", "0")}
            if len(licenses) == 1:
                unique_license_ids.add(gid)
                match_counts["exact_street_name_one_valid_license"] += 1
            elif len(licenses) > 1 and len(examples["multiple_exact_name_licenses"]) < 5:
                examples["multiple_exact_name_licenses"].append({"google_name": record["name"], "street": record["_street"], "licenses": sorted(licenses)})
            coords = (record.get("latitude"), record.get("longitude"))
            distances = [distance_m(coords, c["coords"]) for c in exact if c["coords"] and None not in coords]
            if distances:
                match_counts["exact_street_name_with_coordinates"] += 1
                match_counts["exact_street_name_nearest_within_100m"] += min(distances) <= 100
                match_counts["exact_street_name_nearest_over_100m"] += min(distances) > 100
            match_counts["exact_name_has_license_address_multiple_dba_names"] += any(
                len(license_names.get((c["license"], record["_street"]), ())) > 1 for c in exact)
        elif candidates and len(examples["address_name_disagreement"]) < 5:
            examples["address_name_disagreement"].append({"google_name": record["name"], "street": record["_street"], "inspection_names": sorted({c["name"] for c in candidates})[:10]})
        elif not candidates:
            lat, lon = record.get("latitude"), record.get("longitude")
            nearby = []
            if lat is not None and lon is not None:
                cell = (math.floor(lat/.002), math.floor(lon/.002))
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nearby.extend(c for c in spatial_grid.get((cell[0]+dx, cell[1]+dy), ())
                                      if distance_m((lat, lon), c["coords"]) <= 100)
            match_counts["no_exact_street_with_any_candidate_within_100m"] += bool(nearby)
            match_counts["no_exact_street_with_exact_name_within_100m"] += any(name_key(record["name"]) in c["aliases"] for c in nearby)
            if len(examples["no_exact_street"]) < 5:
                examples["no_exact_street"].append({"google_name": record["name"], "address": record["address"], "nearby_names": sorted({c["name"] for c in nearby})[:8]})

    review_counts = Counter()
    review_keys = Counter()
    ratings = Counter()
    reviewed_ids = set()
    review_min = review_max = None
    pizza_min = pizza_max = None
    pizza_years = Counter()
    pizza_per_place = Counter()
    pizza_unique_per_place = Counter()
    pizza_review_hashes = set()
    pizza_identity_hashes = {}
    pizza_author_place = set()
    pizza_ratings = Counter()
    pizza_temporal_inspections = Counter()
    for line in (ROOT / "review-Illinois.json").open(encoding="utf-8"):
        record = json.loads(line)
        review_counts["rows"] += 1
        gid = record.get("gmap_id")
        reviewed_ids.add(gid)
        review_keys.update(record.keys())
        review_counts["rows_with_metadata_id"] += gid in meta_ids
        review_counts["missing_text_rows"] += not bool((record.get("text") or "").strip())
        review_counts["missing_user_id_rows"] += not bool(record.get("user_id"))
        review_counts["missing_gmap_id_rows"] += not bool(gid)
        ratings[str(record.get("rating"))] += 1
        timestamp = record.get("time")
        valid_time = isinstance(timestamp, (int, float)) and math.isfinite(timestamp) and 0 <= timestamp <= 4102444800000
        if valid_time:
            review_min = min(review_min if review_min is not None else timestamp, timestamp)
            review_max = max(review_max if review_max is not None else timestamp, timestamp)
        else:
            review_counts["missing_or_out_of_range_time_rows"] += 1
        if gid in chicago:
            review_counts["chicago_rows"] += 1
        if gid in pizzas:
            review_counts["chicago_pizza_rows"] += 1
            pizza_per_place[gid] += 1
            sig = digest(record)
            identity = (record.get("user_id"), gid, timestamp)
            review_counts["pizza_duplicate_full_record_extra_rows"] += sig in pizza_review_hashes
            review_counts["pizza_duplicate_user_place_time_extra_rows"] += identity in pizza_identity_hashes
            review_counts["pizza_same_identity_different_record_extra_rows"] += identity in pizza_identity_hashes and pizza_identity_hashes[identity] != sig
            pair = (record.get("user_id"), gid)
            review_counts["pizza_repeated_user_place_extra_rows"] += pair in pizza_author_place
            pizza_author_place.add(pair)
            pizza_identity_hashes[identity] = sig
            if sig not in pizza_review_hashes:
                pizza_unique_per_place[gid] += 1
                pizza_ratings[str(record.get("rating"))] += 1
                if valid_time:
                    day = datetime.fromtimestamp(timestamp / 1000, timezone.utc).date()
                    pizza_years[str(day.year)] += 1
                    # Temporal overlap only: this does not establish business identity.
                    if any(c["first_date"] <= day.isoformat() for c in by_address.get(pizzas[gid]["_street"], {}).values()
                           if name_key(pizzas[gid]["name"]) in c["aliases"]):
                        pizza_temporal_inspections["unique_reviews_on_or_after_first_exact_name_candidate_inspection"] += 1
            pizza_review_hashes.add(sig)
            if valid_time:
                pizza_min = min(pizza_min if pizza_min is not None else timestamp, timestamp)
                pizza_max = max(pizza_max if pizza_max is not None else timestamp, timestamp)
        if review_counts["rows"] % 1000000 == 0:
            print("Review rows scanned:", review_counts["rows"], flush=True)
    print("Reviews profiled:", dict(review_counts), flush=True)
    review_counts["unique_place_ids"] = len(reviewed_ids)
    review_counts["place_ids_absent_from_metadata"] = len(reviewed_ids - meta_ids)
    review_counts["metadata_places_without_reviews"] = len(meta_ids - reviewed_ids)
    review_counts["pizza_places_with_reviews"] = len(pizza_per_place)
    review_counts["pizza_places_without_reviews"] = len(pizzas) - len(pizza_per_place)
    review_counts["pizza_unique_full_records"] = len(pizza_review_hashes)
    review_counts["pizza_places_with_street_and_name_candidate_and_reviews"] = len(exact_name_ids & pizza_per_place.keys())
    review_counts["pizza_places_one_exact_candidate_license_and_reviews"] = len(unique_license_ids & pizza_per_place.keys())
    snapshot_comparison = Counter()
    for gid, record in pizzas.items():
        count = pizza_unique_per_place[gid]
        expected = record.get("num_of_reviews")
        if isinstance(expected, (int, float)):
            snapshot_comparison["places_export_count_equals_metadata_count"] += count == expected
            snapshot_comparison["places_export_count_below_metadata_count"] += count < expected
            snapshot_comparison["places_export_count_above_metadata_count"] += count > expected
    profile_data = {
        "scope_notes": ["Chicago selected by address city in metadata and normalized City/State in inspections; not a geographic boundary test.",
                        "Pizza category substring selection uses the first Chicago metadata record per gmap_id; all duplicate IDs in this run have identical full records.",
                        "Exact street and name matches are candidates, not approved links. Units are excluded from street blocking keys.",
                        "Review duplicates analyzed in the Chicago pizza subset only; date ranges reported in UTC.",
                        "Inspection candidate date ranges describe observed inspections, not confirmed business operating dates."],
        "source_bytes": {p.name: p.stat().st_size for p in [ROOT / "meta-Illinois.json", ROOT / "review-Illinois.json", ROOT / "Food_Inspections_20261103.csv"]},
        "metadata": {"counts": meta_counts, "missing": meta_missing, "fields": sorted(meta_keys),
                     "duplicate_id_field_differences_vs_first": duplicate_differences,
                     "pizza_state_values": Counter(str(r.get("state")) for r in pizzas.values()).most_common(10)},
        "inspections": {"counts": inspection_counts, "columns": inspection_columns, "missing": inspection_missing,
                        "date_min": inspection_min, "date_max": inspection_max, "years": inspection_years,
                        "results": inspection_results, "types": inspection_types.most_common(12),
                        "facility_types": facilities.most_common(10), "cities": cities.most_common(10),
                        "normalized_chicago_addresses": len(by_address),
                        "license_address_groups_with_multiple_normalized_dba_names": sum(len(v)>1 for v in license_names.values()),
                        "valid_licenses_at_multiple_normalized_addresses": sum(len(v)>1 for v in license_addresses.values())},
        "pizza_inspection_candidates": {"counts": match_counts, "examples": examples},
        "reviews": {"counts": review_counts, "fields": sorted(review_keys), "rating_counts": ratings,
                    "time_min_utc": utc_date(review_min), "time_max_utc": utc_date(review_max),
                    "pizza_time_min_utc": utc_date(pizza_min), "pizza_time_max_utc": utc_date(pizza_max),
                    "pizza_unique_review_years_utc": pizza_years, "pizza_unique_rating_counts": pizza_ratings,
                    "pizza_export_counts_vs_metadata": snapshot_comparison,
                    "pizza_temporal_candidate_overlap": pizza_temporal_inspections},
    }
    destination = ROOT / "merge_input_profile.json"
    destination.write_text(json.dumps(profile_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Wrote", destination.name, flush=True)


if __name__ == "__main__":
    profile()
