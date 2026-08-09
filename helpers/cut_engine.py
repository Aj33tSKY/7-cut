"""Automatic cut engine: script alignment + filler/silence removal → EDL.

Three phases:
  1. Parse actor script into ordered beats, align clips via fuzzy matching
  2. Remove filler words and dead silence from selected clips
  3. Output EDL JSON with beat labels, emphasis words, and cut stats

Usage:
    python helpers/cut_engine.py <project_dir>
    python helpers/cut_engine.py <project_dir> --edit-dir /custom/edit
    python helpers/cut_engine.py <project_dir> --silence-threshold 0.5
    python helpers/cut_engine.py <project_dir> --pre-pad 0.05 --post-pad 0.08
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

VIDEO_EXTS = {".mp4", ".MP4", ".mov", ".MOV", ".mkv", ".MKV", ".avi", ".AVI", ".m4v"}

FILLER_WORDS = {
    "um", "uh", "uhm", "umm", "erm", "er", "ah", "hm", "hmm",
    "mm", "mmm", "mhm", "uh-huh",
}

PAD_MIN = 0.03
PAD_MAX = 0.20


# -------- Script parsing ------------------------------------------------------


def parse_script(script_path: Path) -> list[dict]:
    """Parse a script file into ordered beats.

    Detects structure automatically:
      - Numbered lines (1. / 1) / SCENE 1:) → split on those
      - Markdown headings (## HOOK) → split on headings
      - Plain paragraphs → split on double-newlines

    Returns list of {index, label, text}.
    """
    raw = script_path.read_text(encoding="utf-8").strip()
    if not raw:
        return []

    beats: list[dict] = []

    numbered_re = re.compile(
        r"^(?:(?:SCENE|BEAT|SECTION)\s*)?(\d+)[.):\s]+\s*(.*)",
        re.IGNORECASE,
    )
    heading_re = re.compile(r"^#{1,3}\s+(.+)")

    lines = raw.split("\n")
    numbered_hits = sum(1 for l in lines if numbered_re.match(l.strip()))
    heading_hits = sum(1 for l in lines if heading_re.match(l.strip()))

    if numbered_hits >= 2:
        current_label = ""
        current_lines: list[str] = []
        for line in lines:
            m = numbered_re.match(line.strip())
            if m:
                if current_lines:
                    beats.append({
                        "index": len(beats),
                        "label": current_label or f"BEAT_{len(beats)}",
                        "text": " ".join(current_lines).strip(),
                    })
                current_label = m.group(2).split(":", 1)[0].strip() if m.group(2) else ""
                rest = m.group(2).split(":", 1)[1].strip() if ":" in m.group(2) else m.group(2)
                current_lines = [rest] if rest else []
            else:
                current_lines.append(line.strip())
        if current_lines:
            beats.append({
                "index": len(beats),
                "label": current_label or f"BEAT_{len(beats)}",
                "text": " ".join(current_lines).strip(),
            })

    elif heading_hits >= 2:
        current_label = ""
        current_lines = []
        for line in lines:
            m = heading_re.match(line.strip())
            if m:
                if current_lines:
                    beats.append({
                        "index": len(beats),
                        "label": current_label or f"BEAT_{len(beats)}",
                        "text": " ".join(current_lines).strip(),
                    })
                current_label = m.group(1).strip()
                current_lines = []
            else:
                current_lines.append(line.strip())
        if current_lines:
            beats.append({
                "index": len(beats),
                "label": current_label or f"BEAT_{len(beats)}",
                "text": " ".join(current_lines).strip(),
            })

    else:
        paragraphs = re.split(r"\n\s*\n", raw)
        if len(paragraphs) >= 2:
            for i, para in enumerate(paragraphs):
                text = " ".join(para.split()).strip()
                if text:
                    beats.append({
                        "index": len(beats),
                        "label": f"BEAT_{len(beats)}",
                        "text": text,
                    })
        else:
            single_lines = [l.strip() for l in raw.split("\n") if l.strip()]
            if len(single_lines) >= 2:
                for i, line in enumerate(single_lines):
                    beats.append({
                        "index": i,
                        "label": f"BEAT_{i}",
                        "text": line,
                    })
            elif single_lines:
                beats.append({
                    "index": 0,
                    "label": "BEAT_0",
                    "text": single_lines[0],
                })

    return [b for b in beats if b["text"]]


# -------- Transcript helpers --------------------------------------------------


def extract_words(transcript: dict) -> list[dict]:
    return [w for w in transcript.get("words", []) if w.get("type") == "word"]


def extract_full_text(transcript: dict) -> str:
    words = extract_words(transcript)
    return " ".join((w.get("text") or "").strip() for w in words)


def is_filler(word: dict) -> bool:
    text = (word.get("text") or "").strip().lower().rstrip(".,!?;:")
    return text in FILLER_WORDS


def count_fillers(transcript: dict) -> int:
    return sum(1 for w in extract_words(transcript) if is_filler(w))


# -------- Script alignment ----------------------------------------------------


def _normalize_word(text: str) -> str:
    return (text or "").strip().lower().rstrip(".,!?;:'\"")


def align_clips_to_script(
    transcripts: dict[str, dict],
    beats: list[dict],
) -> list[dict]:
    """Align clips to script for ordering and waffle trimming.

    The script is a *reference*, not a 1:1 map.  One clip may cover several
    script lines; one script line may span several clips.  The algorithm:

      1. Concatenate all beats into one token stream.
      2. For each clip, find where its words best match in that stream
         (sliding-window fuzzy match) → gives a *script position* for ordering.
      3. Within each clip, trim to the on-script word range so off-script
         waffle at the head/tail is dropped.
      4. Order clips by script position.

    Returns list of {source, beat_index, beat_label, start, end, similarity,
    words_in_range} ordered by script position.
    """
    if not beats:
        return _fallback_no_script(transcripts)

    full_script_tokens: list[str] = []
    full_script_raw: list[str] = []
    beat_boundaries: list[tuple[int, int, dict]] = []
    for beat in beats:
        raw = beat["text"].split()
        tokens = [_normalize_word(t) for t in raw]
        beat_boundaries.append(
            (len(full_script_tokens), len(full_script_tokens) + len(tokens), beat)
        )
        full_script_tokens.extend(tokens)
        full_script_raw.extend(raw)

    clip_data: dict[str, dict] = {}
    for name, tr in transcripts.items():
        words = extract_words(tr)
        if not words:
            continue
        clip_tokens = [_normalize_word(w.get("text", "")) for w in words]
        if not any(clip_tokens):
            continue

        pos, matched_len, ratio = _find_clip_script_position(
            full_script_tokens, clip_tokens,
        )

        if ratio < 0.15:
            print(f"  skip: {name} (ratio={ratio:.2f})")
            continue

        primary_beat = beats[-1]
        for bstart, bend, beat in beat_boundaries:
            if pos < bend:
                primary_beat = beat
                break

        clip_data[name] = {
            "words": words,
            "clip_tokens": clip_tokens,
            "script_pos": pos,
            "script_matched_len": matched_len,
            "ratio": ratio,
            "primary_beat": primary_beat,
        }

    ordered = sorted(clip_data.keys(), key=lambda n: (clip_data[n]["script_pos"], n))

    assignments: list[dict] = []
    for name in ordered:
        d = clip_data[name]
        words = d["words"]
        beat = d["primary_beat"]

        kept_words = _dedup_retakes(words, full_script_tokens, full_script_raw)

        orig_count = len(words)
        if len(kept_words) < orig_count:
            print(f"  retake dedup: {name} — {orig_count} → {len(kept_words)} words")

        assignments.append({
            "source": name,
            "beat_index": beat["index"],
            "beat_label": beat["label"],
            "start": kept_words[0].get("start", 0.0),
            "end": kept_words[-1].get("end", 0.0),
            "similarity": d["ratio"],
            "words_in_range": kept_words,
            "alternates": [],
        })

    return assignments


def _find_clip_script_position(
    script_tokens: list[str],
    clip_tokens: list[str],
) -> tuple[int, int, float]:
    """Find where in the concatenated script this clip best matches.

    Slides windows of the script over the clip (not the other way around),
    so long clips with retakes still match their short script section.

    Returns (script_position, matched_window_size, similarity_ratio).
    """
    n_script = len(script_tokens)
    n_clip = len(clip_tokens)

    if n_clip == 0 or n_script == 0:
        return 0, 0, 0.0

    clip_str = " ".join(clip_tokens)

    best_pos = 0
    best_wsize = n_script
    best_ratio = 0.0

    lo = max(1, min(n_clip, n_script) // 3)
    hi = min(n_script, n_clip * 2) + 1

    for wsize in range(lo, hi):
        step = max(1, wsize // 4)
        for i in range(0, n_script - wsize + 1, step):
            window_str = " ".join(script_tokens[i:i + wsize])
            ratio = difflib.SequenceMatcher(None, clip_str, window_str).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_pos = i
                best_wsize = wsize

    return best_pos, best_wsize, best_ratio


def _find_on_script_range(
    clip_tokens: list[str],
    script_section: list[str],
) -> tuple[int, int]:
    """Find the on-script word range within a clip.

    Compares clip tokens against the matching script section and returns
    (start_idx, end_idx) covering the first-to-last matched word.
    Off-script waffle at the head/tail is excluded.
    """
    if not clip_tokens or not script_section:
        return 0, len(clip_tokens)

    sm = difflib.SequenceMatcher(None, clip_tokens, script_section)
    blocks = sm.get_matching_blocks()

    matched = [i for b in blocks if b.size > 0 for i in range(b.a, b.a + b.size)]
    if not matched:
        return 0, len(clip_tokens)

    return min(matched), max(matched) + 1


def _dedup_retakes(
    words: list[dict],
    script_tokens: list[str],
    raw_script_tokens: list[str] | None = None,
) -> list[dict]:
    """Remove retakes from a clip, keeping only the best attempt of each script line.

    For single-take clips with retakes, the actor may attempt the same line
    multiple times.  This function:
      1. Splits the clip at large pauses into segments (natural take boundaries).
      2. For each script line (beat), finds the best-matching segment.
      3. Picks one segment per line, in chronological order, skipping
         earlier failed attempts.
      4. Filters out clapper words ("One", "one-", etc.).

    Uses word-level timestamps from ElevenLabs Scribe to identify take
    boundaries at pauses >= 0.4s.
    """
    if not words or not script_tokens:
        return words

    clip_tokens = [_normalize_word(w.get("text", "")) for w in words]
    clapper_words = {"one", "one-", "one..."}

    segments: list[tuple[int, int]] = []
    seg_start = 0
    for i in range(1, len(words)):
        gap = words[i].get("start", 0) - words[i - 1].get("end", 0)
        if gap >= 0.4:
            segments.append((seg_start, i))
            seg_start = i
    segments.append((seg_start, len(words)))

    segments = [
        (s, e) for s, e in segments
        if not all(clip_tokens[i] in clapper_words for i in range(s, e))
    ]

    if len(segments) <= 1:
        return words

    script_lines = _split_script_into_lines(script_tokens, raw_script_tokens)
    if not script_lines:
        return words

    merged_lines: list[list[str]] = []
    buf: list[str] = []
    for line in script_lines:
        buf.extend(line)
        if len(buf) >= 5:
            merged_lines.append(buf)
            buf = []
    if buf:
        if merged_lines:
            merged_lines[-1].extend(buf)
        else:
            merged_lines.append(buf)

    line_winners: list[tuple[int, int]] = []
    min_start = 0

    for line_tokens in merged_lines:
        line_str = " ".join(line_tokens)

        best_span: tuple[int, int] | None = None
        best_ratio = 0.0

        for si in range(len(segments)):
            if segments[si][0] < min_start:
                continue
            for sj in range(si, min(si + 4, len(segments))):
                span_start = segments[si][0]
                span_end = segments[sj][1]

                span_tokens = [
                    clip_tokens[i] for i in range(span_start, span_end)
                    if clip_tokens[i] not in clapper_words
                ]
                if not span_tokens:
                    continue

                span_str = " ".join(span_tokens)
                ratio = difflib.SequenceMatcher(
                    None, line_str, span_str
                ).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_span = (span_start, span_end)

        if best_span is not None and best_ratio >= 0.30:
            line_winners.append(best_span)
            min_start = best_span[1]

    if not line_winners:
        return words

    kept_indices: set[int] = set()
    for span_start, span_end in line_winners:
        for i in range(span_start, span_end):
            if clip_tokens[i] not in clapper_words:
                kept_indices.add(i)

    result = [words[i] for i in sorted(kept_indices)]
    return result if result else words


def _split_script_into_lines(
    script_tokens: list[str],
    raw_tokens: list[str] | None = None,
) -> list[list[str]]:
    """Split a flat token list into logical lines at sentence boundaries.

    Uses raw_tokens (pre-normalization) to detect punctuation boundaries,
    but returns the normalized tokens for matching.
    """
    raw = raw_tokens if raw_tokens else script_tokens
    lines: list[list[str]] = []
    current: list[str] = []
    for i, token in enumerate(script_tokens):
        current.append(token)
        raw_t = raw[i] if i < len(raw) else token
        if raw_t.rstrip().endswith((".")) or raw_t.rstrip().endswith("?") or raw_t.rstrip().endswith("!"):
            if len(current) >= 2:
                lines.append(current)
                current = []
    if current and len(current) >= 2:
        lines.append(current)
    return lines if lines else [script_tokens]


def _fallback_no_script(transcripts: dict[str, dict]) -> list[dict]:
    """When no script is available, use all clips in filename order as one continuous take."""
    assignments = []
    for i, (name, tr) in enumerate(sorted(transcripts.items())):
        words = extract_words(tr)
        if not words:
            continue
        assignments.append({
            "source": name,
            "beat_index": i,
            "beat_label": f"CLIP_{i}",
            "start": words[0].get("start", 0.0),
            "end": words[-1].get("end", 0.0),
            "similarity": 1.0,
            "words_in_range": words,
            "alternates": [],
        })
    return assignments


# -------- Filler & dead space removal -----------------------------------------


def build_keep_ranges(
    words: list[dict],
    all_word_entries: list[dict],
    source: str,
    beat_index: int,
    beat_label: str,
    silence_threshold: float,
    pre_pad: float,
    post_pad: float,
    min_segment: float,
) -> list[dict]:
    """Build keep-ranges from a word list, removing fillers and dead space.

    Returns list of {source, start, end, beat, quote, words, emphasis_words}.
    """
    silence_map = _build_silence_map(all_word_entries, silence_threshold)

    kept_runs: list[list[dict]] = []
    current_run: list[dict] = []

    for w in words:
        if is_filler(w):
            if current_run:
                kept_runs.append(current_run)
                current_run = []
            continue

        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue

        if current_run:
            prev_end = current_run[-1].get("end", 0.0)
            gap = ws - prev_end
            if gap >= silence_threshold or _in_silence(prev_end, ws, silence_map):
                kept_runs.append(current_run)
                current_run = []

        current_run.append(w)

    if current_run:
        kept_runs.append(current_run)

    avg_word_dur = _avg_word_duration(words)

    ranges: list[dict] = []
    for run in kept_runs:
        if not run:
            continue

        raw_start = run[0].get("start", 0.0)
        raw_end = run[-1].get("end", 0.0)
        duration = raw_end - raw_start

        if duration < min_segment:
            continue

        padded_start = raw_start - max(PAD_MIN, min(PAD_MAX, pre_pad))
        padded_end = raw_end + max(PAD_MIN, min(PAD_MAX, post_pad))
        padded_start = max(0.0, padded_start)

        quote = " ".join((w.get("text") or "").strip() for w in run[:8])
        if len(run) > 8:
            quote += " ..."

        emphasis = _score_emphasis(run, avg_word_dur)

        ranges.append({
            "source": source,
            "start": round(padded_start, 3),
            "end": round(padded_end, 3),
            "beat": beat_label,
            "quote": quote,
            "reason": f"auto-cut: beat {beat_index}",
            "_words": run,
            "emphasis_words": emphasis,
        })

    ranges = _merge_micro_gaps(ranges, merge_threshold=0.1)
    return ranges


def _build_silence_map(
    all_entries: list[dict], threshold: float,
) -> list[tuple[float, float]]:
    """Extract silence gaps from spacing entries."""
    gaps: list[tuple[float, float]] = []
    for w in all_entries:
        if w.get("type") != "spacing":
            continue
        s = w.get("start")
        e = w.get("end")
        if s is not None and e is not None and (e - s) >= threshold:
            gaps.append((s, e))
    return gaps


def _in_silence(
    t_start: float, t_end: float, silence_map: list[tuple[float, float]],
) -> bool:
    for gs, ge in silence_map:
        if gs <= t_start and ge >= t_end:
            return True
        if gs < t_end and ge > t_start:
            overlap = min(ge, t_end) - max(gs, t_start)
            if overlap > (t_end - t_start) * 0.5:
                return True
    return False


def _avg_word_duration(words: list[dict]) -> float:
    durations = []
    for w in words:
        if is_filler(w):
            continue
        s = w.get("start")
        e = w.get("end")
        if s is not None and e is not None:
            d = e - s
            if d > 0:
                durations.append(d)
    return sum(durations) / len(durations) if durations else 0.3


def _score_emphasis(words: list[dict], avg_dur: float, top_n: int = 3) -> list[dict]:
    """Score words for emphasis. Returns top-N emphasis words."""
    scored: list[tuple[dict, float]] = []
    prev_end: float | None = None

    for w in words:
        s = w.get("start")
        e = w.get("end")
        text = (w.get("text") or "").strip()
        if not text or s is None or e is None:
            continue

        dur = e - s
        if avg_dur > 0:
            score = dur / avg_dur
        else:
            score = 1.0

        if prev_end is not None and (s - prev_end) > 0.3:
            score += 0.5

        if len(text) > 5:
            score += 0.2

        scored.append((w, score))
        prev_end = e

    scored.sort(key=lambda x: x[1], reverse=True)

    return [
        {
            "text": (w.get("text") or "").strip(),
            "start": w.get("start"),
            "end": w.get("end"),
            "score": round(score, 2),
        }
        for w, score in scored[:top_n]
    ]


def _merge_micro_gaps(ranges: list[dict], merge_threshold: float = 0.1) -> list[dict]:
    if len(ranges) <= 1:
        return ranges

    merged: list[dict] = [ranges[0]]
    for r in ranges[1:]:
        prev = merged[-1]
        if (r["source"] == prev["source"]
                and r["start"] - prev["end"] < merge_threshold):
            prev["end"] = r["end"]
            prev["quote"] = prev["quote"].rstrip(" .") + " ... " + r["quote"]
            prev["emphasis_words"] = (
                prev.get("emphasis_words", []) + r.get("emphasis_words", [])
            )[:5]
            prev["_words"] = prev.get("_words", []) + r.get("_words", [])
        else:
            merged.append(r)

    return merged


# -------- EDL assembly --------------------------------------------------------


def build_edl(
    project_dir: Path,
    edit_dir: Path,
    silence_threshold: float,
    pre_pad: float,
    post_pad: float,
    min_segment: float,
) -> dict:
    """Full pipeline: parse script → align → cut → EDL."""
    transcripts_dir = edit_dir / "transcripts"
    if not transcripts_dir.is_dir():
        sys.exit(f"no transcripts directory at {transcripts_dir}")

    json_files = sorted(transcripts_dir.glob("*.json"))
    if not json_files:
        sys.exit(f"no transcript files in {transcripts_dir}")

    transcripts: dict[str, dict] = {}
    for jf in json_files:
        transcripts[jf.stem] = json.loads(jf.read_text())

    script_path = _find_script(project_dir)
    beats = parse_script(script_path) if script_path else []

    if script_path:
        print(f"script: {script_path.name} ({len(beats)} beats)")
    else:
        print("no script found — using clips in filename order")

    assignments = align_clips_to_script(transcripts, beats)

    if not assignments:
        sys.exit("no clips could be aligned to the script")

    print(f"aligned {len(assignments)} beat(s) across {len(set(a['source'] for a in assignments))} clip(s)")

    sources: dict[str, str] = {}
    for name in transcripts:
        video_path = _find_source_video(project_dir, name)
        if video_path:
            sources[name] = str(video_path)

    all_ranges: list[dict] = []
    total_fillers = 0
    total_silence_removed = 0.0
    original_duration = 0.0

    for assignment in assignments:
        src = assignment["source"]
        words_in_beat = assignment.get("words_in_range", [])

        all_entries = transcripts[src].get("words", [])
        beat_entries = [w for w in all_entries
                        if w.get("start") is not None
                        and w.get("start") >= assignment["start"]
                        and (w.get("end") or w.get("start", 0)) <= assignment["end"] + 0.5]

        fillers_in_beat = sum(1 for w in words_in_beat if is_filler(w))
        total_fillers += fillers_in_beat

        beat_dur = assignment["end"] - assignment["start"]
        original_duration += beat_dur

        ranges = build_keep_ranges(
            words=words_in_beat,
            all_word_entries=beat_entries,
            source=src,
            beat_index=assignment["beat_index"],
            beat_label=assignment["beat_label"],
            silence_threshold=silence_threshold,
            pre_pad=pre_pad,
            post_pad=post_pad,
            min_segment=min_segment,
        )

        kept_dur = sum(r["end"] - r["start"] for r in ranges)
        removed_dur = beat_dur - kept_dur
        total_silence_removed += max(0, removed_dur)

        all_ranges.extend(ranges)

    output_offset = 0.0
    for r in all_ranges:
        seg_dur = r["end"] - r["start"]
        for ew in r.get("emphasis_words", []):
            if ew.get("start") is not None:
                ew["output_start"] = round(ew["start"] - r["start"] + output_offset, 3)
            if ew.get("end") is not None:
                ew["output_end"] = round(ew["end"] - r["start"] + output_offset, 3)
        output_offset += seg_dur

    trimmed_duration = sum(r["end"] - r["start"] for r in all_ranges)

    clean_ranges = []
    for r in all_ranges:
        cr = {k: v for k, v in r.items() if k != "_words"}
        clean_ranges.append(cr)

    script_alignment = {}
    for a in assignments:
        script_alignment[a["beat_label"]] = {
            "clip": a["source"],
            "similarity": round(a["similarity"], 3),
            "alternates": a.get("alternates", []),
        }

    edl = {
        "version": 1,
        "sources": sources,
        "ranges": clean_ranges,
        "grade": "auto",
        "total_duration_s": round(trimmed_duration, 2),
        "script_alignment": script_alignment,
        "stats": {
            "cuts_made": len(clean_ranges),
            "fillers_removed": total_fillers,
            "silence_removed_s": round(total_silence_removed, 2),
            "original_duration_s": round(original_duration, 2),
            "trimmed_duration_s": round(trimmed_duration, 2),
        },
    }

    return edl


def _find_script(project_dir: Path) -> Path | None:
    for name in ["script.md", "script.txt", "Script.md", "Script.txt", "SCRIPT.md"]:
        p = project_dir / name
        if p.exists():
            return p
    return None


def _find_source_video(project_dir: Path, stem: str) -> Path | None:
    for ext in VIDEO_EXTS:
        p = project_dir / f"{stem}{ext}"
        if p.exists():
            return p
    return None


# -------- CLI -----------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Automatic cut engine: script alignment + filler/silence removal → EDL"
    )
    ap.add_argument("project_dir", type=Path, help="Project directory with clips + script.md")
    ap.add_argument("--edit-dir", type=Path, default=None,
                    help="Edit output directory (default: <project_dir>/edit)")
    ap.add_argument("--silence-threshold", type=float, default=0.4,
                    help="Minimum silence gap to cut (seconds). Default 0.4")
    ap.add_argument("--pre-pad", type=float, default=0.05,
                    help="Padding before first kept word (seconds). Default 0.05")
    ap.add_argument("--post-pad", type=float, default=0.08,
                    help="Padding after last kept word (seconds). Default 0.08")
    ap.add_argument("--min-segment", type=float, default=0.3,
                    help="Drop segments shorter than this (seconds). Default 0.3")
    args = ap.parse_args()

    project_dir = args.project_dir.resolve()
    if not project_dir.is_dir():
        sys.exit(f"not a directory: {project_dir}")

    edit_dir = (args.edit_dir or (project_dir / "edit")).resolve()

    edl = build_edl(
        project_dir=project_dir,
        edit_dir=edit_dir,
        silence_threshold=args.silence_threshold,
        pre_pad=args.pre_pad,
        post_pad=args.post_pad,
        min_segment=args.min_segment,
    )

    out_path = edit_dir / "edl.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(edl, indent=2))

    stats = edl["stats"]
    print(f"\nedl → {out_path}")
    print(f"  {stats['cuts_made']} segments")
    print(f"  {stats['fillers_removed']} fillers removed")
    print(f"  {stats['silence_removed_s']:.1f}s silence removed")
    print(f"  {stats['original_duration_s']:.1f}s → {stats['trimmed_duration_s']:.1f}s "
          f"({stats['original_duration_s'] - stats['trimmed_duration_s']:.1f}s cut)")


if __name__ == "__main__":
    main()
