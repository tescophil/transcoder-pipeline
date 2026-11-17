#!/command/with-contenv bashio

# /data is this app's persistent volume. The dashboard saves its settings there
# (Jellyfin key, webhook token, path mappings, FFmpeg options), so they survive
# restarts and updates and are included in Home Assistant backups.
export TRANSCODER_CONFIG=/data/config.json

# Use Intel Quick Sync on a first run. Whatever you later choose in Settings is saved and wins.
export TRANSCODER_HWACCEL=qsv
export LIBVA_DRIVER_NAME=iHD

# bashio scripts run with "exit on any error". The checks below are informational only, so a
# failing check must never stop the app from starting.
set +e
set +o pipefail

bashio::log.info "Starting Transcoder Pipeline (port 5000 for webhooks, 8099 for the Home Assistant panel)"
bashio::log.info "Settings file: ${TRANSCODER_CONFIG}"

# Report whether the Intel GPU is usable, so problems show up in this log instead of mid-queue.
if ls /dev/dri/renderD* > /dev/null 2>&1; then
    GPU="$(ls /dev/dri/renderD* | head -n 1)"
    bashio::log.info "GPU render device found: ${GPU}"
    DRIVER="$(timeout 20 vainfo --display drm --device "${GPU}" 2>&1 | grep 'Driver version' | head -n 1)" || true
    bashio::log.info "VA-API: ${DRIVER:-no driver information (vainfo failed)}"

    if timeout 30 ffmpeg -hide_banner -loglevel error \
        -init_hw_device "qsv=hw" -filter_hw_device hw \
        -f lavfi -i testsrc2=size=1280x720:rate=25:duration=1 \
        -vf "format=nv12,hwupload=extra_hw_frames=16" \
        -c:v h264_qsv -f null - > /tmp/qsv-test.log 2>&1; then
        bashio::log.info "Quick Sync (QSV) self-test: OK"
    elif timeout 30 ffmpeg -hide_banner -loglevel error \
        -init_hw_device "vaapi=va:${GPU}" -filter_hw_device va \
        -f lavfi -i testsrc2=size=1280x720:rate=25:duration=1 \
        -vf "format=nv12,hwupload" -c:v h264_vaapi -f null - > /tmp/vaapi-test.log 2>&1; then
        bashio::log.warning "QSV self-test failed but VAAPI on the same GPU works; the app will use VAAPI."
        bashio::log.warning "QSV error: $(tail -n 3 /tmp/qsv-test.log 2>/dev/null | tr '\n' ' ')"
    else
        bashio::log.warning "Hardware encoding self-test failed; the app will fall back to the CPU."
        bashio::log.warning "QSV error: $(tail -n 3 /tmp/qsv-test.log 2>/dev/null | tr '\n' ' ')"
        bashio::log.warning "VAAPI error: $(tail -n 3 /tmp/vaapi-test.log 2>/dev/null | tr '\n' ' ')"
    fi
else
    bashio::log.warning "No GPU found at /dev/dri. Check that the app has the GPU device; transcoding will use the CPU."
fi

cd /opt/transcoder || exit 1

# One worker only: the queue is held in memory.
# gthread keeps the web page responsive even if a browser holds an idle connection.
# nice lowers the CPU priority so heavy FFmpeg jobs don't starve Home Assistant itself.
exec nice -n 10 python3 -m gunicorn \
    --bind 0.0.0.0:5000 \
    --bind 0.0.0.0:8099 \
    --workers 1 \
    --worker-class gthread \
    --threads 8 \
    --timeout 240 \
    --capture-output \
    app:app
