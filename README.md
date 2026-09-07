# Pratham Transcription

Local Streamlit app that turns a sheet of Google Drive video links into **one transcript bundle per video**.

- Works with files shared as **Anyone with the link** — **no Google OAuth / credentials.json needed**
- Downloads **one source video at a time**, extracts audio, then immediately deletes the video
- OAuth downloads resume automatically after transient SSL/network failures
- Keeps only small temporary 16 kHz mono Opus audio (~32 kbps)
- Transcribes on Apple Silicon with **Parakeet MLX** (`parakeet-tdt-0.6b-v3`)
- Writes **TXT + SRT + VTT + JSON** for every video
- Resumes completed videos if you stop and restart
- Cost target: **₹0** (runs entirely on your Mac)

## Requirements

- Mac with Apple Silicon (M1/M2/M3/M4/M5…)
- Python **3.10+** (3.12 recommended; `parakeet-mlx` does not support 3.9)
- [FFmpeg](https://ffmpeg.org/) on `PATH` (`brew install ffmpeg`)
- Drive videos shared as **Anyone with the link** → Viewer

## One-time setup

```bash
cd pratham-transcription
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.env .env
```

### Sharing (required for default mode)

For each video in Google Drive:

1. Click **Share**
2. Set **General access** to **Anyone with the link**
3. Role: **Viewer**
4. Copy the link into your sheet

Optional: private-file mode needs a Desktop OAuth `credentials.json` (sidebar → Private files).

## Run the UI

```bash
source .venv/bin/activate
streamlit run app.py
# or: npm start
```

Then:

1. Leave **Drive access** on **Anyone with the link (no login)**
2. Upload a CSV/XLSX with one Google Drive URL per row
3. Click **Start transcription**
4. Watch live download percentage, GB transferred, speed, ETA, active stage,
   elapsed time, and per-video status on the dashboard
5. Stop anytime — completed videos are skipped and partial downloads resume
6. Download the ZIP when finished

## Sheet format

Only URLs are required. Example CSV:

```csv
url
https://drive.google.com/file/d/FILE_ID_1/view
https://drive.google.com/file/d/FILE_ID_2/view
```

Excel works the same — put links in any column; the first Drive-looking cell in each row is used.

## Outputs

```text
output/transcripts/
  my-video-title/
    my-video-title.txt
    my-video-title.srt
    my-video-title.vtt
    my-video-title.json
  manifest.json
  transcripts.zip
```

Temporary source video and audio files live under `.tmp/audio/`. A complete
source video is deleted immediately after extraction; audio is deleted after
its transcript bundle passes validation. Interrupted downloads retain a
`.part` file so the next run can resume instead of starting over.

## Speed / deadline notes

| Stage | What dominates |
| --- | --- |
| Download | Your internet + Drive throttling |
| Transcription | Local MLX (often 40–100× realtime on recent Mac Pros) |

For ~21–29 hours of clear English audio, ASR alone is typically tens of
minutes on an M5 Pro. **Wall-clock time is usually limited by Drive download
bandwidth**, not the model. OAuth mode intentionally downloads one video at a
time for reliability.

## Validate

```bash
npm run build
```

This checks required files, Python syntax, and Drive URL parsing.

## Troubleshooting

| Issue | Fix |
| --- | --- |
| HTML / access error | Share as **Anyone with the link → Viewer**, or use OAuth |
| Private files | Sidebar → **Private files (Google sign-in)** + `credentials.json` |
| FFmpeg failed | Ensure `ffmpeg` works; confirm the Drive file has an audio track |
| Empty transcript | Confirm the file is reachable and contains speech audio |
| Disk filling up | Stop the run and remove stale `.part` files under `.tmp/audio/source/` |

## Privacy

All processing stays on your machine. For public-link mode, no Google login is stored. Optional OAuth tokens live in `.token.json` (gitignored). No paid transcription API is required.

## Pitch Studio

A separate app in `pitch-studio/` that turns the five One Company / One Story axes into a script, PPTX deck, and asset pack. See [pitch-studio/README.md](pitch-studio/README.md).

```bash
source .venv/bin/activate
pip install -r pitch-studio/requirements.txt
cd pitch-studio
PYTHONPATH=. uvicorn backend.main:app --reload --port 8000
# in another terminal:
cd pitch-studio/frontend && npm install && npm run dev
```

Default login: `admin@example.com` / `changeme`. Set `OPENROUTER_API_KEY` before generating.
