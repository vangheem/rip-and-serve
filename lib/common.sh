# Shared helpers for the automatic Blu-ray ripper and encoder.
# shellcheck shell=bash

RIP_CONF="${RIP_CONF:-/etc/rip-bluray.conf}"

load_config() {
  PROFILE=hd
  DEVICE=/dev/sr0
  DEST_ROOT=/mnt/data/videos/Movies
  WORK_ROOT=/mnt/data/rip-work
  LOCK_TIMEOUT=1200
  POLL_SECONDS=15
  CACHE=512
  SCAN_TIMEOUT=300
  if [[ -f "$RIP_CONF" ]]; then
    # shellcheck disable=SC1090
    source "$RIP_CONF"
  fi
  JOBS="$WORK_ROOT/jobs"
  LOCKS="$WORK_ROOT/locks"
  LOG_DIR="$WORK_ROOT/logs"
  STATUS_DIR="$WORK_ROOT/status"
  STATE_DIR="$WORK_ROOT/state"
  mkdir -p "$JOBS" "$LOCKS" "$LOG_DIR" "$STATUS_DIR" "$STATE_DIR" "$DEST_ROOT"
  # A scratched disc can knock the drive off USB. It comes back as another device.
  if [[ ! -b "${DEVICE:-}" ]]; then
    local candidate
    for candidate in /dev/sr1 /dev/sr2 /dev/sr3 /dev/sr0; do
      if [[ -b "$candidate" ]]; then
        DEVICE="$candidate"
        break
      fi
    done
  fi
}

log() {
  local line
  line="$(date -Is) [$ROLE] $*"
  printf '%s\n' "$line" | tee -a "$LOG_DIR/${ROLE}.log"
  if [[ "${ROLE:-}" == "ripper" ]]; then
    case "$*" in
      "rip produced no MKV for "*|"rip failed for "*)
        abandon_repeated_rip_failure "$*"
        ;;
      "rip complete: "*)
        rm -f "$STATE_DIR/rip-fail-count" "$STATE_DIR/rip-fail-name"
        identify_finished_rip "${*#rip complete: }"
        ;;
    esac
  fi
  if [[ "${ROLE:-}" == "encoder" && "$*" == "done: "* ]]; then
    record_finished_quality "${*#done: }"
  fi
}

# The ripper has already marked the job ripped. Pull it back until the title
# is known, or the encoder starts with the disc label.
identify_finished_rip() {
  local mkv="$1" job
  [[ -n "$mkv" && -f "$mkv" ]] || return 0
  job="$(dirname "$mkv")"
  [[ -f "$job/disc-name" ]] || return 0
  printf '%s\n' identifying > "$job/status"
  write_status ripping "identifying $(cat "$job/disc-name")" "$job"
  if ! timeout 300 python3 /usr/local/lib/rip-bluray/identify-title.py --apply-job "$job"; then
    printf '%s\n' "$(date -Is) [ripper] could not identify $(cat "$job/disc-name")" | tee -a "$LOG_DIR/ripper.log"
  fi
  if [[ "$(cat "$job/status" 2>/dev/null || true)" == "identifying" ]]; then
    printf '%s\n' ripped > "$job/status"
  fi
  return 0
}

# A scratched disc makes MakeMKV exit cleanly with no MKV. The ripper then
# retries forever. Two failures of the same disc is enough: eject it.
abandon_repeated_rip_failure() {
  local message="$1"
  local name count latest disc_id
  name="${message#rip produced no MKV for }"
  name="${name#rip failed for }"
  name="${name%%;*}"
  count=0
  if [[ -f "$STATE_DIR/rip-fail-name" && "$(cat "$STATE_DIR/rip-fail-name")" == "$name" ]]; then
    count="$(cat "$STATE_DIR/rip-fail-count" 2>/dev/null || echo 0)"
  fi
  [[ "$count" =~ ^[0-9]+$ ]] || count=0
  count=$((count + 1))
  printf '%s\n' "$name" > "$STATE_DIR/rip-fail-name"
  printf '%s\n' "$count" > "$STATE_DIR/rip-fail-count"
  if (( count < 2 )); then
    return 0
  fi
  latest=""
  local candidate
  shopt -s nullglob
  for candidate in "$JOBS"/*/disc-id; do
    if [[ -z "$latest" || "$candidate" -nt "$latest" ]]; then
      latest="$candidate"
    fi
  done
  disc_id=""
  if [[ -n "$latest" ]]; then
    disc_id="$(cat "$latest" 2>/dev/null || true)"
  fi
  if [[ -n "$disc_id" ]]; then
    printf '%s\n' "$disc_id" > "$STATE_DIR/hold-disc-id"
  fi
  printf '%s\n' "$(date -Is) [$ROLE] giving up on $name after $count failed rips (usually a scratched disc). Ejecting so it is not retried all day." | tee -a "$LOG_DIR/${ROLE}.log"
  write_status failed "$name failed $count times and was ejected. Clean the disc or skip it."
  eject -r "$DEVICE" || eject -s "$DEVICE" || true
}

write_status() {
  local state="$1"
  local message="$2"
  local job="${3:-}"
  # A scratch copy owns the drive. Do not report the disc done, and do not
  # let a status rewrite look like the rip stopped.
  if [[ -f "$STATE_DIR/scratch-copy" ]] && pgrep -f '/lib/copy-disc.py' >/dev/null 2>&1; then
    state=ripping
    message="copying around the scratch"
    job="$(cat "$STATE_DIR/scratch-copy")"
  fi
  cat > "$STATUS_DIR/$ROLE" <<EOF
state=$state
job=$job
updated=$(date -Is)
message=$message
EOF
}

profile_preset() {
  case "$1" in
    fast) printf '%s\n' "Fast 720p30" ;;
    balanced) printf '%s\n' "HQ 720p30 Surround" ;;
    hd) printf '%s\n' "HQ 1080p30 Surround" ;;
    max) printf '%s\n' "Super HQ 1080p30 Surround" ;;
    *) return 1 ;;
  esac
}

# The preset is only the base. DVD and Blu-ray then get their own quality.
# A finished movie is redone when its recorded tag does not match.
encode_quality_tag() {
  case "$1" in
    dvd) printf '%s\n' "dvd-x264-rf16-slow" ;;
    *) printf '%s\n' "bd-x264-rf18-slow" ;;
  esac
}

disc_kind_from_media() {
  local input="$1" line height
  line="$(command HandBrakeCLI --scan --title 1 --input "$input" 2>&1 | grep -m1 -E "size: [0-9]+x[0-9]+" || true)"
  height="${line#*x}"
  height="${height%%,*}"
  height="${height%% *}"
  if [[ "$height" =~ ^[0-9]+$ && "$height" -lt 700 ]]; then
    printf '%s\n' dvd
  else
    printf '%s\n' bluray
  fi
}

# The encoder calls HandBrakeCLI by name. This function replaces that command
# once the encoder process has loaded this file.
HandBrakeCLI() {
  local input="" kind quality tag i
  for ((i=1; i<=$#; i++)); do
    if [[ "${!i}" == "--input" ]]; then
      i=$((i + 1))
      input="${!i}"
    fi
  done
  kind="bluray"
  quality=18
  if [[ -n "$input" && -s "$input" ]]; then
    kind="$(disc_kind_from_media "$input")"
  fi
  if [[ "$kind" == "dvd" ]]; then
    quality=16
  fi
  tag="$(encode_quality_tag "$kind")"
  printf '%s\n' "$tag" > "$STATE_DIR/last-encode-tag"
  log "encode quality $tag"
  command HandBrakeCLI "$@" --encoder x264 --encoder-preset slow --quality "$quality"
}


# The ripper calls makemkvcon by name. A scratch ends the rip. Imaging the
# disc to skip bad sectors grinds the drive for hours.

# A read error plus a few hundred megabytes is the file ending at the scratch.
saved_movie() {
  local dest="$1" line size path
  line="$(find "$dest" -type f -name '*.mkv' -printf '%s\t%p\n' 2>/dev/null | sort -nr | head -1)"
  [[ -n "$line" ]] || return 1
  size="${line%%$'\t'*}"
  path="${line#*$'\t'}"
  if [[ "$size" -lt 1048576 ]]; then
    return 1
  fi
  if grep -q -E "UNCORRECTABLE|MEDIUM ERROR" "$dest/rip.log" 2>/dev/null && [[ "$size" -lt 800000000 ]]; then
    printf '%s\n' "saved file is too small after a read error"
    rm -f "$path"
    return 1
  fi
  return 0
}

makemkvcon() {
  local cmd="" source="" title="" dest="" i next
  for ((i=1; i<=$#; i++)); do
    if [[ "${!i}" == "mkv" ]]; then
      cmd=mkv
      next=$((i + 1))
      source="${!next}"
      next=$((i + 2))
      title="${!next}"
      next=$((i + 3))
      dest="${!next}"
    fi
  done
  if [[ "$cmd" != "mkv" || "$source" != dev:* ]]; then
    command makemkvcon "$@"
    return
  fi
  # MakeMKV exits 0 even when it saved nothing. A short file after a read
  # error is the movie cut off at the scratch, not a finished rip. Do not
  # image the disc to get past it.
  rm -f "${STATE_DIR:-}/skip-bad-sectors"
  command makemkvcon "$@" || true
  if saved_movie "$dest"; then
    return 0
  fi
  return 1
}

record_finished_quality() {
  local output="$1" tag
  tag="$(cat "$STATE_DIR/last-encode-tag" 2>/dev/null || true)"
  [[ -n "$tag" && -n "$output" ]] || return 0
  python3 - "$output" "$tag" << 'PY'
import sys
from pathlib import Path
output, tag = sys.argv[1], sys.argv[2]
base = Path(output).name
ledger = Path("/mnt/data/rip-work/state/encode-quality")
previous = Path("/mnt/data/rip-work/previous-encode") / base
lines = []
if ledger.exists():
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if line.split(" = ", 1)[0] != base:
            lines.append(line)
lines.append(f"{base} = {tag}")
ledger.parent.mkdir(parents=True, exist_ok=True)
ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
if previous.is_file():
    previous.unlink()
PY
}

lock_dir() {
  printf '%s\n' "$LOCKS/$1"
}

lock_busy() {
  local name="$1"
  local dir pid hb now timeout
  dir="$(lock_dir "$name")"
  [[ -d "$dir" ]] || return 1
  pid="$(cat "$dir/pid" 2>/dev/null || true)"
  hb="$(cat "$dir/heartbeat" 2>/dev/null || echo 0)"
  now="$(date +%s)"
  timeout="${LOCK_TIMEOUT}"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && (( now - hb < timeout )); then
    return 0
  fi
  return 1
}

reclaim_stale_lock() {
  local name="$1"
  local dir pid
  dir="$(lock_dir "$name")"
  [[ -d "$dir" ]] || return 0
  if lock_busy "$name"; then
    return 1
  fi
  pid="$(cat "$dir/pid" 2>/dev/null || true)"
  log "reclaiming stale $name lock pid=${pid:-unknown}"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 2
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -rf "$dir"
}

acquire_lock() {
  local name="$1"
  local dir
  dir="$(lock_dir "$name")"
  if mkdir "$dir" 2>/dev/null; then
    echo "$$" > "$dir/pid"
    date +%s > "$dir/heartbeat"
    return 0
  fi
  reclaim_stale_lock "$name" || return 1
  mkdir "$dir" 2>/dev/null || return 1
  echo "$$" > "$dir/pid"
  date +%s > "$dir/heartbeat"
}

release_lock() {
  local name="$1"
  local dir pid
  dir="$(lock_dir "$name")"
  pid="$(cat "$dir/pid" 2>/dev/null || true)"
  if [[ "$pid" == "$$" ]]; then
    rm -rf "$dir"
  fi
}

start_heartbeat() {
  local name="$1"
  local dir
  dir="$(lock_dir "$name")"
  (
    while true; do
      date +%s > "$dir/heartbeat"
      sleep 20
    done
  ) &
  HEARTBEAT_PID=$!
}

stop_heartbeat() {
  if [[ -n "${HEARTBEAT_PID:-}" ]]; then
    kill "$HEARTBEAT_PID" 2>/dev/null || true
    wait "$HEARTBEAT_PID" 2>/dev/null || true
    HEARTBEAT_PID=""
  fi
}

cdrom_drive_status() {
  python3 - "$DEVICE" << 'PY'
import fcntl, os, sys
# linux/cdrom.h: CDROM_DRIVE_STATUS
CDROM_DRIVE_STATUS = 0x5326
names = {
    0: "NO_INFO",
    1: "NO_DISC",
    2: "TRAY_OPEN",
    3: "DRIVE_NOT_READY",
    4: "DISC_OK",
}
try:
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK)
except OSError:
    print("NO_DISC")
    raise SystemExit(0)
try:
    status = fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0)
finally:
    os.close(fd)
print(names.get(status, "NO_INFO"))
PY
}

note_once() {
  local key="$1"
  shift
  local now last stamp
  now="$(date +%s)"
  stamp="$STATE_DIR/note-$key"
  last="$(cat "$stamp" 2>/dev/null || true)"
  [[ "$last" =~ ^[0-9]+$ ]] || last=0
  if (( now - last < 300 )); then
    return 0
  fi
  printf '%s\n' "$now" > "$stamp" || return 0
  log "$*"
}

# Some discs are seated (CDROM_DRIVE_STATUS == DISC_OK) but a normal open of
# /dev/sr0 fails with "No medium found". Blu-ray and CSS discs often do this,
# and so does a disc the drive rejects ("Incompatible medium installed").
# dd-based detection treated all of those as an empty tray and logged nothing.
medium_reject_reason() {
  local sense
  sense="$(mktemp)"
  timeout 8 sg_raw -t 5 -r 8 "$DEVICE" 28 00 00 00 00 00 00 00 01 00 >/dev/null 2>"$sense" || true
  if grep -q "Incompatible medium" "$sense"; then
    rm -f "$sense"
    printf '%s\n' "incompatible medium (often upside down, badly damaged, or a format this drive rejects)"
    return 0
  fi
  rm -f "$sense"
  return 1
}

drive_has_medium() {
  local status reason
  # Asking a busy drive if a disc is in it blocks for seconds and stalls the copy.
  if pgrep -f '/lib/copy-disc.py' >/dev/null 2>&1; then
    return 0
  fi
  status="$(cdrom_drive_status)"
  if [[ "$status" == "DISC_OK" || "$status" == "NO_DISC" ]]; then
    rm -f "$STATE_DIR/note-tray-open"
  fi
  if [[ "$status" == "TRAY_OPEN" || "$status" == "NO_DISC" ]]; then
    rm -f "$STATE_DIR/rip-fail-count" "$STATE_DIR/rip-fail-name"
  fi
  if [[ "$status" == "TRAY_OPEN" ]]; then
    note_once tray-open "tray is open; close it after inserting a disc (autoclose is off)"
    return 1
  fi
  if [[ "$status" != "DISC_OK" ]]; then
    return 1
  fi
  if reason="$(medium_reject_reason)"; then
    note_once unreadable "disc is in $DEVICE but the drive will not read it: $reason"
    write_status failed "disc in the drive is not readable ($reason). Flip it or remove it."
  else
    rm -f "$STATE_DIR/note-unreadable"
  fi
  return 0
}

wait_for_data_disk() {
  local i
  for i in $(seq 1 60); do
    if [[ -d /mnt/data && -w /mnt/data ]]; then
      return 0
    fi
    sleep 2
  done
  return 1
}
