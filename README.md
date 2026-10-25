# rip-and-serve

Watch an optical drive, rip a disc, encode it, name it, and serve it.

The ripper and the encoder are separate. A disc can be ripping while an earlier disc is encoding. Finished files go to a Movies folder. Jellyfin serves that folder. A small status page shows what is happening.

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

A title printed on screen is tried first. Frames from the start of the file and the end credits are read with Tesseract and matched against the local IMDb list.

If that finds nothing, and `~/.elevenlabs.key` and `~/.tavily.key` exist, two short dialogue clips are transcribed and searched. Those key files are not part of this repo.

A name learned this way is stored in the work directory, under `state/identified-names`. Putting that disc back in does not create a second copy under the old label.

`config/disc-names.example` shows how to force a name for a disc whose label is useless.

## Encode quality

DVD and Blu-ray do not share one quality setting. The HandBrake preset is the base. The encoder then uses x264 slow at RF 16 for a DVD and RF 18 for a Blu-ray. Neither is upscaled.

A finished file counts as done only if it was encoded with the current setting. An older file is moved aside and the disc is ripped again.

## Status and playback

The status page listens on port 8790. Cron starts it at boot.

Jellyfin listens on port 8096. The Movies folder is mounted read-only. The admin password, if you keep one for the library refresh, goes in `~/jellyfin/admin-password`. Do not commit that file.

## Services

`rip-bluray-ripper.service` watches the drive. `rip-bluray-encoder.service` encodes whatever has already been ripped. Do not restart the encoder while it is encoding. The install script reloads the ripper only when it is idle.
