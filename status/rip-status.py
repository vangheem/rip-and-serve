#!/usr/bin/env python3
"""Small status page for the automatic Blu-ray ripper and encoder."""

import argparse
import html
import json
import os
import re
import subprocess
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORK = Path(os.environ.get("RIP_WORK", "/mnt/data/rip-work"))
DEVICE = os.environ.get("RIP_DEVICE", "/dev/sr0")
JOBS = WORK / "jobs"
LOGS = WORK / "logs"
STATUS = WORK / "status"
UNITS = ("rip-bluray-ripper", "rip-bluray-encoder")
ROLES = ("ripper", "encoder")

DRIVE_NAMES = {
    0: "NO_INFO",
    1: "NO_DISC",
    2: "TRAY_OPEN",
    3: "DRIVE_NOT_READY",
    4: "DISC_OK",
}
DRIVE_TEXT = {
    "NO_INFO": "drive status unknown",
    "NO_DISC": "no disc",
    "TRAY_OPEN": "tray is open",
    "DRIVE_NOT_READY": "drive is spinning up",
    "DISC_OK": "disc in the drive",
    "MISSING": "drive is not present",
}
VAGUE_ERRORS = {
    "makemkv produced no mkv",
    "makemkv failed or stalled",
    "ripper restarted before the rip finished",
    "handbrake failed or stalled",
    "handbrake produced an empty mp4",
    "encode failed",
}
INTERESTING = (
    "UNCORRECTABLE",
    "Failed to save",
    "Encountered",
    "incompatible medium",
    "could not open",
    "info timed out",
    "no rip started",
    "No medium",
    "stalled",
    "empty MP4",
    "not overwriting",
    "MKV missing",
    "unknown profile",
    "Copy complete",
)


def run(args, timeout=5):
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout, result.stderr


def read_text(path, limit=200_000):
    try:
        data = Path(path).read_bytes()[-limit:]
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


def parse_status_file(role):
    text = read_text(STATUS / role)
    info = {"state": "unknown", "job": "", "updated": "", "message": ""}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in info:
            info[key] = value
    return info


def unit_info(unit):
    code, out, _ = run(
        [
            "systemctl",
            "show",
            unit,
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "MainPID",
            "-p",
            "ActiveEnterTimestamp",
            "--no-pager",
        ]
    )
    info = {
        "active": "unknown" if code else "inactive",
        "sub": "",
        "pid": 0,
        "since": "",
    }
    for line in out.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "ActiveState":
            info["active"] = value or "inactive"
        elif key == "SubState":
            info["sub"] = value
        elif key == "MainPID" and value.isdigit():
            info["pid"] = int(value)
        elif key == "ActiveEnterTimestamp":
            info["since"] = value
    return info


def optical_device():
    """A scratched disc can knock the drive off USB. It comes back as another device."""
    if os.path.exists(DEVICE):
        return DEVICE
    import glob

    found = sorted(glob.glob("/dev/sr*"))
    return found[0] if found else DEVICE


def drive_status():
    status = "MISSING"
    device = optical_device()
    if os.path.exists(device):
        try:
            import fcntl

            fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
            try:
                code = fcntl.ioctl(fd, 0x5326, 0)
            finally:
                os.close(fd)
            status = DRIVE_NAMES.get(code, "NO_INFO")
        except OSError:
            status = "NO_INFO"
    label = ""
    size = ""
    code, out, _ = run(["lsblk", "-dn", "-o", "LABEL,SIZE", device])
    if code == 0 and out.strip():
        parts = out.split()
        if len(parts) >= 2 and not parts[0].endswith("G") and not parts[0].endswith("M"):
            label = parts[0]
            size = parts[-1]
        elif parts:
            size = parts[-1]
    return {
        "status": status,
        "text": DRIVE_TEXT.get(status, status),
        "label": label,
        "size": size,
    }


def disk_usage():
    code, out, _ = run(["df", "-h", "/mnt/data"])
    lines = [line for line in out.splitlines() if line.strip()]
    if code != 0 or len(lines) < 2:
        return {"used": "?", "avail": "?", "pct": "?", "size": "?"}
    parts = lines[-1].split()
    if len(parts) < 5:
        return {"used": "?", "avail": "?", "pct": "?", "size": "?"}
    return {"size": parts[1], "used": parts[2], "avail": parts[3], "pct": parts[4]}


def parse_time(value):
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def ago(value, now):
    parsed = parse_time(value) if isinstance(value, str) else value
    if parsed is None:
        return ""
    seconds = int((now - parsed).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h ago"


def human_size(num):
    if num is None:
        return ""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < step or unit == "TB":
            if unit == "B":
                return f"{int(num)} {unit}"
            return f"{num:.1f} {unit}"
        num /= step
    return ""


def interesting_lines(path):
    text = read_text(path).replace("\r", "\n")
    found = []
    for line in text.splitlines():
        clean = " ".join(line.split())
        if clean and any(token in clean for token in INTERESTING):
            found.append(clean)
    unique = []
    for line in found:
        if not unique or unique[-1] != line:
            unique.append(line)
    return unique[-6:]


def progress_of(path):
    text = read_text(path, 200_000).replace("\r", "\n")
    percent = None
    detail = ""
    copy_total = None
    for line in text.splitlines():
        clean = " ".join(line.split())
        match = re.search(r"copying \S+ \((\d+) bytes\)", clean)
        if match:
            copy_total = int(match.group(1))
            continue
        match = re.search(r"copied (\d+) bytes(?:, skipped (\d+))?", clean)
        if match:
            copied = int(match.group(1))
            skipped = int(match.group(2) or 0)
            if copy_total:
                percent = min(99, int(copied * 100 / copy_total))
            detail = f"copying around the scratch, {human_size(copied)}"
            if copy_total:
                detail += f" of {human_size(copy_total)}"
            if skipped:
                detail += f", skipped {human_size(skipped)}"
            continue
        match = re.search(r"Total progress -\s*(\d+)%", clean)
        if match:
            percent = int(match.group(1))
            detail = clean
            continue
        match = re.search(
            r"Encoding: task \d+ of \d+, ([0-9.]+) %.*?ETA ([0-9hms]+)",
            clean,
        )
        if match:
            percent = int(float(match.group(1)))
            detail = f"{match.group(1)}% · ETA {match.group(2)}"
            continue
        match = re.search(r"Encoding: task \d+ of \d+, ([0-9.]+) %", clean)
        if match:
            percent = int(float(match.group(1)))
            detail = f"{match.group(1)}%"
    return percent, detail


def largest_media(directory):
    movie = None
    movie_size = -1
    movie_mtime = None
    image = None
    image_size = -1
    image_mtime = None
    try:
        names = os.listdir(directory)
    except OSError:
        return None, None, None
    for name in names:
        # The movie file does not exist yet while a scratch is being copied around.
        if not name.endswith((".mkv", ".mp4", ".iso")):
            continue
        path = directory / name
        try:
            stat = path.stat()
        except OSError:
            continue
        if name.endswith(".iso"):
            if stat.st_size > image_size:
                image = path
                image_size = stat.st_size
                image_mtime = stat.st_mtime
            continue
        if stat.st_size > movie_size:
            movie = path
            movie_size = stat.st_size
            movie_mtime = stat.st_mtime
    if movie is not None:
        return movie.name, movie_size, movie_mtime
    if image is not None:
        return image.name, image_size, image_mtime
    return None, None, None


def vague(text):
    lowered = text.strip().lower()
    if lowered in VAGUE_ERRORS:
        return True
    return lowered.startswith("copy complete")


def best_line(lines):
    ranked = (
        "UNCORRECTABLE",
        "incompatible medium",
        "Failed to save",
        "stalled",
        "could not open",
        "No medium",
        "empty MP4",
        "not overwriting",
        "MKV missing",
        "Encountered",
    )
    for token in ranked:
        for line in reversed(lines):
            if token in line:
                return line
    return lines[-1] if lines else ""


def parse_job_start(job_id):
    match = re.match(r"(\d{8})-(\d{6})", job_id or "")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def job_total_time(directory, status, output, now):
    """Wall clock from rip start to finish. Start comes from the job id.

    Older jobs have no stored duration. The id timestamp is enough to show
    elapsed time without writing anything into a job that is still running.
    """
    started = parse_time(read_text(directory / "started").strip()) or parse_job_start(directory.name)
    if started is None:
        return ""
    finished = parse_time(read_text(directory / "finished").strip())
    if finished is None and status == "done" and output and os.path.isfile(output):
        try:
            finished = datetime.fromtimestamp(os.path.getmtime(output), timezone.utc)
        except OSError:
            finished = None
    if finished is None and status == "failed":
        for name in ("error", "status"):
            path = directory / name
            try:
                finished = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                break
            except OSError:
                continue
    end = finished or now
    label = format_duration((end - started).total_seconds())
    if finished is None and status in {"ripping", "ripped", "encoding"}:
        return label + " so far"
    return label


def job_record(directory, now):
    name = read_text(directory / "disc-name").strip() or directory.name
    status = read_text(directory / "status").strip() or "unknown"
    error = read_text(directory / "error").strip()
    output = read_text(directory / "output").strip()
    profile = read_text(directory / "profile").strip()
    title = read_text(directory / "title-id").strip()
    disc_id = read_text(directory / "disc-id").strip()
    try:
        updated = datetime.fromtimestamp(directory.stat().st_mtime, timezone.utc)
    except OSError:
        updated = now
    lines = interesting_lines(directory / "rip.log")
    if not lines:
        lines = interesting_lines(directory / "encode.log")
    percent, progress_detail = progress_of(directory / "rip.log")
    encode_percent, encode_detail = progress_of(directory / "encode.log")
    if encode_percent is not None:
        percent, progress_detail = encode_percent, encode_detail
    media_name, media_size, media_mtime = largest_media(directory)
    output_size = None
    if output and os.path.isfile(output):
        try:
            output_size = os.path.getsize(output)
        except OSError:
            output_size = None
    reason = best_line(lines) or error
    return {
        "id": directory.name,
        "name": name,
        "status": status,
        "error": error,
        "reason": reason,
        "detail": lines,
        "profile": profile,
        "title": title,
        "disc_id": disc_id,
        "output": output,
        "output_size": output_size,
        "updated": updated.isoformat(),
        "updated_ago": ago(updated, now),
        "progress": percent,
        "progress_detail": progress_detail,
        "media_name": media_name or "",
        "media_size": media_size,
        "media_age": ago(datetime.fromtimestamp(media_mtime, timezone.utc), now)
        if media_mtime
        else "",
        "total_time": job_total_time(directory, status, output, now),
    }


def better_reason(job, earlier):
    if job["reason"] and not vague(job["reason"]):
        return job["reason"], job["detail"]
    for old in earlier:
        if old["reason"] and not vague(old["reason"]):
            return f"{old['reason']} (earlier attempt)", old["detail"]
    return job["reason"] or job["error"] or "failed, no reason recorded", job["detail"]


def recent_logs(role, limit=12):
    text = read_text(LOGS / f"{role}.log", 80_000).replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def collect():
    now = datetime.now(timezone.utc)
    services = {}
    for role, unit in zip(ROLES, UNITS):
        status = parse_status_file(role)
        unit_state = unit_info(unit + ".service")
        services[role] = {**status, **unit_state, "unit": unit}
    jobs = []
    if JOBS.is_dir():
        dirs = [JOBS / name for name in os.listdir(JOBS)]
        dirs = [path for path in dirs if path.is_dir()]
        dirs.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
        for directory in dirs[:40]:
            jobs.append(job_record(directory, now))
    jobs_by_id = {job["id"]: job for job in jobs}
    attention = []
    # Only the work in progress. Older failures stay on /failed.
    for role, service in services.items():
        if service.get("state") != "failed":
            continue
        job = jobs_by_id.get(Path(service.get("job") or "").name)
        reason = (job or {}).get("reason") or service.get("message") or "failed"
        detail = (job or {}).get("detail") or []
        attention.append(
            {
                "name": (job or {}).get("name") or role,
                "id": (job or {}).get("id") or "",
                "when": (job or {}).get("updated_ago") or "",
                "attempts": 0,
                "current": "failed",
                "reason": reason,
                "detail": detail[-2:],
                "error": (job or {}).get("error") or "",
            }
        )
    for role, service in services.items():
        if service["active"] != "active":
            attention.insert(
                0,
                {
                    "name": role,
                    "id": "",
                    "when": "",
                    "attempts": 1,
                    "reason": f"{service['unit']} is {service['active']}",
                    "detail": [],
                    "error": "",
                },
            )
    drive = drive_status()
    ripper = services["ripper"]
    if drive["status"] == "DISC_OK" and ripper["state"] in {"idle", "failed", "unknown"}:
        record_ignored_disc(drive, ripper, now)
        attention.insert(
            0,
            {
                "name": drive["label"] or "disc",
                "id": "",
                "when": "",
                "attempts": 1,
                "reason": "a disc is in the drive, but the ripper is not working on it",
                "detail": [],
                "error": ripper.get("message", ""),
            },
        )
    return {
        "now": now.isoformat(),
        "host": os.uname().nodename,
        "drive": drive,
        "disk": disk_usage(),
        "services": services,
        "jobs": jobs,
        "encode_queue": encode_queue(),
        "attention": attention[:8],
        "logs": {role: recent_logs(role) for role in ROLES},
    }


def encode_queue():
    """Jobs the encoder will pick up. It only takes status=ripped, oldest first."""
    if not JOBS.is_dir():
        return {"count": 0, "next": ""}
    waiting = []
    for name in os.listdir(JOBS):
        directory = JOBS / name
        if not directory.is_dir():
            continue
        if read_text(directory / "status").strip() != "ripped":
            continue
        waiting.append(name)
    waiting.sort()
    next_name = ""
    if waiting:
        next_name = read_text(JOBS / waiting[0] / "disc-name").strip() or waiting[0]
    return {"count": len(waiting), "next": next_name}


def all_jobs(now=None):
    now = now or datetime.now(timezone.utc)
    if not JOBS.is_dir():
        return []
    dirs = [JOBS / name for name in os.listdir(JOBS)]
    dirs = [path for path in dirs if path.is_dir()]
    dirs.sort(key=lambda path: path.name, reverse=True)
    return [job_record(directory, now) for directory in dirs]


def advice_for(reason):
    text = (reason or "").lower()
    if "uncorrectable" in text or "read error" in text:
        return "Clean the disc and try again. The drive hit a bad spot. If it fails in the same place, that part of the movie is damaged."
    if "incompatible medium" in text:
        return "The drive rejected the disc. Flip it, or check that it is a DVD or Blu-ray this drive can read."
    if "not working on it" in text or "no rip started" in text or "info timed out" in text:
        return "This disc was in the drive and no rip started. Take it out, then put it back in and close the tray to try again."
    if "could not open" in text or "no medium" in text:
        return "The drive could not open the disc. Close the tray firmly and try again."
    if "restarted before" in text or "interrupted" in text:
        return "The last attempt was interrupted. Insert the disc and close the tray."
    if "stalled" in text:
        return "The last attempt stopped making progress. Insert the disc and close the tray."
    return "Insert the disc and close the tray. The ripper starts on its own."


def hold_id():
    return read_text(WORK / "state" / "hold-disc-id").strip()


def release_hold(name):
    hold_path = WORK / "state" / "hold-disc-id"
    hold = read_text(hold_path).strip()
    if not name or not hold:
        return False
    matched = False
    for job in all_jobs():
        if job["name"] != name:
            continue
        if hold in {job["name"], job.get("disc_id") or ""}:
            matched = True
            break
    if not matched:
        return False
    try:
        hold_path.unlink()
    except OSError:
        return False
    for extra in ("rip-fail-count", "rip-fail-name"):
        try:
            (WORK / "state" / extra).unlink()
        except OSError:
            pass
    return True


def dest_root():
    for line in read_text("/etc/rip-bluray.conf").splitlines():
        if line.startswith("DEST_ROOT="):
            return line.split("=", 1)[1].strip() or "/mnt/data/videos/Movies"
    return "/mnt/data/videos/Movies"


def record_ignored_disc(drive, ripper, now):
    """Keep a failed job when a seated disc never gets a rip.

    The ripper does not create a job if MakeMKV cannot open the disc, so the
    failed page would otherwise forget it as soon as the disc is removed.
    """
    if drive.get("status") != "DISC_OK":
        return None
    if ripper.get("state") not in {"idle", "failed", "unknown"}:
        return None
    label = (drive.get("label") or "").strip() or "unlabeled disc"
    if not JOBS.is_dir():
        return None
    logs = recent_logs("ripper", 40)
    saw_open_failure = any(
        "could not open" in line or "info timed out" in line for line in logs
    )
    idle_since = parse_time(ripper.get("updated") or "")
    idle_long = idle_since is not None and (now - idle_since).total_seconds() >= 120
    if not saw_open_failure and not idle_long:
        return None
    newest = None
    for directory in JOBS.iterdir():
        if not directory.is_dir() or read_text(directory / "disc-name").strip() != label:
            continue
        if newest is None or directory.name > newest.name:
            newest = directory
    if newest is not None:
        newest_status = read_text(newest / "status").strip()
        if (newest / "ignored").is_file():
            return newest
        if newest_status in {"ripping", "ripped", "encoding"}:
            return None
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-") or "disc"
    stamp = now.strftime("%Y%m%d-%H%M%S")
    directory = JOBS / f"{stamp}-ignored-{slug}"
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return directory
    reason = "a disc is in the drive, but the ripper is not working on it"
    detail = "MakeMKV could not open the disc. No rip started."
    for line in reversed(logs):
        if "info timed out" in line or "could not open" in line:
            detail = line.split("] ", 1)[-1]
            break
    (directory / "status").write_text("failed\n")
    (directory / "disc-name").write_text(label + "\n")
    (directory / "disc-id").write_text(label + "\n")
    (directory / "error").write_text(reason + "\n")
    (directory / "title-id").write_text("\n")
    (directory / "profile").write_text("hd\n")
    (directory / "output").write_text(f"{dest_root()}/{label}.mp4\n")
    (directory / "ignored").write_text("1\n")
    (directory / "rip.log").write_text(
        detail + "\n" + reason + ". No rip started.\n"
    )
    return directory


def retry_succeeded(job):
    return job["status"] in {"ripped", "encoding", "done"} or (job.get("output_size") or 0) > 0


def same_disc(failed, other):
    if failed["name"] and failed["name"] == other["name"]:
        return True
    failed_id = failed.get("disc_id") or ""
    other_id = other.get("disc_id") or ""
    if not failed_id or not other_id:
        return False
    return other_id == failed_id or other_id.startswith(failed_id + "-")


def failed_review():
    now = datetime.now(timezone.utc)
    record_ignored_disc(drive_status(), parse_status_file("ripper"), now)
    jobs = all_jobs(now)
    successes = [job for job in jobs if retry_succeeded(job)]

    def resolved(job):
        return any(
            success["id"] > job["id"] and same_disc(job, success) for success in successes
        )

    unresolved = [job for job in jobs if job["status"] == "failed" and not resolved(job)]
    grouped = {}
    for job in unresolved:
        grouped.setdefault(job.get("disc_id") or job["name"], []).append(job)
    hold = hold_id()
    drive = drive_status()
    needs = []
    for items in grouped.values():
        items.sort(key=lambda item: item["id"], reverse=True)
        newest = items[0]
        source = newest
        reason, detail = better_reason(source, items[1:])
        names = {item["name"] for item in items}
        disc_ids = {item.get("disc_id") or "" for item in items}
        disc_ids.update(names)
        card = {
            "name": newest["name"],
            "attempts": len(items),
            "current": newest["status"],
            "when": source["updated_ago"],
            "updated": source["updated"],
            "reason": reason,
            "advice": advice_for(reason),
            "detail": detail,
            "output": source["output"] or newest["output"],
            "output_size": next((item["output_size"] for item in items if item["output_size"]), None),
            "in_drive": drive["status"] == "DISC_OK" and drive["label"] in disc_ids,
            "held": hold in disc_ids,
            "failures": [
                {
                    "id": item["id"],
                    "when": item["updated_ago"],
                    "error": item["error"] or item["reason"],
                }
                for item in items
            ],
        }
        needs.append(card)
    needs.sort(key=lambda item: item["updated"], reverse=True)
    return {
        "now": now.isoformat(),
        "host": os.uname().nodename,
        "hold": hold,
        "drive": drive,
        "needs": needs,
    }


def render_failed(data):
    def card(item, finished=False):
        state = "finished" if finished else item["current"]
        where = []
        if item["in_drive"]:
            where.append("in the drive now")
        if item["held"]:
            where.append("ripper is ignoring it until the tray has been empty")
        where_text = f"<p class='muted'>{esc(' · '.join(where))}</p>" if where else ""
        action = ""
        if not finished:
            if item["held"]:
                action = (
                    "<form method='post' action='/failed/release'>"
                    f"<input type='hidden' name='name' value='{esc(item['name'])}'>"
                    "<button type='submit'>Release hold and try again</button>"
                    "</form>"
                    "<p class='muted'>Clears the ignore flag. If this disc is in the drive, the ripper starts on the next check.</p>"
                )
            elif item["current"] in {"ripping", "encoding"}:
                action = "<p class='muted'>Already running. Leave it in the drive.</p>"
            else:
                action = f"<p>{esc(item['advice'])}</p>"
        detail = "".join(f"<li>{esc(line)}</li>" for line in item["detail"][-4:])
        attempts = "".join(
            f"<li><span class='muted'>{esc(fail['when'])}</span> {esc(fail['error'] or 'failed')}</li>"
            for fail in item["failures"]
        )
        size = f" · saved {esc(human_size(item['output_size']))}" if item["output_size"] else ""
        return (
            "<article class='disc'>"
            f"<div class='row'><h2>{esc(item['name'])}</h2>{badge(state if state != 'finished' else 'done')}</div>"
            f"<p class='message'>{esc(item['reason'])}</p>"
            f"<p class='muted'>{item['attempts']} failed attempt{'s' if item['attempts'] != 1 else ''}"
            f" · last {esc(item['when'])}{size}</p>"
            f"{where_text}{action}"
            f"<p class='muted'>Output: {esc(item['output'] or 'not recorded')}</p>"
            f"<details><summary>Error detail</summary><ul>{detail}</ul></details>"
            f"<details><summary>Attempts</summary><ul>{attempts}</ul></details>"
            "</article>"
        )

    needs = "".join(card(item) for item in data["needs"]) or "<article class='ok'>No discs are waiting for another try.</article>"
    hold = f"<p class='muted'>Hold file: {esc(data['hold'])}</p>" if data["hold"] else ""
    drive = data["drive"]
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Failed discs · {esc(data['host'])}</title>
<style>
{page_css()}
.disc {{ background: #1b232e; border: 1px solid #2c3848; border-radius: 12px; padding: 16px; margin: 0 0 12px; }}
button {{ background: #31527a; color: #e8eef6; border: 0; border-radius: 8px; padding: 8px 12px; font: inherit; }}
details {{ margin-top: 8px; }}
summary {{ cursor: pointer; color: #9ec1ff; }}
</style>
</head>
<body>
<main>
<header>
  <div>
    <h1>Failed discs</h1>
    <div class="muted"><a href="/">Status</a> · put one of these back in and close the tray</div>
  </div>
  <div class="muted">{esc(data['now'].replace('+00:00', ' UTC'))}</div>
</header>
<p>These still need another try. A disc leaves this page once a later rip succeeds. {esc(drive['text'])}{(' · ' + esc(drive['label'])) if drive['label'] else ''}.</p>
{hold}
{needs}
</main>
</body>
</html>
"""


def page_css():
    return """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 16px/1.45 ui-sans-serif, system-ui, sans-serif; background: #10141a; color: #e8eef6; }
main { max-width: 980px; margin: 0 auto; padding: 28px 18px 48px; }
header { display: flex; justify-content: space-between; gap: 16px; align-items: baseline; margin-bottom: 18px; }
h1 { font-size: 28px; margin: 0; }
h2 { font-size: 15px; margin: 0; text-transform: uppercase; letter-spacing: .04em; color: #9aabbe; }
.disc h2 { font-size: 22px; text-transform: none; letter-spacing: 0; color: #e8eef6; }
a { color: #9ec1ff; }
.muted { color: #93a2b5; font-size: 14px; }
.cards { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.card, .alert, .ok, .drive, table { background: #1b232e; border: 1px solid #2c3848; border-radius: 12px; }
.card { padding: 16px; }
.row { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.message { font-size: 18px; margin: 10px 0 4px; }
.queue { margin: 8px 0 0; font-size: 15px; }
.queue strong { color: #ffe7ad; font-size: 20px; }
.badge { border-radius: 999px; padding: 2px 8px; font-size: 12px; font-weight: 700; background: #314052; color: #d5deea; }
.badge.failed { background: #6d2c28; color: #ffd2cc; }
.badge.ripping, .badge.encoding { background: #6a5420; color: #ffe7ad; }
.badge.done, .badge.ripped { background: #1d5a40; color: #c9ffe4; }
.bar { height: 8px; background: #2c3848; border-radius: 99px; overflow: hidden; margin-top: 10px; }
.bar span { display: block; height: 100%; background: #e6b450; }
.drive { margin: 12px 0; padding: 12px 16px; }
.drive.warn { border-color: #8a6a2a; }
.alert { margin: 0 0 10px; padding: 14px 16px; border-color: #8a4038; background: #2a1b1b; }
.alert strong { color: #ffb4ac; }
.alert p { margin: 6px 0 0; }
.alert ul, .log { margin: 8px 0 0; padding-left: 18px; }
.ok { margin-bottom: 12px; padding: 14px 16px; border-color: #246246; color: #b7f3d4; }
table { width: 100%; border-collapse: collapse; margin-top: 8px; overflow: hidden; }
th, td { text-align: left; vertical-align: top; padding: 10px 12px; border-bottom: 1px solid #2c3848; }
th { color: #9aabbe; font-size: 12px; text-transform: uppercase; }
.logs { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 18px; }
.log { max-height: 280px; overflow: auto; font: 12px/1.4 ui-monospace, monospace; color: #c5d0dc; }
footer { margin-top: 18px; }
nav { display: flex; gap: 14px; margin-top: 6px; }
@media (max-width: 740px) {
  .cards, .logs { grid-template-columns: 1fr; }
  table { display: block; overflow-x: auto; }
}
"""


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def badge(status):
    kind = status if status in {"failed", "ripping", "encoding", "done", "ripped", "idle"} else "idle"
    return f'<span class="badge {esc(kind)}">{esc(status)}</span>'


def progress_bar(percent):
    if percent is None:
        return ""
    width = max(0, min(100, int(percent)))
    return (
        f'<div class="bar" aria-label="{width} percent"><span style="width:{width}%"></span></div>'
        f'<div class="muted">{width}%</div>'
    )


def render(data):
    cards = []
    for role in ROLES:
        service = data["services"][role]
        job = next((item for item in data["jobs"] if item["id"] and item["id"] == Path(service.get("job") or "").name), None)
        bits = [
            "<section class='card'>",
            f"<div class='row'><h2>{esc(role)}</h2>{badge(service.get('state') or 'unknown')}</div>",
            f"<p class='message'>{esc(service.get('message') or 'no status yet')}</p>",
        ]
        if role == "encoder":
            queued = data.get("encode_queue") or {}
            count = int(queued.get("count") or 0)
            noun = "disc" if count == 1 else "discs"
            line = f"<strong>{count}</strong> {noun} queued to encode"
            if queued.get("next"):
                line += f"<div class='muted'>next: {esc(queued['next'])}</div>"
            bits.append(f"<p class='queue'>{line}</p>")
        if service.get("updated"):
            bits.append(
                f"<p class='muted'>status {esc(ago(service['updated'], parse_time(data['now'])))}"
                f" · service {esc(service['active'])}</p>"
            )
        else:
            bits.append(f"<p class='muted'>service {esc(service['active'])}</p>")
        if job and job["progress"] is not None:
            bits.append(progress_bar(job["progress"]))
            if job["progress_detail"]:
                bits.append(f"<p class='muted'>{esc(job['progress_detail'])}</p>")
        if job and job["media_size"]:
            bits.append(
                f"<p class='muted'>{esc(job['media_name'])} · {esc(human_size(job['media_size']))}"
                f" · updated {esc(job['media_age'])}</p>"
            )
        bits.append("</section>")
        cards.append("".join(bits))

    drive = data["drive"]
    drive_bits = [esc(drive["text"])]
    if drive["label"]:
        drive_bits.append(esc(drive["label"]))
    if drive["size"] and drive["status"] == "DISC_OK":
        drive_bits.append(esc(drive["size"]))
    drive_class = "warn" if drive["status"] in {"TRAY_OPEN", "DRIVE_NOT_READY"} else ""

    attention = []
    for item in data["attention"]:
        title = esc(item["name"])
        if item.get("current") == "failed":
            title += " failed"
        detail = "".join(f"<li>{esc(line)}</li>" for line in item["detail"][-4:])
        extra = f"<ul>{detail}</ul>" if detail else ""
        when = f" · {esc(item['when'])}" if item["when"] else ""
        attention.append(
            f"<article class='alert'><strong>{title}</strong>{when}"
            f"<p>{esc(item['reason'])}</p>{extra}</article>"
        )

    rows = []
    for job in data["jobs"]:
        reason = job["error"] or job["reason"]
        if job["status"] == "failed":
            reason = job["reason"] or job["error"]
        size = ""
        if job["output_size"]:
            size = human_size(job["output_size"])
        elif job["media_size"]:
            size = human_size(job["media_size"])
        rows.append(
            "<tr>"
            f"<td>{badge(job['status'])}</td>"
            f"<td><strong>{esc(job['name'])}</strong><div class='muted'>{esc(job['id'])}</div></td>"
            f"<td>{esc(job['updated_ago'])}</td>"
            f"<td>{esc(reason)}</td>"
            f"<td>{esc(job.get('total_time') or '')}</td>"
            f"<td>{esc(size)}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='6' class='muted'>No jobs yet.</td></tr>")

    logs = []
    for role in ROLES:
        lines = "".join(f"<li>{esc(line)}</li>" for line in reversed(data["logs"][role]))
        logs.append(f"<section><h2>{esc(role)} log</h2><ul class='log'>{lines or '<li class=muted>empty</li>'}</ul></section>")

    disk = data["disk"]
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>Rip status · {esc(data['host'])}</title>
<style>
{page_css()}
</style>
</head>
<body>
<main>
<header>
  <div>
    <h1>Rip status</h1>
    <nav><a href="/failed">Failed discs</a><span class="muted">{esc(data['host'])} · refreshes every 10 seconds</span></nav>
  </div>
  <div class="muted">{esc(data['now'].replace('+00:00', ' UTC'))}</div>
</header>
{''.join(attention)}
<div class="cards">{''.join(cards)}</div>
<div class="drive {drive_class}"><strong>Drive.</strong> {' · '.join(drive_bits)}</div>
<h2>Recent jobs</h2>
<table>
<thead><tr><th>Status</th><th>Disc</th><th>When</th><th>What happened</th><th>Total time</th><th>Size</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
<div class="logs">{''.join(logs)}</div>
<footer class="muted">Movies disk {esc(disk['used'])} used, {esc(disk['avail'])} free of {esc(disk['size'])}. Read-only. This page is on port 8790 of this machine</footer>
</main>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "rip-status/1.0"

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            body = b"ok\n"
            kind = "text/plain; charset=utf-8"
        elif path == "/api/status":
            body = json.dumps(collect(), default=str).encode()
            kind = "application/json; charset=utf-8"
        elif path in {"/", "/index.html"}:
            body = render(collect()).encode()
            kind = "text/html; charset=utf-8"
        elif path == "/failed":
            body = render_failed(failed_review()).encode()
            kind = "text/html; charset=utf-8"
        elif path == "/api/failed":
            body = json.dumps(failed_review(), default=str).encode()
            kind = "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = urllib.parse.parse_qs(raw)
        if path == "/failed/release":
            release_hold(form.get("name", [""])[0])
            self.send_response(303)
            self.send_header("Location", "/failed")
            self.end_headers()
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default=os.environ.get("RIP_STATUS_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RIP_STATUS_PORT", "8790")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"rip-status listening on {args.bind}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
