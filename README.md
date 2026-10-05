```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
coldvideo-downloader --help
```

```sh
coldvideo-downloader 'https://.../<track-path>' -o track.m4a
```

| Option | Behavior |
| --- | --- |
| `--grace-seconds 60` | Extra allowance after remaining playback time. |
| `--max-seconds N` | Explicit playback-time cap across attempts. |
| `--stall-seconds 45` | Stop when playback and capture remain idle. |
| `--broken-seconds 20` | Check capture health and renew breakpoints when needed. |
| `--no-reconnect` | Stop after the first browser/CDP disconnect. |
| `--no-fix` | Keep validated fragmented MP4 without remuxing. |
| `--no-verify` | Skip decode checks; timeline checks remain mandatory and status is unverified. |

`python -m coldvideo_downloader` is also supported.

## Docker

```sh
docker build -t coldvideo-downloader .
mkdir -p downloads
docker run --rm --init --shm-size=1g \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD/downloads,target=/downloads" \
  coldvideo-downloader 'https://.../<track-path>' -o track.m4a
```