# Transcoder Pipeline

Scans your media library for MKV, AVI and other video files and re-encodes them to MP4 with FFmpeg
(H.264 video, AAC audio by default). It can tell Jellyfin to rescan each finished file, and Sonarr
and Radarr can add newly imported files to the queue automatically.

> **Important:** after a transcode has finished and been verified, the **original file is deleted**
> and only the new `.mp4` is kept. Try it on a few files first.

## Hardware acceleration (Intel Quick Sync)

The app is set up for Intel Quick Sync (built for chips such as the N100).
It passes the GPU (`/dev/dri/renderD128`) into the app and uses it by default.

- Each file is tried in this order: QSV with GPU decoding, QSV with CPU decoding, VAAPI on the same
  GPU, then the CPU. The console in the dashboard says which method was used.
- On start, the app's **Log** tab shows a Quick Sync self-test. `Quick Sync (QSV) self-test: OK` means
  it works. A warning that VAAPI is used instead is still hardware encoding.
- In **Settings** the *Hardware Acceleration* box shows whether the GPU was detected. Choose
  *None* there to go back to the CPU.
- With QSV the CRF value is used as the quality target, and `-vf` / `-pix_fmt` in the extra flags are
  ignored.
- If the log says no GPU was found, the render device on your machine has a different name. Open
  **Terminal & SSH**, run `ls /dev/dri`, and change the `devices` line in `config.yaml` to match.

## Codec presets

**Settings** has ten numbered preset slots, like the buttons on a car radio. Each slot stores the
hardware acceleration, video codec, encoder preset, CRF, audio codec, audio bitrate and extra flags,
plus an optional name.

- **Load a slot:** click its number. The fields fill in; press **Save Settings** to use them.
- **Store a slot:** set the fields the way you want, click a slot number, and press **Store in slot**.
- **Clear a slot:** pick it and press **Clear slot**.
- A green dot marks the slot that matches the settings currently in use. Presets are kept across
  restarts, and **Restore Defaults** does not remove them.

## Which MP4 files are skipped

When a scan includes existing `.mp4` files, a file is skipped only if it already matches the settings
currently in use. The check reads three things from them:

- **Video codec:** from the video codec setting (`libx264` / `h264_qsv` means H.264, `libx265` /
  `hevc_qsv` means HEVC, and so on). With QSV on, the QSV encoder that will actually be used counts.
- **Pixel format:** from `-pix_fmt` or `format=` in `-vf` in the extra flags. With QSV it is always
  `yuv420p`.
- **Audio channels:** from `-ac` in the extra flags. If there is no `-ac`, channels aren't checked.

The audio codec is not checked. The scan log shows exactly what is being compared. Note that when you
switch to a different preset, existing MP4 files made with the old one no longer match, so a scan
including MP4 files will queue them for re-encoding. Untick *Include existing .mp4 files* in the scan
dialog to avoid that.

## Appearance

**Settings > Appearance** offers *Dark*, *Light* and *Follow the Home Assistant theme*. The last one
switches whenever Home Assistant switches between its light and dark themes, as long as the dashboard
is open inside Home Assistant. Opened directly on port 5000 it follows your browser's light/dark
setting instead. Changes preview straight away and are kept when you press Save.

## 1. Give the app access to your media

The app can see Home Assistant's `/media` and `/share` folders, and nothing else on the host.
To use media stored on a NAS, mount it in Home Assistant:

1. Go to **Settings > System > Storage** and select **Add network storage**.
2. Enter a **Name** (letters, numbers and underscores only, for example `nas`), set **Usage** to
   **Media**, and fill in the server, protocol (NFS or CIFS/SMB), share and credentials.
3. Select **Connect**. The share now appears at `/media/nas`.

If the folder does not show up in the app's folder picker, restart the app once.

## 2. Open the dashboard

Start the app, then select **Open Web UI** or use the **Transcoder** entry in the Home Assistant
sidebar. It opens inside Home Assistant, so it works from anywhere you can reach Home Assistant
without forwarding any ports. On your home network you can also use
`http://<your-home-assistant-ip>:5000` directly.

Use **Choose Directory & Scan** to pick a folder (for example `/media/nas/tv`). The scan options
(minimum file size, include existing `.mp4` files) are chosen in that dialog each time you scan.
Press **Start Queue** to begin.

## 3. Settings (saved automatically)

Everything you enter under **Settings** is stored in `/data/config.json`. It survives restarts and
app updates and is included in Home Assistant backups. The file contains your Jellyfin API key and
webhook token, so keep your backups private.

## 4. Jellyfin rescans (optional)

In **Settings**, tick *Notify Jellyfin to rescan each file after it is transcoded* and enter your
Jellyfin URL (for example `http://192.168.1.20:8096`) and an API key (create one in Jellyfin under
**Dashboard > API Keys**).

If Jellyfin sees your media at a different path than this app does, fill in the two path prefixes.
For example, this app sees `/media/nas/tv` and Jellyfin sees `/data/tv`.

## 5. Sonarr and Radarr webhook (optional)

1. In **Settings > Sonarr & Radarr > Webhook**, press **Generate** to create a token, then save.
2. Copy the **Webhook URL**. Sonarr and Radarr call the app directly on your local network, not
   through the Home Assistant web address, so use your Home Assistant's local address, for example `http://192.168.1.10:5000/api/webhook/arr?token=...`.
3. In Sonarr or Radarr go to **Settings > Connect > Add > Webhook**, paste the URL, set the method
   to **POST**, and enable **On File Import** and **On File Upgrade** (or **On Import Complete**).
   Use the **Test** button; the app's console should log a test event.
4. If Sonarr/Radarr see your files at different paths than this app, add path mappings, one per
   line, in the form `their path => this app's path`:

   ```
   /tv => /media/nas/tv
   /movies => /media/nas/movies
   ```

Imported files are added to the end of the queue, and the queue starts by itself if it is not
already running (unless you stopped it manually; press **Start Queue** to switch that back on).

## 6. Rescans in Sonarr and Radarr (optional)

The settings are split into tabs: **General**, **Transcoding**, **Jellyfin** and **Sonarr & Radarr**.
On the **Sonarr & Radarr** tab, tick *Tell Radarr/Sonarr to rescan...* and enter the address
(for example `http://192.168.1.10:7878` for Radarr, `:8989` for Sonarr) and the API key (Settings >
General > Security in Radarr/Sonarr). Use **Test connection** to check them.

After each verified transcode the app finds the movie or series whose folder contains the file and
asks Radarr/Sonarr to rescan it (a plain rescan), so they notice the changed file. The same path
mappings as the webhook are used, so add them if Sonarr/Radarr see your media at different paths. A
warning in the console means no movie or series matched the file.

## 7. Skipping files that would not shrink enough (optional)

Under **Settings > Transcoding**, set **Minimum estimated space saving (%)**, for example `20`. Before
each MP4 file is re-encoded the app encodes five short clips from it with your current settings,
estimates the final size and, if the saving would be smaller than your limit, marks the file
**Skipped** and leaves it as it is. The estimate is approximate (in testing it was within a few
percent) and adds roughly a minute per file. `0` switches the test off. It only applies to MP4 files; all other file types are always transcoded.

## Good to know

- **CPU load:** transcoding runs on the Home Assistant machine. The app runs at a lowered priority
  so Home Assistant stays responsive, but a small device will still be busy and may run warm.
- **Stopping:** press **Stop Queue** before stopping, restarting or updating the app. It cancels the
  current file and deletes the unfinished `.mp4`. Stopping the app mid-transcode leaves the
  unfinished file behind.
- **Port 5000:** if another app already uses port 5000, change the port on this app's
  **Configuration > Network** section.
- **Logs:** the app's **Log** tab shows startup messages and errors. The dashboard's own
  **Live Console Output** shows what the queue is doing, including where the settings file is
  saved and any Jellyfin or webhook problems.
