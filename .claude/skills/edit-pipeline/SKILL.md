---
name: edit-pipeline
description: One-command automated video editing — script-aligned clip ordering, filler/silence removal, retake deduplication, word-by-word animated captions with style presets + overlay transitions (HyperFrames), render to final.mp4. Visual review via HyperFrames Studio.
---

# Edit Pipeline

Automated video editing pipeline. Takes a project folder with raw clips + `script.md`, produces `edit/final.mp4` with script-ordered cuts, filler removal, animated captions, overlay transitions, color grading, and loudness normalization.

Target: vertical 9:16 (1080×1920) for TikTok/Reels/Shorts.

## Input

A project folder with raw clips + `script.md`:

```
project-name/
├── clip_001.mp4        ← raw footage (any order, any number, any format)
├── clip_002.mp4
├── take_b.mov
└── script.md           ← actor script (paragraphs, numbered beats, or markdown headings)
```

**Script format:** flexible — plain paragraphs, numbered scenes, or beat-labeled sections. The pipeline auto-detects structure:
- Numbered/labeled sections (`1.`, `## HOOK`, `SCENE 1:`) → split on those markers
- Plain paragraphs → split on double-newlines
- Each section becomes a "beat" in script order

**Clips can span multiple script sections** — a single continuous take might cover multiple beats. The pipeline handles this with fuzzy transcript-to-script alignment.

**Single-clip retakes** — if one clip contains multiple attempts of the same lines (actor mis-speaks and retries), the cut engine deduplicates automatically, keeping only the best take of each line.

## One-command run

```bash
python helpers/pipeline.py <project_dir>
```

### All options

| Flag | Default | Description |
|---|---|---|
| `--edit-dir DIR` | `<project_dir>/edit` | Output directory |
| `--preview` | off | Fast 1080p CRF 22 render for QC |
| `--grade MODE` | `auto` | Color grade: `auto`, `warm_cinematic`, `neutral_punch`, `none` |
| `--accent-color COLOR` | `#FF5A00` | Caption active-word highlight color |
| `--caption-style STYLE` | `bold-outline` | Caption font/look preset (see below) |
| `--caption-mode MODE` | `phrase` | `phrase` (karaoke groups) or `word` (one at a time) |
| `--transition TRANS` | `film-burn` | Overlay transition at clip boundaries (see below) |
| `--silence-threshold N` | `0.4` | Silence gap threshold for cuts (seconds) |
| `--pre-pad N` | `0.05` | Padding before kept words (seconds) |
| `--post-pad N` | `0.08` | Padding after kept words (seconds) |
| `--no-captions` | off | Skip caption generation entirely |
| `--no-loudnorm` | off | Skip audio loudness normalization |
| `--studio` | off | Open HyperFrames Studio for visual review before render |
| `--language CODE` | auto | ISO language code for transcription (e.g. `en`) |
| `--num-speakers N` | auto | Number of speakers (improves diarization) |

**Output:** `<project_dir>/edit/final.mp4` (or `preview.mp4` with `--preview`)

### Caption style presets

| Name | Look | Font |
|---|---|---|
| `clean` | Subtle shadow, no outline | Helvetica Neue |
| `bold-outline` | Thick black stroke, CapCut style | Montserrat 900 |
| `pop` | Bouncy, playful, thick outline | Bangers |
| `neon` | Glowing text | Bebas Neue |
| `handwritten` | Casual marker style | Permanent Marker |

### Caption modes

- **`phrase`** — groups of 3 words shown together with karaoke highlighting (upcoming → active → spoken). Active word pops in accent color; spoken words turn white.
- **`word`** — one word at a time, larger font, pop-in animation. Clean TikTok/Reels look.

### Transition presets

All transitions are rendered as transparent overlays in the caption layer — they don't change video duration or cause sync issues.

| Name | Effect |
|---|---|
| `flash` | Quick white flash |
| `swipe` | Colored gradient swipe across screen |
| `film-burn` | Multi-layer radial orange film burn |
| `glare` | Diagonal light streak sweep |
| `glitch` | RGB split + horizontal displacement |
| `warp` | Expanding ring shockwave |
| `stripe-wipe` | Alternating horizontal stripe wipe |
| `ink` | Expanding ink blot |
| `none` | No transition overlay |

## Pipeline steps

The pipeline runs 5 steps in sequence:

1. **Transcribe** — ElevenLabs Scribe on every clip (word-level timestamps, speaker diarization). Cached: won't re-transcribe existing clips.
2. **Pack** — Transcripts → `takes_packed.md` (phrase-level reading view).
3. **Cut engine** — Script alignment + filler removal + retake deduplication → `edl.json`.
4. **Captions** — HyperFrames composition with animated karaoke captions + transition overlays → ProRes 4444 MOV with alpha transparency.
5. **Render** — Per-segment extract → concat → overlay composite → loudness normalization → `final.mp4`.

## Visual review flow

1. `python helpers/pipeline.py <project_dir> --studio`
2. HyperFrames Studio opens in browser — scrub timeline, review captions live
3. User describes changes → edit `edit/animations/captions/index.html` → Studio hot-reloads
4. Read Studio state: `npx hyperframes preview --context --json` (from captions dir)
5. Read selected element: `npx hyperframes preview --selection --json`
6. When ready, render manually:
   ```bash
   npx hyperframes render edit/animations/captions --format png-sequence -o edit/animations/captions/_alpha_frames
   # then encode to ProRes and render final
   python helpers/render.py edit/edl.json -o edit/final.mp4
   ```

## Manual steps

Run each component individually:

```bash
# 1. Transcribe (cached — won't re-upload existing clips)
python helpers/transcribe_batch.py <project_dir>

# 2. Pack transcripts
python helpers/pack_transcripts.py --edit-dir <project_dir>/edit

# 3. Cut engine (script alignment + filler removal → edl.json)
python helpers/cut_engine.py <project_dir>

# 4. Captions (HyperFrames composition + render to ProRes MOV)
python helpers/captions_hf.py edit/edl.json --render \
    --caption-style bold-outline \
    --caption-mode phrase \
    --transition film-burn

# 5. Final render
python helpers/render.py edit/edl.json -o edit/final.mp4
```

## Components

| Component | File | What it does |
|---|---|---|
| Transcribe | `helpers/transcribe.py` | Single-file ElevenLabs Scribe call (word-level, cached) |
| Transcribe Batch | `helpers/transcribe_batch.py` | Parallel transcription of all clips in a directory |
| Pack | `helpers/pack_transcripts.py` | Transcripts → phrase-level markdown reading view |
| Cut Engine | `helpers/cut_engine.py` | Script alignment + filler/silence removal + retake dedup → `edl.json` |
| Caption Generator | `helpers/captions_hf.py` | EDL + transcripts → HyperFrames HTML → ProRes 4444 MOV overlay |
| Renderer | `helpers/render.py` | EDL → per-segment extract → concat → overlay composite → final.mp4 |
| Pipeline Runner | `helpers/pipeline.py` | Orchestrates all of the above in one command |

## Cut engine details

The cut engine (`helpers/cut_engine.py`) does three things:

1. **Script alignment** — Uses `difflib.SequenceMatcher` to fuzzy-match each clip's transcript against the script. Orders clips by their position in the script. Handles clips that span multiple beats.

2. **Filler/silence removal** — Tags filler words (`um`, `uh`, `erm`, etc.) and dead space (silence ≥ threshold). Builds keep-ranges of clean speech with configurable padding.

3. **Retake deduplication** — For single-clip scenarios with multiple attempts: splits at pauses, matches segments to script lines, keeps only the best take of each line in chronological order. Filters clapper words ("one", "one...").

**CLI:**
```bash
python helpers/cut_engine.py <project_dir> \
    --edit-dir DIR \
    --silence-threshold 0.4 \
    --pre-pad 0.05 \
    --post-pad 0.08 \
    --min-segment 0.3
```

## Caption generator details

The caption generator (`helpers/captions_hf.py`) produces a HyperFrames HTML composition with:

- **Word-by-word karaoke animation** — 3-state system: upcoming (dimmed), active (accent color + scale pop), spoken (white)
- **Style presets** — font, weight, stroke, shadow, letter-spacing from preset library
- **Overlay transitions** — visual effects at clip boundaries (film burn, glitch, glare, etc.)
- **ProRes 4444 output** — PNG-sequence render → ffmpeg encode with `yuva444p10le` for alpha transparency

**CLI:**
```bash
python helpers/captions_hf.py <edl.json> --render \
    --accent-color "#FF5A00" \
    --font-size 64 \
    --words-per-group 3 \
    --caption-style bold-outline \
    --caption-mode phrase \
    --transition film-burn
```

## EDL adjustments

After the cut engine produces `edl.json`, you can manually adjust:
- **Re-include a filler:** add a range covering the word's timestamps
- **Swap a take:** change the `source` field to a different clip
- **Adjust timing:** shift `start`/`end` values (snap to word boundaries from transcripts)
- **Change grade:** set the `grade` field (`auto`, `warm_cinematic`, `neutral_punch`, `none`, or raw ffmpeg filter)
- Then re-run render: `python helpers/render.py edit/edl.json -o edit/final.mp4`

## Output directory structure

```
<project_dir>/edit/
├── transcripts/*.json       ← cached Scribe word-level transcripts
├── takes_packed.md          ← phrase-level reading view
├── edl.json                 ← cut decisions + overlay references
├── animations/captions/
│   ├── index.html           ← HyperFrames composition
│   ├── hyperframes.json     ← HyperFrames config
│   └── render.mov           ← ProRes 4444 caption overlay (alpha)
├── clips_preview/           ← per-segment extracted clips (preview mode)
├── clips_graded/            ← per-segment extracted clips (final mode)
├── preview.mp4              ← fast QC render
└── final.mp4                ← production render
```

## Requirements

- Python 3.10+ with `requests` (for ElevenLabs API)
- `ffmpeg` + `ffprobe` on PATH
- Node.js 22+ with npm (for HyperFrames)
- ElevenLabs API key in `.env` at repo root (`ELEVENLABS_API_KEY=...`)
