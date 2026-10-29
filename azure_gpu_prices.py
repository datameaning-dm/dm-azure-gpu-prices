#!/usr/bin/env python3
"""Azure GPU VM prices per hour, every size and region, from Microsoft's Retail Prices API.

    python3 azure_gpu_prices.py --snapshot-out data/raw/azure_retail_prices_n_series.csv --out data
    python3 azure_gpu_prices.py --snapshot data/raw/azure_retail_prices_n_series.csv --out data
    python3 azure_gpu_prices.py --snapshot data/raw/azure_retail_prices_n_series.csv --out /tmp/planted \
        --plant duplicate-row,zero-price,bad-unit,sp-above-price,spot-above-price,partial-fetch

The first form asks the live API (https://prices.azure.com/api/retail/prices, api-version
2023-01-01-preview) for every Consumption row whose armSkuName starts with "Standard_N" and saves the raw
rows. The second re-runs every number from that saved answer. The third plants faults in the saved answer
to show the checks catch them. Needs Python 3 only (standard library). It exits 1, and writes nothing, if
a check fails.

GPU count, GPU model and GPU memory are not in the price API. They come from gpu_specs.csv, one row per
size, each with the Microsoft Learn page it was read from. FPGA (NP) and media-accelerator (NM) sizes are
priced by the API under the same prefix and are left out. Low-priority meters are left out (Azure retired
low-priority VMs for Spot). Prices are list prices in USD: compute meter only, no disks, network, egress
or support, and no discount except the savings-plan rates the API itself lists.
"""
import argparse
import csv
import datetime
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PRICES = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"
HOURS_PER_MONTH = 730
MAX_ROW_DROP = 0.10   # fail if this many fewer rows than the previous run: a partial answer from the API
UA = "azure_gpu_prices.py (https://github.com/datameaning-dm/dm-azure-gpu-prices; info@datameaning.com)"
HERE = os.path.dirname(os.path.abspath(__file__))
SPECS = os.path.join(HERE, "gpu_specs.csv")
PLANTS = {"duplicate-row", "zero-price", "bad-unit", "sp-above-price", "spot-above-price", "partial-fetch"}
RAW_FIELDS = ["fetched_at", "armSkuName", "armRegionName", "location", "skuName", "productName", "meterName",
              "type", "isPrimaryMeterRegion", "effectiveStartDate", "unitOfMeasure", "currencyCode",
              "retailPrice", "savingsPlan1y", "savingsPlan3y", "meterId"]
SPEC_FIELDS = ["series", "accelerator", "accelerators", "accelerator_fraction", "gpu_memory_gb_each", "vcpus",
               "memory_gb", "status"]
# Region codes the API returns that are not public Azure regions: by prefix, as Microsoft names them
REGION_TYPES = (("usgov", "government"), ("usdod", "government"), ("att", "edge zone"), ("sgx", "edge zone"))
PRICE_FIELDS = ["sku", *SPEC_FIELDS, "region", "location", "region_type", "linux_per_hour", "linux_spot_per_hour",
                "linux_savings_1y_per_hour", "linux_savings_3y_per_hour", "windows_per_hour",
                "windows_spot_per_hour", "linux_per_gpu_hour", "linux_per_month_730h", "effective_start_date",
                "fetched_at"]
SUMMARY_FIELDS = ["sku", *SPEC_FIELDS, "regions", "linux_min_per_hour", "linux_median_per_hour",
                  "linux_max_per_hour", "cheapest_region", "dearest_region", "linux_min_per_gpu_hour",
                  "linux_spot_min_per_hour", "linux_savings_3y_min_per_hour", "fetched_at"]


def get_json(url):
    """GET one page at a time; honor retry-after on 429/503."""
    for _ in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503):
                raise
            wait = int(e.headers.get("retry-after", "5"))
            print(f"        HTTP {e.code}, retry-after {wait}s")
            time.sleep(wait)
    sys.exit(f"gave up on {url}")


def fetch(snapshot_out):
    flt = "serviceName eq 'Virtual Machines' and priceType eq 'Consumption' and startswith(armSkuName,'Standard_N')"
    url = PRICES + "?" + urllib.parse.urlencode({"api-version": API_VERSION, "currencyCode": "USD", "$filter": flt},
                                                quote_via=urllib.parse.quote)
    fetched_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items, pages = [], 0
    while url:
        d = get_json(url)
        pages += 1
        items += d["Items"]
        url = d.get("NextPageLink")
        if url:
            time.sleep(0.5)
    print(f"source  {PRICES}?api-version={API_VERSION} fetched {fetched_at}: {len(items)} rows in {pages} pages")
    os.makedirs(os.path.dirname(os.path.abspath(snapshot_out)), exist_ok=True)
    tmp = snapshot_out + ".part"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RAW_FIELDS, lineterminator="\n")
        w.writeheader()
        for i in sorted(items, key=lambda i: (i["armSkuName"], i["armRegionName"], i["productName"], i["skuName"],
                                              i["meterId"])):
            sp = {s["term"]: s["retailPrice"] for s in i.get("savingsPlan", [])}
            w.writerow({"fetched_at": fetched_at, "savingsPlan1y": sp.get("1 Year", ""),
                        "savingsPlan3y": sp.get("3 Years", ""),
                        **{k: i.get(k, "") for k in RAW_FIELDS if k not in ("fetched_at", "savingsPlan1y", "savingsPlan3y")}})
    return tmp   # moved to snapshot_out only after every check passes


def meter_class(r):
    if r["type"] != "Consumption":
        return "other"
    if "Low Priority" in r["skuName"]:
        return "low priority"
    # Windows meters end "Windows", except the NCCadsv5 ones, which end "Srs Win"
    return ("windows" if r["productName"].endswith((" Windows", " Win")) else "linux") + \
           (" spot" if "Spot" in r["skuName"] else "")


def kind(sku, specs):
    """gpu, fpga or media: the specs row says, else the size prefix (NP is FPGA, NM a media accelerator)."""
    k = specs.get(sku, {}).get("accelerator_kind")
    if k:
        return k
    return "fpga" if sku.startswith("Standard_NP") else "media" if sku.startswith("Standard_NM") else "gpu"


def region_type(code):
    return next((t for p, t in REGION_TYPES if code.startswith(p)), "public")


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt(x):
    return "" if x is None else f"{x:.6f}".rstrip("0").rstrip(".")


def plant_faults(raw, plant):
    if "partial-fetch" in plant:
        n = len(raw)
        del raw[n - n // 5:]
        print(f"PLANTED: the last {n // 5} of {n} rows dropped, as if the API stopped answering part way")
    spot_keys = {(r["armSkuName"], r["armRegionName"]) for r in raw if meter_class(r) == "linux spot"}
    lin = [r for r in raw if meter_class(r) == "linux"]
    # each fault on its own row, so each trips one check: zero-price on a row with no savings plan and no Spot
    lin.sort(key=lambda r: bool(r["savingsPlan3y"]) or (r["armSkuName"], r["armRegionName"]) in spot_keys)
    if "duplicate-row" in plant:
        raw.append(dict(lin[0]))
        print("PLANTED: one Linux price row loaded twice")
    if "zero-price" in plant:
        lin[1]["retailPrice"] = "0"
        print("PLANTED: one Linux price set to 0")
    if "bad-unit" in plant:
        lin[2]["unitOfMeasure"] = "1 Month"
        print("PLANTED: one Linux price quoted per '1 Month' instead of '1 Hour'")
    if "sp-above-price" in plant:
        r = next(r for r in lin[3:] if num(r["savingsPlan3y"]))
        r["savingsPlan3y"] = str(num(r["retailPrice"]) * 1.5)
        print("PLANTED: one 3-year savings-plan rate set 50% above the pay-as-you-go price")
    if "spot-above-price" in plant:
        spot = next(r for r in raw if meter_class(r) == "linux spot"
                    and (r["armSkuName"], r["armRegionName"]) not in {(x["armSkuName"], x["armRegionName"]) for x in lin[:3]})
        spot["retailPrice"] = str(num(spot["retailPrice"]) * 100)
        print("PLANTED: one Linux Spot price set 100 times higher, above pay-as-you-go")
    return raw


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="folder for azure_gpu_vm_prices.csv and azure_gpu_vm_summary.csv")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--snapshot", help="re-run from a saved raw price file")
    g.add_argument("--snapshot-out", help="ask the live API and save the raw rows here")
    ap.add_argument("--previous", help="last published azure_gpu_vm_prices.csv, to catch a partial API answer")
    ap.add_argument("--plant", default="", help=f"comma list of test faults: {', '.join(sorted(PLANTS))}")
    a = ap.parse_args()
    plant = [x for x in a.plant.split(",") if x]
    if set(plant) - PLANTS:
        ap.error(f"unknown --plant value(s) {sorted(set(plant) - PLANTS)}; use {sorted(PLANTS)}")
    if plant and a.snapshot_out:
        ap.error("plant faults only run from a --snapshot, so the live API is never touched with a fault")
    print(f"python {sys.version.split()[0]}")

    snapshot = a.snapshot or fetch(a.snapshot_out)   # a live fetch is read from its .part file
    with open(snapshot, "rb") as f:
        file_lines = f.read().count(b"\n") - 1
    raw = plant_faults(read_csv(snapshot), plant)
    fetched = sorted({r["fetched_at"] for r in raw})
    print(f"prices  {a.snapshot_out or snapshot}: {file_lines} data lines, fetched {', '.join(fetched)}")

    specs = {s["sku"].strip(): s for s in read_csv(SPECS)}   # one API size name ends in spaces
    kinds = {}
    for s in specs.values():
        kinds[s["accelerator_kind"]] = kinds.get(s["accelerator_kind"], 0) + 1
    print(f"specs   gpu_specs.csv: {len(specs)} sizes (" + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) + ")")

    classes = {}
    for r in raw:
        c = meter_class(r)
        classes[c] = classes.get(c, 0) + 1
    print("rows by meter class: " + ", ".join(f"{c} {n}" for c, n in sorted(classes.items())))

    failures, warnings = [], []
    if len(raw) != file_lines:   # duplicate-row
        failures.append(f"loaded {len(raw)} price rows but the file has {file_lines} data lines "
                        f"({len(raw) - file_lines:+d})")
    if len(fetched) != 1:
        failures.append(f"the snapshot mixes {len(fetched)} fetch times")

    # one cell per size, region and meter class
    cells, seen_dup = {}, 0
    for r in raw:
        c = meter_class(r)
        if c in ("low priority", "other"):
            continue
        key = (r["armSkuName"].strip(), r["armRegionName"], c)
        if key in cells:
            seen_dup += 1
        cells[key] = r
    if seen_dup:   # duplicate-row
        failures.append(f"{seen_dup} size, region and meter combinations have more than one row")
    kept = [r for r in cells.values()]
    bad_price = [r for r in kept if not num(r["retailPrice"]) or num(r["retailPrice"]) <= 0]
    if bad_price:   # zero-price
        failures.append(f"{len(bad_price)} rows have a price that is missing or not above zero")
    bad_unit = [r for r in kept if r["unitOfMeasure"] != "1 Hour" or r["currencyCode"] != "USD"]
    if bad_unit:   # bad-unit
        failures.append(f"{len(bad_unit)} rows are not priced in USD per '1 Hour'")
    bad_sp = [r for r in kept if (num(r["savingsPlan1y"]) or 0) > num(r["retailPrice"] or 0)
              or (num(r["savingsPlan3y"]) or 0) > num(r["retailPrice"] or 0)
              or (num(r["savingsPlan1y"]) and num(r["savingsPlan3y"]) and num(r["savingsPlan3y"]) > num(r["savingsPlan1y"]))]
    if bad_sp:   # sp-above-price
        failures.append(f"{len(bad_sp)} rows have a savings-plan rate above the pay-as-you-go price "
                        f"(or a 3-year rate above the 1-year rate)")
    bad_spot = [k for k, r in cells.items() if k[2].endswith(" spot")
                and (k[0], k[1], k[2][:-5]) in cells
                and num(r["retailPrice"]) > num(cells[(k[0], k[1], k[2][:-5])]["retailPrice"])]
    if bad_spot:   # spot-above-price
        failures.append(f"{len(bad_spot)} Spot prices are above the pay-as-you-go price for the same size and region")

    skus = sorted({k[0] for k in cells})
    unmapped = [s for s in skus if s not in specs]
    if unmapped:
        warnings.append(f"{len(unmapped)} size(s) priced by the API are not in gpu_specs.csv yet, published with "
                        f"blank specs: " + ", ".join(unmapped))
    gpu_skus = [s for s in skus if kind(s, specs) == "gpu"]
    left_out = [s for s in skus if s not in gpu_skus]
    print(f"sizes   {len(skus)} priced; {len(gpu_skus)} GPU sizes kept; {len(left_out)} left out as "
          f"FPGA or media accelerator: {', '.join(left_out) or 'none'}")

    out_rows = []
    for s in gpu_skus:
        sp = specs.get(s, {})
        for reg in sorted({k[1] for k in cells if k[0] == s}):
            lin, win = cells.get((s, reg, "linux")), cells.get((s, reg, "windows"))
            lsp, wsp = cells.get((s, reg, "linux spot")), cells.get((s, reg, "windows spot"))
            if not (lin or win):
                continue   # Spot only in this region: no pay-as-you-go price to quote
            ph = num(lin["retailPrice"]) if lin else None
            devices = num(sp.get("accelerators"))
            frac = sp.get("accelerator_fraction") or "1"
            fr = num(frac.split("/")[0]) / num(frac.split("/")[1]) if "/" in frac else num(frac)
            any_row = lin or win
            out_rows.append({"sku": s, **{f: sp.get(f, "") for f in SPEC_FIELDS}, "region": reg,
                             "location": any_row["location"], "region_type": region_type(reg),
                             "linux_per_hour": fmt(ph),
                             "linux_spot_per_hour": fmt(num(lsp["retailPrice"]) if lsp else None),
                             "linux_savings_1y_per_hour": fmt(num(lin["savingsPlan1y"]) if lin else None),
                             "linux_savings_3y_per_hour": fmt(num(lin["savingsPlan3y"]) if lin else None),
                             "windows_per_hour": fmt(num(win["retailPrice"]) if win else None),
                             "windows_spot_per_hour": fmt(num(wsp["retailPrice"]) if wsp else None),
                             "linux_per_gpu_hour": fmt(ph / (devices * fr)) if ph and devices and fr else "",
                             "linux_per_month_730h": f"{ph * HOURS_PER_MONTH:.2f}" if ph else "",
                             "effective_start_date": any_row["effectiveStartDate"][:10],
                             "fetched_at": any_row["fetched_at"]})

    if a.previous and os.path.exists(a.previous):
        prev = read_csv(a.previous)
        if len(out_rows) < len(prev) * (1 - MAX_ROW_DROP):   # partial-fetch
            failures.append(f"{len(out_rows)} size and region rows, {len(prev) - len(out_rows)} fewer than the "
                            f"{len(prev)} last published: more than {MAX_ROW_DROP:.0%} gone at once")
        gone = sorted({r["sku"] for r in prev} - {r["sku"] for r in out_rows})
        if gone:
            warnings.append(f"{len(gone)} size(s) in the last published file are no longer priced: " + ", ".join(gone))

    summary = []
    for s in gpu_skus:
        rs = [r for r in out_rows if r["sku"] == s and r["linux_per_hour"]]
        if not rs:
            continue
        # the summary counts public regions only, unless a size is priced nowhere else
        rs = [r for r in rs if r["region_type"] == "public"] or rs
        p = sorted(rs, key=lambda r: (float(r["linux_per_hour"]), r["region"]))
        spot = [float(r["linux_spot_per_hour"]) for r in rs if r["linux_spot_per_hour"]]
        sp3 = [float(r["linux_savings_3y_per_hour"]) for r in rs if r["linux_savings_3y_per_hour"]]
        pg = [float(r["linux_per_gpu_hour"]) for r in rs if r["linux_per_gpu_hour"]]
        summary.append({"sku": s, **{f: rs[0][f] for f in SPEC_FIELDS}, "regions": len(rs),
                        "linux_min_per_hour": p[0]["linux_per_hour"],
                        "linux_median_per_hour": fmt(round(statistics.median(float(r["linux_per_hour"]) for r in rs), 4)),
                        "linux_max_per_hour": p[-1]["linux_per_hour"],
                        "cheapest_region": p[0]["region"],
                        "dearest_region": max(rs, key=lambda r: (float(r["linux_per_hour"]), r["region"]))["region"],
                        "linux_min_per_gpu_hour": fmt(min(pg)) if pg else "",
                        "linux_spot_min_per_hour": fmt(min(spot)) if spot else "",
                        "linux_savings_3y_min_per_hour": fmt(min(sp3)) if sp3 else "",
                        "fetched_at": rs[0]["fetched_at"]})

    print()
    if failures:
        print("SELF-CHECK FAILED: nothing written")
        for f in failures:
            print("  -", f)
        sys.exit(1)

    print(f"Linux pay-as-you-go, USD per hour as the API lists it, {len(summary)} GPU sizes, public regions "
          f"(per GPU-hour = hourly price / GPUs, fraction included)")
    head = ["size", "accelerator", "gpus", "gb/gpu", "regions", "min/h", "median/h", "max/h", "min/gpu-h",
            "spot min/h", "3y min/h", "cheapest"]
    widths = [38, 26, 5, 7, 7, 8, 9, 8, 10, 10, 9, 18]
    print("  ".join(h.rjust(w) for h, w in zip(head, widths)))
    for r in sorted(summary, key=lambda r: float(r["linux_min_per_hour"])):
        gpus = r["accelerator_fraction"] if r["accelerator_fraction"] not in ("", "1") else r["accelerators"]
        cells_ = [r["sku"], r["accelerator"][:26] or "-", gpus or "-", r["gpu_memory_gb_each"].split(" ")[0] or "-", str(r["regions"]),
                  r["linux_min_per_hour"], r["linux_median_per_hour"], r["linux_max_per_hour"],
                  r["linux_min_per_gpu_hour"] or "-", r["linux_spot_min_per_hour"] or "-",
                  r["linux_savings_3y_min_per_hour"] or "-", r["cheapest_region"]]
        print("  ".join(c.rjust(w) for c, w in zip(cells_, widths)))
    print()
    for w in warnings:
        print("WARNING:", w)
    print(f"SELF-CHECK PASSED: {len(raw)} price rows equal the {file_lines} data lines of the file, one fetch time, "
          f"one row per size, region and meter, every price above zero in USD per hour, no savings-plan rate above "
          f"its price, no Spot price above pay-as-you-go; {len(out_rows)} size and region rows for {len(summary)} "
          f"GPU sizes in {len({r['region'] for r in out_rows})} regions")

    if a.snapshot_out:
        os.replace(snapshot, a.snapshot_out)
        print(f"wrote {a.snapshot_out}")
    os.makedirs(a.out, exist_ok=True)
    for name, fields, rows in (("azure_gpu_vm_prices.csv", PRICE_FIELDS, out_rows),
                               ("azure_gpu_vm_summary.csv", SUMMARY_FIELDS, summary)):
        path = os.path.join(a.out, name)
        with open(path + ".part", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        os.replace(path + ".part", path)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
