FROM debian:trixie-slim AS ffmpeg-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates curl nasm pkg-config xz-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl -fsSL https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz -o ffmpeg.tar.xz \
    && echo '8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e  ffmpeg.tar.xz' | sha256sum -c - \
    && tar -xf ffmpeg.tar.xz --strip-components=1 \
    && ./configure --prefix=/opt/ffmpeg \
        --disable-everything --disable-autodetect --disable-network --disable-doc --disable-debug \
        --enable-small --enable-ffmpeg --enable-ffprobe \
        --enable-decoder=aac,aac_fixed,alac,flac,mp3,opus,vorbis,pcm_s16le \
        --enable-encoder=aac,pcm_s16le \
        --enable-parser=aac,flac,mpegaudio,opus \
        --enable-demuxer=mov --enable-muxer=mp4,ipod,null --enable-protocol=file,pipe \
        --enable-indev=lavfi --enable-filter=sine,aresample,anull,aformat \
    && make -j4 \
    && make install \
    && mkdir -p /opt/ffmpeg/share/licenses \
    && cp COPYING.LGPLv2.1 LICENSE.md /opt/ffmpeg/share/licenses/ \
    && echo 'https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz' > /opt/ffmpeg/share/licenses/SOURCE

FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp

WORKDIR /opt/coldvideo-downloader
COPY pyproject.toml ./
RUN python -c 'import subprocess, tomllib; subprocess.check_call(["python", "-m", "pip", "install", *tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]])' \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && python -m patchright install --with-deps chrome \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg-build /opt/ffmpeg/bin/ /usr/local/bin/
COPY --from=ffmpeg-build /opt/ffmpeg/share/licenses/ /usr/local/share/ffmpeg/
COPY --from=ffmpeg-build /build/ffmpeg.tar.xz /usr/local/share/ffmpeg/ffmpeg-9.0.2.tar.xz
COPY README.md ./
COPY src/ ./src/
RUN python -m pip install --no-deps . \
    && rm -rf /opt/coldvideo-downloader \
    && useradd --uid 1000 --no-create-home downloader \
    && mkdir /downloads \
    && chown downloader:downloader /downloads

USER downloader
WORKDIR /downloads
ENTRYPOINT ["coldvideo-downloader"]
CMD ["--help"]
