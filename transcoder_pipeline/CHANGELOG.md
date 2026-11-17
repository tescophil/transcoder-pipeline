# Changelog

## 2.13.0

- The Help window is now organised in tabs: Scanning, Queue, Space saving, Jellyfin, Webhook and Sonarr & Radarr.
- Added help for the Minimum estimated space saving setting and for the Sonarr/Radarr rescan settings alongside the webhook. Removed the FFmpeg extra flags reference.

## 2.12.2

- The Codec Presets section is now at the bottom of the General tab in Settings.
- The size estimate now says "12% larger" instead of "-12% smaller" when a file would grow, on the queue card, in the skip reason and in the console.

## 2.12.1

- The minimum-space-saving test now applies only to MP4 files. Every other file type (MKV, AVI and so on) is always transcoded without the test.

## 2.12.0

- New setting under Settings > Transcoding: **Minimum estimated space saving (%)**. When it is above 0, each file is tested as it starts being processed: the app encodes five 20-second clips spread through it with your current settings and scales the result to the whole file. If the estimated saving is below your limit, the file is not transcoded. It is marked **Skipped** in the queue (with the reason) and left untouched. Otherwise the estimate is shown on the card and processing carries on as usual.
- A skipped file is only tried again if you lower the limit. Files shorter than 2.5 minutes are not tested, and if the clips cannot be encoded the file is simply transcoded.
- Stop Queue and removing an item work while the test is running; the temporary clips are deleted.

## 2.11.0

- After a verified transcode the app can ask Radarr and/or Sonarr to rescan the movie or series that contains the file (a plain rescan), in the same way it already notifies Jellyfin. Enable and configure each under Settings > Sonarr & Radarr, with a Test connection button. Files are matched to movies and series using the existing path mappings.
- Settings are now organised in tabs: General, Transcoding, Jellyfin and Sonarr & Radarr. A green dot on a tab shows that its integration is switched on, and the tabs can be used with the arrow keys and scroll sideways on narrow screens.
- Restore Defaults keeps the Radarr and Sonarr connections, as it already did for Jellyfin.

## 2.10.3

- Files whose audio had no channel layout now also get their channel conversion (for example -ac 5) done inside the audio filter. In 2.10.2 newer FFmpeg ignored -ac in that case and left the audio at 6 channels, so the file would have been flagged non-compliant again on the next scan.

## 2.10.2

- Fixes files that failed in about a second with "aac … error code -22 (Invalid argument)" on every method. Their audio has a channel count but no speaker layout ("unknown"), which newer FFmpeg refuses to convert (for example with -ac 5). The app now labels such audio by its channel count (6 = 5.1, 8 = 7.1, and so on) before your channel setting is applied.
- As a last resort, if a file with AAC audio still fails, it is tried once more with the audio copied unchanged (your channel setting is not applied to that file) and the log says so.
- When every method fails, the log now includes FFmpeg's actual error for the last attempt.

## 2.10.1

- The remove button on each queue item now uses the bin icon (as on the console Clear button), and removing any item asks for confirmation first. Cancel, Escape or clicking outside leaves the queue untouched.

## 2.10.0

- Remove individual items from the queue with the ✕ button on each job. Pending and finished items are removed straight away. If the item is transcoding, you are asked to confirm; the transcode is then stopped, its incomplete output file is deleted, your original file is left untouched and the queue carries on with the next item.
- The setting is now labelled "Quality (CRF / global quality)". The value is sent to FFmpeg as `-crf` for software encoding and as `-global_quality` for Quick Sync (`-qp` for VAAPI).

## 2.9.0

- Stop Queue and Clear Queue now ask for confirmation first. Cancel, Escape or clicking outside the dialog leaves everything running; the dialog opens with Cancel selected. Clear Queue on an empty queue doesn't ask.

## 2.8.0

- Codec presets: ten "car radio" slots in Settings. Each stores the hardware acceleration, video codec, encoder preset, CRF, audio codec, audio bitrate and extra flags, with an optional name. Click a number to load it, press Store to save the current fields into it. They are kept across restarts and "Restore Defaults" does not clear them.
- Scanning existing MP4 files now checks them against the codec settings currently in use (video codec, pixel format and audio channels from the extra flags), instead of a fixed H.264 / yuv420p / 5-channel test. This also applies to files added by the Sonarr/Radarr webhook.
- The "FFmpeg Engine" line on the main page shows the encoder and preset in use.

## 2.7.2

- Fix: a hardware transcode that reached 100% could be killed while FFmpeg was finalising the file (the +faststart rewrite of a large output produces no video frames) and then restarted from scratch on the CPU. The stall check no longer applies once the encode has reached the end of the source, and the mid-file stall limit is now 5 minutes.

## 2.7.1

- Fix: the quick GPU decode trial could take as long as reading the whole video (41 s on a large file). It now reads only the first 3 seconds, so it finishes in about a second.

## 2.7.0

- Fail fast when the GPU can't decode a file: a 3-second trial of the GPU decode runs first, and if it fails the file goes straight to CPU decode with the GPU encoder.
- Hardware attempts that produce no video frame for 60 seconds (or stop producing it for 120 seconds) are abandoned and the next method is tried, instead of running to the end of the file.

## 2.6.1

- Fix: the Clear button on Live Console Output now clears the log on the server too, so the lines no longer come back.

## 2.6.0

- Layout for phones and small screens: the header buttons and queue controls wrap and share the row instead of running off the side, long file names and paths wrap inside their cards, and the dialogs fit short screens.

## 2.5.0

- New light theme. Settings > Appearance: Dark, Light, or Follow the Home Assistant theme (switches live when Home Assistant changes theme).
- Restore Defaults no longer switches hardware acceleration off in the Home Assistant app.

## 2.4.0

- The dashboard now opens inside Home Assistant (ingress) and gets a sidebar entry. No port forwarding needed.
- Port 5000 stays available on the local network for the Sonarr/Radarr webhook.

## 2.3.1

- Fix: the app exited right after the GPU was detected, because a failing startup check stopped the start script.

## 2.3.0

- Intel Quick Sync (QSV) hardware encoding is on by default. Drivers added to the image and the GPU (`/dev/dri/renderD128`) is passed to the app.
- Fallback order per file: QSV with GPU decode, QSV with CPU decode, VAAPI on the same GPU, then CPU.
- The app log reports a Quick Sync self-test at startup.
- Now amd64 only.

## 2.2.0

- First release as a Home Assistant app.
- Settings are saved to the app's persistent `/data` folder.
- Runs under gunicorn with a threaded worker and a watchdog.
