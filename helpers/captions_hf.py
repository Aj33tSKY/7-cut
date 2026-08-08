"""HyperFrames caption generator: EDL + transcripts → animated karaoke captions.

Generates a HyperFrames HTML composition with word-by-word animated captions:
  - Active-word color highlights (karaoke 3-state: upcoming → active → spoken)
  - Scale pop on active words, extra glow on emphasis words
  - Phrase-level entrance/exit transitions
  - Dip-to-black overlays at clip boundaries
  - Renders to transparent WebM for compositing

Usage:
    python helpers/captions_hf.py <edl.json>
    python helpers/captions_hf.py <edl.json> --accent-color "#FF5A00"
    python helpers/captions_hf.py <edl.json> --words-per-group 4
    python helpers/captions_hf.py <edl.json> --render   (scaffold + render in one shot)
    python helpers/captions_hf.py <edl.json> --studio   (scaffold + open Studio for review)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


PUNCT_BREAK = set(".,!?;:")

DEFAULT_ACCENT = "#FF5A00"
DEFAULT_FONT_SIZE = 64
DEFAULT_WORDS_PER_GROUP = 3
DEFAULT_ZOOM_SCALE = 1.08
DEFAULT_CAPTION_MODE = "phrase"
DEFAULT_CAPTION_STYLE = "bold-outline"
DEFAULT_TRANSITION = "film-burn"


# -------- Caption style presets -----------------------------------------------

CAPTION_STYLES: dict[str, dict] = {
    "clean": {
        "label": "Clean — subtle shadow, no outline",
        "font_family": '"Helvetica Neue", Helvetica, Arial, sans-serif',
        "font_import": None,
        "font_weight": "800",
        "text_transform": "uppercase",
        "stroke": "none",
        "stroke_width": "0",
        "shadow": "0 2px 8px rgba(0,0,0,0.5)",
        "shadow_active": "0 0 20px {accent}80, 0 0 40px {accent}40, 0 2px 8px rgba(0,0,0,0.6)",
        "shadow_emphasis": "0 0 30px {accent}AA, 0 0 60px {accent}60, 0 2px 12px rgba(0,0,0,0.7)",
        "letter_spacing": "0.02em",
    },
    "bold-outline": {
        "label": "Bold Outline — thick stroke, CapCut style",
        "font_family": '"Montserrat", "Helvetica Neue", sans-serif',
        "font_import": "https://fonts.googleapis.com/css2?family=Montserrat:wght@900&display=swap",
        "font_weight": "900",
        "text_transform": "uppercase",
        "stroke": "black",
        "stroke_width": "3px",
        "shadow": "0 4px 12px rgba(0,0,0,0.7)",
        "shadow_active": "0 0 24px {accent}90, 0 4px 12px rgba(0,0,0,0.7)",
        "shadow_emphasis": "0 0 36px {accent}BB, 0 0 60px {accent}60, 0 4px 16px rgba(0,0,0,0.8)",
        "letter_spacing": "0.03em",
    },
    "pop": {
        "label": "Pop — bouncy, playful, thick outline",
        "font_family": '"Bangers", sans-serif',
        "font_import": "https://fonts.googleapis.com/css2?family=Bangers&display=swap",
        "font_weight": "400",
        "text_transform": "uppercase",
        "stroke": "black",
        "stroke_width": "4px",
        "shadow": "4px 4px 0 rgba(0,0,0,0.6)",
        "shadow_active": "4px 4px 0 {accent}AA, 0 0 20px {accent}60",
        "shadow_emphasis": "4px 4px 0 {accent}CC, 0 0 40px {accent}80",
        "letter_spacing": "0.04em",
    },
    "neon": {
        "label": "Neon — glowing text, dark background feel",
        "font_family": '"Bebas Neue", sans-serif',
        "font_import": "https://fonts.googleapis.com/css2?family=Bebas+Neue&display=swap",
        "font_weight": "400",
        "text_transform": "uppercase",
        "stroke": "none",
        "stroke_width": "0",
        "shadow": "0 0 10px rgba(255,255,255,0.5), 0 0 20px rgba(255,255,255,0.2)",
        "shadow_active": "0 0 15px {accent}, 0 0 30px {accent}80, 0 0 60px {accent}40",
        "shadow_emphasis": "0 0 20px {accent}, 0 0 40px {accent}AA, 0 0 80px {accent}60, 0 0 120px {accent}30",
        "letter_spacing": "0.06em",
    },
    "handwritten": {
        "label": "Handwritten — casual marker style",
        "font_family": '"Permanent Marker", cursive',
        "font_import": "https://fonts.googleapis.com/css2?family=Permanent+Marker&display=swap",
        "font_weight": "400",
        "text_transform": "none",
        "stroke": "black",
        "stroke_width": "2px",
        "shadow": "3px 3px 6px rgba(0,0,0,0.5)",
        "shadow_active": "0 0 20px {accent}80, 3px 3px 6px rgba(0,0,0,0.5)",
        "shadow_emphasis": "0 0 30px {accent}AA, 3px 3px 8px rgba(0,0,0,0.6)",
        "letter_spacing": "0.01em",
    },
}


# -------- Transition presets --------------------------------------------------


def _build_transition(
    name: str, boundaries: list[float], accent: str,
) -> tuple[str, str, str]:
    """Build (html, css, js) for a named transition preset.

    Returns three strings to embed in the composition.
    """
    builders = {
        "flash": _tr_flash,
        "swipe": _tr_swipe,
        "film-burn": _tr_film_burn,
        "glare": _tr_glare,
        "glitch": _tr_glitch,
        "warp": _tr_warp,
        "stripe-wipe": _tr_stripe_wipe,
        "ink": _tr_ink,
        "none": lambda b, a: ("", "", ""),
    }
    builder = builders.get(name, _tr_film_burn)
    return builder(boundaries, accent)


def _tr_flash(boundaries: list[float], accent: str) -> tuple[str, str, str]:
    css = """
    .tr-flash {
      position: absolute; top: 0; left: 0; width: 100%; height: 100%;
      background: white; opacity: 0; pointer-events: none; z-index: 50;
    }"""
    html_parts = []
    for i in range(len(boundaries)):
        html_parts.append(f'      <div id="flash-{i}" class="tr-flash"></div>')
    js_lines = []
    for i, bt in enumerate(boundaries):
        t = bt - 0.03
        js_lines.append(
            f'  tl.fromTo("#flash-{i}", {{opacity:0}}, {{opacity:0.85, duration:0.03, ease:"power2.in"}}, {t:.3f});'
            f'\n  tl.to("#flash-{i}", {{opacity:0, duration:0.15, ease:"power3.out"}}, {bt:.3f});'
        )
    return "\n".join(html_parts), css, "\n".join(js_lines)


def _tr_swipe(boundaries: list[float], accent: str) -> tuple[str, str, str]:
    html_parts = []
    css = f"""
    .tr-swipe {{
      position: absolute; top: 0; left: 0; width: 100%; height: 100%;
      background: linear-gradient(90deg, transparent 0%, {accent}40 35%, {accent}DD 48%, white 50%, {accent}DD 52%, {accent}40 65%, transparent 100%);
      transform: translateX(-120%); pointer-events: none; z-index: 50;
    }}"""
    for i in range(len(boundaries)):
        html_parts.append(f'      <div id="swipe-{i}" class="tr-swipe"></div>')
    js_lines = []
    for i, bt in enumerate(boundaries):
        t = bt - 0.06
        js_lines.append(
            f'  tl.fromTo("#swipe-{i}", {{x:"-120%"}}, {{x:"120%", duration:0.2, ease:"power3.inOut"}}, {t:.3f});'
        )
    return "\n".join(html_parts), css, "\n".join(js_lines)


def _tr_film_burn(boundaries: list[float], accent: str) -> tuple[str, str, str]:
    html_parts = []
    css = """
    .tr-burn {
      position: absolute; top: 0; left: 0; width: 100%; height: 100%;
      pointer-events: none; z-index: 50; opacity: 0;
    }
    .tr-burn-a {
      background: radial-gradient(ellipse at 70% 30%, rgba(255,160,40,0.9) 0%, rgba(255,80,0,0.4) 40%, transparent 70%);
    }
    .tr-burn-b {
      background: radial-gradient(ellipse at 30% 70%, rgba(255,200,60,0.7) 0%, rgba(255,120,0,0.3) 35%, transparent 65%);
    }
    .tr-burn-c {
      background: radial-gradient(ellipse at 50% 50%, rgba(255,255,200,0.6) 0%, transparent 50%);
    }"""
    for i in range(len(boundaries)):
        html_parts.append(
            f'      <div id="burn-a-{i}" class="tr-burn tr-burn-a"></div>\n'
            f'      <div id="burn-b-{i}" class="tr-burn tr-burn-b"></div>\n'
            f'      <div id="burn-c-{i}" class="tr-burn tr-burn-c"></div>'
        )
    js_lines = []
    for i, bt in enumerate(boundaries):
        t = bt - 0.12
        js_lines.append(
            f'  tl.fromTo("#burn-a-{i}", {{opacity:0, scale:1.1}}, {{opacity:1, scale:1.3, duration:0.15, ease:"power2.in"}}, {t:.3f});\n'
            f'  tl.to("#burn-a-{i}", {{opacity:0, duration:0.2, ease:"power2.out"}}, {t+0.15:.3f});\n'
            f'  tl.fromTo("#burn-b-{i}", {{opacity:0, scale:0.9}}, {{opacity:0.8, scale:1.2, duration:0.18, ease:"power1.in"}}, {t+0.03:.3f});\n'
            f'  tl.to("#burn-b-{i}", {{opacity:0, duration:0.22, ease:"power2.out"}}, {t+0.18:.3f});\n'
            f'  tl.fromTo("#burn-c-{i}", {{opacity:0}}, {{opacity:0.9, duration:0.08, ease:"power3.in"}}, {bt-0.04:.3f});\n'
            f'  tl.to("#burn-c-{i}", {{opacity:0, duration:0.12, ease:"power3.out"}}, {bt:.3f});'
        )
    return "\n".join(html_parts), css, "\n".join(js_lines)


def _tr_glare(boundaries: list[float], accent: str) -> tuple[str, str, str]:
    html_parts = []
    css = """
    .tr-glare {
      position: absolute; top: 0; left: 0; width: 150%; height: 150%;
      pointer-events: none; z-index: 50; opacity: 0;
      background: linear-gradient(135deg, transparent 30%, rgba(255,255,255,0.8) 45%, rgba(255,255,255,0.95) 50%, rgba(255,255,255,0.8) 55%, transparent 70%);
      transform: translate(-80%, -30%);
    }"""
    for i in range(len(boundaries)):
        html_parts.append(f'      <div id="glare-{i}" class="tr-glare"></div>')
    js_lines = []
    for i, bt in enumerate(boundaries):
        t = bt - 0.1
        js_lines.append(
            f'  tl.set("#glare-{i}", {{opacity:1}}, {t:.3f});\n'
            f'  tl.fromTo("#glare-{i}", {{x:"-80%", y:"-30%"}}, {{x:"60%", y:"30%", duration:0.25, ease:"power2.inOut"}}, {t:.3f});\n'
            f'  tl.to("#glare-{i}", {{opacity:0, duration:0.08}}, {t+0.2:.3f});'
        )
    return "\n".join(html_parts), css, "\n".join(js_lines)


def _tr_glitch(boundaries: list[float], accent: str) -> tuple[str, str, str]:
    html_parts = []
    css = f"""
    .tr-glitch-wrap {{
      position: absolute; top: 0; left: 0; width: 100%; height: 100%;
      pointer-events: none; z-index: 50; opacity: 0; overflow: hidden;
    }}
    .tr-glitch-wrap::before, .tr-glitch-wrap::after {{
      content: ""; position: absolute; left: 0; width: 100%;
      height: 8px; background: {accent};
      mix-blend-mode: screen;
    }}
    .tr-glitch-wrap::before {{ top: 30%; background: rgba(255,0,50,0.7); }}
    .tr-glitch-wrap::after {{ top: 65%; background: rgba(0,200,255,0.7); }}"""
    for i in range(len(boundaries)):
        html_parts.append(f'      <div id="glitch-{i}" class="tr-glitch-wrap"></div>')
    js_lines = []
    for i, bt in enumerate(boundaries):
        t = bt - 0.08
        js_lines.append(
            f'  tl.set("#glitch-{i}", {{opacity:1}}, {t:.3f});\n'
            f'  tl.fromTo("#glitch-{i}", {{x:0}}, {{x:12, duration:0.02, ease:"steps(4)"}}, {t:.3f});\n'
            f'  tl.to("#glitch-{i}", {{x:-8, duration:0.02, ease:"steps(3)"}}, {t+0.02:.3f});\n'
            f'  tl.to("#glitch-{i}", {{x:5, duration:0.02, ease:"steps(2)"}}, {t+0.04:.3f});\n'
            f'  tl.to("#glitch-{i}", {{x:0, duration:0.02}}, {t+0.06:.3f});\n'
            f'  tl.set("#glitch-{i}", {{opacity:0}}, {t+0.12:.3f});'
        )
    return "\n".join(html_parts), css, "\n".join(js_lines)


def _tr_warp(boundaries: list[float], accent: str) -> tuple[str, str, str]:
    html_parts = []
    css = f"""
    .tr-warp-ring {{
      position: absolute; top: 50%; left: 50%; width: 200px; height: 200px;
      margin: -100px 0 0 -100px; border-radius: 50%;
      border: 3px solid rgba(255,255,255,0.8);
      box-shadow: 0 0 40px rgba(255,255,255,0.4), 0 0 80px {accent}40, inset 0 0 40px rgba(255,255,255,0.2);
      opacity: 0; pointer-events: none; z-index: 50;
    }}"""
    for i in range(len(boundaries)):
        html_parts.append(f'      <div id="warp-{i}" class="tr-warp-ring"></div>')
    js_lines = []
    for i, bt in enumerate(boundaries):
        t = bt - 0.05
        js_lines.append(
            f'  tl.fromTo("#warp-{i}", {{scale:0.3, opacity:0.9}}, {{scale:4, opacity:0, duration:0.3, ease:"power2.out"}}, {t:.3f});'
        )
    return "\n".join(html_parts), css, "\n".join(js_lines)


def _tr_stripe_wipe(boundaries: list[float], accent: str) -> tuple[str, str, str]:
    n_stripes = 6
    html_parts = []
    css = f"""
    .tr-stripe {{
      position: absolute; left: 0; width: 100%;
      pointer-events: none; z-index: 50;
      background: {accent};
    }}"""
    for i in range(len(boundaries)):
        for s in range(n_stripes):
            h = 100 / n_stripes
            top = s * h
            html_parts.append(
                f'      <div id="stripe-{i}-{s}" class="tr-stripe" style="top:{top:.1f}%;height:{h:.1f}%;transform:translateX({"-120" if s%2==0 else "120"}%)"></div>'
            )
    js_lines = []
    for i, bt in enumerate(boundaries):
        t = bt - 0.08
        for s in range(n_stripes):
            start_x = "-120%" if s % 2 == 0 else "120%"
            delay = s * 0.015
            js_lines.append(
                f'  tl.fromTo("#stripe-{i}-{s}", {{x:"{start_x}"}}, {{x:"0%", duration:0.06, ease:"power3.in"}}, {t+delay:.3f});\n'
                f'  tl.to("#stripe-{i}-{s}", {{x:"{"-120%" if s%2==1 else "120%"}", duration:0.06, ease:"power3.out"}}, {bt+delay:.3f});'
            )
    return "\n".join(html_parts), css, "\n".join(js_lines)


def _tr_ink(boundaries: list[float], accent: str) -> tuple[str, str, str]:
    html_parts = []
    css = """
    .tr-ink {
      position: absolute; top: 50%; left: 50%; width: 200px; height: 200px;
      margin: -100px 0 0 -100px; border-radius: 50%;
      background: radial-gradient(circle, rgba(0,0,0,0.9) 0%, rgba(0,0,0,0.7) 40%, transparent 70%);
      opacity: 0; pointer-events: none; z-index: 50;
    }"""
    for i in range(len(boundaries)):
        html_parts.append(f'      <div id="ink-{i}" class="tr-ink"></div>')
    js_lines = []
    for i, bt in enumerate(boundaries):
        t = bt - 0.08
        js_lines.append(
            f'  tl.fromTo("#ink-{i}", {{scale:0.5, opacity:0}}, {{scale:12, opacity:1, duration:0.1, ease:"power4.in"}}, {t:.3f});\n'
            f'  tl.to("#ink-{i}", {{opacity:0, duration:0.15, ease:"power2.out"}}, {bt:.3f});'
        )
    return "\n".join(html_parts), css, "\n".join(js_lines)


# -------- Word mapping (EDL → output timeline) --------------------------------


def map_words_to_output_timeline(
    edl: dict, edit_dir: Path,
) -> list[dict]:
    """Map transcript words into the output timeline.

    Returns a flat list of {text, start, end, source, is_emphasis, emphasis_score}
    sorted by output start time.
    """
    transcripts_dir = edit_dir / "transcripts"
    output_words: list[dict] = []
    seg_offset = 0.0

    emphasis_set: dict[str, float] = {}
    for r in edl.get("ranges", []):
        for ew in r.get("emphasis_words", []):
            key = f"{ew.get('start', 0):.3f}"
            emphasis_set[key] = ew.get("score", 1.0)

    for r in edl.get("ranges", []):
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_duration = seg_end - seg_start

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text())
        for w in transcript.get("words", []):
            if w.get("type") != "word":
                continue
            ws = w.get("start")
            we = w.get("end")
            text = (w.get("text") or "").strip()
            if not text or ws is None or we is None:
                continue
            if we <= seg_start or ws >= seg_end:
                continue

            out_start = max(0.0, ws - seg_start) + seg_offset
            out_end = max(0.0, we - seg_start) + seg_offset

            key = f"{ws:.3f}"
            is_emph = key in emphasis_set

            output_words.append({
                "text": text,
                "start": round(out_start, 3),
                "end": round(out_end, 3),
                "source": src_name,
                "is_emphasis": is_emph,
                "emphasis_score": emphasis_set.get(key, 0.0),
            })

        seg_offset += seg_duration

    output_words.sort(key=lambda w: w["start"])
    return output_words


def get_clip_boundaries(edl: dict) -> list[float]:
    """Get output-timeline timestamps where clips transition."""
    boundaries: list[float] = []
    offset = 0.0
    for r in edl.get("ranges", []):
        seg_dur = float(r["end"]) - float(r["start"])
        offset += seg_dur
        boundaries.append(round(offset, 3))
    if boundaries:
        boundaries.pop()
    return boundaries


# -------- Word grouping -------------------------------------------------------


def group_words_into_phrases(
    words: list[dict],
    max_words: int = DEFAULT_WORDS_PER_GROUP,
    pause_break: float = 0.3,
) -> list[list[dict]]:
    """Group words into display phrases for caption rendering.

    Breaks on: max_words reached, punctuation, or pause >= pause_break.
    """
    if not words:
        return []

    phrases: list[list[dict]] = []
    current: list[dict] = []

    for w in words:
        if current:
            prev = current[-1]
            gap = w["start"] - prev["end"]
            prev_text = prev["text"]
            ends_punct = bool(prev_text) and prev_text[-1] in PUNCT_BREAK

            if len(current) >= max_words or ends_punct or gap >= pause_break:
                phrases.append(current)
                current = []

        current.append(w)

    if current:
        phrases.append(current)

    return phrases


# -------- HTML composition generation -----------------------------------------


def generate_composition_html(
    phrases: list[list[dict]],
    clip_boundaries: list[float],
    total_duration: float,
    accent_color: str = DEFAULT_ACCENT,
    font_size: int = DEFAULT_FONT_SIZE,
    zoom_scale: float = DEFAULT_ZOOM_SCALE,
    caption_style: str = DEFAULT_CAPTION_STYLE,
    caption_mode: str = DEFAULT_CAPTION_MODE,
    transition: str = DEFAULT_TRANSITION,
) -> str:
    """Generate a HyperFrames HTML composition for karaoke captions.

    Each phrase is absolutely positioned at the same location so captions
    stay anchored — no vertical hopping between lines.
    """
    style = CAPTION_STYLES.get(caption_style, CAPTION_STYLES["bold-outline"])
    is_word_mode = caption_mode == "word"

    shadow_default = style["shadow"]
    shadow_active = style["shadow_active"].format(accent=accent_color)
    shadow_emphasis = style["shadow_emphasis"].format(accent=accent_color)

    font_import_css = ""
    if style["font_import"]:
        font_import_css = f'@import url(\'{style["font_import"]}\');'

    stroke_css = ""
    if style["stroke"] != "none":
        sw = style["stroke_width"]
        sc = style["stroke"]
        stroke_css = f"-webkit-text-stroke: {sw} {sc}; paint-order: stroke fill;"

    phrase_divs: list[str] = []
    timeline_js_lines: list[str] = []

    word_idx = 0
    for pi, phrase in enumerate(phrases):
        spans = []
        for w in phrase:
            wid = f"w{word_idx}"
            cls = "word"
            if w.get("is_emphasis"):
                cls += " emphasis"
            spans.append(f'        <span id="{wid}" class="{cls}">{_escape_html(w["text"])}</span>')
            word_idx += 1
        phrase_html = "\n".join(spans)
        phrase_divs.append(
            f'      <div id="phrase-{pi}" class="phrase">\n{phrase_html}\n      </div>'
        )

    all_phrases_html = "\n".join(phrase_divs)

    flat_words = [ww for pp in phrases for ww in pp]
    word_idx = 0
    for pi, phrase in enumerate(phrases):
        phrase_start = phrase[0]["start"]
        enter_time = max(0, phrase_start - 0.08)

        if pi > 0:
            timeline_js_lines.append(
                f'  tl.set("#phrase-{pi - 1}", {{ visibility: "hidden", opacity: 0 }}, {enter_time:.3f});'
            )

        timeline_js_lines.append(
            f'  tl.set("#phrase-{pi}", {{ visibility: "visible", opacity: 1 }}, {enter_time:.3f});'
        )

        for wi, w in enumerate(phrase):
            wid = f"w{word_idx}"
            ws = w["start"]

            global_idx = word_idx
            next_word_start = w["end"] + 0.05
            if global_idx + 1 < len(flat_words):
                next_word_start = flat_words[global_idx + 1]["start"]

            if is_word_mode:
                timeline_js_lines.append(
                    f'  tl.fromTo("#{wid}", '
                    f'{{ scale: 0.7, opacity: 0 }}, '
                    f'{{ scale: 1, opacity: 1, duration: 0.1, ease: "back.out(3)" }}, '
                    f'{ws:.3f});'
                )
            else:
                timeline_js_lines.append(
                    f'  tl.set("#{wid}", {{ className: "word upcoming" }}, 0);'
                )
                is_emph = w.get("is_emphasis", False)
                active_cls = "word active emphasis" if is_emph else "word active"
                timeline_js_lines.append(
                    f'  tl.set("#{wid}", {{ className: "{active_cls}" }}, {ws:.3f});'
                )
                timeline_js_lines.append(
                    f'  tl.fromTo("#{wid}", '
                    f'{{ scale: 0.9 }}, '
                    f'{{ scale: 1, duration: 0.15, ease: "back.out(2)" }}, '
                    f'{ws:.3f});'
                )
                timeline_js_lines.append(
                    f'  tl.set("#{wid}", {{ className: "word spoken" }}, {next_word_start:.3f});'
                )

            word_idx += 1

    tr_html, tr_css, tr_js = _build_transition(transition, clip_boundaries, accent_color)

    timeline_js = "\n".join(timeline_js_lines)

    word_color = "rgba(255, 255, 255, 1.0)" if is_word_mode else "rgba(255, 255, 255, 0.35)"
    word_font_size = int(font_size * 1.4) if is_word_mode else font_size

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=1080, height=1920" />
  <title>Captions</title>
  <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
  <style>
    {font_import_css}

    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html, body {{ background: transparent !important; }}

    #caption-container {{
      position: absolute;
      bottom: 30%;
      left: 40px;
      right: 40px;
      z-index: 10;
    }}

    .phrase {{
      visibility: hidden;
      position: absolute;
      left: 0; right: 0;
      bottom: 0;
      text-align: center;
      line-height: 1.25;
      white-space: nowrap;
    }}

    .word {{
      display: inline-block;
      font-family: {style["font_family"]};
      font-size: {word_font_size}px;
      font-weight: {style["font_weight"]};
      color: {word_color};
      text-transform: {style["text_transform"]};
      letter-spacing: {style["letter_spacing"]};
      margin: 0 4px;
      will-change: transform, opacity;
      text-shadow: {shadow_default};
      {stroke_css}
    }}

    .word.upcoming {{
      color: rgba(255, 255, 255, 0.35);
    }}

    .word.active {{
      color: {accent_color};
      text-shadow: {shadow_active};
    }}

    .word.active.emphasis {{
      text-shadow: {shadow_emphasis};
    }}

    .word.spoken {{
      color: rgba(255, 255, 255, 1.0);
      text-shadow: {shadow_default};
    }}

{tr_css}
  </style>
</head>
<body>
  <div id="root" data-composition-id="main" data-start="0" data-duration="{total_duration:.3f}" data-width="1080" data-height="1920" data-fps="30">
    <div class="clip" data-start="0" data-duration="{total_duration:.3f}" data-track-index="0">
{tr_html}
      <div id="caption-container">
{all_phrases_html}
      </div>
    </div>
  </div>

  <script>
    window.__timelines = window.__timelines || {{}};
    var tl = gsap.timeline({{ paused: true }});

{timeline_js}

{tr_js}

    window.__timelines["main"] = tl;
  </script>
</body>
</html>"""

    return html


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# -------- Scaffold & render ---------------------------------------------------


def scaffold_project(captions_dir: Path) -> None:
    """Initialize a HyperFrames project in the captions directory."""
    captions_dir.mkdir(parents=True, exist_ok=True)

    existing_html = captions_dir / "index.html"
    existing_json = captions_dir / "hyperframes.json"
    if existing_html.exists() and existing_json.exists():
        print(f"  HyperFrames project already exists at {captions_dir}")
        return

    print(f"  scaffolding HyperFrames project at {captions_dir}")
    subprocess.run(
        [
            "npx", "--yes", "hyperframes", "init", str(captions_dir),
            "--example", "blank", "--non-interactive", "--skip-skills",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def validate_composition(captions_dir: Path) -> bool:
    """Run HyperFrames check on the composition."""
    print("  running hyperframes check...")
    result = subprocess.run(
        ["npx", "--yes", "hyperframes", "check", str(captions_dir)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  check warnings:\n{result.stdout}\n{result.stderr}")
        return False
    print("  check passed")
    return True


def render_overlay(captions_dir: Path, output_path: Path) -> None:
    """Render the caption composition to transparent WebM.

    HyperFrames renders RGBA PNG frames, then we encode to VP9+alpha WebM
    with ffmpeg to guarantee yuva420p pixel format.
    """
    frames_dir = captions_dir / "_alpha_frames"
    if frames_dir.exists():
        import shutil
        shutil.rmtree(frames_dir)

    print(f"  rendering caption frames (png-sequence)...")
    cmd_frames = [
        "npx", "--yes", "hyperframes", "render", str(captions_dir),
        "--format", "png-sequence",
        "--fps", "30",
        "-o", str(frames_dir),
    ]
    subprocess.run(cmd_frames, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    pngs = sorted(frames_dir.glob("*.png"))
    if not pngs:
        raise RuntimeError(f"no PNG frames in {frames_dir}")
    print(f"  {len(pngs)} frames captured")

    output_path = output_path.with_suffix(".mov")
    print(f"  encoding ProRes 4444+alpha → {output_path.name}")
    cmd_encode = [
        "ffmpeg", "-y",
        "-framerate", "30",
        "-i", str(frames_dir / "frame_%06d.png"),
        "-c:v", "prores_ks",
        "-profile:v", "4444",
        "-pix_fmt", "yuva444p10le",
        str(output_path),
    ]
    subprocess.run(cmd_encode, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    import shutil
    shutil.rmtree(frames_dir, ignore_errors=True)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  rendered: {output_path} ({size_mb:.1f} MB)")


def open_studio(captions_dir: Path) -> None:
    """Open HyperFrames Studio for visual review."""
    print(f"  opening HyperFrames Studio for {captions_dir}")
    subprocess.Popen(
        ["npx", "--yes", "hyperframes", "preview", str(captions_dir), "--background"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("  Studio running — scrub the timeline to review captions")
    print("  tell Claude what to change, or say 'render' when ready")


# -------- Main ----------------------------------------------------------------


def generate_captions(
    edl_path: Path,
    accent_color: str = DEFAULT_ACCENT,
    font_size: int = DEFAULT_FONT_SIZE,
    words_per_group: int = DEFAULT_WORDS_PER_GROUP,
    zoom_scale: float = DEFAULT_ZOOM_SCALE,
    caption_style: str = DEFAULT_CAPTION_STYLE,
    caption_mode: str = DEFAULT_CAPTION_MODE,
    transition: str = DEFAULT_TRANSITION,
    do_render: bool = False,
    do_studio: bool = False,
) -> Path:
    """Full caption generation pipeline. Returns path to captions directory."""
    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent

    print("generating captions")
    output_words = map_words_to_output_timeline(edl, edit_dir)
    print(f"  {len(output_words)} words mapped to output timeline")

    wpg = 1 if caption_mode == "word" else words_per_group
    phrases = group_words_into_phrases(output_words, max_words=wpg)
    print(f"  {len(phrases)} {'word' if caption_mode == 'word' else 'phrase'} groups")
    print(f"  style: {caption_style}  mode: {caption_mode}  transition: {transition}")

    clip_boundaries = get_clip_boundaries(edl)
    total_duration = float(edl.get("total_duration_s", 0))

    html = generate_composition_html(
        phrases=phrases,
        clip_boundaries=clip_boundaries,
        total_duration=total_duration,
        accent_color=accent_color,
        font_size=font_size,
        zoom_scale=zoom_scale,
        caption_style=caption_style,
        caption_mode=caption_mode,
        transition=transition,
    )

    captions_dir = edit_dir / "animations" / "captions"
    scaffold_project(captions_dir)

    html_path = captions_dir / "index.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"  composition → {html_path}")

    validate_composition(captions_dir)

    if do_studio:
        open_studio(captions_dir)
    elif do_render:
        render_path = captions_dir / "render.mov"
        render_overlay(captions_dir, render_path)

        edl["overlays"] = edl.get("overlays", [])
        edl["overlays"].append({
            "file": str(render_path),
            "start_in_output": 0.0,
            "duration": total_duration,
        })
        edl_path.write_text(json.dumps(edl, indent=2))
        print(f"  EDL updated with caption overlay")

    return captions_dir


def main() -> None:
    style_names = ", ".join(CAPTION_STYLES.keys())
    transition_names = "flash, swipe, film-burn, glare, glitch, warp, stripe-wipe, ink, none"

    ap = argparse.ArgumentParser(
        description="Generate HyperFrames word-by-word animated captions from an EDL"
    )
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("--accent-color", type=str, default=DEFAULT_ACCENT,
                    help=f"Active word highlight color. Default: {DEFAULT_ACCENT}")
    ap.add_argument("--font-size", type=int, default=DEFAULT_FONT_SIZE,
                    help=f"Caption font size in px. Default: {DEFAULT_FONT_SIZE}")
    ap.add_argument("--words-per-group", type=int, default=DEFAULT_WORDS_PER_GROUP,
                    help=f"Max words per caption phrase. Default: {DEFAULT_WORDS_PER_GROUP}")
    ap.add_argument("--zoom-scale", type=float, default=DEFAULT_ZOOM_SCALE,
                    help=f"Scale pop on active words. Default: {DEFAULT_ZOOM_SCALE}")
    ap.add_argument("--caption-style", type=str, default=DEFAULT_CAPTION_STYLE,
                    help=f"Caption style preset: {style_names}. Default: {DEFAULT_CAPTION_STYLE}")
    ap.add_argument("--caption-mode", type=str, default=DEFAULT_CAPTION_MODE,
                    choices=["phrase", "word"],
                    help=f"Caption display mode. Default: {DEFAULT_CAPTION_MODE}")
    ap.add_argument("--transition", type=str, default=DEFAULT_TRANSITION,
                    help=f"Transition preset: {transition_names}. Default: {DEFAULT_TRANSITION}")
    ap.add_argument("--render", action="store_true",
                    help="Render the composition to ProRes MOV after generating")
    ap.add_argument("--studio", action="store_true",
                    help="Open HyperFrames Studio for visual review")
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    generate_captions(
        edl_path=edl_path,
        accent_color=args.accent_color,
        font_size=args.font_size,
        words_per_group=args.words_per_group,
        zoom_scale=args.zoom_scale,
        caption_style=args.caption_style,
        caption_mode=args.caption_mode,
        transition=args.transition,
        do_render=args.render,
        do_studio=args.studio,
    )


if __name__ == "__main__":
    main()
