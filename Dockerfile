FROM alpine:3.22

RUN apk add --no-cache python3 py3-pillow ffmpeg exiftool tzdata

COPY src/snaparchive /opt/snaparchive/snaparchive

ENV PYTHONPATH=/opt/snaparchive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

WORKDIR /tmp
ENTRYPOINT ["python3", "-m", "snaparchive"]
