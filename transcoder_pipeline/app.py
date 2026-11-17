import hmac
import html
import itertools
import json
import os
import tempfile
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from flask import Flask, jsonify, request, render_template_string


app = Flask(__name__)


# Global state
JOBS = []
JOBS_LOCK = threading.RLock()  # guards removing items from JOBS and handing out ids
_JOB_IDS = itertools.count(1)
CURRENT_JOB = None  # the job the worker is transcoding right now
QUEUE_RUNNING = False
CURRENT_INDEX = 0
QUEUE_START_TIME = None
LOGS = []
SCAN_STATE = {
    "running": False,
    "logs": []
}
CANCEL_EVENT = threading.Event()
CURRENT_PROCESS = None
WORKER_THREAD = None
QUEUE_LOCK = threading.Lock()
AUTO_START_PAUSED = False  # set when the user stops the queue; cleared again by Start Queue

# Supported video extensions for recursive search (max depth 10)
VIDEO_EXTENSIONS = ('.mkv', '.avi', '.m4v', '.divx', '.mov', '.wmv', '.flv', '.webm', '.mpeg', '.mpg', '.mp4')


# Default FFmpeg settings with your exact requested defaults
CONFIG = {
    "vcodec": "libx264",
    "preset": "medium",
    "crf": "28",
    "acodec": "aac",
    "audio_bitrate": "192k",
    "extra_flags": "-ac 5 -vf format=yuv420p -movflags +faststart",
    # "none" = CPU (libx264), "qsv" = Intel Quick Sync Video. The Home Assistant app sets
    # TRANSCODER_HWACCEL=qsv so a first run already uses the GPU; saved settings override it.
    "hwaccel": os.environ.get("TRANSCODER_HWACCEL", "none"),
    "theme": "dark",  # "dark", "light" or "ha" (follow the Home Assistant theme)
    "presets": [None] * 10,  # ten "car radio" slots, each a saved set of video/audio codec settings
    "min_size_val": 0,
    "min_size_unit": "GB",
    "include_mp4": True,
    # Skip files whose estimated space saving is below this percentage (0 = no test, process everything)
    "min_savings_percent": 0,
    "jellyfin_enabled": False,
    "jellyfin_url": "",
    "jellyfin_api_key": "",
    "jellyfin_path_from": "",
    "jellyfin_path_to": "",
    # Ask Radarr / Sonarr to rescan the movie / series after a file has been transcoded
    "radarr_enabled": False,
    "radarr_url": "",
    "radarr_api_key": "",
    "sonarr_enabled": False,
    "sonarr_url": "",
    "sonarr_api_key": "",
    # Sonarr/Radarr webhook. The env vars let these survive an app restart.
    "webhook_token": os.environ.get("WEBHOOK_TOKEN", ""),
    "webhook_path_map": os.environ.get("WEBHOOK_PATH_MAP", "")
}


THEME_CHOICES = ("dark", "light", "ha")


def add_log(msg, level='info'):
    timestamp = time.strftime("%H:%M:%S")
    color_class = "text-slate-300"
    if level == 'error':
        color_class = "text-rose-400 font-medium"
    elif level == 'success':
        color_class = "text-emerald-400 font-medium"
    elif level == 'warning':
        color_class = "text-amber-400"
    elif level == 'transcode':
        color_class = "text-indigo-300"
    
    formatted = f'<span class="text-slate-500">[{timestamp}]</span> <span class="{color_class}">{msg}</span>'
    LOGS.append(formatted)
    if len(LOGS) > 300:
        LOGS.pop(0)


def add_scan_log(msg, level='info'):
    timestamp = time.strftime("%H:%M:%S")
    color_class = "text-slate-300"
    if level == 'error':
        color_class = "text-rose-400"
    elif level == 'success':
        color_class = "text-emerald-400"
    elif level == 'warning':
        color_class = "text-amber-400"
    
    formatted = f'<span class="text-slate-500">[{timestamp}]</span> <span class="{color_class}">{msg}</span>'
    SCAN_STATE["logs"].append(formatted)
    if len(SCAN_STATE["logs"]) > 200:
        SCAN_STATE["logs"].pop(0)


# Settings are saved to a JSON file so they survive restarts. By default it lives in a "data" folder
# next to this file if one exists (the Docker volume ./data:/app/data), otherwise next to this file.
# Set the TRANSCODER_CONFIG environment variable to keep it somewhere else.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG_DIR = os.path.join(_APP_DIR, "data") if os.path.isdir(os.path.join(_APP_DIR, "data")) else _APP_DIR
CONFIG_FILE = os.path.abspath(os.environ.get("TRANSCODER_CONFIG") or os.path.join(_DEFAULT_CONFIG_DIR, "config.json"))
CONFIG_LOCK = threading.Lock()


def _setting_type_ok(default, value):
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, list)
    return isinstance(value, str)


PRESET_SLOTS = 10
PRESET_FIELDS = ("hwaccel", "vcodec", "preset", "crf", "acodec", "audio_bitrate", "extra_flags")


def _clean_preset(p):
    """A stored preset as a plain dict of strings, or None if it isn't usable."""
    if not isinstance(p, dict):
        return None
    out = {"name": str(p.get("name", ""))[:40].strip()}
    for key in PRESET_FIELDS:
        value = p.get(key, "")
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            return None
        out[key] = str(value).strip()
    if out["hwaccel"] not in ("none", "qsv"):
        out["hwaccel"] = "none"
    if not out["vcodec"]:
        return None
    return out


def _clean_presets(values):
    values = values if isinstance(values, list) else []
    return [_clean_preset(p) for p in (values + [None] * PRESET_SLOTS)[:PRESET_SLOTS]]


def load_config():
    """Load saved settings over the defaults. Unknown or wrongly typed entries are ignored."""
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            raise ValueError("expected a JSON object")
    except FileNotFoundError:
        add_log(f"Settings file: {html.escape(CONFIG_FILE)} (not created yet; it is written the first time you save settings)", level='info')
        return
    except Exception as e:
        add_log(f"Settings: could not read {html.escape(CONFIG_FILE)} ({html.escape(str(e))}). Using defaults.", level='error')
        return
    for key, default in list(CONFIG.items()):
        if key in saved and _setting_type_ok(default, saved[key]):
            CONFIG[key] = saved[key]
    CONFIG["presets"] = _clean_presets(CONFIG.get("presets"))
    add_log(f"Settings loaded from {html.escape(CONFIG_FILE)}", level='info')


def save_config():
    """Write the current settings to disk (atomically, readable only by the owner). Never raises."""
    with CONFIG_LOCK:
        tmp_path = CONFIG_FILE + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(CONFIG_FILE)), exist_ok=True)
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(CONFIG, f, indent=2)
            os.replace(tmp_path, CONFIG_FILE)
            add_log(f"Settings saved to {html.escape(CONFIG_FILE)}", level='success')
            return True
        except Exception as e:
            add_log(f"Settings: could not save to {html.escape(CONFIG_FILE)}: {html.escape(str(e))}. They will be lost when the app restarts.", level='error')
            return False


add_log("[System initialized. Waiting for actions...]", level='info')
load_config()


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" class="dark" data-theme-pref="{{ theme_pref }}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Transcoder Pipeline</title>
    <!-- Browser Tab Icon (Favicon Data URI) -->
    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 576 512%22><path fill=%22%236366f1%22 d=%22M549.65 172.91l-112-64C425.41 101.38 416 109.97 416 120v96c0 10.03 9.41 18.62 21.65 11.09l112-64c12.3-7.03 12.3-25.15 0-32.18zM576 384c0 35.35-28.65 64-64 64H64c-35.35 0-64-28.65-64-64V128c0-35.35 28.65-64 64-64h448c35.35 0 64 28.65 64 64v256zM512 128H64v256h448V128z%22/></svg>">
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    // Every colour is a CSS variable so the light/dark theme can swap the whole palette at once
                    colors: {
                        darker: 'rgb(var(--darker) / <alpha-value>)', darkcard: 'rgb(var(--darkcard) / <alpha-value>)',
                        slate: { 50: 'rgb(var(--slate-50) / <alpha-value>)', 100: 'rgb(var(--slate-100) / <alpha-value>)', 200: 'rgb(var(--slate-200) / <alpha-value>)', 300: 'rgb(var(--slate-300) / <alpha-value>)', 400: 'rgb(var(--slate-400) / <alpha-value>)', 500: 'rgb(var(--slate-500) / <alpha-value>)', 600: 'rgb(var(--slate-600) / <alpha-value>)', 700: 'rgb(var(--slate-700) / <alpha-value>)', 800: 'rgb(var(--slate-800) / <alpha-value>)', 900: 'rgb(var(--slate-900) / <alpha-value>)', 950: 'rgb(var(--slate-950) / <alpha-value>)' },
                        indigo: { 50: 'rgb(var(--indigo-50) / <alpha-value>)', 100: 'rgb(var(--indigo-100) / <alpha-value>)', 200: 'rgb(var(--indigo-200) / <alpha-value>)', 300: 'rgb(var(--indigo-300) / <alpha-value>)', 400: 'rgb(var(--indigo-400) / <alpha-value>)', 500: 'rgb(var(--indigo-500) / <alpha-value>)', 600: 'rgb(var(--indigo-600) / <alpha-value>)', 700: 'rgb(var(--indigo-700) / <alpha-value>)', 800: 'rgb(var(--indigo-800) / <alpha-value>)', 900: 'rgb(var(--indigo-900) / <alpha-value>)', 950: 'rgb(var(--indigo-950) / <alpha-value>)' },
                        emerald: { 50: 'rgb(var(--emerald-50) / <alpha-value>)', 100: 'rgb(var(--emerald-100) / <alpha-value>)', 200: 'rgb(var(--emerald-200) / <alpha-value>)', 300: 'rgb(var(--emerald-300) / <alpha-value>)', 400: 'rgb(var(--emerald-400) / <alpha-value>)', 500: 'rgb(var(--emerald-500) / <alpha-value>)', 600: 'rgb(var(--emerald-600) / <alpha-value>)', 700: 'rgb(var(--emerald-700) / <alpha-value>)', 800: 'rgb(var(--emerald-800) / <alpha-value>)', 900: 'rgb(var(--emerald-900) / <alpha-value>)', 950: 'rgb(var(--emerald-950) / <alpha-value>)' },
                        rose: { 50: 'rgb(var(--rose-50) / <alpha-value>)', 100: 'rgb(var(--rose-100) / <alpha-value>)', 200: 'rgb(var(--rose-200) / <alpha-value>)', 300: 'rgb(var(--rose-300) / <alpha-value>)', 400: 'rgb(var(--rose-400) / <alpha-value>)', 500: 'rgb(var(--rose-500) / <alpha-value>)', 600: 'rgb(var(--rose-600) / <alpha-value>)', 700: 'rgb(var(--rose-700) / <alpha-value>)', 800: 'rgb(var(--rose-800) / <alpha-value>)', 900: 'rgb(var(--rose-900) / <alpha-value>)', 950: 'rgb(var(--rose-950) / <alpha-value>)' },
                        amber: { 50: 'rgb(var(--amber-50) / <alpha-value>)', 100: 'rgb(var(--amber-100) / <alpha-value>)', 200: 'rgb(var(--amber-200) / <alpha-value>)', 300: 'rgb(var(--amber-300) / <alpha-value>)', 400: 'rgb(var(--amber-400) / <alpha-value>)', 500: 'rgb(var(--amber-500) / <alpha-value>)', 600: 'rgb(var(--amber-600) / <alpha-value>)', 700: 'rgb(var(--amber-700) / <alpha-value>)', 800: 'rgb(var(--amber-800) / <alpha-value>)', 900: 'rgb(var(--amber-900) / <alpha-value>)', 950: 'rgb(var(--amber-950) / <alpha-value>)' }
                    }
                }
            }
        }
    </script>
    <script>
        // Theme: "dark", "light", or "ha" (follow the Home Assistant theme this page is shown in).
        (function () {
            var root = document.documentElement;
            var pref = root.getAttribute('data-theme-pref') || 'dark';
            function toRgb(value) {
                var probe = document.createElement('span');
                probe.style.color = value;
                if (!probe.style.color) return null;
                root.appendChild(probe);
                var c = getComputedStyle(probe).color;
                root.removeChild(probe);
                var m = c.match(/[\d.]+/g);
                if (!m || m.length < 3 || (m.length > 3 && parseFloat(m[3]) === 0)) return null;
                return [parseFloat(m[0]), parseFloat(m[1]), parseFloat(m[2])];
            }
            function homeAssistantIsDark() {
                try {
                    var p = window.parent;
                    if (p && p !== window) {
                        var cs = p.getComputedStyle(p.document.documentElement);
                        var names = ['--primary-background-color', '--card-background-color', '--lovelace-background'];
                        for (var i = 0; i < names.length; i++) {
                            var v = cs.getPropertyValue(names[i]).trim();
                            var rgb = v && toRgb(v);
                            if (rgb) return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255 < 0.5;
                        }
                    }
                } catch (e) { /* not embedded in Home Assistant, or not allowed to look */ }
                return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
            }
            function effective() {
                return pref === 'light' ? 'light' : pref === 'dark' ? 'dark' : (homeAssistantIsDark() ? 'dark' : 'light');
            }
            function refresh() {
                var t = effective();
                if (root.getAttribute('data-theme') !== t) root.setAttribute('data-theme', t);
            }
            window.applyTheme = function (newPref) { pref = newPref || 'dark'; refresh(); };
            refresh();
            // Follow live changes: Home Assistant changes its theme on its own page, so watch for that
            try {
                new MutationObserver(function () { if (pref === 'ha') refresh(); })
                    .observe(window.parent.document.documentElement, { attributes: true, attributeFilter: ['style', 'class'] });
            } catch (e) {}
            try { window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () { if (pref === 'ha') refresh(); }); } catch (e) {}
            setInterval(function () { if (pref === 'ha') refresh(); }, 2000);
        })();
    </script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root, html[data-theme="dark"] { color-scheme: dark; --slate-50: 248 250 252; --slate-100: 241 245 249; --slate-200: 226 232 240; --slate-300: 203 213 225; --slate-400: 148 163 184; --slate-500: 100 116 139; --slate-600: 71 85 105; --slate-700: 51 65 85; --slate-800: 30 41 59; --slate-900: 15 23 42; --slate-950: 2 6 23; --indigo-50: 238 242 255; --indigo-100: 224 231 255; --indigo-200: 199 210 254; --indigo-300: 165 180 252; --indigo-400: 129 140 248; --indigo-500: 99 102 241; --indigo-600: 79 70 229; --indigo-700: 67 56 202; --indigo-800: 55 48 163; --indigo-900: 49 46 129; --indigo-950: 30 27 75; --emerald-50: 236 253 245; --emerald-100: 209 250 229; --emerald-200: 167 243 208; --emerald-300: 110 231 183; --emerald-400: 52 211 153; --emerald-500: 16 185 129; --emerald-600: 5 150 105; --emerald-700: 4 120 87; --emerald-800: 6 95 70; --emerald-900: 6 78 59; --emerald-950: 2 44 34; --rose-50: 255 241 242; --rose-100: 255 228 230; --rose-200: 254 205 211; --rose-300: 253 164 175; --rose-400: 251 113 133; --rose-500: 244 63 94; --rose-600: 225 29 72; --rose-700: 190 18 60; --rose-800: 159 18 57; --rose-900: 136 19 55; --rose-950: 76 5 25; --amber-50: 255 251 235; --amber-100: 254 243 199; --amber-200: 253 230 138; --amber-300: 252 211 77; --amber-400: 251 191 36; --amber-500: 245 158 11; --amber-600: 217 119 6; --amber-700: 180 83 9; --amber-800: 146 64 14; --amber-900: 120 53 15; --amber-950: 69 26 3; --darker: 15 23 42; --darkcard: 30 41 59; }
        html[data-theme="light"] { color-scheme: light; --slate-50: 2 6 23; --slate-100: 15 23 42; --slate-200: 30 41 59; --slate-300: 51 65 85; --slate-400: 71 85 105; --slate-500: 100 116 139; --slate-600: 148 163 184; --slate-700: 203 213 225; --slate-800: 226 232 240; --slate-900: 241 245 249; --slate-950: 248 250 252; --indigo-50: 30 27 75; --indigo-100: 49 46 129; --indigo-200: 55 48 163; --indigo-300: 67 56 202; --indigo-400: 79 70 229; --indigo-500: 99 102 241; --indigo-600: 79 70 229; --indigo-700: 67 56 202; --indigo-800: 199 210 254; --indigo-900: 224 231 255; --indigo-950: 238 242 255; --emerald-50: 2 44 34; --emerald-100: 6 78 59; --emerald-200: 6 95 70; --emerald-300: 4 120 87; --emerald-400: 4 120 87; --emerald-500: 16 185 129; --emerald-600: 5 150 105; --emerald-700: 4 120 87; --emerald-800: 167 243 208; --emerald-900: 209 250 229; --emerald-950: 236 253 245; --rose-50: 76 5 25; --rose-100: 136 19 55; --rose-200: 159 18 57; --rose-300: 190 18 60; --rose-400: 225 29 72; --rose-500: 244 63 94; --rose-600: 225 29 72; --rose-700: 190 18 60; --rose-800: 254 205 211; --rose-900: 255 228 230; --rose-950: 255 241 242; --amber-50: 69 26 3; --amber-100: 120 53 15; --amber-200: 146 64 14; --amber-300: 180 83 9; --amber-400: 180 83 9; --amber-500: 180 83 9; --amber-600: 217 119 6; --amber-700: 180 83 9; --amber-800: 253 230 138; --amber-900: 254 243 199; --amber-950: 255 251 235; --darker: 241 245 249; --darkcard: 255 255 255; }
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: rgb(var(--darker)); border-radius: 4px; }
        ::-webkit-scrollbar-thumb { background: rgb(var(--slate-700)); border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: rgb(var(--slate-600)); }
        html, body { min-height: 100vh; overflow-y: auto; overflow-x: hidden; }
    </style>
</head>
<body class="bg-darker text-slate-100 font-sans flex flex-col min-h-screen">
    <!-- Top Navigation Bar -->
    <header class="bg-darkcard border-b border-slate-800 px-3 sm:px-6 py-3 sm:py-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-3 shadow-md flex-shrink-0 min-h-[70px]">
        <div class="flex items-center space-x-3 min-w-0">
            <div class="bg-indigo-600 p-2 rounded-lg text-white">
                <i class="fa-solid fa-video text-xl"></i>
            </div>
            <div>
                <h1 class="text-lg sm:text-xl font-bold tracking-wide">Transcoder Pipeline <span class="text-xs bg-indigo-900 text-indigo-200 px-2 py-0.5 rounded-full ml-1">v2.13</span></h1>
                <p class="text-xs text-slate-400 truncate hidden sm:block">Debian Media Library Transcoder</p>
            </div>
        </div>
        <div class="flex items-center gap-2 sm:gap-3 w-full sm:w-auto">
            <button onclick="openHelpModal()" class="flex-1 sm:flex-none justify-center bg-slate-800 hover:bg-slate-700 text-slate-200 px-3 py-2 rounded-lg text-sm font-medium transition flex items-center space-x-2 border border-slate-700 shadow">
                <i class="fa-solid fa-circle-question text-indigo-400"></i>
                <span>Help</span>
            </button>
            <button onclick="openSettingsModal()" class="flex-1 sm:flex-none justify-center bg-slate-800 hover:bg-slate-700 text-slate-200 px-3.5 py-2 rounded-lg text-sm font-medium transition flex items-center space-x-2 border border-slate-700 shadow">
                <i class="fa-solid fa-gear text-indigo-400"></i>
                <span>Settings</span>
            </button>
            <button onclick="openBrowseModal()" class="flex-1 sm:flex-none justify-center bg-indigo-600 hover:bg-indigo-500 text-white px-4 py-2 rounded-lg text-sm font-medium transition flex items-center space-x-2 shadow">
                <i class="fa-solid fa-folder-open"></i>
                <span class="hidden sm:inline">Choose Directory & Scan</span><span class="sm:hidden">Scan</span>
            </button>
        </div>
    </header>


    <main class="flex-1 p-3 sm:p-6 max-w-7xl mx-auto w-full grid grid-cols-1 lg:grid-cols-3 gap-4 sm:gap-6 min-w-0">
        <!-- Left Column: Transcode Queue Window -->
        <section class="lg:col-span-2 min-w-0 bg-darkcard rounded-xl border border-slate-800 p-3 sm:p-5 shadow-lg flex flex-col h-[75vh] min-h-[440px] lg:min-h-0 lg:h-[calc(100vh-140px)]">
            <div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 mb-4 border-b border-slate-800 pb-3 flex-shrink-0">
                <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
                    <h2 class="text-lg font-semibold flex items-center space-x-2">
                        <i class="fa-solid fa-list-check text-indigo-400"></i>
                        <span>Transcode Queue</span>
                    </h2>
                    <label class="flex items-center space-x-1.5 cursor-pointer select-none text-xs text-slate-400 hover:text-slate-200 border-l border-slate-700 pl-3">
                        <input type="checkbox" id="autoscroll-toggle" class="rounded bg-slate-900 border-slate-700 text-indigo-600 focus:ring-0 focus:ring-offset-0 w-3.5 h-3.5 cursor-pointer" />
                        <span>Auto-Scroll</span>
                    </label>
                </div>
                <div class="flex flex-wrap items-center gap-2 w-full sm:w-auto">
                    <div class="text-xs text-slate-400 sm:mr-2 w-full sm:w-auto" id="queueCounter">0 jobs</div>
                    <button onclick="startQueue()" id="btn-start-queue" class="flex-1 sm:flex-none justify-center bg-emerald-600 hover:bg-emerald-500 text-white px-3 py-2 sm:py-1.5 rounded text-xs font-medium transition flex items-center space-x-1 shadow">
                        <i class="fa-solid fa-play"></i><span>Start Queue</span>
                    </button>
                    <button onclick="stopQueue()" id="btn-stop-queue" class="flex-1 sm:flex-none justify-center bg-rose-600 hover:bg-rose-500 text-white px-3 py-2 sm:py-1.5 rounded text-xs font-medium transition flex items-center space-x-1 shadow hidden">
                        <i class="fa-solid fa-stop"></i><span>Stop Queue</span>
                    </button>
                    <button onclick="clearQueue()" id="btn-clear-queue" class="flex-1 sm:flex-none justify-center bg-slate-700 hover:bg-slate-600 text-slate-100 px-3 py-2 sm:py-1.5 rounded text-xs font-medium transition flex items-center space-x-1 shadow">
                        <i class="fa-solid fa-trash-can"></i><span>Clear Queue</span>
                    </button>
                </div>
            </div>
            <!-- Scrollable Job List Container -->
            <div class="flex-1 overflow-y-auto pr-2 space-y-3 min-h-0" id="job-list-container">
                <div class="text-center py-20 text-slate-500">
                    <i class="fa-solid fa-inbox text-4xl mb-3 block"></i>
                    <p>No media files loaded yet. Click <strong>Choose Directory & Scan</strong> above.</p>
                </div>
            </div>
        </section>


        <!-- Right Column Stack -->
        <section class="flex flex-col gap-4 min-w-0 lg:h-[calc(100vh-140px)]">
            <div class="bg-darkcard rounded-xl border border-slate-800 p-4 shadow-lg flex-shrink-0">
                <h3 class="text-md font-semibold mb-3 flex items-center space-x-2 border-b border-slate-800 pb-2">
                    <i class="fa-solid fa-server text-emerald-400"></i>
                    <span>Container Status</span>
                </h3>
                <div class="space-y-2 text-sm">
                    <div class="flex justify-between items-center"><span class="text-slate-400">Target Root:</span><code class="bg-slate-900 px-2 py-1 rounded text-xs text-indigo-300">/media</code></div>
                    <div class="flex justify-between items-center"><span class="text-slate-400">FFmpeg Engine:</span><span id="engine-label" class="text-emerald-400 font-medium text-right">Ready (libx264)</span></div>
                    <div class="flex justify-between items-center"><span class="text-slate-400">Worker Status:</span><span class="text-indigo-400 font-medium" id="worker-status">Idle</span></div>
                    <div class="flex justify-between items-center" id="processing-time-row" style="display: none;"><span class="text-slate-400">Processing Time:</span><span class="text-indigo-300 font-mono text-xs" id="processing-time-val">0d 00h 00m</span></div>
                    <div class="flex justify-between items-center"><span class="text-slate-400">Size Delta:</span><span id="container-size-delta" class="font-mono text-xs font-medium text-slate-300">0.0% (0GB)</span></div>
                </div>
            </div>


            <!-- Folder Summary Window -->
            <div class="bg-darkcard rounded-xl border border-slate-800 p-4 shadow-lg flex-shrink-0 flex flex-col h-[200px]">
                <h3 class="text-md font-semibold mb-3 flex items-center space-x-2 border-b border-slate-800 pb-2 flex-shrink-0">
                    <i class="fa-solid fa-folder-tree text-indigo-400"></i>
                    <span>Folder Summary</span>
                </h3>
                <div class="space-y-2 overflow-y-auto pr-1 text-xs flex-1 min-h-0" id="folder-summary-container">
                    <div class="text-slate-500 italic text-center py-2">No folders scanned yet.</div>
                </div>
            </div>


            <!-- Live Console Output Window -->
            <div class="bg-darkcard rounded-xl border border-slate-800 p-4 shadow-lg flex flex-col flex-none h-80 lg:h-auto lg:flex-1 min-h-0">
                <div class="flex items-center justify-between mb-3 border-b border-slate-800 pb-2 flex-shrink-0">
                    <h3 class="text-md font-semibold flex items-center space-x-2">
                        <i class="fa-solid fa-terminal text-amber-400"></i>
                        <span>Live Console Output</span>
                    </h3>
                    <button onclick="clearConsoleLogs()" class="text-xs text-slate-500 hover:text-rose-400 transition" title="Clear console history"><i class="fa-solid fa-trash-can"></i> Clear</button>
                </div>
                <div id="logContainer" class="bg-slate-950 font-mono text-xs p-3 rounded-lg flex-1 overflow-y-auto border border-slate-900 shadow-inner whitespace-pre-wrap break-words min-h-0"><span class="text-slate-500">[System initialized...]</span></div>
            </div>
        </section>
    </main>


    <!-- Directory Browser Modal -->
    <div id="browse-modal" class="fixed inset-0 bg-black/70 backdrop-blur-sm hidden items-center justify-center z-50 p-4">
        <div class="bg-darkcard border border-slate-800 w-full max-w-xl rounded-xl shadow-2xl flex flex-col overflow-hidden max-h-[92vh]">
            <div class="px-4 sm:px-6 py-4 border-b border-slate-800 flex items-center justify-between flex-shrink-0">
                <h3 class="text-md font-semibold flex items-center space-x-2"><i class="fa-solid fa-folder-open text-indigo-400"></i><span>Select Media Directory</span></h3>
                <button onclick="closeBrowseModal()" class="text-slate-400 hover:text-slate-200"><i class="fa-solid fa-xmark text-lg"></i></button>
            </div>
            <div class="p-4 sm:p-6 space-y-4 overflow-y-auto flex-1 min-h-0">
                <div>
                    <label class="block text-xs text-slate-400 mb-1">Current Path:</label>
                    <input type="text" id="modal-current-path" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300 focus:outline-none focus:border-indigo-500" />
                </div>
                <div class="flex items-center justify-between text-xs text-slate-400">
                    <span id="modal-mkv-count">0 media files found in this specific folder</span>
                    <button id="btn-up-dir" onclick="navigateParent()" class="hover:text-slate-100 flex items-center space-x-1 text-indigo-400 font-medium disabled:opacity-40" disabled><i class="fa-solid fa-arrow-up"></i> <span>Parent Directory</span></button>
                </div>
                <div class="border border-slate-800 rounded-lg bg-slate-950 h-56 overflow-y-auto p-2 space-y-1" id="modal-subdirs-list">
                    <div class="text-center py-16 text-slate-500 text-sm">Loading directories...</div>
                </div>
                <div class="grid grid-cols-2 gap-3 pt-3 border-t border-slate-800">
                    <div>
                        <label class="block text-xs text-slate-300 font-medium mb-1">Minimum File Size Filter (`>`)</label>
                        <input type="number" id="scan-min-size-val" min="0" step="0.1" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                    </div>
                    <div>
                        <label class="block text-xs text-slate-300 font-medium mb-1">Size Unit</label>
                        <select id="scan-min-size-unit" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300">
                            <option value="MB">MB</option>
                            <option value="GB">GB</option>
                        </select>
                    </div>
                    <div class="col-span-2">
                        <label class="flex items-center space-x-2 cursor-pointer select-none">
                            <input type="checkbox" id="scan-include-mp4" onchange="loadDirectory(activeBrowsePath)" class="rounded bg-slate-900 border-slate-700 text-indigo-600 focus:ring-0 focus:ring-offset-0 w-4 h-4 cursor-pointer" />
                            <span class="text-xs text-slate-300 font-medium">Include existing .mp4 files in scan</span>
                        </label>
                        <p class="text-[11px] text-slate-500 mt-1">When enabled, MP4 files are checked for compliance and queued if non-compliant. When disabled, all .mp4 files are ignored.</p>
                    </div>
                </div>
            </div>
            <div class="px-4 sm:px-6 py-4 border-t border-slate-800 bg-slate-900/50 flex flex-wrap justify-end gap-2 sm:gap-3 flex-shrink-0">
                <button onclick="closeBrowseModal()" class="px-4 py-2 rounded text-sm bg-slate-800 hover:bg-slate-700 text-slate-300 transition">Cancel</button>
                <button onclick="scanCurrentModalPath()" class="px-4 py-2 rounded text-sm bg-indigo-600 hover:bg-indigo-500 text-white font-medium transition shadow flex items-center justify-center space-x-2 flex-1 sm:flex-none"><i class="fa-solid fa-magnifying-glass"></i><span>Scan This Directory & Subfolders</span></button>
            </div>
        </div>
    </div>


    <!-- Settings Modal -->
    <div id="settings-modal" class="fixed inset-0 bg-black/70 backdrop-blur-sm hidden items-center justify-center z-50 p-4">
        <div class="bg-darkcard border border-slate-800 w-full max-w-2xl rounded-xl shadow-2xl flex flex-col overflow-hidden h-[90vh] sm:h-[44rem] max-h-[90vh]">
            <div class="px-6 py-4 border-b border-slate-800 flex items-center justify-between flex-shrink-0">
                <h3 class="text-md font-semibold flex items-center space-x-2"><i class="fa-solid fa-gear text-indigo-400"></i><span>Settings</span></h3>
                <button onclick="closeSettingsModal()" class="text-slate-400 hover:text-slate-200"><i class="fa-solid fa-xmark text-lg"></i></button>
            </div>
<div class="border-b border-slate-800 flex-shrink-0 overflow-x-auto">
                <div role="tablist" aria-label="Settings sections" id="settings-tabs" class="flex gap-1 px-3 sm:px-4 min-w-max">
                    <button type="button" role="tab" id="tab-general" data-tab="general" aria-controls="settings-panel-general" aria-selected="false" tabindex="-1" onclick="showSettingsTab('general')" class="settings-tab -mb-px px-3 py-3 text-sm font-medium border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center gap-2 whitespace-nowrap transition"><i class="fa-solid fa-sliders"></i><span>General</span><span id="tab-dot-general" class="hidden w-1.5 h-1.5 rounded-full bg-emerald-400" title="Enabled"></span></button>
                    <button type="button" role="tab" id="tab-transcoding" data-tab="transcoding" aria-controls="settings-panel-transcoding" aria-selected="false" tabindex="-1" onclick="showSettingsTab('transcoding')" class="settings-tab -mb-px px-3 py-3 text-sm font-medium border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center gap-2 whitespace-nowrap transition"><i class="fa-solid fa-film"></i><span>Transcoding</span><span id="tab-dot-transcoding" class="hidden w-1.5 h-1.5 rounded-full bg-emerald-400" title="Enabled"></span></button>
                    <button type="button" role="tab" id="tab-jellyfin" data-tab="jellyfin" aria-controls="settings-panel-jellyfin" aria-selected="false" tabindex="-1" onclick="showSettingsTab('jellyfin')" class="settings-tab -mb-px px-3 py-3 text-sm font-medium border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center gap-2 whitespace-nowrap transition"><i class="fa-solid fa-circle-play"></i><span>Jellyfin</span><span id="tab-dot-jellyfin" class="hidden w-1.5 h-1.5 rounded-full bg-emerald-400" title="Enabled"></span></button>
                    <button type="button" role="tab" id="tab-arr" data-tab="arr" aria-controls="settings-panel-arr" aria-selected="false" tabindex="-1" onclick="showSettingsTab('arr')" class="settings-tab -mb-px px-3 py-3 text-sm font-medium border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center gap-2 whitespace-nowrap transition"><i class="fa-solid fa-tower-broadcast"></i><span>Sonarr &amp; Radarr</span><span id="tab-dot-arr" class="hidden w-1.5 h-1.5 rounded-full bg-emerald-400" title="Enabled"></span></button>
                </div>
            </div>
            <div class="p-4 sm:p-6 overflow-y-auto flex-1">
                <div id="settings-panel-general" role="tabpanel" aria-labelledby="tab-general" class="settings-panel space-y-6">
                    <div>
                    <label class="block text-xs text-slate-300 font-medium mb-1">Appearance</label>
                    <select id="cfg-theme" onchange="applyTheme(this.value)" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm text-indigo-300">
                        <option value="dark">Dark</option>
                        <option value="light">Light</option>
                        <option value="ha">Follow the Home Assistant theme</option>
                    </select>
                    <p class="text-[11px] text-slate-500 mt-1">The choice is previewed straight away and applied when you save. "Follow the Home Assistant theme" switches along with Home Assistant when this page is open inside it; elsewhere it follows your browser's light/dark setting.</p>
                </div>
                    <div class="border border-slate-800 rounded-lg p-3 sm:p-4 bg-slate-900/40 text-[11px] text-slate-400 space-y-1">
                        <p class="text-xs text-slate-300 font-medium"><i class="fa-solid fa-circle-info text-indigo-400 mr-1"></i>About these settings</p>
                        <p>Settings are saved on the server, so they apply to every browser and survive restarts and updates. Press <strong>Save Settings</strong> to keep changes made on any tab. <strong>Restore Defaults</strong> resets the FFmpeg options only; your presets and the Jellyfin, Sonarr and Radarr connections are kept.</p>
                    </div>
                    <div class="border border-slate-800 rounded-lg p-3 bg-slate-900/40">
                        <div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 mb-2">
                            <label class="text-xs text-slate-300 font-medium"><i class="fa-solid fa-radio text-indigo-400 mr-1"></i>Codec Presets</label>
                            <span id="preset-msg" class="text-[11px] text-slate-500"></span>
                        </div>
                        <div class="grid grid-cols-5 sm:grid-cols-10 gap-2" id="preset-buttons"></div>
                        <p id="preset-summary" class="text-[11px] text-slate-400 mt-2 break-words"></p>
                        <div class="flex flex-wrap items-center gap-2 mt-2">
                            <input type="text" id="preset-name" maxlength="40" placeholder="Preset name (optional)" class="flex-1 min-w-[10rem] bg-slate-900 border border-slate-700 rounded px-3 py-2 text-xs text-indigo-300" />
                            <button type="button" id="btn-preset-store" onclick="storePreset()" class="px-3 py-2 rounded text-xs bg-indigo-600 hover:bg-indigo-500 text-white font-medium shadow disabled:opacity-40">Store in slot</button>
                            <button type="button" id="btn-preset-clear" onclick="clearPreset()" class="px-3 py-2 rounded text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 disabled:opacity-40">Clear slot</button>
                        </div>
                        <p class="text-[11px] text-slate-500 mt-2">Each slot stores the hardware acceleration, video and audio codec options on the Transcoding tab. Click a number to load it into the fields (then press Save Settings to use it); set the fields how you want them, pick a slot and press Store to save them there. A green dot marks the slot that matches the current settings. Existing MP4 files are checked against the settings in use.</p>
                    </div>
                </div>
                <div id="settings-panel-transcoding" role="tabpanel" aria-labelledby="tab-transcoding" class="settings-panel hidden">
                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div class="md:col-span-2">
                        <label class="block text-xs text-slate-300 font-medium mb-1">Hardware Acceleration</label>
                        <select id="cfg-hwaccel" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300">
                            <option value="none">None (CPU, libx264)</option>
                            <option value="qsv">Intel Quick Sync Video (QSV)</option>
                        </select>
                        <p id="cfg-hwaccel-status" class="text-[11px] text-slate-500 mt-1"></p>
                        <p class="text-[11px] text-slate-500 mt-1">With QSV the video codec becomes h264_qsv (or hevc_qsv if you enter a x265/hevc codec), the CRF value is used as the quality target, and any <code>-vf</code> / <code>-pix_fmt</code> in the extra flags is ignored. If the GPU run fails the file is retried automatically, finally on the CPU.</p>
                    </div>
                    <div>
                        <label class="block text-xs text-slate-300 font-medium mb-1">Video Codec (`-c:v`)</label>
                        <input type="text" id="cfg-vcodec" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                    </div>
                    <div>
                        <label class="block text-xs text-slate-300 font-medium mb-1">Encoder Preset (`-preset`)</label>
                        <select id="cfg-preset" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300">
                            <option value="ultrafast">ultrafast</option>
                            <option value="superfast">superfast</option>
                            <option value="veryfast">veryfast</option>
                            <option value="faster">faster</option>
                            <option value="fast">fast</option>
                            <option value="medium">medium</option>
                            <option value="slow">slow</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs text-slate-300 font-medium mb-1">Quality (CRF / global quality)</label>
                        <input type="number" id="cfg-crf" min="0" max="51" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                    </div>
                    <div>
                        <label class="block text-xs text-slate-300 font-medium mb-1">Audio Codec (`-c:a`)</label>
                        <input type="text" id="cfg-acodec" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-xs text-slate-300 font-medium mb-1">Audio Bitrate (`-b:a`)</label>
                        <input type="text" id="cfg-audio-bitrate" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-xs text-slate-300 font-medium mb-1">Extra Custom Flags / Arguments</label>
                        <input type="text" id="cfg-extra" placeholder="e.g. -ac 5 -vf format=yuv420p -movflags +faststart" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                        <p class="text-[11px] text-slate-500 mt-1">Additional space-separated FFmpeg arguments injected directly before output filename.</p>
                    </div>
                    <div class="md:col-span-2 pt-3 border-t border-slate-800">
                        <label class="block text-xs text-slate-300 font-medium mb-1">Minimum estimated space saving (%)</label>
                        <input type="number" id="cfg-min-savings" min="0" max="95" step="1" placeholder="0 (off)" class="w-full md:w-48 bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                        <p class="text-[11px] text-slate-500 mt-1">0 switches this off. Otherwise, when an MP4 file starts being processed the app first encodes five short clips spread through it with the settings above and estimates the final size. If the estimated saving is below this percentage, the file is not transcoded: it is marked <strong>Skipped</strong> and left untouched. The estimate is approximate and adds roughly a minute per file. The test only applies to MP4 files; all other file types are always transcoded. Files shorter than 2.5 minutes are not tested. A skipped file is only tried again if you lower this limit.</p>
                    </div>
                    </div>
                </div>
                <div id="settings-panel-jellyfin" role="tabpanel" aria-labelledby="tab-jellyfin" class="settings-panel hidden">
                    <div class="space-y-3">
                        <label class="flex items-center space-x-2 cursor-pointer select-none">
                            <input type="checkbox" id="cfg-jf-enabled" onchange="updateTabIndicators()" class="rounded bg-slate-900 border-slate-700 text-indigo-600 focus:ring-0 focus:ring-offset-0 w-4 h-4 cursor-pointer" />
                            <span class="text-xs text-slate-300 font-medium">Notify Jellyfin to rescan each file after it is transcoded</span>
                        </label>
                        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                            <div>
                                <label class="block text-xs text-slate-300 font-medium mb-1">Jellyfin Server URL</label>
                                <input type="text" id="cfg-jf-url" placeholder="http://192.168.1.10:8096" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                            </div>
                            <div>
                                <label class="block text-xs text-slate-300 font-medium mb-1">Jellyfin API Key</label>
                                <input type="password" id="cfg-jf-key" autocomplete="off" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                            </div>
                            <div>
                                <label class="block text-xs text-slate-300 font-medium mb-1">Path prefix in this app (optional)</label>
                                <input type="text" id="cfg-jf-path-from" placeholder="/media" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                            </div>
                            <div>
                                <label class="block text-xs text-slate-300 font-medium mb-1">Same prefix in Jellyfin (optional)</label>
                                <input type="text" id="cfg-jf-path-to" placeholder="/data/media" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                            </div>
                        </div>
                        <p class="text-[11px] text-slate-500">Create an API key in Jellyfin under Dashboard &gt; API Keys. Only fill in the path prefixes if Jellyfin sees your media at a different path than this app does.</p>
                    </div>
                </div>
                <div id="settings-panel-arr" role="tabpanel" aria-labelledby="tab-arr" class="settings-panel hidden space-y-6">
                    <div class="space-y-3">
                        <h4 class="text-xs text-slate-200 font-semibold">Webhook (add new downloads to the queue)</h4>
                        <div>
                            <label class="block text-xs text-slate-300 font-medium mb-1">Webhook Token</label>
                            <div class="flex space-x-2">
                                <input type="text" id="cfg-wh-token" oninput="updateWebhookUrl(); updateTabIndicators()" autocomplete="off" class="flex-1 min-w-0 bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                                <button type="button" onclick="generateWebhookToken()" class="px-3 py-2 rounded text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700">Generate</button>
                            </div>
                        </div>
                        <div>
                            <label class="block text-xs text-slate-300 font-medium mb-1">Webhook URL (paste into Sonarr / Radarr)</label>
                            <input type="text" id="cfg-wh-url" readonly onclick="this.select()" class="w-full bg-slate-900 border border-slate-800 rounded px-3 py-2 text-xs font-mono text-slate-400" />
                        </div>
                        <div>
                            <label class="block text-xs text-slate-300 font-medium mb-1">Path mappings (optional)</label>
                            <textarea id="cfg-wh-map" rows="3" placeholder="/tv =&gt; /media/tv&#10;/movies =&gt; /media/movies" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300"></textarea>
                        </div>
                        <p class="text-[11px] text-slate-500">One mapping per line: the path as Sonarr/Radarr see it, then <code>=&gt;</code>, then the same location as this app sees it. Only needed if the two differ. If the host in the URL above is not reachable from Sonarr/Radarr, replace it with this machine's address.</p>
                    </div>
                    <div class="space-y-3">
                        <h4 class="text-xs text-slate-200 font-semibold">Sonarr (rescan after transcoding)</h4>
                        <div class="border border-slate-800 rounded-lg p-3 sm:p-4 bg-slate-900/40 space-y-3">
                            <label class="flex items-center space-x-2 cursor-pointer select-none">
                                <input type="checkbox" id="cfg-sonarr-enabled" onchange="updateTabIndicators()" class="rounded bg-slate-900 border-slate-700 text-indigo-600 focus:ring-0 focus:ring-offset-0 w-4 h-4 cursor-pointer" />
                                <span class="text-xs text-slate-300 font-medium">Tell Sonarr to rescan the series after a file is transcoded</span>
                            </label>
                            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                                <div>
                                    <label class="block text-xs text-slate-300 font-medium mb-1">Sonarr URL</label>
                                    <input type="text" id="cfg-sonarr-url" placeholder="http://192.168.1.10:8989" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                                </div>
                                <div>
                                    <label class="block text-xs text-slate-300 font-medium mb-1">Sonarr API Key</label>
                                    <input type="password" id="cfg-sonarr-key" autocomplete="off" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                                </div>
                            </div>
                            <div class="flex flex-wrap items-center gap-3">
                                <button type="button" id="btn-test-sonarr" onclick="testArr('sonarr')" class="px-3 py-2 rounded text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 disabled:opacity-40"><i class="fa-solid fa-plug mr-1"></i>Test connection</button>
                                <span id="arr-test-sonarr" class="text-[11px] text-slate-500 break-words min-w-0" role="status"></span>
                            </div>
                            <p class="text-[11px] text-slate-500">Find the API key in Sonarr under Settings &gt; General &gt; Security. Use the address this app can reach Sonarr at. After each verified transcode the app finds the series that contains the file and asks Sonarr to rescan it, so it notices the changed file.</p>
                        </div>
                    </div>
                    <div class="space-y-3">
                        <h4 class="text-xs text-slate-200 font-semibold">Radarr (rescan after transcoding)</h4>
                        <div class="border border-slate-800 rounded-lg p-3 sm:p-4 bg-slate-900/40 space-y-3">
                            <label class="flex items-center space-x-2 cursor-pointer select-none">
                                <input type="checkbox" id="cfg-radarr-enabled" onchange="updateTabIndicators()" class="rounded bg-slate-900 border-slate-700 text-indigo-600 focus:ring-0 focus:ring-offset-0 w-4 h-4 cursor-pointer" />
                                <span class="text-xs text-slate-300 font-medium">Tell Radarr to rescan the movie after a file is transcoded</span>
                            </label>
                            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                                <div>
                                    <label class="block text-xs text-slate-300 font-medium mb-1">Radarr URL</label>
                                    <input type="text" id="cfg-radarr-url" placeholder="http://192.168.1.10:7878" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                                </div>
                                <div>
                                    <label class="block text-xs text-slate-300 font-medium mb-1">Radarr API Key</label>
                                    <input type="password" id="cfg-radarr-key" autocomplete="off" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm font-mono text-indigo-300" />
                                </div>
                            </div>
                            <div class="flex flex-wrap items-center gap-3">
                                <button type="button" id="btn-test-radarr" onclick="testArr('radarr')" class="px-3 py-2 rounded text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 disabled:opacity-40"><i class="fa-solid fa-plug mr-1"></i>Test connection</button>
                                <span id="arr-test-radarr" class="text-[11px] text-slate-500 break-words min-w-0" role="status"></span>
                            </div>
                            <p class="text-[11px] text-slate-500">Find the API key in Radarr under Settings &gt; General &gt; Security. Use the address this app can reach Radarr at. After each verified transcode the app finds the movie that contains the file and asks Radarr to rescan it, so it notices the changed file.</p>
                        </div>
                    </div>
                    <p class="text-[11px] text-slate-500">The path mappings above are also used to match files to movies and series: they translate the path Sonarr/Radarr use into the path this app sees.</p>
                </div>
            </div>
            <div class="px-4 sm:px-6 py-4 border-t border-slate-800 bg-slate-900/50 flex flex-wrap items-center justify-between gap-2 flex-shrink-0">
                <button onclick="resetSettingsDefaults()" class="px-4 py-2 rounded text-sm bg-rose-600/20 text-rose-300 border border-rose-500/30">Restore Defaults</button>
                <div class="flex gap-2 sm:gap-3">
                    <button onclick="closeSettingsModal()" class="px-4 py-2 rounded text-sm bg-slate-800 text-slate-300">Cancel</button>
                    <button onclick="saveSettings()" class="px-4 py-2 rounded text-sm bg-indigo-600 text-white font-medium shadow">Save Settings</button>
                </div>
            </div>
        </div>
    </div>


    <!-- Help Modal -->
    <!-- Confirmation Dialog (Stop Queue / Clear Queue) -->
    <div id="confirm-modal" class="fixed inset-0 bg-black/70 backdrop-blur-sm hidden items-center justify-center z-[60] p-4" onclick="if (event.target === this) closeConfirm(false)">
        <div class="bg-darkcard border border-slate-800 w-full max-w-md rounded-xl shadow-2xl overflow-hidden" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-text">
            <div class="p-5 flex items-start gap-3">
                <div class="bg-rose-500/20 text-rose-300 w-10 h-10 rounded-full flex items-center justify-center flex-shrink-0"><i class="fa-solid fa-triangle-exclamation"></i></div>
                <div class="min-w-0">
                    <h3 id="confirm-title" class="text-md font-semibold text-slate-200"></h3>
                    <p id="confirm-text" class="text-xs text-slate-400 mt-1 break-words"></p>
                </div>
            </div>
            <div class="px-5 py-4 border-t border-slate-800 bg-slate-900/50 flex flex-wrap justify-end gap-2 sm:gap-3">
                <button id="confirm-cancel" type="button" onclick="closeConfirm(false)" class="px-4 py-2 rounded text-sm bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 transition">Cancel</button>
                <button id="confirm-ok" type="button" onclick="closeConfirm(true)" class="px-4 py-2 rounded text-sm bg-rose-600 hover:bg-rose-500 text-white font-medium shadow transition">Confirm</button>
            </div>
        </div>
    </div>

    <div id="help-modal" class="fixed inset-0 bg-black/70 backdrop-blur-sm hidden items-center justify-center z-50 p-4">
        <div class="bg-darkcard border border-slate-800 w-full max-w-2xl rounded-xl shadow-2xl flex flex-col overflow-hidden h-[90vh] sm:h-[38rem] max-h-[90vh]">
            <div class="px-6 py-4 border-b border-slate-800 flex items-center justify-between flex-shrink-0">
                <h3 class="text-md font-semibold flex items-center space-x-2"><i class="fa-solid fa-circle-question text-indigo-400"></i><span>Pipeline Documentation &amp; Help</span></h3>
                <button onclick="closeHelpModal()" aria-label="Close" class="text-slate-400 hover:text-slate-200"><i class="fa-solid fa-xmark text-lg"></i></button>
            </div>
            <div class="border-b border-slate-800 flex-shrink-0 overflow-x-auto">
                <div role="tablist" aria-label="Help sections" id="help-tabs" class="flex gap-1 px-3 sm:px-4 min-w-max">
                    <button type="button" role="tab" id="help-tab-scanning" aria-controls="help-panel-scanning" aria-selected="false" tabindex="-1" onclick="showHelpTab('scanning')" class="help-tab -mb-px px-3 py-3 text-sm font-medium border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center gap-2 whitespace-nowrap transition"><i class="fa-solid fa-folder-open"></i><span>Scanning</span></button>
                    <button type="button" role="tab" id="help-tab-queue" aria-controls="help-panel-queue" aria-selected="false" tabindex="-1" onclick="showHelpTab('queue')" class="help-tab -mb-px px-3 py-3 text-sm font-medium border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center gap-2 whitespace-nowrap transition"><i class="fa-solid fa-list-check"></i><span>Queue</span></button>
                    <button type="button" role="tab" id="help-tab-savings" aria-controls="help-panel-savings" aria-selected="false" tabindex="-1" onclick="showHelpTab('savings')" class="help-tab -mb-px px-3 py-3 text-sm font-medium border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center gap-2 whitespace-nowrap transition"><i class="fa-solid fa-scale-balanced"></i><span>Space saving</span></button>
                    <button type="button" role="tab" id="help-tab-jellyfin" aria-controls="help-panel-jellyfin" aria-selected="false" tabindex="-1" onclick="showHelpTab('jellyfin')" class="help-tab -mb-px px-3 py-3 text-sm font-medium border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center gap-2 whitespace-nowrap transition"><i class="fa-solid fa-circle-play"></i><span>Jellyfin</span></button>
                    <button type="button" role="tab" id="help-tab-webhook" aria-controls="help-panel-webhook" aria-selected="false" tabindex="-1" onclick="showHelpTab('webhook')" class="help-tab -mb-px px-3 py-3 text-sm font-medium border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center gap-2 whitespace-nowrap transition"><i class="fa-solid fa-plug"></i><span>Webhook</span></button>
                    <button type="button" role="tab" id="help-tab-rescan" aria-controls="help-panel-rescan" aria-selected="false" tabindex="-1" onclick="showHelpTab('rescan')" class="help-tab -mb-px px-3 py-3 text-sm font-medium border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center gap-2 whitespace-nowrap transition"><i class="fa-solid fa-rotate"></i><span>Sonarr &amp; Radarr</span></button>
                </div>
            </div>
            <div class="p-4 sm:p-6 overflow-y-auto flex-1">
                <div id="help-panel-scanning" role="tabpanel" aria-labelledby="help-tab-scanning" class="help-panel hidden space-y-4 text-sm text-slate-300">
                    <h4 class="font-semibold text-slate-100">Scanning directories</h4>
                    <p class="text-xs text-slate-400">Click <strong>Choose Directory &amp; Scan</strong> to explore your folders. The scanner searches the chosen folder and everything below it for video files, sorts them alphabetically and <strong>adds them to the end of the existing queue</strong>, so you can scan several folders one after another.</p>
                    <h5 class="text-xs font-semibold text-slate-200 pt-1">Scan options</h5>
                    <ul class="text-xs text-slate-400 space-y-1.5 list-disc pl-4">
                        <li><strong>Minimum file size</strong>: files smaller than this are ignored.</li>
                        <li><strong>Include .mp4 files</strong>: tick this to also check existing MP4 files. An MP4 is added only if it does <em>not</em> already match the settings currently in use (video codec, pixel format and audio channels), so changing your codec settings makes the scanner pick up older files again.</li>
                        <li>The folder summary lists the folders in queue order with their progress.</li>
                    </ul>
                </div>
                <div id="help-panel-queue" role="tabpanel" aria-labelledby="help-tab-queue" class="help-panel hidden space-y-4 text-sm text-slate-300">
                    <h4 class="font-semibold text-slate-100">Running the queue</h4>
                    <p class="text-xs text-slate-400"><strong>Start Queue</strong> processes the files one at a time with FFmpeg. Finished items show the original size and the new MP4 size in green, and the header shows the total space saved.</p>
                    <h5 class="text-xs font-semibold text-slate-200 pt-1">How a file is processed</h5>
                    <ul class="text-xs text-slate-400 space-y-1.5 list-disc pl-4">
                        <li>Non-MP4 files (MKV, AVI and so on) are converted to a new MP4 and the original is deleted <strong>only after</strong> the new file has been checked with ffprobe.</li>
                        <li>MP4 files that do not match your settings are re-encoded and replace the original, again only after the new file has been verified.</li>
                        <li>With Intel Quick Sync the app tries GPU decoding first and falls back automatically (Quick Sync with CPU decoding, then VAAPI, then software) if a method fails. The console says which one is used.</li>
                        <li>If a file fails every method, it is marked <strong>Failed</strong>, the original is left alone, and the console shows FFmpeg's error.</li>
                    </ul>
                    <h5 class="text-xs font-semibold text-slate-200 pt-1">Controlling the queue</h5>
                    <ul class="text-xs text-slate-400 space-y-1.5 list-disc pl-4">
                        <li><strong>Stop Queue</strong> stops the current transcode, deletes its unfinished file and keeps the original. The item goes back to Pending. You are asked to confirm first.</li>
                        <li><strong>Clear Queue</strong> empties the list (after confirmation).</li>
                        <li>The bin button on an item removes just that item. If it is the one being transcoded, you are asked to confirm, the transcode is stopped and its unfinished file deleted, and the queue carries on with the next item. The original is never touched.</li>
                        <li>The <strong>Clear</strong> button on the console wipes the log on the server as well as the screen.</li>
                    </ul>
                </div>
                <div id="help-panel-savings" role="tabpanel" aria-labelledby="help-tab-savings" class="help-panel hidden space-y-4 text-sm text-slate-300">
                    <h4 class="font-semibold text-slate-100">Minimum estimated space saving</h4>
                    <p class="text-xs text-slate-400">Under <strong>Settings &gt; Transcoding</strong>, <strong>Minimum estimated space saving (%)</strong> lets the app skip MP4 files that would not get meaningfully smaller. Set it to, for example, <code class="text-indigo-300">20</code> to skip any MP4 whose estimated saving is under 20%. <code class="text-indigo-300">0</code> switches the test off.</p>
                    <h5 class="text-xs font-semibold text-slate-200 pt-1">What happens</h5>
                    <ul class="text-xs text-slate-400 space-y-1.5 list-disc pl-4">
                        <li>When an MP4 file starts being processed, the app encodes five 20-second clips spread through it using your current settings, measures their size and scales that up to the whole file. This takes roughly a minute and the card shows &quot;Estimating the space saving&hellip;&quot;.</li>
                        <li>If the estimated saving is <strong>below your limit</strong>, the file is not transcoded. It is marked <strong>Skipped</strong>, the reason is shown on the card (for example &quot;Estimated saving 12% is below the 20% minimum&quot;, or &quot;Estimated to be 12% larger&quot; if it would grow) and the original is left untouched.</li>
                        <li>Otherwise the card shows the estimate (for example &quot;&asymp; 55% smaller&quot;) and processing carries on as normal.</li>
                    </ul>
                    <h5 class="text-xs font-semibold text-slate-200 pt-1">Good to know</h5>
                    <ul class="text-xs text-slate-400 space-y-1.5 list-disc pl-4">
                        <li>The test applies <strong>only to MP4 files</strong>. MKV, AVI and every other type are always transcoded.</li>
                        <li>It is an estimate: in testing it was typically within a few percent of the real result, so a file estimated close to your limit could go either way.</li>
                        <li>MP4 files shorter than 2.5 minutes are not tested and are simply transcoded. If the clips cannot be encoded, the file is transcoded too and the console says so.</li>
                        <li>A skipped file is only tried again if you lower the limit (or set it to 0) and press Start Queue.</li>
                        <li>Stop Queue and removing an item both work while the test is running.</li>
                    </ul>
                </div>
                <div id="help-panel-jellyfin" role="tabpanel" aria-labelledby="help-tab-jellyfin" class="help-panel hidden space-y-4 text-sm text-slate-300">
                    <h4 class="font-semibold text-slate-100">Jellyfin integration</h4>
                    <p class="text-xs text-slate-400">Open <strong>Settings &gt; Jellyfin</strong>, tick <em>Enable</em>, then enter your Jellyfin address and an API key (Jellyfin Dashboard &gt; API Keys). As soon as a file has been transcoded and verified, the app tells Jellyfin about that one file so it rescans just that file instead of the whole library. The results appear in the Live Console Output.</p>
                    <ul class="text-xs text-slate-400 space-y-1.5 list-disc pl-4">
                        <li>If Jellyfin sees your media at a different path than this app does, fill in the two <strong>path prefixes</strong> so the app can translate the path.</li>
                        <li>Jellyfin is told about the new MP4, and about the deleted original when an MKV or similar was replaced.</li>
                    </ul>
                </div>
                <div id="help-panel-webhook" role="tabpanel" aria-labelledby="help-tab-webhook" class="help-panel hidden space-y-4 text-sm text-slate-300">
                    <h4 class="font-semibold text-slate-100">Sonarr / Radarr webhook</h4>
                    <p class="text-xs text-slate-400">The webhook adds newly imported downloads to the queue automatically. It is configured on the <strong>Sonarr &amp; Radarr</strong> tab in Settings.</p>
                    <h5 class="text-xs font-semibold text-slate-200 pt-1">Set it up</h5>
                    <ul class="text-xs text-slate-400 space-y-1.5 list-disc pl-4">
                        <li>In Settings press <strong>Generate</strong> to create a webhook token, then copy the Webhook URL shown there.</li>
                        <li>In Sonarr or Radarr go to <strong>Settings &gt; Connect &gt; Add &gt; Webhook</strong>, paste the URL, set the method to <strong>POST</strong>, and enable <strong>On File Import</strong> and <strong>On File Upgrade</strong> (or <strong>On Import Complete</strong>).</li>
                        <li>Replace the host in the copied URL with the address Sonarr/Radarr can reach this app at (when running in Home Assistant, <code class="text-indigo-300">http://&lt;home-assistant-ip&gt;:5000/api/webhook/arr?token=&hellip;</code>).</li>
                    </ul>
                    <h5 class="text-xs font-semibold text-slate-200 pt-1">What it does</h5>
                    <ul class="text-xs text-slate-400 space-y-1.5 list-disc pl-4">
                        <li>Each imported file is added to the end of the queue, and the queue starts by itself if it is not already running. If you stopped the queue manually, it will not auto-start until you press Start Queue again.</li>
                        <li>Files that are already in the queue, not supported video files, or already compliant MP4s are skipped, and the console says why.</li>
                        <li><strong>Path mappings</strong> translate the paths Sonarr/Radarr report into the paths this app sees (one per line, for example <code class="text-indigo-300">/tv =&gt; /media/nas/tv</code>).</li>
                    </ul>
                </div>
                <div id="help-panel-rescan" role="tabpanel" aria-labelledby="help-tab-rescan" class="help-panel hidden space-y-4 text-sm text-slate-300">
                    <h4 class="font-semibold text-slate-100">Sonarr / Radarr rescans</h4>
                    <p class="text-xs text-slate-400">Separate from the webhook, the app can <strong>tell Sonarr and/or Radarr that a file has changed</strong> once it has been transcoded and verified, so they pick up the new file. This is set up on the <strong>Sonarr &amp; Radarr</strong> tab in Settings, in the Sonarr and Radarr sections.</p>
                    <h5 class="text-xs font-semibold text-slate-200 pt-1">Set it up (once for each of Sonarr and Radarr)</h5>
                    <ul class="text-xs text-slate-400 space-y-1.5 list-disc pl-4">
                        <li>Tick <em>Tell Sonarr/Radarr to rescan&hellip;</em>.</li>
                        <li>Enter the address this app can reach it at, for example <code class="text-indigo-300">http://192.168.1.10:8989</code> for Sonarr or <code class="text-indigo-300">http://192.168.1.10:7878</code> for Radarr (include any URL base such as <code class="text-indigo-300">/radarr</code>).</li>
                        <li>Enter the API key from Sonarr/Radarr under <strong>Settings &gt; General &gt; Security</strong>.</li>
                        <li>Press <strong>Test connection</strong> to check the address and key, then <strong>Save Settings</strong>.</li>
                    </ul>
                    <h5 class="text-xs font-semibold text-slate-200 pt-1">What it does</h5>
                    <ul class="text-xs text-slate-400 space-y-1.5 list-disc pl-4">
                        <li>After each verified transcode the app finds the movie or series whose folder contains the file and asks Radarr/Sonarr for a plain rescan of it. Radarr is used for movies and Sonarr for series.</li>
                        <li>Files are matched using the same <strong>path mappings</strong> as the webhook, so add them if Sonarr/Radarr see your media at different paths.</li>
                        <li>The console reports each rescan. A warning means no movie or series matched the file (check the path mappings), or that Sonarr/Radarr could not be reached. Either way the transcode itself is never affected.</li>
                        <li>A series gets one rescan request per transcoded file.</li>
                    </ul>
                </div>
            </div>
            <div class="px-6 py-4 border-t border-slate-800 bg-slate-900/50 flex justify-end flex-shrink-0">
                <button onclick="closeHelpModal()" class="px-4 py-2 rounded text-sm bg-indigo-600 text-white font-medium shadow">Got It</button>
            </div>
        </div>
    </div>

    <!-- Scanning Overlay with Console Window -->
    <div id="scanning-modal" class="fixed inset-0 bg-black/70 backdrop-blur-sm hidden items-center justify-center z-50 p-4">
        <div class="bg-darkcard border border-slate-800 w-full max-w-2xl rounded-xl shadow-2xl p-6 flex flex-col space-y-4">
            <div class="flex items-center space-x-3">
                <div class="bg-indigo-600/20 text-indigo-400 p-3 rounded-full animate-pulse"><i class="fa-solid fa-magnifying-glass text-xl"></i></div>
                <div>
                    <h3 class="text-md font-semibold text-slate-200">Scanning Directory</h3>
                    <p class="text-xs text-slate-400 mt-0.5" id="scanning-path-label">Indexing media files...</p>
                </div>
            </div>
            <!-- Scanning Console Window -->
            <div class="bg-slate-950 font-mono text-xs p-3 rounded-lg h-64 overflow-y-auto border border-slate-900 shadow-inner whitespace-pre-wrap break-words" id="scanLogContainer"><span class="text-slate-500">Initializing scan...</span></div>
            <div class="flex justify-end pt-2">
                <button id="btn-close-scan" onclick="closeScanningModal()" class="px-4 py-2 rounded text-sm bg-slate-800 hover:bg-slate-700 text-slate-300 transition hidden">Close / Finish</button>
            </div>
        </div>
    </div>


    <script>
        let activeBrowsePath = '/media';
        let lastRenderedLogCount = 0;
        let queueStartTime = null;
        let scanPollInterval = null;


        function formatBytesJs(n) {
            const units = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; n = Number(n) || 0;
            while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
            return n.toFixed(i >= 3 ? 2 : (i === 0 ? 0 : 1)) + ' ' + units[i];
        }
        function escapeHtml(str) {
            return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }


        function renderLogs(logs) {
            const term = document.getElementById('logContainer');
            const autoScrollToggle = document.getElementById('autoscroll-toggle');
            const autoScroll = autoScrollToggle ? autoScrollToggle.checked : true;
            
            if (!logs || logs.length === 0) { 
                term.innerHTML = '<span class="text-slate-500">[System initialized...]</span>'; 
                lastRenderedLogCount = 0; 
                return; 
            }
            
            const newContent = logs.join('\n');
            if (logs.length !== lastRenderedLogCount || term.innerHTML !== newContent) {
                term.innerHTML = newContent;
                lastRenderedLogCount = logs.length;
            }


            if (autoScroll) {
                term.scrollTop = term.scrollHeight;
            }
        }


        let logClearEpoch = 0;  // bumped on Clear so a poll that was already in flight can't redraw old lines
        async function pollServerLogs() {
            const epoch = logClearEpoch;
            try { 
                const res = await fetch('api/logs'); 
                const data = await res.json(); 
                if (data.logs && epoch === logClearEpoch) { renderLogs(data.logs); } 
            } catch (e) {}
        }
        async function clearConsoleLogs() {
            logClearEpoch++;
            renderLogs([]);
            try { await fetch('api/logs/clear', { method: 'POST' }); } catch (e) {}
        }


        async function fetchDashboardData() {
            try {
                const res = await fetch('api/jobs');
                const data = await res.json();
                queueStartTime = data.queue_start_time;
                lastQueueInfo = { running: !!data.queue_running, total: (data.jobs || []).length };
                renderJobs(data.jobs);
                updateFolderSummary(data.jobs);
                updateQueueStatus(data.queue_running, data.current_index, data.total_jobs);
                
                const sizeDeltaEl = document.getElementById('container-size-delta');
                if (sizeDeltaEl) {
                    sizeDeltaEl.innerText = data.size_delta_str || '0.0% (0GB)';
                    sizeDeltaEl.className = `font-mono text-xs font-medium ${data.size_delta_color || 'text-slate-300'}`;
                }

                await pollServerLogs();
            } catch (e) {}
        }


        async function pollScanLogs() {
            try {
                const res = await fetch('api/scan/logs');
                const data = await res.json();
                if (data.logs) {
                    const scanTerm = document.getElementById('scanLogContainer');
                    scanTerm.innerHTML = data.logs.join('\n');
                    scanTerm.scrollTop = scanTerm.scrollHeight;
                }
                if (data.finished) {
                    clearInterval(scanPollInterval);
                    document.getElementById('btn-close-scan').classList.remove('hidden');
                }
            } catch (e) {}
        }


        function closeScanningModal() {
            document.getElementById('scanning-modal').classList.remove('flex');
            document.getElementById('scanning-modal').classList.add('hidden');
            fetchDashboardData();
        }


        function updateQueueStatus(isRunning, currentIndex, totalJobs) {
            const workerStatus = document.getElementById('worker-status');
            const btnStart = document.getElementById('btn-start-queue');
            const btnStop = document.getElementById('btn-stop-queue');
            const timeRow = document.getElementById('processing-time-row');
            const timeVal = document.getElementById('processing-time-val');


            if (isRunning) {
                workerStatus.innerText = `Processing ${currentIndex} of ${totalJobs}`;
                workerStatus.className = "text-emerald-400 font-medium animate-pulse";
                btnStart.classList.add('hidden'); btnStop.classList.remove('hidden'); timeRow.style.display = 'flex';
                if (queueStartTime) {
                    const elapsed = Math.floor(Date.now() / 1000 - queueStartTime);
                    const d = Math.floor(elapsed / 86400), h = Math.floor((elapsed % 86400) / 3600), m = Math.floor((elapsed % 3600) / 60), s = elapsed % 60;
                    timeVal.innerText = `${d}d ${String(h).padStart(2, '0')}h ${String(m).padStart(2, '0')}m ${String(s).padStart(2, '0')}s`;
                }
            } else {
                workerStatus.innerText = "Idle"; workerStatus.className = "text-indigo-400 font-medium";
                btnStart.classList.remove('hidden'); btnStop.classList.add('hidden'); timeRow.style.display = 'none';
            }
        }


        function updateFolderSummary(jobs) {
            const container = document.getElementById('folder-summary-container');
            const autoScrollToggle = document.getElementById('autoscroll-toggle');


            if (!jobs || jobs.length === 0) { 
                container.innerHTML = `<div class="text-slate-500 italic text-center py-2">No folders scanned yet.</div>`; 
                return; 
            }
            const stats = {};
            jobs.forEach(j => {
                const folder = j.path.substring(0, j.path.lastIndexOf('/')) || '(Root)';
                if (!stats[folder]) stats[folder] = { total: 0, completed: 0, running: false };
                stats[folder].total++;
                if (j.status === 'Completed') stats[folder].completed++;
                if (j.status === 'Running') stats[folder].running = true;
            });


            container.innerHTML = Object.keys(stats).map(f => {
                const isRunning = stats[f].running;
                return `
                    <div class="flex items-center justify-between border px-3 py-2 rounded ${isRunning ? 'active-running-folder border-indigo-500 bg-indigo-950/40' : 'border-slate-800 bg-slate-900/60'}">
                        <span class="font-mono text-indigo-300 truncate flex-1 min-w-0 text-left mr-2" style="direction: rtl;" title="${f}"><bdi dir="ltr">${f}</bdi></span>
                        <span class="bg-indigo-600/20 text-indigo-300 border border-indigo-500/30 px-2 py-0.5 rounded-full font-semibold text-[11px] flex-shrink-0">${stats[f].completed} of ${stats[f].total}</span>
                    </div>
                `;
            }).join('');


            if (autoScrollToggle && autoScrollToggle.checked) {
                const activeFolderCard = container.querySelector('.active-running-folder');
                if (activeFolderCard) {
                    activeFolderCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                }
            }
        }


        async function startQueue() { await fetch('api/queue/start', { method: 'POST' }); fetchDashboardData(); }
        // Themed confirmation dialog. Resolves true on Confirm, false on Cancel / Escape / click outside.
        let confirmResolver = null;
        function askConfirm(title, text, okLabel) {
            if (confirmResolver) closeConfirm(false);
            document.getElementById('confirm-title').textContent = title;
            document.getElementById('confirm-text').textContent = text;
            document.getElementById('confirm-ok').textContent = okLabel || 'Confirm';
            const modal = document.getElementById('confirm-modal');
            modal.classList.remove('hidden'); modal.classList.add('flex');
            document.getElementById('confirm-cancel').focus();  // the safe choice has focus, so a stray Enter cancels
            return new Promise(resolve => { confirmResolver = resolve; });
        }
        function closeConfirm(result) {
            const modal = document.getElementById('confirm-modal');
            modal.classList.remove('flex'); modal.classList.add('hidden');
            const resolve = confirmResolver; confirmResolver = null;
            if (resolve) resolve(result);
        }
        document.addEventListener('keydown', e => { if (e.key === 'Escape' && confirmResolver) closeConfirm(false); });

        // Remove one item. Always asks first. If it is being transcoded the transcode is stopped and its
        // unfinished file deleted (the queue then carries on with the next item).
        async function removeJob(id, name, running) {
            const ok = await askConfirm('Remove this item?',
                running
                    ? `"${name}" is being transcoded right now. The transcode will be stopped and its unfinished file deleted, then the queue carries on with the next item. Your original file is not touched.`
                    : `"${name}" will be removed from the queue. Your original file is not touched.`,
                'Remove');
            if (!ok) return;
            try {
                // The server re-checks: the item may have started transcoding while the dialog was open,
                // in which case it needs "confirm" (already given above) to stop it.
                await fetch('api/queue/remove/' + id, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm: true }) });
            } catch (e) {}
            fetchDashboardData();
        }

        let lastQueueInfo = { running: false, total: 0 };
        async function stopQueue() {
            const ok = await askConfirm('Stop the queue?',
                'The file being transcoded right now will be stopped and its unfinished output deleted. Finished files are not affected, and the stopped file stays in the queue so you can start it again.',
                'Stop Queue');
            if (!ok) return;
            // The queue may have finished while the dialog was open: don't flag a stop in that case
            if (document.getElementById('btn-stop-queue').classList.contains('hidden')) { fetchDashboardData(); return; }
            await fetch('api/queue/stop', { method: 'POST' }); fetchDashboardData();
        }
        async function clearQueue() {
            if (lastQueueInfo.total > 0) {
                const n = lastQueueInfo.total;
                const ok = await askConfirm('Clear the queue?',
                    `All ${n} item${n === 1 ? '' : 's'} will be removed from the queue.` +
                    (lastQueueInfo.running ? ' The file being transcoded right now will be stopped and its unfinished output deleted.' : '') +
                    ' Your media files are not deleted by clearing the queue.',
                    'Clear Queue');
                if (!ok) return;
            }
            await fetch('api/queue/clear', { method: 'POST' }); fetchDashboardData();
        }


        // ---- Help tabs ----
        const HELP_TABS = ['scanning', 'queue', 'savings', 'jellyfin', 'webhook', 'rescan'];
        let currentHelpTab = 'scanning';
        function showHelpTab(name, focus) {
            if (!HELP_TABS.includes(name)) name = 'scanning';
            currentHelpTab = name;
            HELP_TABS.forEach(t => {
                const on = t === name;
                const tab = document.getElementById('help-tab-' + t), panel = document.getElementById('help-panel-' + t);
                tab.setAttribute('aria-selected', on ? 'true' : 'false');
                tab.tabIndex = on ? 0 : -1;
                tab.classList.toggle('border-indigo-500', on);
                tab.classList.toggle('text-indigo-300', on);
                tab.classList.toggle('border-transparent', !on);
                tab.classList.toggle('text-slate-400', !on);
                panel.classList.toggle('hidden', !on);
                if (on && focus) tab.focus();
                if (on) { try { tab.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (e) {} }
            });
            document.getElementById('help-panel-' + name).parentElement.scrollTop = 0;
        }
        document.getElementById('help-tabs').addEventListener('keydown', e => {
            const i = HELP_TABS.indexOf(currentHelpTab);
            let n = null;
            if (e.key === 'ArrowRight') n = (i + 1) % HELP_TABS.length;
            else if (e.key === 'ArrowLeft') n = (i + HELP_TABS.length - 1) % HELP_TABS.length;
            else if (e.key === 'Home') n = 0;
            else if (e.key === 'End') n = HELP_TABS.length - 1;
            if (n !== null) { e.preventDefault(); showHelpTab(HELP_TABS[n], true); }
        });
        function openHelpModal() { showHelpTab(currentHelpTab); document.getElementById('help-modal').classList.remove('hidden'); document.getElementById('help-modal').classList.add('flex'); }
        function closeHelpModal() { document.getElementById('help-modal').classList.remove('flex'); document.getElementById('help-modal').classList.add('hidden'); }


        async function openBrowseModal() {
            try {
                const cfgRes = await fetch('api/config'); const cfg = await cfgRes.json();
                document.getElementById('scan-min-size-val').value = cfg.min_size_val;
                document.getElementById('scan-min-size-unit').value = cfg.min_size_unit;
                document.getElementById('scan-include-mp4').checked = cfg.include_mp4 !== false;
            } catch (e) {}
            document.getElementById('browse-modal').classList.remove('hidden'); document.getElementById('browse-modal').classList.add('flex');
            await loadDirectory(activeBrowsePath);
        }
        function closeBrowseModal() { document.getElementById('browse-modal').classList.remove('flex'); document.getElementById('browse-modal').classList.add('hidden'); }


        // ---- Codec presets: 10 "car radio" slots ----
        const PRESET_FIELD_IDS = { hwaccel: 'cfg-hwaccel', vcodec: 'cfg-vcodec', preset: 'cfg-preset', crf: 'cfg-crf', acodec: 'cfg-acodec', audio_bitrate: 'cfg-audio-bitrate', extra_flags: 'cfg-extra' };
        let presetData = new Array(10).fill(null);
        let selectedPreset = null;

        function presetFormValues() {
            const out = {};
            for (const [key, id] of Object.entries(PRESET_FIELD_IDS)) out[key] = String(document.getElementById(id).value ?? '').trim();
            return out;
        }
        function presetMatches(p, values) {
            return !!p && Object.keys(PRESET_FIELD_IDS).every(k => String(p[k] ?? '').trim() === String(values[k] ?? '').trim());
        }
        function presetSummary(p) {
            return `${p.hwaccel === 'qsv' ? 'QSV, ' : ''}${p.vcodec}, preset ${p.preset}, quality ${p.crf}, audio ${p.acodec} ${p.audio_bitrate}`;
        }
        function setPresetMsg(text, ok) {
            const el = document.getElementById('preset-msg');
            el.textContent = text || '';
            el.className = 'text-[11px] ' + (ok === false ? 'text-rose-400' : 'text-emerald-400');
        }
        function renderPresets() {
            const form = presetFormValues();
            document.getElementById('preset-buttons').innerHTML = presetData.map((p, i) => {
                const n = i + 1;
                const look = selectedPreset === n ? 'border-indigo-500 bg-indigo-600/20 text-indigo-300'
                    : (p ? 'border-slate-600 bg-slate-800 text-slate-200 hover:bg-slate-700' : 'border-dashed border-slate-700 text-slate-500 hover:text-slate-300');
                const tip = p ? `${p.name || 'Preset ' + n}: ${presetSummary(p)}` : 'Empty slot';
                const dot = presetMatches(p, form) ? '<span class="absolute top-1 right-1 w-2 h-2 rounded-full bg-emerald-400"></span>' : '';
                return `<button type="button" onclick="selectPreset(${n})" title="${escapeHtml(tip)}" class="relative h-10 rounded-lg border text-sm font-semibold transition ${look}">${n}${dot}</button>`;
            }).join('');
            const p = selectedPreset ? presetData[selectedPreset - 1] : null;
            const summary = document.getElementById('preset-summary');
            if (!selectedPreset) summary.textContent = 'Select a slot to load or store a preset.';
            else if (!p) summary.textContent = `Slot ${selectedPreset} is empty.`;
            else summary.textContent = `Slot ${selectedPreset}${p.name ? ' \u2013 ' + p.name : ''}: ${presetSummary(p)}${presetMatches(p, form) ? ' (matches the current settings)' : ''}`;
            const store = document.getElementById('btn-preset-store'), clear = document.getElementById('btn-preset-clear');
            store.disabled = !selectedPreset; clear.disabled = !selectedPreset || !p;
            store.textContent = selectedPreset ? `Store in slot ${selectedPreset}` : 'Store in slot';
            clear.textContent = selectedPreset ? `Clear slot ${selectedPreset}` : 'Clear slot';
        }
        function selectPreset(n) {
            selectedPreset = n;
            const p = presetData[n - 1];
            if (p) {
                for (const [key, id] of Object.entries(PRESET_FIELD_IDS)) document.getElementById(id).value = p[key];
                document.getElementById('preset-name').value = p.name || '';
                setPresetMsg(`Loaded slot ${n}. Press Save Settings to use it.`, true);
            } else {
                document.getElementById('preset-name').value = '';
                setPresetMsg('', true);
            }
            renderPresets();
        }
        async function storePreset() {
            if (!selectedPreset) return;
            const n = selectedPreset;
            try {
                const res = await fetch('api/presets/' + n, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: document.getElementById('preset-name').value.trim(), settings: presetFormValues() }) });
                const data = await res.json();
                if (data.status === 'success') { presetData = data.presets; setPresetMsg(`Stored in slot ${n}.`, true); }
                else setPresetMsg(data.message || 'Could not store the preset.', false);
            } catch (e) { setPresetMsg('Could not store the preset.', false); }
            renderPresets();
            refreshEngineLabel();
        }
        async function clearPreset() {
            if (!selectedPreset) return;
            const n = selectedPreset;
            try {
                const res = await fetch('api/presets/' + n, { method: 'DELETE' });
                const data = await res.json();
                if (data.status === 'success') { presetData = data.presets; document.getElementById('preset-name').value = ''; setPresetMsg(`Cleared slot ${n}.`, true); }
                else setPresetMsg(data.message || 'Could not clear the slot.', false);
            } catch (e) { setPresetMsg('Could not clear the slot.', false); }
            renderPresets();
            refreshEngineLabel();
        }
        function encoderName(cfg) {
            const vc = String(cfg.vcodec || '').trim().toLowerCase();
            if (cfg.hwaccel === 'qsv') {
                if (vc.endsWith('_qsv')) return vc;
                return (vc.includes('265') || vc.includes('hevc')) ? 'hevc_qsv' : 'h264_qsv';
            }
            return cfg.vcodec || '';
        }
        async function refreshEngineLabel() {
            try {
                const cfg = await (await fetch('api/config')).json();
                const idx = (cfg.presets || []).findIndex(p => presetMatches(p, cfg));
                const named = idx >= 0 && cfg.presets[idx].name ? ` \u00b7 ${cfg.presets[idx].name}` : '';
                document.getElementById('engine-label').textContent = `Ready (${encoderName(cfg)}${idx >= 0 ? ' \u00b7 Preset ' + (idx + 1) : ''}${named})`;
            } catch (e) {}
        }
        Object.values(PRESET_FIELD_IDS).forEach(id => {
            const el = document.getElementById(id);
            ['input', 'change'].forEach(ev => el.addEventListener(ev, renderPresets));
        });

        // ---- Settings tabs ----
        const SETTINGS_TABS = ['general', 'transcoding', 'jellyfin', 'arr'];
        let currentSettingsTab = 'general';
        function showSettingsTab(name, focus) {
            if (!SETTINGS_TABS.includes(name)) name = 'general';
            currentSettingsTab = name;
            SETTINGS_TABS.forEach(t => {
                const on = t === name;
                const tab = document.getElementById('tab-' + t), panel = document.getElementById('settings-panel-' + t);
                tab.setAttribute('aria-selected', on ? 'true' : 'false');
                tab.tabIndex = on ? 0 : -1;
                tab.classList.toggle('border-indigo-500', on);
                tab.classList.toggle('text-indigo-300', on);
                tab.classList.toggle('border-transparent', !on);
                tab.classList.toggle('text-slate-400', !on);
                panel.classList.toggle('hidden', !on);
                if (on && focus) tab.focus();
                if (on) { try { tab.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (e) {} }
            });
            const body = document.getElementById('settings-panel-' + name).parentElement;
            body.scrollTop = 0;
        }
        document.getElementById('settings-tabs').addEventListener('keydown', e => {
            const i = SETTINGS_TABS.indexOf(currentSettingsTab);
            let n = null;
            if (e.key === 'ArrowRight') n = (i + 1) % SETTINGS_TABS.length;
            else if (e.key === 'ArrowLeft') n = (i + SETTINGS_TABS.length - 1) % SETTINGS_TABS.length;
            else if (e.key === 'Home') n = 0;
            else if (e.key === 'End') n = SETTINGS_TABS.length - 1;
            if (n !== null) { e.preventDefault(); showSettingsTab(SETTINGS_TABS[n], true); }
        });
        // A green dot on a tab shows that its integration is switched on
        function updateTabIndicators() {
            const on = {
                jellyfin: document.getElementById('cfg-jf-enabled').checked,
                arr: document.getElementById('cfg-radarr-enabled').checked || document.getElementById('cfg-sonarr-enabled').checked || !!document.getElementById('cfg-wh-token').value.trim()
            };
            Object.keys(on).forEach(k => document.getElementById('tab-dot-' + k).classList.toggle('hidden', !on[k]));
        }
        function setArrTestMsg(svc, text, neutral, ok) {
            const el = document.getElementById('arr-test-' + svc);
            el.textContent = text;
            el.className = 'text-[11px] break-words min-w-0 ' + (neutral ? 'text-slate-500' : (ok ? 'text-emerald-400' : 'text-rose-400'));
        }
        async function testArr(svc) {
            const btn = document.getElementById('btn-test-' + svc);
            btn.disabled = true; setArrTestMsg(svc, 'Testing\u2026', true);
            try {
                const res = await fetch('api/arr/test', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ service: svc, url: document.getElementById(`cfg-${svc}-url`).value.trim(), api_key: document.getElementById(`cfg-${svc}-key`).value.trim() }) });
                const data = await res.json();
                setArrTestMsg(svc, data.message || 'No reply.', false, !!data.ok);
            } catch (e) { setArrTestMsg(svc, 'Could not reach the app.', false, false); }
            btn.disabled = false;
        }

        async function openSettingsModal() {
            const res = await fetch('api/config'); const cfg = await res.json();
            document.getElementById('cfg-theme').value = cfg.theme || 'dark';
            document.getElementById('cfg-hwaccel').value = cfg.hwaccel || 'none';
            loadHwStatus();
            document.getElementById('cfg-vcodec').value = cfg.vcodec;
            document.getElementById('cfg-preset').value = cfg.preset;
            document.getElementById('cfg-crf').value = cfg.crf;
            document.getElementById('cfg-acodec').value = cfg.acodec;
            document.getElementById('cfg-audio-bitrate').value = cfg.audio_bitrate;
            document.getElementById('cfg-extra').value = cfg.extra_flags;
            document.getElementById('cfg-min-savings').value = cfg.min_savings_percent || 0;
            presetData = (cfg.presets && cfg.presets.length === 10) ? cfg.presets : new Array(10).fill(null);
            const activeIdx = presetData.findIndex(p => presetMatches(p, presetFormValues()));
            selectedPreset = activeIdx >= 0 ? activeIdx + 1 : null;
            document.getElementById('preset-name').value = activeIdx >= 0 ? (presetData[activeIdx].name || '') : '';
            setPresetMsg('', true);
            renderPresets();
            document.getElementById('cfg-jf-enabled').checked = !!cfg.jellyfin_enabled;
            document.getElementById('cfg-jf-url').value = cfg.jellyfin_url || '';
            document.getElementById('cfg-jf-key').value = cfg.jellyfin_api_key || '';
            document.getElementById('cfg-jf-path-from').value = cfg.jellyfin_path_from || '';
            document.getElementById('cfg-jf-path-to').value = cfg.jellyfin_path_to || '';
            ['radarr', 'sonarr'].forEach(svc => {
                document.getElementById(`cfg-${svc}-enabled`).checked = !!cfg[`${svc}_enabled`];
                document.getElementById(`cfg-${svc}-url`).value = cfg[`${svc}_url`] || '';
                document.getElementById(`cfg-${svc}-key`).value = cfg[`${svc}_api_key`] || '';
                setArrTestMsg(svc, '', true);
            });
            document.getElementById('cfg-wh-token').value = cfg.webhook_token || '';
            document.getElementById('cfg-wh-map').value = cfg.webhook_path_map || '';
            updateWebhookUrl();
            updateTabIndicators();
            showSettingsTab(currentSettingsTab);
            document.getElementById('settings-modal').classList.remove('hidden'); document.getElementById('settings-modal').classList.add('flex');
        }
        async function loadHwStatus() {
            const el = document.getElementById('cfg-hwaccel-status');
            try {
                const info = await (await fetch('api/hwaccel')).json();
                if (info.devices.length && info.ffmpeg_qsv) {
                    el.textContent = 'Detected: ' + info.devices.join(', ') + ' (FFmpeg has QSV support)';
                    el.className = 'text-[11px] text-emerald-400 mt-1';
                } else if (!info.devices.length) {
                    el.textContent = 'No GPU found at /dev/dri. QSV will fall back to the CPU until the GPU is passed through to this app.';
                    el.className = 'text-[11px] text-amber-400 mt-1';
                } else {
                    el.textContent = 'This FFmpeg build has no QSV encoder.';
                    el.className = 'text-[11px] text-amber-400 mt-1';
                }
            } catch (e) { el.textContent = ''; }
        }
        function updateWebhookUrl() {
            const token = document.getElementById('cfg-wh-token').value.trim();
            // Inside Home Assistant (ingress) the page address is the public Home Assistant URL,
            // which Sonarr/Radarr cannot use. They need the app's own port on the local network.
            const viaIngress = location.pathname.indexOf('/api/hassio_ingress/') === 0;
            const base = viaIngress ? 'http://<home-assistant-ip>:5000' : location.origin;
            document.getElementById('cfg-wh-url').value = token
                ? `${base}/api/webhook/arr?token=${encodeURIComponent(token)}`
                : 'Set a token to enable the webhook';
        }
        function generateWebhookToken() {
            const bytes = new Uint8Array(18);
            crypto.getRandomValues(bytes);
            document.getElementById('cfg-wh-token').value = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
            updateWebhookUrl(); updateTabIndicators();
        }
        function closeSettingsModal() { applyTheme(window.__savedTheme || document.documentElement.getAttribute('data-theme-pref') || 'dark'); document.getElementById('settings-modal').classList.remove('flex'); document.getElementById('settings-modal').classList.add('hidden'); }


        async function saveSettings() {
            const payload = {
                theme: document.getElementById('cfg-theme').value,
                hwaccel: document.getElementById('cfg-hwaccel').value,
                vcodec: document.getElementById('cfg-vcodec').value,
                preset: document.getElementById('cfg-preset').value,
                crf: document.getElementById('cfg-crf').value,
                acodec: document.getElementById('cfg-acodec').value,
                audio_bitrate: document.getElementById('cfg-audio-bitrate').value,
                extra_flags: document.getElementById('cfg-extra').value,
                min_savings_percent: parseFloat(document.getElementById('cfg-min-savings').value) || 0,
                jellyfin_enabled: document.getElementById('cfg-jf-enabled').checked,
                jellyfin_url: document.getElementById('cfg-jf-url').value.trim(),
                jellyfin_api_key: document.getElementById('cfg-jf-key').value.trim(),
                jellyfin_path_from: document.getElementById('cfg-jf-path-from').value.trim(),
                jellyfin_path_to: document.getElementById('cfg-jf-path-to').value.trim(),
                radarr_enabled: document.getElementById('cfg-radarr-enabled').checked,
                radarr_url: document.getElementById('cfg-radarr-url').value.trim(),
                radarr_api_key: document.getElementById('cfg-radarr-key').value.trim(),
                sonarr_enabled: document.getElementById('cfg-sonarr-enabled').checked,
                sonarr_url: document.getElementById('cfg-sonarr-url').value.trim(),
                sonarr_api_key: document.getElementById('cfg-sonarr-key').value.trim(),
                webhook_token: document.getElementById('cfg-wh-token').value.trim(),
                webhook_path_map: document.getElementById('cfg-wh-map').value.trim()
            };
            await fetch('api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
            window.__savedTheme = payload.theme;
            refreshEngineLabel();
            closeSettingsModal();
        }


        async function resetSettingsDefaults() {
            const res = await fetch('api/config/reset', { method: 'POST' }); const data = await res.json();
            if (data.status === 'success') { openSettingsModal(); }
        }


        async function loadDirectory(path) {
            const includeMp4 = document.getElementById('scan-include-mp4').checked;
            const res = await fetch('api/browse', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path, include_mp4: includeMp4 }) });
            const data = await res.json();
            activeBrowsePath = data.current_path;
            document.getElementById('modal-current-path').value = activeBrowsePath;
            document.getElementById('modal-mkv-count').innerText = `${data.mkv_count} media file(s) found`;
            const parentBtn = document.getElementById('btn-up-dir');
            if (data.parent_path) { parentBtn.disabled = false; parentBtn.dataset.parent = data.parent_path; } else { parentBtn.disabled = true; }
            document.getElementById('modal-subdirs-list').innerHTML = data.subdirs.map(sub => `
                <div data-path="${escapeHtml(sub.path)}" onclick="loadDirectory(this.dataset.path)" class="flex items-center justify-between px-3 py-2 rounded hover:bg-slate-900 cursor-pointer text-sm text-slate-300">
                    <div class="flex items-center space-x-2"><i class="fa-solid fa-folder text-amber-500"></i><span>${escapeHtml(sub.name)}</span></div>
                    <i class="fa-solid fa-chevron-right text-xs text-slate-600"></i>
                </div>
            `).join('') || '<div class="text-center py-10 text-slate-600 text-xs">No subdirectories</div>';
        }


        function navigateParent() {
            const parentBtn = document.getElementById('btn-up-dir');
            if (!parentBtn.disabled) loadDirectory(parentBtn.dataset.parent);
        }


        async function scanCurrentModalPath() {
            const targetPath = document.getElementById('modal-current-path').value;
            closeBrowseModal();
            
            const scanModal = document.getElementById('scanning-modal');
            document.getElementById('scanning-path-label').innerText = `Scanning: ${targetPath}`;
            document.getElementById('scanLogContainer').innerHTML = '<span class="text-slate-500">Initializing recursive directory traversal...</span>';
            document.getElementById('btn-close-scan').classList.add('hidden');
            scanModal.classList.remove('hidden'); scanModal.classList.add('flex');
            
            fetch('api/scan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
                path: targetPath,
                min_size_val: parseFloat(document.getElementById('scan-min-size-val').value) || 0,
                min_size_unit: document.getElementById('scan-min-size-unit').value,
                include_mp4: document.getElementById('scan-include-mp4').checked
            }) });
            
            if (scanPollInterval) clearInterval(scanPollInterval);
            scanPollInterval = setInterval(pollScanLogs, 500);
        }


        function renderJobs(jobs) {
            const container = document.getElementById('job-list-container');
            const autoScrollToggle = document.getElementById('autoscroll-toggle');
            document.getElementById('queueCounter').innerText = `${jobs.length} files loaded`;
            
            if (jobs.length === 0) {
                container.innerHTML = `<div class="text-center py-20 text-slate-500"><i class="fa-solid fa-inbox text-4xl mb-3 block"></i><p>No media files loaded yet.</p></div>`;
                return;
            }


            container.innerHTML = jobs.map(job => {
                let badgeColor = 'bg-slate-700 text-slate-300';
                let isRunning = job.status === 'Running';
                
                if (isRunning) {
                    badgeColor = 'bg-amber-500/20 text-amber-300 border border-amber-500/30 animate-pulse';
                } else if (job.status === 'Completed') {
                    badgeColor = 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30';
                } else if (job.status === 'Failed') {
                    badgeColor = 'bg-rose-500/20 text-rose-300 border border-rose-500/30';
                } else if (job.status === 'Skipped') {
                    badgeColor = 'bg-sky-500/20 text-sky-300 border border-sky-500/30';
                }
                
                let sizeDisplayHTML = `<span class="text-[11px] font-mono bg-slate-800 text-indigo-300 px-1.5 py-0.5 rounded border border-slate-700">${job.size_human}</span>`;
                if (job.status === 'Completed' && job.output_size_human) {
                    sizeDisplayHTML += ` <span class="text-[11px] font-mono bg-emerald-950/60 text-emerald-300 px-1.5 py-0.5 rounded border border-emerald-800/60 ml-1" title="New Output File Size">➔ ${job.output_size_human}</span>`;
                }
                if (job.estimate && job.status !== 'Completed') {
                    sizeDisplayHTML += ` <span class="text-[11px] font-mono bg-slate-800 text-sky-300 px-1.5 py-0.5 rounded border border-slate-700 ml-1" title="Estimated size after transcoding: about ${escapeHtml(formatBytesJs(job.estimate.est_bytes))}">\u2248 ${Math.abs(Math.round(job.estimate.saved_pct))}% ${job.estimate.saved_pct < 0 ? 'larger' : 'smaller'}</span>`;
                }
                if (job.duration_text && job.duration_text !== '-') {
                    sizeDisplayHTML += ` <span class="text-[11px] font-mono bg-slate-800 text-slate-300 px-1.5 py-0.5 rounded border border-slate-700 ml-1" title="Processing Duration">⏱ ${job.duration_text}</span>`;
                }


                return `
                    <div class="bg-slate-900/60 border border-slate-800 rounded-lg p-3 sm:p-4 flex flex-col space-y-3 ${isRunning ? 'active-running-job border-indigo-500/80' : ''}">
                        <div class="flex items-start justify-between gap-3">
                            <div class="min-w-0 flex-1">
                                <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
                                    <h4 class="font-medium text-sm text-slate-200 break-words min-w-0">${job.filename}</h4>
                                    <span class="whitespace-nowrap">${sizeDisplayHTML}</span>
                                </div>
                                <p class="text-xs text-slate-400 truncate">${job.path}</p>
                                ${job.phase ? `<p class="text-xs text-sky-300 mt-1"><i class="fa-solid fa-flask mr-1"></i>${escapeHtml(job.phase)}\u2026</p>` : ''}
                                ${job.skip_reason ? `<p class="text-xs text-sky-300 mt-1"><i class="fa-solid fa-forward mr-1"></i>${escapeHtml(job.skip_reason)}. The file was left untouched.</p>` : ''}
                            </div>
                            <div class="flex items-center gap-1 flex-shrink-0">
                                <span class="px-2.5 py-1 rounded-full text-xs font-semibold whitespace-nowrap ${badgeColor}">${job.remove_requested ? 'Removing\u2026' : job.status}</span>
                                <button type="button" onclick="removeJob(${job.id}, this.dataset.name, this.dataset.running === '1')" data-name="${escapeHtml(job.filename)}" data-running="${job.status === 'Running' ? '1' : '0'}" ${job.remove_requested ? 'disabled' : ''} title="Remove from queue" aria-label="Remove ${escapeHtml(job.filename)} from the queue" class="w-7 h-7 flex items-center justify-center rounded text-slate-500 hover:text-rose-400 hover:bg-slate-800 transition disabled:opacity-40"><i class="fa-solid fa-trash-can"></i></button>
                            </div>
                        </div>
                        <div class="space-y-1">
                            <div class="flex justify-between text-xs text-slate-400 font-mono"><span>Progress</span><span>${job.progress}%</span></div>
                            <div class="w-full bg-slate-800 rounded-full h-2 overflow-hidden">
                                <div class="bg-indigo-500 h-2 transition-all duration-300" style="width: ${job.progress}%"></div>
                            </div>
                        </div>
                    </div>
                `;
            }).join('');


            if (autoScrollToggle && autoScrollToggle.checked) {
                const activeCard = container.querySelector('.active-running-job');
                if (activeCard) {
                    activeCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                }
            }
        }


        setInterval(fetchDashboardData, 2000);
        fetchDashboardData();
        refreshEngineLabel();
    </script>
</body>
</html>
"""


def format_size(size_bytes):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def format_duration(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    elif m > 0:
        return f"{m}m {s}s"
    else:
        return f"{s}s"


def map_jellyfin_path(path):
    """Translate a path as seen by this app into the path Jellyfin sees (optional prefix mapping)."""
    src = (CONFIG.get("jellyfin_path_from") or "").strip().rstrip('/')
    dst = (CONFIG.get("jellyfin_path_to") or "").strip().rstrip('/')
    if src and (path == src or path.startswith(src + '/')):
        return dst + path[len(src):]
    return path


def notify_jellyfin(updates):
    """Tell Jellyfin that individual files changed.
    updates is a list of (path, 'Created' | 'Modified' | 'Deleted'). Never raises."""
    if not updates or not CONFIG.get("jellyfin_enabled"):
        return
    base_url = (CONFIG.get("jellyfin_url") or "").strip().rstrip('/')
    api_key = (CONFIG.get("jellyfin_api_key") or "").strip()
    if not base_url or not api_key:
        add_log("Jellyfin: notifications are enabled but the URL or API key is missing. Skipped.", level='warning')
        return

    payload = {"Updates": [{"Path": map_jellyfin_path(p), "UpdateType": t} for p, t in updates]}
    req = urllib.request.Request(
        base_url + "/Library/Media/Updated",
        data=json.dumps(payload).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": f'MediaBrowser Token="{api_key}"',
            "X-Emby-Token": api_key
        },
        method="POST"
    )
    names = ", ".join(os.path.basename(p) for p, _ in updates)
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
        add_log(f"Jellyfin: rescan requested for {names}", level='success')
    except urllib.error.HTTPError as e:
        add_log(f"Jellyfin: request rejected (HTTP {e.code}) for {names}. Check the URL and API key.", level='error')
    except Exception as e:
        add_log(f"Jellyfin: could not reach server for {html.escape(names)}: {html.escape(str(e))}", level='error')


ARR_SERVICES = {
    # name: (API path listing everything, key holding the id, command name, command id field, noun)
    'radarr': ('movie', 'RescanMovie', 'movieId', 'movie'),
    'sonarr': ('series', 'RescanSeries', 'seriesId', 'series'),
}


def arr_request(base_url, api_key, method, path, payload=None, timeout=20):
    """Call a Radarr / Sonarr v3 API endpoint and return the decoded JSON. Raises RuntimeError with a readable message."""
    base_url = (base_url or '').strip().rstrip('/')
    if not base_url.lower().startswith(('http://', 'https://')):
        raise RuntimeError("the address must start with http:// or https://")
    req = urllib.request.Request(
        f"{base_url}/api/v3/{path}",
        data=json.dumps(payload).encode('utf-8') if payload is not None else None,
        headers={"X-Api-Key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
        method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8', errors='replace')
        return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError("the API key was rejected (HTTP %d)" % e.code)
        if e.code == 404:
            raise RuntimeError("not found (HTTP 404). Check the address, including any URL base such as /radarr")
        raise RuntimeError(f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not connect ({e.reason})")
    except (ValueError, OSError) as e:
        raise RuntimeError(f"unexpected reply ({e})")


def notify_arr(paths):
    """After files were transcoded, ask Radarr / Sonarr (whichever are enabled) to rescan the movie or
    series that owns each path, so they notice the changed file. Never raises."""
    paths = [p for p in (paths or []) if p]
    if not paths:
        return
    names = ", ".join(os.path.basename(p) for p in paths)
    enabled = [svc for svc in ARR_SERVICES if CONFIG.get(f"{svc}_enabled")]
    matched_any = False
    all_reached = True
    for svc in enabled:
        listing, command, id_field, noun = ARR_SERVICES[svc]
        label = svc.capitalize()
        base = (CONFIG.get(f"{svc}_url") or "").strip().rstrip('/')
        key = (CONFIG.get(f"{svc}_api_key") or "").strip()
        if not base or not key:
            all_reached = False
            add_log(f"{label}: rescans are enabled but the URL or API key is missing. Skipped.", level='warning')
            continue
        try:
            items = arr_request(base, key, 'GET', listing)
            if not isinstance(items, list):
                raise RuntimeError("unexpected reply")
        except Exception as e:
            all_reached = False
            add_log(f"{label}: could not look up your {noun} list ({html.escape(str(e))}). Rescan skipped for {html.escape(names)}.", level='error')
            continue
        found = {}
        for p in paths:
            norm = p.replace('\\', '/')
            best = None
            for it in items:
                folder = map_webhook_path(str(it.get('path') or '')).replace('\\', '/').rstrip('/')
                if folder and (norm == folder or norm.startswith(folder + '/')):
                    if best is None or len(folder) > best[0]:
                        best = (len(folder), it)
            if best:
                found[best[1].get('id')] = best[1].get('title') or best[1].get('id')
        for item_id, title in found.items():
            matched_any = True
            try:
                arr_request(base, key, 'POST', 'command', {"name": command, id_field: item_id})
                add_log(f"{label}: rescan requested for {html.escape(str(title))}", level='success')
            except Exception as e:
                add_log(f"{label}: rescan of {html.escape(str(title))} failed ({html.escape(str(e))}).", level='error')
    if enabled and all_reached and not matched_any:
        add_log(f"Radarr/Sonarr: no movie or series folder matches {html.escape(names)}. "
                "If they see your media at a different path, add a path mapping under Settings > Sonarr & Radarr.", level='warning')


def video_codec_family(name):
    """The codec name ffprobe reports for files made by this encoder, or None if unknown / 'copy'."""
    n = (name or '').strip().lower()
    if not n or n == 'copy':
        return None
    if '265' in n or 'hevc' in n:
        return 'hevc'
    if '264' in n or 'avc' in n:
        return 'h264'
    if 'av1' in n or 'aom' in n or 'rav1e' in n:
        return 'av1'
    if 'vp9' in n:
        return 'vp9'
    if 'vp8' in n:
        return 'vp8'
    if 'mpeg4' in n:
        return 'mpeg4'
    if 'mpeg2' in n:
        return 'mpeg2video'
    return None


def qsv_encoder_name(vcodec):
    """The Quick Sync encoder used for a given video codec setting."""
    vc = str(vcodec).strip().lower()
    if vc.endswith('_qsv'):
        return vc
    if '265' in vc or 'hevc' in vc:
        return 'hevc_qsv'
    return 'h264_qsv'


def compliance_targets():
    """What an existing MP4 must look like to count as already done, taken from the CURRENT settings:
    video codec, pixel format and audio channel count. Any of them can be None (= not enforced)."""
    vc = str(CONFIG.get('vcodec', '')).strip()
    use_qsv = CONFIG.get('hwaccel') == 'qsv' and bool(qsv_devices())
    if use_qsv:
        vc = qsv_encoder_name(vc)
    extra = CONFIG.get('extra_flags') or ''
    try:
        flags = shlex.split(extra)
    except ValueError:
        flags = extra.split()
    channels = pix_opt = vf_fmt = None
    for i, token in enumerate(flags[:-1]):
        nxt = flags[i + 1]
        if token == '-ac':
            try:
                channels = int(nxt)
            except ValueError:
                pass
        elif token == '-pix_fmt':
            pix_opt = nxt.strip().lower()
        elif token in ('-vf', '-filter:v'):
            m = re.search(r'(?:^|,)format=([a-z0-9_]+)', nxt.lower())
            if m:
                vf_fmt = m.group(1)
    # With QSV the pipeline always outputs 8-bit 4:2:0 and ignores -vf / -pix_fmt
    pix_fmt = 'yuv420p' if use_qsv else (pix_opt or vf_fmt)
    return {'codec': video_codec_family(vc), 'pix_fmt': pix_fmt, 'channels': channels}


def describe_compliance_targets(t=None):
    t = t or compliance_targets()
    parts = [f"video codec {t['codec'] or 'any'}",
             f"pixel format {t['pix_fmt'] or 'any'}",
             f"audio channels {t['channels'] if t['channels'] is not None else 'any'}"]
    return ", ".join(parts)


def check_mp4_compliance(file_path):
    """True if the MP4 already matches the current codec settings (so it needn't be re-encoded)."""
    try:
        targets = compliance_targets()
        cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name,pix_fmt',
            '-of', 'default=noprint_wrappers=1',
            file_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        v_codec, pix_fmt = "", ""
        for line in res.stdout.splitlines():
            if line.startswith('codec_name='):
                v_codec = line.split('=')[1].strip().lower()
            elif line.startswith('pix_fmt='):
                pix_fmt = line.split('=')[1].strip().lower()
        if not v_codec:
            return False  # no readable video stream: needs processing

        channels = 0
        if targets['channels'] is not None:
            cmd_a = [
                'ffprobe', '-v', 'error',
                '-select_streams', 'a:0',
                '-show_entries', 'stream=channels',
                '-of', 'default=noprint_wrappers=1',
                file_path
            ]
            res_a = subprocess.run(cmd_a, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            for line in res_a.stdout.splitlines():
                if line.startswith('channels='):
                    try:
                        channels = int(line.split('=')[1].strip())
                    except Exception:
                        pass

        if targets['codec'] and v_codec != targets['codec']:
            return False
        if targets['pix_fmt'] and pix_fmt != targets['pix_fmt']:
            return False
        if targets['channels'] is not None and channels != targets['channels']:
            return False
        return True
    except Exception:
        return False


def background_scan_worker(path):
    global JOBS, SCAN_STATE
    SCAN_STATE["running"] = True
    SCAN_STATE["logs"] = []
    add_scan_log(f"Scan Started. Traversing: {path}", level='info')
    
    try:
        min_val = float(CONFIG.get("min_size_val", 0))
    except Exception:
        min_val = 0.0
    unit = CONFIG.get("min_size_unit", "GB").upper()
    if unit == "MB":
        min_bytes = min_val * 1024 * 1024
    else:
        min_bytes = min_val * 1024 * 1024 * 1024
        
    add_scan_log(f"Minimum size threshold: > {min_val} {unit} ({min_bytes:,.0f} bytes)", level='info')

    include_mp4 = bool(CONFIG.get("include_mp4", True))
    add_scan_log(f"MP4 files: {'included' if include_mp4 else 'excluded'}", level='info')
    if include_mp4:
        add_scan_log(f"An MP4 counts as already done when it matches the current settings: {describe_compliance_targets()}", level='info')

    found_files = []
    
    if os.path.exists(path):
        base_depth = path.rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(path):
            current_depth = root.rstrip(os.sep).count(os.sep) - base_depth
            add_scan_log(f"Traversing: {root} (Depth: {current_depth})", level='info')
            if len(SCAN_STATE["logs"]) > 200:
                SCAN_STATE["logs"].pop(0)
                
            if current_depth > 10:
                add_scan_log(f"Max depth 10 reached. Skipping subfolders of {root}", level='warning')
                dirs.clear()
                continue
                
            for file in files:
                ext = file.lower()
                if ext.endswith('.mp4') and not include_mp4:
                    continue
                if ext.endswith(VIDEO_EXTENSIONS):
                    full_path = os.path.join(root, file)
                    
                    try:
                        size_bytes = os.path.getsize(full_path)
                    except Exception:
                        size_bytes = 0
                        
                    if size_bytes <= min_bytes:
                        add_scan_log(f"Filtered out: {file} ({format_size(size_bytes)}) below threshold.", level='warning')
                        continue

                    if ext.endswith('.mp4'):
                        add_scan_log(f"Checking MP4 compliance: {file}...", level='info')
                        if check_mp4_compliance(full_path):
                            add_scan_log(f"  └─ Compliant. Skipped.", level='success')
                            continue
                        else:
                            add_scan_log(f"  └─ Non-compliant. Added to queue.", level='warning')
                    else:
                        add_scan_log(f"Found media file: {file} ({format_size(size_bytes)})", level='success')

                    found_files.append({
                        "filename": file,
                        "path": full_path,
                        "size_bytes": size_bytes,
                        "size_human": format_size(size_bytes),
                        "status": "Pending",
                        "progress": 0,
                        "output_size_human": None,
                        "output_size_bytes": 0,
                        "duration_text": "-"
                    })
    
    found_files.sort(key=lambda x: x['path'].lower())
    existing_paths = {j['path'] for j in JOBS}
    new_files = [f for f in found_files if f['path'] not in existing_paths]
    skipped = len(found_files) - len(new_files)
    JOBS.extend(new_files)
    summary = f"Added {len(new_files)} new file(s) to the queue"
    if skipped:
        summary += f" ({skipped} already queued)"
    summary += f". Queue total: {len(JOBS)}."
    add_scan_log(f"Scan complete. {summary}", level='success')
    SCAN_STATE["running"] = False
    add_log(f"Scanned directory {path}. {summary}", level='success')


@app.route('/')
def index():
    theme = CONFIG.get("theme") if CONFIG.get("theme") in THEME_CHOICES else "dark"
    return render_template_string(HTML_TEMPLATE, theme_pref=theme)


@app.route('/api/jobs', methods=['GET'])
def get_jobs():
    ensure_job_ids()
    current_idx = 0
    for i, j in enumerate(JOBS):
        if j['status'] == 'Running':
            current_idx = i + 1
            if 'start_time' in j:
                j['duration_text'] = format_duration(time.time() - j['start_time'])
            break

    total_src = sum(j.get('size_bytes', 0) for j in JOBS if j['status'] == 'Completed')
    total_out = sum(j.get('output_size_bytes', 0) for j in JOBS if j['status'] == 'Completed')
    
    if total_src > 0:
        delta_pct = ((total_out - total_src) / total_src) * 100
        diff_bytes = total_src - total_out  # positive means space saved
        gb_saved = diff_bytes / (1024 ** 3)
        gb_str = f"{abs(gb_saved):.1f}GB" if abs(gb_saved) >= 0.1 else f"{abs(diff_bytes) / (1024**2):.1f}MB"
        delta_str = f"{delta_pct:+.1f}% ({gb_str} saved)" if diff_bytes >= 0 else f"{delta_pct:+.1f}% (-{gb_str})"
        delta_color = "text-rose-400" if delta_pct > 0 else ("text-emerald-400" if delta_pct < 0 else "text-slate-300")
    else:
        delta_str = "0.0% (0GB)"
        delta_color = "text-slate-300"

    return jsonify({
        "jobs": JOBS,
        "queue_running": QUEUE_RUNNING,
        "current_index": current_idx,
        "total_jobs": len(JOBS),
        "queue_start_time": QUEUE_START_TIME,
        "size_delta_str": delta_str,
        "size_delta_color": delta_color
    })


@app.route('/api/logs', methods=['GET'])
def get_logs():
    return jsonify({"logs": LOGS})


@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    LOGS.clear()
    return jsonify({"status": "success"})


@app.route('/api/scan/logs', methods=['GET'])
def get_scan_logs():
    return jsonify({
        "logs": SCAN_STATE["logs"],
        "finished": not SCAN_STATE["running"]
    })


@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    global CONFIG
    if request.method == 'POST':
        data = request.json or {}
        for key in CONFIG:
            if key in data and key != "presets":  # presets have their own endpoint
                CONFIG[key] = data[key]
        if CONFIG.get("theme") not in THEME_CHOICES:
            CONFIG["theme"] = "dark"
        try:
            CONFIG["min_savings_percent"] = min(95.0, max(0.0, float(CONFIG.get("min_savings_percent") or 0)))
        except (TypeError, ValueError):
            CONFIG["min_savings_percent"] = 0
        saved = save_config()
        return jsonify({"status": "success", "config": CONFIG, "persisted": saved})
    return jsonify(CONFIG)


@app.route('/api/presets/<int:slot>', methods=['POST', 'DELETE'])
def handle_preset(slot):
    """Store the given codec settings in a preset slot (1-10), or clear the slot."""
    if slot < 1 or slot > PRESET_SLOTS:
        return jsonify({"status": "error", "message": f"Slot must be 1 to {PRESET_SLOTS}."}), 400
    presets = _clean_presets(CONFIG.get("presets"))
    if request.method == 'DELETE':
        presets[slot - 1] = None
    else:
        data = request.get_json(silent=True) or {}
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        entry = _clean_preset({**settings, "name": data.get("name", "")})
        if entry is None:
            return jsonify({"status": "error", "message": "Those settings can't be stored (a video codec is required)."}), 400
        presets[slot - 1] = entry
    CONFIG["presets"] = presets
    saved = save_config()
    return jsonify({"status": "success", "presets": presets, "persisted": saved})


@app.route('/api/config/reset', methods=['POST'])
def reset_config():
    global CONFIG
    CONFIG = {
        "vcodec": "libx264",
        "preset": "medium",
        "crf": "28",
        "acodec": "aac",
        "audio_bitrate": "192k",
        "extra_flags": "-ac 5 -vf format=yuv420p -movflags +faststart",
        "hwaccel": os.environ.get("TRANSCODER_HWACCEL", "none"),
        "theme": CONFIG.get("theme", "dark"),
        "presets": _clean_presets(CONFIG.get("presets")),  # Restore Defaults keeps your stored presets
        "min_size_val": 0,
        "min_size_unit": "GB",
        "include_mp4": True,
        "min_savings_percent": CONFIG.get("min_savings_percent", 0),
        # Jellyfin connection details are kept so Restore Defaults doesn't wipe the API key
        "jellyfin_enabled": CONFIG.get("jellyfin_enabled", False),
        "jellyfin_url": CONFIG.get("jellyfin_url", ""),
        "jellyfin_api_key": CONFIG.get("jellyfin_api_key", ""),
        "jellyfin_path_from": CONFIG.get("jellyfin_path_from", ""),
        "jellyfin_path_to": CONFIG.get("jellyfin_path_to", ""),
        "radarr_enabled": CONFIG.get("radarr_enabled", False),
        "radarr_url": CONFIG.get("radarr_url", ""),
        "radarr_api_key": CONFIG.get("radarr_api_key", ""),
        "sonarr_enabled": CONFIG.get("sonarr_enabled", False),
        "sonarr_url": CONFIG.get("sonarr_url", ""),
        "sonarr_api_key": CONFIG.get("sonarr_api_key", ""),
        "webhook_token": CONFIG.get("webhook_token", ""),
        "webhook_path_map": CONFIG.get("webhook_path_map", "")
    }
    save_config()
    return jsonify({"status": "success", "config": CONFIG})


@app.route('/api/arr/test', methods=['POST'])
def arr_test():
    """Check a Radarr / Sonarr address and API key (the values typed in the form, falling back to the saved ones)."""
    data = request.get_json(silent=True) or {}
    svc = data.get('service')
    if svc not in ARR_SERVICES:
        return jsonify({"ok": False, "message": "Unknown service."}), 400
    listing, _, _, noun = ARR_SERVICES[svc]
    base = (data.get('url') or CONFIG.get(f"{svc}_url") or '').strip()
    key = (data.get('api_key') or CONFIG.get(f"{svc}_api_key") or '').strip()
    if not base or not key:
        return jsonify({"ok": False, "message": "Enter the address and the API key first."})
    try:
        status = arr_request(base, key, 'GET', 'system/status', timeout=10)
        items = arr_request(base, key, 'GET', listing, timeout=30)
        count = len(items) if isinstance(items, list) else 0
        name = status.get('appName') or svc.capitalize()
        version = status.get('version') or ''
        return jsonify({"ok": True, "message": f"Connected to {name} {version}".strip() + f" ({count} {noun}{'' if count == 1 else 's'} in the library)."})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Could not connect: {e}."})


@app.route('/api/browse', methods=['POST'])
def browse_directory():
    data = request.json or {}
    path = data.get('path', '/media')
    include_mp4 = bool(data.get('include_mp4', CONFIG.get("include_mp4", True)))
    if not os.path.exists(path):
        path = '/media'
    try:
        items = os.listdir(path)
    except Exception:
        items = []
    
    subdirs = []
    mkv_count = 0
    for item in sorted(items):
        full_path = os.path.join(path, item)
        if os.path.isdir(full_path):
            try:
                subdirs.append({"name": item, "path": full_path})
            except Exception:
                pass
        elif item.lower().endswith(VIDEO_EXTENSIONS) and (include_mp4 or not item.lower().endswith('.mp4')):
            mkv_count += 1
            
    parent_path = os.path.dirname(path) if path != '/' else None
    return jsonify({
        "current_path": path,
        "parent_path": parent_path,
        "subdirs": subdirs,
        "mkv_count": mkv_count
    })


@app.route('/api/scan', methods=['POST'])
def scan_directory():
    data = request.json or {}
    path = data.get('path', '/media')
    
    if SCAN_STATE["running"]:
        return jsonify({"status": "already_running"})

    # Per-scan options chosen in the directory dialog (remembered as the next defaults)
    options_before = (CONFIG.get('min_size_val'), CONFIG.get('min_size_unit'), CONFIG.get('include_mp4'))
    if 'min_size_val' in data:
        try:
            CONFIG['min_size_val'] = float(data['min_size_val'])
        except (TypeError, ValueError):
            CONFIG['min_size_val'] = 0
    if data.get('min_size_unit') in ('MB', 'GB'):
        CONFIG['min_size_unit'] = data['min_size_unit']
    if 'include_mp4' in data:
        CONFIG['include_mp4'] = bool(data['include_mp4'])
    if options_before != (CONFIG.get('min_size_val'), CONFIG.get('min_size_unit'), CONFIG.get('include_mp4')):
        save_config()

    t = threading.Thread(target=background_scan_worker, args=(path,))
    t.daemon = True
    t.start()
    
    return jsonify({"status": "started"})


def parse_webhook_path_map():
    """Parse 'from => to' lines (newline or ; separated) into [(from, to), ...], longest prefix first."""
    pairs = []
    raw = CONFIG.get("webhook_path_map") or ""
    for line in re.split(r'[\n;]+', raw):
        if '=>' not in line:
            continue
        src, dst = line.split('=>', 1)
        src = src.strip().replace('\\', '/').rstrip('/')
        dst = dst.strip().rstrip('/')
        if src:
            pairs.append((src, dst))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def map_webhook_path(path):
    """Translate a path as seen by Sonarr/Radarr into the path this app sees."""
    norm = path.replace('\\', '/')
    for src, dst in parse_webhook_path_map():
        if norm == src or norm.startswith(src + '/'):
            return dst + norm[len(src):]
    return path


def extract_arr_file_paths(data):
    """Pull the imported file path(s) out of a Sonarr/Radarr webhook payload."""
    series_path = (data.get('series') or {}).get('path') or ''
    movie_path = (data.get('movie') or {}).get('folderPath') or ''
    found = []
    for key, base in (('episodeFile', series_path), ('episodeFiles', series_path),
                      ('movieFile', movie_path), ('movieFiles', movie_path)):
        value = data.get(key)
        if not value:
            continue
        for f in (value if isinstance(value, list) else [value]):
            if not isinstance(f, dict):
                continue
            path = f.get('path')
            if not path and f.get('relativePath') and base:
                path = base.rstrip('/\\') + '/' + f['relativePath']
            if path:
                found.append(path)
    return list(dict.fromkeys(found))


@app.route('/api/webhook/arr', methods=['POST'])
def arr_webhook():
    token = (CONFIG.get("webhook_token") or "").strip()
    if not token:
        return jsonify({"status": "error", "message": "Webhook is disabled. Set a webhook token in Settings."}), 403

    supplied = request.args.get('token') or request.headers.get('X-Webhook-Token') or ''
    if not supplied and request.authorization and request.authorization.password:
        supplied = request.authorization.password
    if not hmac.compare_digest(supplied.encode('utf-8'), token.encode('utf-8')):
        return jsonify({"status": "error", "message": "Invalid token"}), 401

    data = request.get_json(silent=True) or {}
    event = data.get('eventType', '')
    source = data.get('instanceName') or ('Sonarr' if 'series' in data else 'Radarr' if 'movie' in data else 'Sonarr/Radarr')

    if event == 'Test':
        add_log(f"Webhook: test event received from {html.escape(str(source))}.", level='success')
        return jsonify({"status": "ok", "message": "Test received"})
    if event not in ('Download', 'ImportComplete'):
        return jsonify({"status": "ignored", "event": event})

    added, skipped = 0, 0
    for raw_path in extract_arr_file_paths(data):
        path = map_webhook_path(raw_path)
        shown = html.escape(path)
        name = html.escape(os.path.basename(path))

        if not path.lower().endswith(VIDEO_EXTENSIONS):
            skipped += 1
            add_log(f"Webhook: {name} is not a supported video file. Skipped.", level='warning')
            continue
        if not os.path.isfile(path):
            skipped += 1
            add_log(f"Webhook: file not found at {shown}. Check the path mappings in Settings. Skipped.", level='error')
            continue
        if any(j['path'] == path and j['status'] in ('Pending', 'Running') for j in JOBS):
            skipped += 1
            add_log(f"Webhook: {name} is already in the queue. Skipped.", level='info')
            continue
        if path.lower().endswith('.mp4') and check_mp4_compliance(path):
            skipped += 1
            add_log(f"Webhook: {name} is already compliant. Skipped.", level='info')
            continue

        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            size_bytes = 0
        JOBS.append({
            "filename": os.path.basename(path),
            "path": path,
            "size_bytes": size_bytes,
            "size_human": format_size(size_bytes),
            "status": "Pending",
            "progress": 0,
            "output_size_human": None,
            "output_size_bytes": 0,
            "duration_text": "-"
        })
        added += 1
        add_log(f"Webhook: {html.escape(str(source))} imported {name}. Added to the queue.", level='success')

    if added:
        if AUTO_START_PAUSED:
            add_log("Webhook: the queue was stopped manually, so it was not started automatically. Press Start Queue to process the new file(s).", level='warning')
        elif start_queue_worker():
            add_log("Webhook: queue started automatically.", level='success')

    return jsonify({"status": "ok", "added": added, "skipped": skipped})


def qsv_devices():
    """Render nodes for the GPU (/dev/dri/renderD*). Empty if the GPU isn't passed through."""
    try:
        return sorted(os.path.join('/dev/dri', n) for n in os.listdir('/dev/dri') if n.startswith('renderD'))
    except OSError:
        return []


_FFMPEG_QSV_CACHE = {}


def ffmpeg_has_qsv():
    if 'v' not in _FFMPEG_QSV_CACHE:
        try:
            out = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, universal_newlines=True, timeout=20).stdout
            _FFMPEG_QSV_CACHE['v'] = 'h264_qsv' in out
        except Exception:
            _FFMPEG_QSV_CACHE['v'] = False
    return _FFMPEG_QSV_CACHE['v']


def ffmpeg_has_encoder(name):
    key = 'enc:' + name
    if key not in _FFMPEG_QSV_CACHE:
        try:
            out = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, universal_newlines=True, timeout=20).stdout
            _FFMPEG_QSV_CACHE[key] = name in out
        except Exception:
            _FFMPEG_QSV_CACHE[key] = False
    return _FFMPEG_QSV_CACHE[key]


@app.route('/api/hwaccel', methods=['GET'])
def hwaccel_status():
    return jsonify({"devices": qsv_devices(), "ffmpeg_qsv": ffmpeg_has_qsv()})


def _drop_video_filter_flags(tokens):
    """Remove -vf/-filter:v/-pix_fmt (and their values): they are software-pipeline flags that
    conflict with the QSV filter chain. Returns (kept_tokens, dropped_anything)."""
    kept, dropped, skip = [], False, False
    for t in tokens:
        if skip:
            skip = False
            continue
        if t in ('-vf', '-filter:v', '-pix_fmt'):
            skip = True
            dropped = True
            continue
        kept.append(t)
    return kept, dropped


# A hardware attempt that has produced no video frame after this long is abandoned. FFmpeg does not
# fail early when the GPU decoder yields nothing: it carries on copying the audio of the whole file,
# then fails at the very end (minutes later on a big file over a network share).
HW_FIRST_FRAME_SECONDS = 60
# ...and one whose frame count has stopped advancing for this long is treated as hung. This is not
# applied once the encode has reached the end of the source: FFmpeg then finalises the file
# (with +faststart it rewrites the whole output, minutes for a big file) and produces no frames.
HW_STALL_SECONDS = 300
GPU_DECODE_TEST_SECONDS = 3  # length of the quick trial decode run before committing to GPU decode


def _last_frame_count(text):
    found = re.findall(r'frame=\s*(\d+)', text or '')
    return int(found[-1]) if found else 0


def qsv_gpu_decode_check(input_path, enc, preset):
    """Quick trial of the exact GPU decode + encode pipeline on the first few seconds of video only
    (no audio). Takes a second or two, so a file the GPU can't decode is spotted immediately instead
    of after a full run. Returns (ok, reason)."""
    global CURRENT_PROCESS
    # -t is placed BEFORE -i on purpose: as an input limit it stops reading after a few seconds of the
    # file. As an output limit it would never trigger when the decoder yields no frames, and FFmpeg would
    # read the whole video track before giving up.
    cmd = ['ffmpeg', '-hide_banner', '-v', 'error', '-stats', '-hwaccel', 'qsv', '-hwaccel_output_format', 'qsv',
           '-t', str(GPU_DECODE_TEST_SECONDS), '-i', input_path, '-map', '0:v:0', '-an', '-sn', '-dn',
           '-vf', 'vpp_qsv=format=nv12', '-c:v', enc, '-preset', preset, '-global_quality', str(CONFIG['crf']),
           '-f', 'null', '-']
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
        CURRENT_PROCESS = proc
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return False, 'the trial decode did not finish within 30 seconds'
        finally:
            CURRENT_PROCESS = None
    except Exception as e:
        return False, str(e)
    if stop_requested():
        return False, 'cancelled'
    if proc.returncode == 0 and _last_frame_count(out) > 0:
        return True, ''
    lines = [l.strip() for l in (out or '').replace('\r', '\n').splitlines() if l.strip() and not l.strip().startswith('frame=')]
    return False, (' | '.join(lines[-3:]) or 'no video frames were produced')


_UNKNOWN_LAYOUTS = ('', 'unknown', 'unspecified', 'n/a')
_LAYOUT_BY_CHANNELS = {1: 'mono', 2: 'stereo', 3: '3.0', 4: '4.0', 5: '5.0', 6: '5.1', 7: '6.1', 8: '7.1'}


def probe_audio(path):
    """(codec, channels, channel_layout) of the first audio stream, or (None, 0, '') if unreadable."""
    try:
        res = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                              '-show_entries', 'stream=codec_name,channels,channel_layout',
                              '-of', 'default=noprint_wrappers=1', path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
    except Exception:
        return None, 0, ''
    codec, channels, layout = None, 0, ''
    for line in res.stdout.splitlines():
        k, _, v = line.partition('=')
        if k == 'codec_name':
            codec = v.strip().lower()
        elif k == 'channels':
            try:
                channels = int(v.strip())
            except ValueError:
                pass
        elif k == 'channel_layout':
            layout = v.strip().lower()
    return codec, channels, layout


def audio_layout_fix(path, extra):
    """Some files have audio with a channel count but no speaker layout ("unknown"). Newer FFmpeg
    can't convert such audio (for example with -ac 5) and the AAC encoder fails to start, so label it
    by its channel count first. Returns the extra FFmpeg arguments to add (possibly none)."""
    if any(t in ('-af', '-filter:a') for t in extra):
        return []
    _, channels, layout = probe_audio(path)
    name = _LAYOUT_BY_CHANNELS.get(channels)
    if channels and layout in _UNKNOWN_LAYOUTS and name:
        mapping = '|'.join(str(i) for i in range(channels))
        filt = f'channelmap=map={mapping}:channel_layout={name}'
        # Newer FFmpeg ignores -ac once an audio filter is present, so do the channel conversion in the
        # filter as well (the -ac setting is still passed, it just no longer has to do the work).
        want = None
        for i, t in enumerate(extra[:-1]):
            if t == '-ac':
                try:
                    want = _LAYOUT_BY_CHANNELS.get(int(extra[i + 1]))
                except ValueError:
                    pass
        if want and want != name:
            filt += f',aformat=channel_layouts={want}'
        add_log(f"Audio has {channels} channels but no layout information; labelling it {name}"
                + (f" and converting it to {want}." if want and want != name else "."), level='info')
        return ['-af', filt]
    return []


def _with_audio_copy(cmd):
    """The same command with the audio stream copied untouched (drops the audio codec, bitrate,
    channel count and audio filter options)."""
    out, i = [], 0
    while i < len(cmd):
        if cmd[i] in ('-c:a', '-b:a', '-ac', '-af', '-filter:a', '-channel_layout') and i + 1 < len(cmd):
            i += 2
            continue
        out.append(cmd[i])
        i += 1
    return out[:-1] + ['-c:a', 'copy'] + out[-1:]


def _build_attempts_core(input_path, output_path):
    """Returns a list of (label, ffmpeg command) to try in order. Software mode has one entry.
    QSV mode tries full GPU decode+encode, then CPU decode + GPU encode, then plain CPU."""
    extra = (CONFIG.get('extra_flags') or '').split()
    audio = audio_layout_fix(input_path, extra) + ['-c:a', CONFIG['acodec'], '-b:a', CONFIG['audio_bitrate']]

    sw_cmd = ['ffmpeg', '-y', '-i', input_path,
              '-c:v', CONFIG['vcodec'], '-preset', CONFIG['preset'], '-crf', str(CONFIG['crf'])] \
             + audio + extra + [output_path]
    software = ('software (CPU)', sw_cmd)

    if CONFIG.get('hwaccel') != 'qsv':
        return [software]
    if not qsv_devices():
        add_log("Hardware acceleration is set to QSV but /dev/dri has no render device; using the CPU instead.", level='warning')
        return [software]

    enc = qsv_encoder_name(CONFIG['vcodec'])
    preset = CONFIG['preset'] if CONFIG['preset'] not in ('ultrafast', 'superfast') else 'veryfast'

    extra_hw, dropped = _drop_video_filter_flags(extra)
    if dropped:
        add_log("QSV: ignoring -vf / -pix_fmt from the extra flags (the GPU pipeline outputs NV12 itself).", level='info')

    common = ['-c:v', enc, '-preset', preset, '-global_quality', str(CONFIG['crf'])]
    if enc == 'hevc_qsv':
        common += ['-tag:v', 'hvc1']
    common += audio + extra_hw + [output_path]

    full_gpu = ['ffmpeg', '-y', '-hwaccel', 'qsv', '-hwaccel_output_format', 'qsv', '-i', input_path,
                '-vf', 'vpp_qsv=format=nv12'] + common
    cpu_decode = ['ffmpeg', '-y', '-init_hw_device', 'qsv=hw', '-filter_hw_device', 'hw', '-i', input_path,
                  '-vf', 'format=nv12,hwupload=extra_hw_frames=64'] + common
    attempts = []
    ok, why = qsv_gpu_decode_check(input_path, enc, preset)
    if ok:
        attempts.append((f'QSV {enc} (GPU decode)', full_gpu))
    elif not stop_requested():
        add_log(f"GPU decode can't handle this file ({html.escape(why)}); using CPU decode with the GPU encoder.", level='info')
    attempts.append((f'QSV {enc} (CPU decode)', cpu_decode))

    # Same Intel GPU through VAAPI: only needs the media driver, so it still works when FFmpeg's
    # QSV runtime is missing or unsupported on this chip.
    venc = 'hevc_vaapi' if enc == 'hevc_qsv' else 'h264_vaapi'
    if ffmpeg_has_encoder(venc):
        vaapi = ['ffmpeg', '-y', '-init_hw_device', f'vaapi=va:{qsv_devices()[0]}', '-filter_hw_device', 'va',
                 '-i', input_path, '-vf', 'format=nv12,hwupload', '-c:v', venc, '-qp', str(CONFIG['crf'])]
        if venc == 'hevc_vaapi':
            vaapi += ['-tag:v', 'hvc1']
        vaapi += audio + extra_hw + [output_path]
        attempts.append((f'VAAPI {venc} (Intel GPU)', vaapi))

    return attempts + [software]


def build_ffmpeg_attempts(input_path, output_path):
    """The attempts from _build_attempts_core, plus a last resort for files with odd audio: keep the audio
    untouched (only offered when it is AAC, which MP4 accepts as it is) and still re-encode the video.
    The channel setting is not applied to such a file."""
    attempts = _build_attempts_core(input_path, output_path)
    a_codec, _, _ = probe_audio(input_path)
    if a_codec == 'aac' and CONFIG.get('acodec') != 'copy':
        base_label, base_cmd = next(((l, c) for l, c in attempts if l.endswith('(CPU decode)')), attempts[-1])
        attempts.append((base_label.replace(' (CPU decode)', '').replace(' (CPU)', '') + ', audio copied unchanged',
                         _with_audio_copy(base_cmd)))
    return attempts


def ensure_job_ids():
    """Give every queued job a stable id (scans and the webhook append plain dicts without one)."""
    with JOBS_LOCK:
        for j in JOBS:
            if 'id' not in j:
                j['id'] = next(_JOB_IDS)


def stop_requested():
    """True if the whole queue was stopped, or the job being transcoded was removed by the user."""
    job = CURRENT_JOB
    return CANCEL_EVENT.is_set() or bool(job and job.get('remove_requested'))


def next_job_to_run(handled):
    """The first job, in queue order, that hasn't been dealt with in this run and isn't finished.
    Looked up fresh each time so items can be removed from (or added to) the queue while it runs."""
    with JOBS_LOCK:
        ensure_job_ids()
        limit = float(CONFIG.get('min_savings_percent') or 0)
        for j in JOBS:
            if j['id'] in handled or j['status'] == 'Completed':
                continue
            if j['status'] == 'Skipped':
                # Stays skipped until the minimum saving is lowered (or switched off) enough for its estimate
                est = (j.get('estimate') or {}).get('saved_pct')
                if est is not None and limit > 0 and est < limit:
                    continue
            return j
    return None


SAVINGS_SAMPLES = 5            # clips encoded for the estimate, spread through the file
SAVINGS_SAMPLE_SECONDS = 20    # length of each clip
SAVINGS_MIN_DURATION = 150     # shorter files are not worth testing: they are just transcoded
SAVINGS_SAMPLE_TIMEOUT = 180   # seconds allowed for one clip


def probe_duration(path):
    """Duration of a media file in seconds, or None."""
    try:
        res = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of',
                              'default=noprint_wrappers=1:nokey=1', path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        value = float(res.stdout.strip())
        return value if value > 0 else None
    except Exception:
        return None


def _run_sample(cmd):
    """Run one short FFmpeg clip. Returns (True|False, error text), or (None, '') if the queue was stopped
    or the item removed meanwhile. The process is registered so Stop / Remove can terminate it."""
    global CURRENT_PROCESS
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors='replace')
    except Exception as e:
        return False, str(e)
    CURRENT_PROCESS = proc
    started = time.time()
    try:
        while True:
            try:
                _, err = proc.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if stop_requested():
                    proc.terminate()
                    proc.communicate()
                    return None, ''
                if time.time() - started > SAVINGS_SAMPLE_TIMEOUT:
                    proc.kill()
                    proc.communicate()
                    return False, 'timed out'
    finally:
        CURRENT_PROCESS = None
    if stop_requested():
        return None, ''
    return proc.returncode == 0, ' | '.join((err or '').strip().splitlines()[-3:])


def estimate_savings(input_path, size_bytes, attempts):
    """Estimate how much smaller the file would get with the current settings.

    Encodes a few short clips spread through the file with the same FFmpeg command a real transcode
    would use, measures the output rate, and scales it to the whole duration. Returns a dict with
    saved_pct / est_bytes / samples / method, 'stopped' if the user stopped or removed the item, or
    None when no estimate could be made (the file is then simply transcoded)."""
    duration = probe_duration(input_path)
    if not duration or duration < SAVINGS_MIN_DURATION or not size_bytes:
        return None
    clip = SAVINGS_SAMPLE_SECONDS
    starts = [max(0.0, duration * (i + 0.5) / SAVINGS_SAMPLES - clip / 2) for i in range(SAVINGS_SAMPLES)]
    candidates = [(l, c) for l, c in attempts if not l.endswith('audio copied unchanged')]
    chosen = None
    total_bytes, total_secs, done = 0, 0.0, 0
    last_error = ''
    for start in starts:
        fd, tmp = tempfile.mkstemp(prefix='transcoder_sample_', suffix='.mp4')
        os.close(fd)
        try:
            for label, cmd in ([chosen] if chosen else candidates):
                try:
                    i = cmd.index('-i')
                except ValueError:
                    continue
                sample_cmd = ([cmd[0], '-hide_banner', '-nostats', '-loglevel', 'error'] + cmd[1:i]
                              + ['-ss', f'{start:.2f}', '-t', str(clip)] + cmd[i:-1] + [tmp])
                ok, err = _run_sample(sample_cmd)
                if ok is None:
                    return 'stopped'
                out_size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
                if ok and out_size > 0:
                    chosen = (label, cmd)
                    total_bytes += out_size
                    total_secs += min(clip, probe_duration(tmp) or clip)
                    done += 1
                    break
                last_error = err
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    if done < 3 or total_secs <= 0:
        if last_error:
            add_log(f"Space-saving test: the sample clips failed ({html.escape(last_error)})", level='warning')
        return None
    est_bytes = total_bytes / total_secs * duration
    return {"saved_pct": round((1 - est_bytes / size_bytes) * 100, 1), "est_bytes": int(est_bytes),
            "samples": done, "method": chosen[0]}


def worker_thread():
    global QUEUE_RUNNING, CURRENT_INDEX, QUEUE_START_TIME, CURRENT_PROCESS, CURRENT_JOB
    QUEUE_RUNNING = True
    QUEUE_START_TIME = time.time()

    handled = set()  # ids of the jobs already dealt with in this run
    while True:
        if not QUEUE_RUNNING or CANCEL_EVENT.is_set():
            break
        job = next_job_to_run(handled)
        if job is None:
            break
        handled.add(job['id'])
        CURRENT_JOB = job

        job['status'] = 'Running'
        job['progress'] = 0
        job['start_time'] = time.time()
        job['duration_text'] = "0s"
        try:
            CURRENT_INDEX = JOBS.index(job) + 1
        except ValueError:
            pass
        input_path = job['path']
        
        base_ext = os.path.splitext(input_path)[1].lower()
        if base_ext == '.mp4':
            output_path = os.path.splitext(input_path)[0] + '_transcoded.mp4'
        else:
            output_path = os.path.splitext(input_path)[0] + '.mp4'
        
        add_log(f"Transcode starting: {job['filename']}", level='transcode')
        
        attempts = build_ffmpeg_attempts(input_path, output_path)

        # Optional space-saving test: skip files that wouldn't shrink enough
        job.pop('skip_reason', None)
        job.pop('estimate', None)
        min_save = float(CONFIG.get('min_savings_percent') or 0)
        # The space-saving test only applies to MP4 files (they are re-encoded in place); every other
        # type is always transcoded to MP4.
        if min_save > 0 and base_ext == '.mp4':
            job['phase'] = 'Estimating the space saving'
            try:
                est = estimate_savings(input_path, job.get('size_bytes', 0), attempts)
            except Exception as e:
                add_log(f"Space-saving test failed for {job['filename']}: {html.escape(str(e))}", level='warning')
                est = None
            job.pop('phase', None)
            if est == 'stopped' or stop_requested():
                est = None  # the stop / removal is handled below
            elif est is None:
                add_log(f"Space-saving test: no estimate for {job['filename']} (too short or the clips could not be encoded); transcoding it anyway.", level='info')
            else:
                job['estimate'] = est
                if est['saved_pct'] < min_save:
                    job['status'] = 'Skipped'
                    job['progress'] = 0
                    job['duration_text'] = format_duration(time.time() - job['start_time'])
                    if est['saved_pct'] < 0:
                        change = f"{abs(est['saved_pct']):.1f}% larger"
                        job['skip_reason'] = f"Estimated to be {abs(est['saved_pct']):.0f}% larger, not smaller (minimum saving {min_save:g}%)"
                    else:
                        change = f"{est['saved_pct']:.1f}% smaller"
                        job['skip_reason'] = f"Estimated saving {est['saved_pct']:.0f}% is below the {min_save:g}% minimum"
                    add_log(f"Skipped {job['filename']}: estimated to be {change} (about {format_size(est['est_bytes'])} instead of "
                            f"{job.get('size_human', '?')}), below the {min_save:g}% minimum saving. The file was left untouched.", level='warning')
                    CURRENT_PROCESS = None
                    CURRENT_JOB = None
                    continue
                add_log(f"Space-saving test: {job['filename']} should shrink by about {est['saved_pct']:.0f}% "
                        f"(to roughly {format_size(est['est_bytes'])}).", level='info')

        try:
            last_tail = []
            for attempt_no, (label, cmd) in enumerate(attempts):
                if job.get('remove_requested'):
                    break
                if attempt_no > 0:
                    # The previous attempt failed: clear its partial output and try the next method
                    job['progress'] = 0
                    try:
                        if os.path.exists(output_path):
                            os.remove(output_path)
                    except Exception:
                        pass
                    add_log(f"Retrying {job['filename']} with {label}", level='warning')
                elif len(attempts) > 1:
                    add_log(f"Encoding with {label}", level='info')

                duration_sec = None
                tail = []
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
                CURRENT_PROCESS = process

                # Watchdog for hardware attempts: abandon one that produces no video (or stops producing it)
                wd = {'frames': 0, 'last': time.time(), 'stalled': None, 'near_end': False}
                wd_done = threading.Event()
                if label.startswith(('QSV', 'VAAPI')):
                    def _watch(proc=process, wd=wd, done=wd_done):
                        while not done.wait(2):
                            if wd['frames'] > 0 and wd['near_end']:
                                continue  # finishing up: no more frames are expected
                            limit = HW_FIRST_FRAME_SECONDS if wd['frames'] == 0 else HW_STALL_SECONDS
                            idle = time.time() - wd['last']
                            if idle > limit:
                                wd['stalled'] = (f"no video frame was produced in {int(idle)}s" if wd['frames'] == 0
                                                 else f"video output stopped for {int(idle)}s")
                                try:
                                    proc.kill()
                                except Exception:
                                    pass
                                return
                    threading.Thread(target=_watch, daemon=True).start()

                for line in process.stdout:
                    if stop_requested():
                        process.terminate()
                        break

                    line_str = line.strip()
                    if line_str and not line_str.startswith('frame='):  # skip the running progress lines
                        tail.append(line_str)
                        del tail[:-4]

                    frame_match = re.search(r"frame=\s*(\d+)", line_str)
                    if frame_match and int(frame_match.group(1)) > wd['frames']:
                        wd['frames'] = int(frame_match.group(1))
                        wd['last'] = time.time()

                    if duration_sec is None:
                        dur_match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", line_str)
                        if dur_match:
                            h, m, s = map(float, dur_match.groups())
                            duration_sec = h * 3600 + m * 60 + s


                    time_match = re.search(r"time=\s*(\d+):(\d+):([\d.]+)", line_str)
                    if time_match and duration_sec and duration_sec > 0:
                        th, tm, ts = map(float, time_match.groups())
                        curr_sec = th * 3600 + tm * 60 + ts
                        pct = int((curr_sec / duration_sec) * 100)
                        wd['near_end'] = curr_sec >= duration_sec * 0.98
                        job['progress'] = min(max(pct, 0), 99)

                    job['duration_text'] = format_duration(time.time() - job['start_time'])


                process.wait()
                wd_done.set()
                CURRENT_PROCESS = None
                last_tail = list(tail)
                if wd['stalled'] and process.returncode != 0:
                    tail = [f"abandoned, {wd['stalled']}"]

                if stop_requested() or process.returncode == 0 or attempt_no == len(attempts) - 1:
                    break
                add_log(f"{html.escape(label)} failed: {html.escape(' | '.join(tail))}", level='warning')
            
            if job.get('remove_requested'):
                # The user removed this item while it was transcoding: delete the unfinished output
                # (the original file is untouched), drop the item and carry on with the next one.
                try:
                    if os.path.exists(output_path):
                        os.remove(output_path)
                    add_log(f"Removed from the queue: {job['filename']} (transcode stopped, incomplete file deleted)", level='warning')
                except Exception as e:
                    add_log(f"Removed from the queue: {job['filename']}, but could not delete the incomplete file: {html.escape(str(e))}", level='error')
                with JOBS_LOCK:
                    if job in JOBS:
                        JOBS.remove(job)
                CURRENT_PROCESS = None
                CURRENT_JOB = None
                continue

            if CANCEL_EVENT.is_set():
                # Stopped by user: remove the partial output, keep the source, requeue the job
                job['status'] = 'Pending'
                job['progress'] = 0
                job['duration_text'] = "-"
                try:
                    if os.path.exists(output_path):
                        os.remove(output_path)
                    add_log(f"Cancelled: removed incomplete file for {job['filename']}", level='warning')
                except Exception as e:
                    add_log(f"Cancelled: could not remove incomplete file for {job['filename']}: {str(e)}", level='error')
                break
            elif process.returncode == 0:
                verify_cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', output_path]
                verify_result = subprocess.run(verify_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                
                if verify_result.returncode == 0 and verify_result.stdout.strip():
                    job['status'] = 'Completed'
                    job['progress'] = 100
                    elapsed = time.time() - job['start_time']
                    job['duration_text'] = format_duration(elapsed)
                    try:
                        out_bytes = os.path.getsize(output_path)
                        job['output_size_bytes'] = out_bytes
                        job['output_size_human'] = format_size(out_bytes)
                    except Exception:
                        job['output_size_bytes'] = 0
                        job['output_size_human'] = "Unknown"
                    
                    jf_updates = []
                    arr_paths = []
                    try:
                        if base_ext == '.mp4':
                            os.replace(output_path, input_path)
                            jf_updates.append((input_path, 'Modified'))
                            arr_paths.append(input_path)
                            add_log(f"Complete & Verified: Replaced non-compliant MP4: {job['filename']}", level='success')
                        else:
                            jf_updates.append((output_path, 'Created'))
                            arr_paths.append(output_path)
                            os.remove(input_path)
                            jf_updates.append((input_path, 'Deleted'))
                            add_log(f"Complete & Verified: Transcoded and removed source: {job['filename']} (Duration: {job['duration_text']})", level='success')
                    except Exception as e:
                        add_log(f"Complete: Verified {job['filename']}, but failed file replacement/removal: {str(e)}", level='warning')

                    notify_jellyfin(jf_updates)
                    notify_arr(arr_paths)
                else:
                    job['status'] = 'Failed'
                    job['progress'] = 0
                    job['duration_text'] = format_duration(time.time() - job['start_time'])
                    add_log(f"Error: ffprobe verification failed for: {job['filename']}", level='error')
                    if os.path.exists(output_path):
                        try:
                            os.remove(output_path)
                        except Exception:
                            pass
            else:
                job['status'] = 'Failed'
                job['progress'] = 0
                job['duration_text'] = format_duration(time.time() - job['start_time'])
                add_log(f"Error: Failed transcoding: {job['filename']}" + (f" ({html.escape(' | '.join(last_tail))})" if last_tail else ""), level='error')
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except Exception:
                        pass
        except Exception as e:
            CURRENT_PROCESS = None
            job['status'] = 'Failed'
            job['progress'] = 0
            job['duration_text'] = format_duration(time.time() - job.get('start_time', time.time()))
            add_log(f"Exception during transcode of {job['filename']}: {str(e)}", level='error')
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception:
                    pass
            
        if job.get('remove_requested'):
            # Asked for too late to stop the transcode (it had just finished): just drop the entry
            with JOBS_LOCK:
                if job in JOBS:
                    JOBS.remove(job)
        CURRENT_JOB = None

    CURRENT_JOB = None
    QUEUE_RUNNING = False
    QUEUE_START_TIME = None
    CANCEL_EVENT.clear()


def start_queue_worker():
    """Start the queue worker unless one is already running. Returns True if a new worker was started.
    Shared by the Start Queue button and the Sonarr/Radarr webhook; the lock stops a burst of
    requests from launching two workers."""
    global WORKER_THREAD
    with QUEUE_LOCK:
        if WORKER_THREAD is not None and WORKER_THREAD.is_alive():
            if not CANCEL_EVENT.is_set():
                return False  # already running normally
            # A stop is still in progress: let that worker finish cleaning up first
            WORKER_THREAD.join(timeout=10)
            if WORKER_THREAD.is_alive():
                return False
        CANCEL_EVENT.clear()
        WORKER_THREAD = threading.Thread(target=worker_thread)
        WORKER_THREAD.daemon = True
        WORKER_THREAD.start()
        return True


@app.route('/api/queue/start', methods=['POST'])
def start_queue():
    global AUTO_START_PAUSED
    AUTO_START_PAUSED = False
    start_queue_worker()
    return jsonify({"status": "started"})


@app.route('/api/queue/stop', methods=['POST'])
def stop_queue():
    global QUEUE_RUNNING, AUTO_START_PAUSED
    AUTO_START_PAUSED = True
    CANCEL_EVENT.set()
    QUEUE_RUNNING = False
    proc = CURRENT_PROCESS
    if proc and proc.poll() is None:
        proc.terminate()
    add_log("Queue stopped by user request.", level='warning')
    return jsonify({"status": "stopped"})


@app.route('/api/queue/remove/<int:job_id>', methods=['POST'])
def remove_job(job_id):
    """Remove one item from the queue. If it is being transcoded right now, that transcode is stopped
    and its unfinished file deleted by the worker, and the queue carries on with the next item."""
    data = request.get_json(silent=True) or {}
    ensure_job_ids()
    with JOBS_LOCK:
        job = next((j for j in JOBS if j.get('id') == job_id), None)
        if job is None:
            return jsonify({"status": "not_found"}), 404
        worker_alive = WORKER_THREAD is not None and WORKER_THREAD.is_alive()
        if job['status'] == 'Running' and worker_alive:
            if not data.get('confirm'):
                return jsonify({"status": "needs_confirmation", "filename": job['filename']}), 409
            job['remove_requested'] = True
            proc = CURRENT_PROCESS
            if proc and proc.poll() is None:
                proc.terminate()
            return jsonify({"status": "stopping"})
        JOBS.remove(job)
    add_log(f"Removed from the queue: {html.escape(job['filename'])}", level='info')
    return jsonify({"status": "removed"})


@app.route('/api/queue/clear', methods=['POST'])
def clear_queue():
    global QUEUE_RUNNING, AUTO_START_PAUSED
    if QUEUE_RUNNING:
        # Stop the running job first; the worker deletes its partial output
        AUTO_START_PAUSED = True
        CANCEL_EVENT.set()
        QUEUE_RUNNING = False
        proc = CURRENT_PROCESS
        if proc and proc.poll() is None:
            proc.terminate()
    JOBS.clear()
    add_log("Queue cleared by user request.", level='warning')
    return jsonify({"status": "cleared"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
