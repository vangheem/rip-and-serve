#!/usr/bin/env bash
# Install rip-and-serve. Safe to run more than once.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(id -un)"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
PREFIX="${RIP_PREFIX:-/usr/local}"
LIB_DIR="$PREFIX/lib/rip-bluray"
BIN_DIR="$PREFIX/bin"
CONF="${RIP_CONF:-/etc/rip-bluray.conf}"

log() {
  printf '%s\n' "$*"
}

link_file() {
  local src="$1" dest="$2"
  mkdir -p "$(dirname "$dest")"
  if [[ -L "$dest" && "$(readlink -f "$dest")" == "$(readlink -f "$src")" ]]; then
    return 0
  fi
  if [[ -e "$dest" && ! -L "$dest" && ! -w "$dest" ]]; then
    log "leave $dest (not writable)"
    return 0
  fi
  ln -sfn "$src" "$dest"
  log "linked $dest"
}

load_dest() {
  DEST_ROOT=/mnt/data/videos/Movies
  WORK_ROOT=/mnt/data/rip-work
  if [[ -f "$CONF" ]]; then
    # shellcheck disable=SC1090
    source "$CONF"
  fi
}

install_libs() {
  mkdir -p "$LIB_DIR"
  local name
  for name in common.sh choose-title.py identify-title.py title_card.py copy-disc.py; do
    link_file "$ROOT/lib/$name" "$LIB_DIR/$name"
  done
  chmod +x "$ROOT"/lib/*.py "$ROOT"/tools/*.py
}

install_bins() {
  local name
  for name in rip-bluray rip-bluray-ripper rip-bluray-encoder; do
    if [[ -w "$BIN_DIR/$name" || ( ! -e "$BIN_DIR/$name" && -w "$BIN_DIR" ) ]]; then
      install -m 755 "$ROOT/bin/$name" "$BIN_DIR/$name"
      log "installed $BIN_DIR/$name"
    elif [[ -f "$BIN_DIR/$name" ]] && cmp -s "$ROOT/bin/$name" "$BIN_DIR/$name"; then
      log "ok $BIN_DIR/$name"
    else
      log "warning: $BIN_DIR/$name is not the repo copy and is not writable"
    fi
  done
}

install_conf() {
  if [[ -f "$CONF" ]]; then
    log "keeping $CONF"
    return 0
  fi
  if [[ -w "$(dirname "$CONF")" ]]; then
    install -m 644 "$ROOT/config/rip-bluray.conf.example" "$CONF"
    log "wrote $CONF"
  else
    log "copy config/rip-bluray.conf.example to $CONF"
  fi
}

install_status() {
  mkdir -p "$HOME_DIR/rip-status" "$HOME_DIR/.local/bin" "$HOME_DIR/.local/share/rip-status"
  link_file "$ROOT/status/rip-status.py" "$HOME_DIR/rip-status/rip-status.py"
  install -m 755 "$ROOT/bin/rip-status-up" "$HOME_DIR/.local/bin/rip-status-up"
  local current cron_line
  cron_line="@reboot $HOME_DIR/.local/bin/rip-status-up"
  current="$(crontab -l 2>/dev/null || true)"
  if ! grep -F "rip-status-up" <<<"$current" >/dev/null; then
    printf '%s\n%s\n' "$current" "$cron_line" | sed '/^$/d' | crontab -
    log "added reboot cron for the status page"
  else
    log "status cron already present"
  fi
  "$HOME_DIR/.local/bin/rip-status-up" --restart
}

install_ocr() {
  mkdir -p "$HOME_DIR/rip-ocr"
  link_file "$ROOT/ocr/Dockerfile" "$HOME_DIR/rip-ocr/Dockerfile"
  if ! command -v docker >/dev/null; then
    log "docker is not installed; title-card reading will not work"
    return 0
  fi
  docker build -t rip-ocr "$ROOT/ocr"
}

group_id() {
  getent group "$1" 2>/dev/null | cut -d: -f3 || true
}

write_compose() {
  local dest="$1" uid gid video render jellyfin_root movies
  uid="$(id -u)"
  gid="$(id -g)"
  video="$(group_id video || true)"
  render="$(group_id render || true)"
  jellyfin_root="${JELLYFIN_ROOT:-/mnt/data/jellyfin}"
  movies="$DEST_ROOT"
  {
    echo "services:"
    echo "  jellyfin:"
    echo "    image: jellyfin/jellyfin:latest"
    echo "    container_name: jellyfin"
    echo "    user: \"${uid}:${gid}\""
    if [[ -n "$video" || -n "$render" ]]; then
      echo "    group_add:"
      [[ -n "$video" ]] && echo "      - \"${video}\""
      [[ -n "$render" ]] && echo "      - \"${render}\""
    fi
    echo "    ports:"
    echo "      - \"8096:8096/tcp\""
    echo "      - \"7359:7359/udp\""
    echo "    volumes:"
    echo "      - ${jellyfin_root}/config:/config"
    echo "      - ${jellyfin_root}/cache:/cache"
    echo "      - type: bind"
    echo "        source: ${movies}"
    echo "        target: /media"
    echo "        read_only: true"
    if [[ -e /dev/dri ]]; then
      echo "    devices:"
      echo "      - /dev/dri:/dev/dri"
    fi
    echo "    restart: unless-stopped"
  } >"$dest"
}

install_jellyfin() {
  if ! command -v docker >/dev/null; then
    log "docker is not installed; Jellyfin was not started"
    return 0
  fi
  mkdir -p "$HOME_DIR/jellyfin" "${JELLYFIN_ROOT:-/mnt/data/jellyfin}/config" "${JELLYFIN_ROOT:-/mnt/data/jellyfin}/cache" "$DEST_ROOT"
  local generated="$HOME_DIR/jellyfin/docker-compose.yml.new"
  local live="$HOME_DIR/jellyfin/docker-compose.yml"
  write_compose "$generated"
  if [[ -f "$live" ]] && cmp -s "$generated" "$live"; then
    rm -f "$generated"
    log "jellyfin compose already matches"
  else
    mv "$generated" "$live"
    log "wrote $live"
  fi
  if [[ ! -f "$HOME_DIR/jellyfin/admin-password" ]]; then
    log "set a Jellyfin password in $HOME_DIR/jellyfin/admin-password after the first login"
  fi
  docker compose -f "$live" up -d
}

ensure_skip_read_errors() {
  local conf="$HOME_DIR/.MakeMKV/settings.conf"
  mkdir -p "$HOME_DIR/.MakeMKV"
  touch "$conf"
  if ! grep -q '^io_IgnoreReadErrors' "$conf"; then
    printf '%s\n' 'io_IgnoreReadErrors = "1"' >>"$conf"
    log "MakeMKV will skip an unreadable stretch instead of stopping"
  fi
  if ! grep -q '^io_ErrorRetryCount' "$conf"; then
    printf '%s\n' 'io_ErrorRetryCount = "2"' >>"$conf"
    log "MakeMKV will retry a bad spot twice, then move on"
  fi
}

install_titles() {
  local state="$WORK_ROOT/state/imdb"
  mkdir -p "$state"
  local basics="$state/title.basics.tsv.gz"
  local ratings="$state/title.ratings.tsv.gz"
  local index="$state/titles.json"
  if [[ ! -s "$basics" ]]; then
    curl -fsSL -o "$basics" https://datasets.imdbws.com/title.basics.tsv.gz
  fi
  if [[ ! -s "$ratings" ]]; then
    curl -fsSL -o "$ratings" https://datasets.imdbws.com/title.ratings.tsv.gz
  fi
  if [[ ! -s "$index" || "$basics" -nt "$index" || "$ratings" -nt "$index" ]]; then
    RIP_IMDB_DIR="$state" python3 "$ROOT/tools/build-title-index.py"
  else
    log "title index already current"
  fi
}

install_units() {
  local unit dest tmp
  if sudo -n true 2>/dev/null; then
    for unit in rip-bluray-ripper.service rip-bluray-encoder.service; do
      dest="/etc/systemd/system/$unit"
      tmp="$(mktemp)"
      sed "s/@RIP_USER@/$USER_NAME/g" "$ROOT/systemd/$unit" >"$tmp"
      if [[ -f "$dest" ]] && cmp -s "$tmp" "$dest"; then
        log "ok $dest"
      else
        sudo install -m 644 "$tmp" "$dest"
        log "installed $dest"
      fi
      rm -f "$tmp"
    done
    if [[ ! -f "$CONF" ]]; then
      sudo install -m 644 "$ROOT/config/rip-bluray.conf.example" "$CONF"
    fi
    sudo systemctl daemon-reload
    sudo systemctl enable rip-bluray-ripper.service rip-bluray-encoder.service
  else
    log "systemd units were not changed (sudo needs a password)"
  fi
}

reload_idle_ripper() {
  local status pid
  status="$WORK_ROOT/status/ripper"
  [[ -f "$status" ]] || return 0
  if [[ "$(sed -n 's/^state=//p' "$status")" != "idle" ]]; then
    log "ripper is busy; it will read the repo copy the next time it starts"
    return 0
  fi
  if ! command -v systemctl >/dev/null; then
    return 0
  fi
  pid="$(systemctl show -p MainPID --value rip-bluray-ripper.service 2>/dev/null || true)"
  if [[ -n "$pid" && "$pid" != "0" ]]; then
    kill "$pid" || true
    log "restarted the idle ripper so it loads the repo copy"
  fi
}

main() {
  load_dest
  install_libs
  ensure_skip_read_errors
  install_bins
  install_conf
  load_dest
  install_status
  install_ocr
  install_jellyfin
  install_titles
  install_units
  reload_idle_ripper
  log "install finished"
}

main "$@"
