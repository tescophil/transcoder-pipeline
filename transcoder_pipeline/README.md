# Transcoder Pipeline

Scan your media library and transcode MKV, AVI and other videos to MP4 with FFmpeg, from a web
dashboard inside your Home Assistant setup.

- Recursive folder scan with size filter, queue, progress and stop/clear controls
- Optional Jellyfin rescan of each finished file
- Webhook so Sonarr and Radarr can queue newly imported files automatically
- Settings are saved and survive restarts and updates

The original file is deleted after a successful, verified transcode.

See the **Documentation** tab for setup.
