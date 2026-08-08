"""End-to-end video editing pipeline.

Orchestrates: transcribe → pack → cut (script-aligned) → captions → render.

Input: a project folder containing raw clips + script.md
Output: <project_dir>/edit/final.mp4

Usage:
    python helpers/pipeline.py <project_dir>
    python helpers/pipeline.py <project_dir> --preview
    python helpers/pipeline.py <project_dir> --studio
    python helpers/pipeline.py <project_dir> --no-captions --grade warm_cinematic
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HELPERS_DIR = Path(__file__).resolve().parent


def run_step(label: str, cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    t0 = time.time()
    result = subprocess.run(cmd, check=check)
    dt = time.time() - t0
    print(f"  [{dt:.1f}s]")
    return result


def run_python(script: str, args: list[str], label: str) -> None:
    cmd = [sys.executable, str(HELPERS_DIR / script)] + args
    run_step(label, cmd)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="End-to-end video editing pipeline: transcribe → cut → captions → render"
    )
    ap.add_argument("project_dir", type=Path, help="Project directory with clips + script.md")
    ap.add_argument("--edit-dir", type=Path, default=None,
                    help="Edit output directory (default: <project_dir>/edit)")
    ap.add_argument("--grade", type=str, default="auto",
                    help="Color grade: auto, warm_cinematic, neutral_punch, none. Default: auto")
    ap.add_argument("--accent-color", type=str, default="#FF5A00",
                    help="Caption active-word color. Default: #FF5A00")
    ap.add_argument("--silence-threshold", type=float, default=0.4,
                    help="Silence gap threshold for cuts (seconds). Default: 0.4")
    ap.add_argument("--pre-pad", type=float, default=0.05,
                    help="Padding before kept words (seconds). Default: 0.05")
    ap.add_argument("--post-pad", type=float, default=0.08,
                    help="Padding after kept words (seconds). Default: 0.08")
    ap.add_argument("--preview", action="store_true",
                    help="Preview mode: 1080p medium CRF 22 — faster, evaluable for QC")
    ap.add_argument("--caption-style", type=str, default="bold-outline",
                    help="Caption style: clean, bold-outline, pop, neon, handwritten. Default: bold-outline")
    ap.add_argument("--caption-mode", type=str, default="phrase",
                    choices=["phrase", "word"],
                    help="Caption mode: phrase (karaoke groups) or word (one at a time). Default: phrase")
    ap.add_argument("--transition", type=str, default="film-burn",
                    help="Transition preset: flash, swipe, film-burn, glare, glitch, warp, stripe-wipe, ink, none. Default: film-burn")
    ap.add_argument("--no-captions", action="store_true",
                    help="Skip HyperFrames caption generation")
    ap.add_argument("--no-loudnorm", action="store_true",
                    help="Skip audio loudness normalization")
    ap.add_argument("--studio", action="store_true",
                    help="Open HyperFrames Studio for visual caption review before render")
    ap.add_argument("--language", type=str, default=None,
                    help="ISO language code for transcription (e.g. 'en'). Auto-detect if omitted.")
    ap.add_argument("--num-speakers", type=int, default=None,
                    help="Number of speakers (improves diarization accuracy)")
    args = ap.parse_args()

    project_dir = args.project_dir.resolve()
    if not project_dir.is_dir():
        sys.exit(f"not a directory: {project_dir}")

    edit_dir = (args.edit_dir or (project_dir / "edit")).resolve()
    edit_dir.mkdir(parents=True, exist_ok=True)

    t_total = time.time()

    # ---- Step 1: Transcribe ----
    transcribe_args = [str(project_dir), "--edit-dir", str(edit_dir)]
    if args.language:
        transcribe_args += ["--language", args.language]
    if args.num_speakers:
        transcribe_args += ["--num-speakers", str(args.num_speakers)]
    run_python("transcribe_batch.py", transcribe_args, "STEP 1/5 — Transcribe")

    # ---- Step 2: Pack transcripts ----
    run_python(
        "pack_transcripts.py",
        ["--edit-dir", str(edit_dir)],
        "STEP 2/5 — Pack transcripts",
    )

    # ---- Step 3: Cut engine (script alignment + filler removal) ----
    cut_args = [
        str(project_dir),
        "--edit-dir", str(edit_dir),
        "--silence-threshold", str(args.silence_threshold),
        "--pre-pad", str(args.pre_pad),
        "--post-pad", str(args.post_pad),
    ]
    run_python("cut_engine.py", cut_args, "STEP 3/5 — Cut engine")

    edl_path = edit_dir / "edl.json"
    if not edl_path.exists():
        sys.exit("cut engine did not produce edl.json")

    edl = json.loads(edl_path.read_text())
    if args.grade != "auto":
        edl["grade"] = args.grade
        edl_path.write_text(json.dumps(edl, indent=2))

    # ---- Step 4: Captions (HyperFrames) ----
    if not args.no_captions:
        caption_args = [
            str(edl_path),
            "--accent-color", args.accent_color,
            "--caption-style", args.caption_style,
            "--caption-mode", args.caption_mode,
            "--transition", args.transition,
        ]
        if args.studio:
            caption_args.append("--studio")
        elif not args.studio:
            caption_args.append("--render")

        run_python("captions_hf.py", caption_args, "STEP 4/5 — Captions (HyperFrames)")

        if args.studio:
            print("\n" + "=" * 60)
            print("  HyperFrames Studio is open — review captions in your browser.")
            print("  When ready, run the render step manually:")
            print(f"    npx hyperframes render {edit_dir}/animations/captions --format webm -o {edit_dir}/animations/captions/render.webm")
            print(f"    python {HELPERS_DIR}/render.py {edl_path} -o {edit_dir}/{'preview.mp4' if args.preview else 'final.mp4'}")
            print("=" * 60)
            return
    else:
        print("\n" + "=" * 60)
        print("  STEP 4/5 — Captions (skipped)")
        print("=" * 60)

    # ---- Step 5: Render ----
    edl = json.loads(edl_path.read_text())

    out_name = "preview.mp4" if args.preview else "final.mp4"
    out_path = edit_dir / out_name

    render_args = [str(edl_path), "-o", str(out_path)]
    if args.preview:
        render_args.append("--preview")
    if args.no_loudnorm:
        render_args.append("--no-loudnorm")
    if args.no_captions:
        render_args.append("--no-subtitles")

    run_python("render.py", render_args, "STEP 5/5 — Render")

    # ---- Report ----
    dt_total = time.time() - t_total
    stats = edl.get("stats", {})

    print("\n" + "=" * 60)
    print("  DONE")
    print("=" * 60)
    print(f"  output: {out_path}")
    if out_path.exists():
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"  size: {size_mb:.1f} MB")
    print(f"  segments: {stats.get('cuts_made', '?')}")
    print(f"  fillers removed: {stats.get('fillers_removed', '?')}")
    print(f"  silence removed: {stats.get('silence_removed_s', '?')}s")
    print(f"  duration: {stats.get('original_duration_s', '?')}s → {stats.get('trimmed_duration_s', '?')}s")
    print(f"  total time: {dt_total:.1f}s")


if __name__ == "__main__":
    main()
