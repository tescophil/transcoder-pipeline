FROM ghcr.io/home-assistant/base:3.24

ENV PYTHONUNBUFFERED=1

# FFmpeg does the transcoding, Python runs the dashboard.
# intel-media-driver is the VA-API driver for Intel GPUs from Broadwell onwards (it includes the N100).
RUN apk add --no-cache ffmpeg python3 py3-pip intel-media-driver

# Extras for Quick Sync. They are separate so the build still succeeds if one is missing:
#  - libva-utils gives the "vainfo" check shown in the app log
#  - onevpl-intel-gpu is the Intel runtime that FFmpeg's QSV encoders load on 11th gen and newer
RUN apk add --no-cache libva-utils \
    && ( apk add --no-cache onevpl-intel-gpu || echo "NOTE: onevpl-intel-gpu not available, QSV will fall back to VAAPI" )

# Python dependencies (the flag is needed because Alpine's system Python is "externally managed";
# this container only ever runs this one app, so that is fine)
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# The dashboard itself
COPY app.py /opt/transcoder/app.py

# Start script
COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
