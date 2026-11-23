#!/usr/bin/env python3
"""Generate a face set for the voice agent page: one photoreal portrait per expression, same person throughout.

    python3 make_faces.py <set-name> ["character description"] [--look photo|creature|3d|"..."] [--only name,name] [--base-only]

Writes faces/<set-name>/<expression>.jpg (768 px). The page uses faces/<agent folder name>/ when it exists and
faces/default/ otherwise, so `make_faces.py scout "a woman in her thirties ..."` gives the agent of the folder `scout` its own face.
The neutral portrait is generated first; every other expression is an edit of it, which is what keeps the
person, framing and light identical so the page can crossfade between them. Look at the results before keeping
them: an edit occasionally drifts (different person, moved head) and is fixed by `--only <name>`.
Needs GEMINI_API_KEY or GOOGLE_API_KEY. About 7 cents an image, 18 images a set.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MODEL = "gemini-3.1-flash-image"
DEFAULT_CHARACTER = ("a man in his late forties with short dark hair greying at the temples, light stubble, "
                     "calm intelligent grey-blue eyes, wearing a plain charcoal crewneck")
FRAME = ("Head and shoulders, centred, facing the camera and looking straight into the lens, mouth closed. Pure "
         "black background. Soft key light from the front left, a faint cool blue rim light on the right edge of the "
         "face and shoulder. No text, no watermark. Square image.")
# How the picture is rendered. --look picks one; any other text is used as given.
LOOKS = {
    "photo": "Photorealistic studio portrait photograph, 85mm lens, shallow depth of field, natural skin texture with pores, no retouching.",
    "creature": "Photorealistic studio portrait photograph, 85mm lens, shallow depth of field, real fur, skin and surface detail, as if the subject truly existed and sat for a portrait.",
    "3d": "A character portrait rendered in the style of a modern 3D animated feature film: stylised proportions, large expressive eyes, soft subsurface skin, detailed hair, cinematic render.",
}

# Kept subtle on purpose: a real person's face moves a little. Exaggerated expressions read as a mask.
EXPRESSIONS = {
    "neutral": "a relaxed neutral expression, mouth closed, at ease",
    "attentive": "an attentive listening expression: eyes slightly more open, brows a touch raised, mouth closed, fully present",
    "curious": "a curious expression: one eyebrow slightly raised, head tilted a few degrees, a hint of a closed-mouth smile",
    "thinking": "a thinking expression: eyes looking up and to one side away from the camera, lips lightly pressed together, brow slightly furrowed",
    "focused": "a focused, concentrating expression: brows drawn slightly together and down, eyes narrowed a little, mouth closed and firm",
    "amused": "an amused expression: a lopsided closed-mouth smile with smiling eyes, as if at a dry joke",
    "delighted": "a delighted expression: a broad genuine smile showing teeth, cheeks raised, eyes crinkled at the corners",
    "warm": "a warm, kind expression: a gentle closed-mouth smile and soft eyes",
    "excited": "an excited expression: eyes bright and wide, brows raised, an open eager smile",
    "surprised": "a surprised expression: eyebrows raised high, eyes wide, lips slightly parted",
    "skeptical": "a skeptical expression: one eyebrow raised, the other lowered, eyes slightly narrowed, mouth pulled a little to one side, unconvinced",
    "puzzled": "a puzzled expression: brows knitted and uneven, head tilted slightly, mouth a little askew, trying to work something out",
    "concerned": "a concerned expression: inner ends of the eyebrows raised and drawn together, a slight frown, worried eyes",
    "serious": "a serious expression: level steady gaze, brows slightly lowered, mouth closed and straight, no smile",
    "confident": "a confident expression: a slight assured closed-mouth smile, chin a touch raised, steady direct gaze",
    "sheepish": "a sheepish, apologetic expression: an awkward half-smile, eyebrows raised in the middle, eyes glancing slightly down and aside",
    "playful": "a playful expression: a mischievous grin, one eyebrow raised, a glint in the eyes",
    "calm": "a calm, serene expression: eyes relaxed and slightly lowered lids, the faintest closed-mouth smile",
}


def key():
    k = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not k:
        sys.exit("Set GEMINI_API_KEY or GOOGLE_API_KEY.")
    return k


def generate(prompt, reference=None):
    parts = [{"text": prompt}]
    if reference:
        parts.insert(0, {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(reference).decode()}})
    body = {"contents": [{"parts": parts}], "generationConfig": {"responseModalities": ["IMAGE"]}}
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": key()})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.load(r)
    for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if "inlineData" in part:
            return base64.b64decode(part["inlineData"]["data"])
    raise RuntimeError("no image returned: " + json.dumps(data)[:300])


def save(png, out_dir, name):
    raw = out_dir / f".{name}.png"
    raw.write_bytes(png)
    subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "86", "-Z", "768", str(raw),
                    "--out", str(out_dir / f"{name}.jpg")], check=True, capture_output=True)
    if name != "neutral":
        raw.unlink()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("set_name")
    ap.add_argument("character", nargs="?", default=DEFAULT_CHARACTER)
    ap.add_argument("--only", help="comma-separated expressions to (re)generate from the existing neutral")
    ap.add_argument("--look", default="photo", help="photo (default), creature, 3d, or your own rendering description")
    ap.add_argument("--base-only", action="store_true", help="generate just the neutral portrait, to check the character")
    a = ap.parse_args()
    look = LOOKS.get(a.look, a.look)
    out = Path(__file__).resolve().parent / "faces" / a.set_name
    out.mkdir(parents=True, exist_ok=True)
    base_png = out / ".neutral.png"
    if not a.only or not base_png.exists():
        print("neutral ...", flush=True)
        save(generate(f"{look} {FRAME} The subject is {a.character}, with {EXPRESSIONS['neutral']}."), out, "neutral")
    if a.base_only:
        return
    base = base_png.read_bytes()
    names = [n for n in (a.only.split(",") if a.only else EXPRESSIONS) if n != "neutral"]

    def one(name):
        try:
            save(generate("Edit this photograph. Keep the same person, the same face and identity, the same hair, "
                          "clothing, lighting, black background, framing, head size and position exactly as they "
                          f"are. Change only the facial expression to {EXPRESSIONS[name]}. Keep it natural and "
                          "understated, as a real person would look in conversation. Photorealistic.", base), out, name)
            return name, None
        except Exception as e:  # report and carry on; rerun the failures with --only
            return name, str(e)[:200]

    with ThreadPoolExecutor(6) as pool:
        for name, err in pool.map(one, names):
            print(f"{name}: {'ok' if not err else 'FAILED ' + err}", flush=True)


if __name__ == "__main__":
    main()
