# rip-and-serve

Watch an optical drive, rip a disc, encode it, name it, and serve it.

The ripper and the encoder are separate. A disc can be ripping while an earlier disc is encoding. A movie goes to the Movies folder. A disc of episodes goes to the TV Shows folder next to it. Jellyfin serves both. A small status page shows what is happening.

## Layout

- `bin/` starts a rip, watches the drive, and encodes the queue
- `lib/` chooses the title, sets encode quality, and names the disc
- `status/` is the status page
- `ocr/` is the Tesseract image used to read title cards
- `tools/build-title-index.py` builds a local title list from the public IMDb datasets
- `config/` and `systemd/` are the templates `install.sh` applies

Nothing personal belongs in this repo. API keys, the Jellyfin password, disc names, and the movie files stay on the machine.

## Requirements

- MakeMKV (`makemkvcon`) and HandBrake (`HandBrakeCLI`)
- Python 3
- Docker, for Jellyfin and title-card reading
- An optical drive, usually `/dev/sr0`
- A data disk for the movies and the work files

`install.sh` can write the systemd units and `/etc/rip-bluray.conf` only when sudo works without a password. The rest of the install does not need root if those files are already in place.

## Install

```sh
./install.sh
```

Running it again does not reset a working machine. It links the programs at `/usr/local/lib/rip-bluray` back to this repo, reloads the status page, builds the `rip-ocr` image if needed, and downloads the IMDb title list if it is not already there.

Copy `config/rip-bluray.conf.example` to `/etc/rip-bluray.conf` if that file does not exist. The services read it when they start.

## Naming

A scratched disc is not a failed movie. MakeMKV is told to skip an unreadable stretch and keep going. If the drive still gives up, the disc is copied with zeros in the bad spots and the rip is finished from that copy. The file will glitch for a moment where the scratch is.

A title printed on screen is tried first. Frames from the start of the file and the end credits are read with Tesseract and matched against the local IMDb list.

If that finds nothing, and `~/.elevenlabs.key` and `~/.tavily.key` exist, two short dialogue clips are transcribed and searched. Those key files are not part of this repo.

A name learned this way is stored on the boot disk, under `META_ROOT/state/identified-names` (`/var/lib/rip-bluray` by default). Putting that disc back in does not create a second copy under the old label. Only the big files — the MKV and MP4 — stay in `WORK_ROOT` on the media disk, so the media disk can idle between rips.

`config/disc-names.example` shows how to force a name for a disc whose label is useless.

## Encode quality

DVD and Blu-ray do not share one quality setting. The HandBrake preset is the base. The encoder then uses x264 slow at RF 16 for a DVD and RF 18 for a Blu-ray. Neither is upscaled.

A finished file counts as done only if it was encoded with the current setting. An older file is moved aside and the disc is ripped again.

## Status and playback

The status page listens on port 8790. Cron starts it at boot.

Jellyfin listens on port 8096. Movies and TV Shows are mounted read-only. The admin password, if you keep one for the library refresh, goes in `~/jellyfin/admin-password`. Do not commit that file.

## TV discs

A disc labeled with a season, and a run of titles the same length, is ripped as episodes. The play-all is skipped. `Bluey - Season 1 - First Half` becomes `TV Shows/Bluey (2018)/Season 01/Bluey - S01E01.mp4` and so on, in the order the titles are on the disc. The second half continues that numbering. Putting the disc back in does not create a second copy. The old play-all in Movies is moved aside once the episodes have been ripped.

## Services

`rip-bluray-ripper.service` watches the drive. `rip-bluray-encoder.service` encodes whatever has already been ripped. Do not restart the encoder while it is encoding. The install script reloads the ripper only when it is idle.
