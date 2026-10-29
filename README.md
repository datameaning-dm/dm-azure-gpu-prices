---
license: cc-by-4.0
pretty_name: Azure GPU VM Prices per Hour by Region
tags:
  - azure
  - gpu
  - cloud-pricing
configs:
  - config_name: prices
    data_files: data/azure_gpu_vm_prices.csv
  - config_name: summary
    data_files: data/azure_gpu_vm_summary.csv
---

# Azure GPU VM prices per hour, every size and region

Microsoft Azure list prices per hour for every GPU virtual machine size (NC, ND, NG and NV series) in
every region, refreshed every Monday from Microsoft's public
[Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices).

Read it as a page, with the cheapest region for each GPU:
https://www.datameaning.com/guides/azure-gpu-vm-prices/

Mirrors of the same files: [Hugging Face](https://huggingface.co/datasets/datameaning/azure-gpu-vm-prices)
and [Kaggle](https://www.kaggle.com/datasets). The copy here, on GitHub, is the source: every weekly
change is a commit, so the price history is the git history.

## Files

| File | One row per | What it holds |
|---|---|---|
| `data/azure_gpu_vm_prices.csv` | size and region | Linux and Windows pay-as-you-go, Linux and Windows Spot, 1-year and 3-year savings-plan rates (Linux), price per GPU-hour, per month at 730 hours |
| `data/azure_gpu_vm_summary.csv` | size | regions, minimum, median and maximum Linux price per hour, cheapest and dearest region, lowest Spot and 3-year rates |
| `gpu_specs.csv` | size | GPU model, GPU count (and fraction, for partial-GPU sizes), GPU memory, vCPUs, memory, status, and the Microsoft Learn page each row was read from |
| `data/raw/azure_retail_prices_n_series.csv` | API row | the API's answer as fetched, so every number can be re-run |
| `data/run-output.txt` | run | what the last run printed, checks included |

Prices are USD per hour as the API lists them. `fetched_at` says when.

## Re-run it

Python 3, standard library only.

```bash
python3 azure_gpu_prices.py --snapshot data/raw/azure_retail_prices_n_series.csv --out /tmp/rerun
python3 azure_gpu_prices.py --snapshot-out /tmp/raw.csv --out /tmp/today
python3 azure_gpu_prices.py --snapshot data/raw/azure_retail_prices_n_series.csv --out /tmp/planted \
    --plant duplicate-row,zero-price,bad-unit,sp-above-price,spot-above-price,partial-fetch
```

The first re-runs the published numbers from the saved answer. The second asks the live API. The third
plants six faults and shows the checks stop the run. The script writes nothing if a check fails: a
duplicate row, a price at or below zero, a price not in USD per hour, a savings-plan rate above its
pay-as-you-go price, a Spot price above pay-as-you-go, or 10% fewer rows than the last published file.

## What it leaves out

- List prices only: no disks, network, egress, support, or negotiated and reserved-instance discounts.
- FPGA (NP) and media-accelerator (NM) sizes, which the API prices under the same `Standard_N` prefix.
- Low-priority meters, which Azure replaced with Spot.
- A size the API prices before its spec row is added to `gpu_specs.csv` is still published, with blank
  specs, and the run prints a warning naming it.

## License

The compilation, the specs table and the code are CC BY 4.0, by
[Data Meaning](https://www.datameaning.com/). The prices are Microsoft's public list prices; check the
[Azure pricing calculator](https://azure.microsoft.com/en-us/pricing/calculator/) before you buy.
