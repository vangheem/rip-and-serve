#!/usr/bin/env python3
"""Build a local title index from the public IMDb datasets."""

import gzip
import os
import json
import re
from pathlib import Path

ROOT = Path(os.environ.get("RIP_IMDB_DIR", "/mnt/data/rip-work/state/imdb"))
KEEP = {"movie", "tvMovie", "tvMiniSeries", "tvSpecial", "tvSeries", "video", "tvShort"}


def norm(value):
    text = value.lower().replace("\u2019", "").replace("'", "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def main():
    ratings = {}
    with gzip.open(ROOT / "title.ratings.tsv.gz", "rt", encoding="utf-8", errors="replace") as handle:
        next(handle)
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3 and parts[2].isdigit():
                ratings[parts[0]] = int(parts[2])
    print(f"ratings {len(ratings)}", flush=True)

    best = {}
    seen = 0
    kept = 0
    with gzip.open(ROOT / "title.basics.tsv.gz", "rt", encoding="utf-8", errors="replace") as handle:
        header = next(handle).rstrip("\n").split("\t")
        for line in handle:
            seen += 1
            parts = line.rstrip("\n").split("\t")
            if len(parts) < len(header):
                continue
            row = dict(zip(header, parts))
            if row.get("titleType") not in KEEP or row.get("isAdult") == "1":
                continue
            votes = ratings.get(row["tconst"], 0)
            runtime = int(row["runtimeMinutes"]) if str(row.get("runtimeMinutes", "")).isdigit() else 0
            if row["titleType"] in {"video", "tvShort"} and (votes < 40 or runtime < 10):
                continue
            if row["titleType"] not in {"video", "tvShort"} and votes < 40:
                continue
            year = int(row["startYear"]) if str(row.get("startYear", "")).isdigit() else 0
            runtime = int(row["runtimeMinutes"]) if str(row.get("runtimeMinutes", "")).isdigit() else 0
            record = {
                "title": row["primaryTitle"],
                "votes": votes,
                "year": year,
                "type": row["titleType"],
                "runtime": runtime,
            }
            for raw in (row.get("primaryTitle") or "", row.get("originalTitle") or ""):
                key = norm(raw)
                if len(key) < 3 or len(key) > 80:
                    continue
                current = best.get(key)
                if current is None or votes > current["votes"]:
                    best[key] = record
            kept += 1
            if kept % 100000 == 0:
                print(f"kept {kept}", flush=True)

    out = ROOT / "titles.json"
    out.write_text(json.dumps(best), encoding="utf-8")
    print(f"rows {seen} kept {kept} keys {len(best)} bytes {out.stat().st_size}")
    for sample in (
        "national treasure",
        "the tale of despereaux",
        "the princess bride",
        "the star of christmas",
        "veggietales the star of christmas",
        "cinderella",
        "the lorax",
        "ever after",
    ):
        hit = best.get(sample)
        if hit:
            print(f"{sample} -> {hit['title']} ({hit['type']}, {hit['votes']} votes)")
        else:
            print(f"{sample} -> MISSING")


if __name__ == "__main__":
    main()
