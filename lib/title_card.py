#!/usr/bin/env python3
"""Match a movie by reading the title card at the start of the file.

IMDb's public title list is the catalog. Tesseract reads frames until a line
is that title, or the opening stretch is exhausted.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"


def work_root():
    root = Path("/mnt/data/rip-work")
    try:
        for line in open("/etc/rip-bluray.conf", encoding="utf-8"):
            if line.startswith("WORK_ROOT="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return Path(value)
    except OSError:
        pass
    return root


def index_path():
    return work_root() / "state/imdb/titles.json"


def cache_dir():
    return work_root() / "state/identify-cache"


def work_dir():
    return work_root() / "state/identify-work"
_INDEX = None
_WORDS = None
STUDIO = {
    "disney", "pixar", "pictures", "starring", "christmas", "animation",
    "universal", "paramount", "warner", "brothers", "presents", "columbia",
    "dreamworks", "nickelodeon", "entertainment", "productions", "studios",
}


def norm(value):
    text = (value or "").lower().replace("\u2019", "").replace("'", "").replace("`", "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def load_index():
    global _INDEX, _WORDS
    if _INDEX is None:
        _INDEX = json.loads(index_path().read_text(encoding="utf-8"))
        words = {}
        for key, hit in _INDEX.items():
            if hit["votes"] < 40:
                continue
            for word in key.split():
                if len(word) < 6 or word in STUDIO:
                    continue
                bucket = words.setdefault(word, [])
                if hit["title"] not in {item["title"] for item in bucket}:
                    bucket.append(hit)
        _WORDS = words
    return _INDEX


def rare_title(text):
    load_index()
    found = []
    seen = set()
    for raw in text.splitlines():
        for word in norm(raw).split():
            if len(word) < 6 or word in STUDIO or word in seen:
                continue
            seen.add(word)
            hits = _WORDS.get(word) or []
            hits = [hit for hit in hits if hit["votes"] >= 80]
            hits.sort(key=lambda item: item["votes"], reverse=True)
            if not hits:
                continue
            if len(hits) == 1 or hits[0]["votes"] > 4 * hits[1]["votes"]:
                found.append((hits[0], word))
    return found


def lookup(key, index):
    if not key or len(key) < 3:
        return None
    hit = index.get(key)
    if hit:
        return hit
    if key.startswith("the "):
        return index.get(key[4:])
    return index.get("the " + key)


def significant(key):
    skip = {"the", "a", "an", "of", "and", "part", "vol", "volume"}
    return [word for word in key.split() if word not in skip and len(word) > 1]


def frame_times(duration):
    times = [2, 8, 18, 35, 55, 80, 110, 150, 190, 240]
    times = [item for item in times if item < max(20, duration - 20)]
    if duration > 180:
        times.extend(int(duration - offset) for offset in (40, 70, 110))
    return times


def docker(image, entrypoint, script, args, timeout):
    command = [
        "docker", "run", "--rm", "-i",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", "/mnt/data:/mnt/data",
        "--entrypoint", entrypoint,
        image,
        "-s",
        *args,
    ]
    completed = subprocess.run(
        command,
        input=script.encode(),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout.decode("utf-8", "replace"), completed.stderr.decode("utf-8", "replace")


def probe(media):
    script = (
        "probe=/usr/lib/jellyfin-ffmpeg/ffprobe\n"
        'dur=$("$probe" -v error -show_entries format=duration -of default=nw=1:nk=1 "$1" || echo 0)\n'
        'echo "DURATION=$dur"\n'
    )
    _code, output, _err = docker("jellyfin/jellyfin", "sh", script, [str(media)], 90)
    for line in output.splitlines():
        if line.startswith("DURATION="):
            try:
                return float(line.split("=", 1)[1] or 0)
            except ValueError:
                return 0.0
    return 0.0


def extract_frames(media, times, work):
    script = r"""
set -eu
ff=/usr/lib/jellyfin-ffmpeg/ffmpeg
input=$1
outdir=$2
shift 2
for offset in "$@"; do
  out="$outdir/f-$offset.png"
  "$ff" -y -ss "$offset" -i "$input" -frames:v 1 \
    -vf "scale=560:-1,crop=iw*0.92:ih*0.5,format=gray" \
    "$out" >/dev/null 2>&1 || true
done
"""
    docker(
        "jellyfin/jellyfin",
        "sh",
        script,
        [str(media), str(work), *[str(item) for item in times]],
        timeout=240,
    )
    return sorted(work.glob("f-*.png"))


def read_frames(frames):
    if not frames:
        return {}
    script = r"""
set -eu
for image in "$@"; do
  echo "FRAME $image"
  timeout 8 tesseract "$image" stdout --psm 6 -l eng --oem 1 2>/dev/null || true
  echo "ENDFRAME"
done
"""
    _code, output, _err = docker(
        "rip-ocr",
        "sh",
        script,
        [str(path) for path in frames],
        timeout=300,
    )
    found = {}
    current = ""
    lines = []
    for line in output.splitlines():
        if line.startswith("FRAME "):
            current = line.split()[-1]
            lines = []
            continue
        if line == "ENDFRAME":
            found[current] = "\n".join(lines)
            current = ""
            continue
        if current:
            lines.append(line)
    return found


def hits_in_text(text, index):
    found = []
    for raw in text.splitlines():
        line = norm(raw)
        words = line.split()
        if not words or len(line) < 3 or len(line) > 70:
            continue
        if any(noise in line for noise in ("directed by", "produced by", "screenplay", "starring")):
            continue
        whole = lookup(line, index)
        if whole:
            found.append((whole, line, True))
        if len(words) == 1:
            continue
        for size in range(min(7, len(words) - 1), 1, -1):
            for start in range(0, len(words) - size + 1):
                key = " ".join(words[start:start + size])
                if len(significant(key)) < 2:
                    continue
                hit = lookup(key, index)
                if hit and hit["votes"] >= 8000:
                    found.append((hit, key, False))
    return found


def score_hit(hit, key, exact, duration):
    words = significant(key)
    score = hit["votes"]
    if exact and len(words) >= 2:
        score += 50000
    elif exact and len(words) == 1 and hit["votes"] >= 30000 and len(key) >= 4:
        score += 20000
    elif not exact and len(words) >= 2 and hit["votes"] >= 8000:
        score += 5000
    else:
        return 0
    runtime = hit.get("runtime") or 0
    if duration and runtime:
        gap = abs(duration / 60 - runtime)
        if gap < 15:
            score += 8000
        elif gap > max(40, runtime * 0.6):
            score -= 15000
    if hit.get("type") in {"movie", "tvMovie", "tvMiniSeries", "tvSpecial"}:
        score += 1000
    return score


def match(media):
    media = Path(media)
    stat = media.stat()
    key = hashlib.sha1(f"{media}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:20]
    cache_path = cache_dir() / f"ocr-{key}.json"
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached = {}
    if cached.get("done"):
        return cached.get("match")

    index = load_index()
    duration = probe(media)
    times = frame_times(duration)
    work = work_dir() / f"ocr-{key}"
    work.mkdir(parents=True, exist_ok=True)
    for child in work.glob("*"):
        child.unlink()
    extract_frames(media, times, work)
    best = None
    best_score = 0
    seen = {}
    for stamp in times:
        image = work / f"f-{stamp}.png"
        if not image.exists() or image.stat().st_size < 2500:
            continue
        texts = read_frames([image])
        text = next(iter(texts.values()), "")
        print(f"  {stamp}s {text.strip()[:80]!r}", flush=True)
        for hit, phrase in rare_title(text):
            value = hit["votes"] + 80000
            if value > best_score:
                best_score = value
                best = {
                    "title": hit["title"],
                    "votes": hit["votes"],
                    "year": hit.get("year") or 0,
                    "type": hit.get("type") or "",
                    "reason": f"title card word {phrase!r} at {stamp}s, {hit['votes']} votes",
                    "duration": duration,
                }
        for hit, phrase, exact in hits_in_text(text, index):
            value = score_hit(hit, phrase, exact, duration)
            if value <= 0:
                continue
            title = hit["title"]
            seen[title] = seen.get(title, 0) + 1
            if seen[title] >= 2:
                value += 20000
            if value > best_score:
                best_score = value
                best = {
                    "title": title,
                    "votes": hit["votes"],
                    "year": hit.get("year") or 0,
                    "type": hit.get("type") or "",
                    "reason": f"title card at {stamp}s {phrase!r}, {hit['votes']} votes",
                    "duration": duration,
                }
        if best_score >= 60000:
            break
    result = best if best and best_score >= 20000 else None
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"done": True, "match": result, "best_score": best_score}), encoding="utf-8")
    return result


def main():
    files = []
    args = sys.argv[1:]
    for index, arg in enumerate(args):
        if arg == "--file" and index + 1 < len(args):
            files.append(args[index + 1])
    if not files:
        raise SystemExit("usage: title_card.py --file PATH")
    for raw in files:
        found = match(Path(raw))
        print(json.dumps({"file": Path(raw).name, "match": found}, indent=2), flush=True)


if __name__ == "__main__":
    main()
