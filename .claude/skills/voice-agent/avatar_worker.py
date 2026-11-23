#!/usr/bin/env python3
"""Lip-sync worker for the voice agent's face. Runs in the OpenTalking virtualenv, not the system Python.

Loads the QuickTalk model once (Apple GPU), prepares every expression portrait in the face set, then turns
"this audio, on this expression" into frames of that face speaking it, streamed as they are made.

Protocol: one JSON object per line on stdin, one per line on stdout.
  in   {"id": 7, "expression": "skeptical", "pcm": "<base64 s16le mono>", "rate": 24000}     or  {"cancel": 7}
  out  {"id": 7, "i": 0, "jpg": "<base64>"} ... one per frame, FPS frames per second of audio
       {"id": 7, "done": true, "frames": 50, "render_seconds": 2.4}     or  {"id": 7, "error": "..."}
  first line out, once ready:  {"ready": true, "expressions": [...], "load_seconds": 9.1, "fps": 16}
Everything else (model warnings) goes to stderr.

Engine and weights live outside the skill (they are large and re-downloadable):
  ~/.cache/voice-agent/opentalking   (git clone of datascale-ai/opentalking, Apache-2.0, with its .venv)
  ~/.cache/voice-agent/models/quicktalk
Setup is in SKILL.md. Bench without the server:  <venv python> avatar_worker.py --bench <faces dir> <wav>
"""
import base64
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

HOME = Path.home() / ".cache" / "voice-agent"
ENGINE = HOME / "opentalking"
ASSETS = HOME / "models" / "quicktalk"
sys.path.insert(0, str(ENGINE))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402
from opentalking.models.quicktalk.runtime_v2 import QuickTalkRebuild, ensure_ffmpeg  # noqa: E402

FPS = 20           # an M4 Pro makes about 25 a second; late frames are skipped, so a slower Mac loses smoothness, not sync
SIZE = 512         # output frame; the page shows it at about this size


class Avatar:
    def __init__(self, faces_dir):
        t0 = time.time()
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.rt = rt = QuickTalkRebuild(ASSETS, device=device, model_backend="pth")
        self.bases = {}
        tune = Path(faces_dir) / "tune.json"
        t = json.loads(tune.read_text()) if tune.is_file() else {}
        self.mouth_dy, self.mouth_dx = float(t.get("mouth_dy", 0)), float(t.get("mouth_dx", 0))
        self.mouth_scale = float(t.get("mouth_scale", 1))      # under 1: a smaller repainted area and mouth
        self.mouth_top = float(t.get("mouth_top", 0))          # 0..1: leave this upper share of the face untouched
        self.mouth_bottom = float(t.get("mouth_bottom", 1))    # and everything below this share (a chin with no jaw)
        self.fallback_marks = None                             # the neutral portrait's, for one the detector cannot read
        # A face the detector cannot read at all (dust, haze, a cloud): carrier.png is a solid twin of the neutral
        # portrait with the eyes, nose and mouth in the same places. Its points place the mouth on every portrait.
        carrier = cv2.imread(str(Path(faces_dir) / "carrier.png"))
        if carrier is not None:
            rgb = cv2.cvtColor(cv2.resize(carrier, (SIZE, SIZE), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
            try:
                rt.image_processor.affine_transform(rgb)    # builds the detector
            except Exception:
                pass
            marks = self._marks(rgb)
            if marks is not None:
                self.fallback_marks = np.array(marks, dtype=np.float32)
        files = sorted((f for f in Path(faces_dir).glob("*.jpg") if not f.name.startswith(".")), key=lambda f: (f.stem != "neutral", f.stem))
        for f in files:
            img = cv2.imread(str(f))
            if img is None:
                continue
            img = cv2.resize(img, (SIZE, SIZE), interpolation=cv2.INTER_AREA)
            try:
                self.bases[f.stem] = self._prepare(img)
            except Exception as e:  # a portrait with no detectable face is skipped, not fatal
                print(f"skip {f.name}: {e}", file=sys.stderr)
        if not self.bases:
            raise RuntimeError(f"no usable portraits in {faces_dir}")
        # the first render is several times slower (GPU warm-up); pay that here, not on the first spoken sentence
        for _ in self.render("neutral", np.zeros(16000, dtype=np.int16), 16000):
            pass
        self.load_seconds = round(time.time() - t0, 1)

    def _marks(self, rgb):
        """The detector's face points, or None. A face that fills the frame is missed: it is tried again smaller."""
        det = self.rt.image_processor.face_detector
        _, marks = det(rgb)
        for s in (0.75, 0.6):
            if marks is not None:
                break
            k = int(SIZE * s); o = (SIZE - k) // 2
            pad = np.full((SIZE, SIZE, 3), 128, np.uint8)
            pad[o:o + k, o:o + k] = cv2.resize(rgb, (k, k), interpolation=cv2.INTER_AREA)
            _, marks = det(pad)
            if marks is not None:
                marks = (np.array(marks, dtype=np.float32) - o) / s
        return marks

    def _detect(self, img):
        """The engine's face crop, corrected by the face set's tune.json (pixels of the 512 frame).

        The engine places and sizes the mouth from the eyes and nose using human proportions. On another kind of
        face that is off: a longer one (an ape, a muzzle) has its mouth lower, {"mouth_dy": 16}; a round head with
        wide-set eyes makes the engine think the face is bigger than it is, {"mouth_scale": 0.8}; "mouth_dx" moves
        it sideways.
        """
        ip = self.rt.image_processor
        ip.restorer.p_bias = None       # no smoothing carried over from the previous portrait
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        if ip.face_detector is None:
            try:
                ip.affine_transform(rgb)    # builds the detector
            except Exception:
                pass
            ip.restorer.p_bias = None
        marks = self._marks(rgb)
        if marks is None:
            # The portraits of a set are edits of its neutral one, with the head in the same place: when the detector
            # cannot read an expression (a strong frown on a non-human face), the neutral portrait's points serve.
            if self.fallback_marks is None:
                raise RuntimeError("Face not detected")
            marks = self.fallback_marks
            print("face not detected, placed by the neutral portrait", file=sys.stderr)
        elif self.fallback_marks is None:
            self.fallback_marks = np.array(marks, dtype=np.float32)
        marks = np.array(marks, dtype=np.float32)
        anchor = np.mean([marks[[43, 48, 49, 51, 50]].mean(0), marks[101:106].mean(0), marks[[74, 77, 83, 86]].mean(0)], axis=0)
        marks = anchor + (marks - anchor) * self.mouth_scale    # the eyes and nose the engine aligns by, drawn together
        marks[:, 0] += self.mouth_dx
        marks[:, 1] += self.mouth_dy
        crop = ip.affine_transform_from_landmarks(rgb, marks)
        face = cv2.cvtColor(crop.face_chw.permute(1, 2, 0).cpu().numpy().astype(np.uint8), cv2.COLOR_RGB2BGR)
        return face, crop.box, crop.affine_matrix

    def _prepare(self, img):
        """Everything about a portrait that does not change from frame to frame.

        The engine pastes each generated mouth back with out = A * warped_mouth + B * portrait, and recomputes the
        masks A and B every frame. For a still portrait they are constant, so they are measured once here by
        pasting an all-white mouth onto black (gives A) and an all-black mouth onto white (gives B).
        """
        rt = self.rt
        det = self._detect(img)
        _, (x1, y1, x2, y2), affine = det
        h, w = int(y2 - y1), int(x2 - x1)
        paste = rt.image_processor.restorer.restore_img
        ones = torch.ones((3, h, w), device=rt.device, dtype=rt.dtype)
        a = paste(np.zeros_like(img), ones, affine, scale_h=rt.scale_h, scale_w=rt.scale_w).astype(np.float32) / 255.0
        b = paste(np.full_like(img, 255), ones * 0, affine, scale_h=rt.scale_h, scale_w=rt.scale_w).astype(np.float32) / 255.0
        if self.mouth_top > 0 or self.mouth_bottom < 1:
            # Repaint the mouth only: the engine redraws the whole face crop, which also makes still eyes shimmer and
            # can smear past a chin. A soft band in the crop's own rows, carried to the frame like the mouth itself.
            rows = (np.arange(h, dtype=np.float32) + 0.5) / h
            soft = 0.08
            band = np.clip((rows - self.mouth_top) / soft, 0, 1) * np.clip((self.mouth_bottom - rows) / soft, 0, 1)
            band = np.repeat(np.repeat(band[:, None, None], w, axis=1), 3, axis=2)
            keep = cv2.warpAffine(band, cv2.invertAffineTransform(np.asarray(affine, dtype=np.float32)), (SIZE, SIZE), flags=cv2.INTER_LINEAR)
            a = a * keep
            b = 1.0 - a
        inv = cv2.invertAffineTransform(np.asarray(affine, dtype=np.float32))
        # the model's picture input (mask + masked face + reference) is also the same for every frame of a still
        face_in = rt._prepare_batch([cv2.cvtColor(det[0], cv2.COLOR_BGR2RGB)], [np.zeros((10, 1024), np.float32)],
                                    [img], [det[1]], [det[2]])[0].astype(np.float32)
        return {"img": img, "size": (h, w), "a": a, "rest": b * img.astype(np.float32), "inv": inv, "face_in": face_in, "affine": affine}

    @torch.inference_mode()
    def render(self, expression, pcm, rate, cancelled=lambda: False, realtime=True):
        """Yield (index, BGR frame) at FPS frames per second of the audio: the portrait, speaking it. pcm: int16 mono."""
        rt = self.rt
        base = self.bases.get(expression) or self.bases.get("neutral") or next(iter(self.bases.values()))
        n = max(1, int(round(len(pcm) / rate * FPS)))
        feat = pcm
        if rate != 16000:   # the audio model only takes 16 kHz
            x = pcm.astype(np.float32) / 32768.0
            feat = np.interp(np.arange(0, len(x), rate / 16000.0), np.arange(len(x)), x).astype(np.float32)
        reps = rt.build_rep_chunks(rt.extract_representations_pcm(feat, 16000), n, FPS)
        hn = np.zeros((2, 1, 576), dtype=np.float32)
        cn = np.zeros((2, 1, 576), dtype=np.float32)
        h, w = base["size"]
        t0 = time.time()
        for i, rep in enumerate(reps):
            if cancelled():
                return
            # Playback is the clock. A frame that would arrive late is skipped rather than shown late, so a slow
            # stretch costs smoothness, never sync. The first frames are always made, since playback waits for them.
            if not realtime or i < 3 or time.time() - t0 <= (i + 1.5) / FPS:
                g, hn, cn = rt.run_model(rep[None].astype(np.float32), base["face_in"], hn, cn)
                mouth = np.clip(rt.transform_output(g.squeeze(0).astype(np.float32)), 0, 1).transpose(1, 2, 0) * 255.0
                mouth = cv2.resize(mouth, (w, h), interpolation=cv2.INTER_CUBIC)
                warped = cv2.warpAffine(mouth, base["inv"], (SIZE, SIZE), flags=cv2.INTER_LINEAR)
                yield i, np.clip(base["a"] * warped + base["rest"], 0, 255).astype(np.uint8)


def bench(faces_dir, wav_path):
    av = Avatar(faces_dir)
    with wave.open(wav_path, "rb") as w:
        rate, pcm = w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    print(f"loaded in {av.load_seconds}s, {len(av.bases)} portraits")
    out = Path(tempfile.mkdtemp(prefix="voice-agent-bench-"))
    for name in ["neutral", "skeptical", "delighted"]:
        t0 = time.time()
        frames = [f for _, f in av.render(name, pcm, rate, realtime=False)]
        took = time.time() - t0
        print(f"{name}: {len(pcm) / rate:.1f}s of speech, {len(frames)} frames in {took:.1f}s = {len(frames) / took:.1f} fps (need {FPS})")
        enc = subprocess.Popen([ensure_ffmpeg(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s",
                                f"{SIZE}x{SIZE}", "-r", str(FPS), "-i", "-", "-i", wav_path, "-c:v", "libx264", "-pix_fmt",
                                "yuv420p", "-c:a", "aac", "-shortest", str(out / f"{name}.mp4")], stdin=subprocess.PIPE)
        for f in frames:
            enc.stdin.write(f.tobytes())
        enc.stdin.close(); enc.wait()
    # the fast paste must match the engine's own
    rt, base = av.rt, av.bases["neutral"]
    h, w = base["size"]
    p = torch.rand((3, h, w), device=rt.device, dtype=rt.dtype)
    ref = rt.image_processor.restorer.restore_img(base["img"], p, base["affine"], scale_h=rt.scale_h, scale_w=rt.scale_w)
    mine = np.clip(base["a"] * cv2.warpAffine(p.permute(1, 2, 0).cpu().numpy() * 255.0, base["inv"], (SIZE, SIZE)) + base["rest"], 0, 255)
    note = " (differs by design: this set repaints the mouth only)" if av.mouth_top > 0 or av.mouth_bottom < 1 else ""
    print(f"paste check: mean abs difference {np.abs(ref.astype(np.float32) - mine).mean():.2f} of 255{note}   clips in {out}")


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--bench":
        return bench(sys.argv[2], sys.argv[3])
    real_out = os.fdopen(os.dup(1), "w")      # keep the protocol channel; anything a library prints goes to stderr
    os.dup2(2, 1)
    send = lambda obj: print(json.dumps(obj), file=real_out, flush=True)
    av = Avatar(sys.argv[1])
    send({"ready": True, "expressions": sorted(av.bases), "load_seconds": av.load_seconds, "fps": FPS})
    jobs, cancelled = queue.Queue(), set()

    def read():
        for line in sys.stdin:
            try:
                req = json.loads(line)
            except ValueError:
                continue
            if "cancel" in req:
                cancelled.add(req["cancel"])
            else:
                jobs.put(req)
        jobs.put(None)

    threading.Thread(target=read, daemon=True).start()
    while True:
        req = jobs.get()
        if req is None:
            return
        rid = req.get("id")
        try:
            t0, n = time.time(), 0
            pcm = np.frombuffer(base64.b64decode(req["pcm"]), dtype=np.int16)
            for i, frame in av.render(req.get("expression") or "neutral", pcm, int(req.get("rate") or 24000), lambda: rid in cancelled):
                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 84])
                send({"id": rid, "i": i, "jpg": base64.b64encode(jpg.tobytes()).decode()})
                n += 1
            send({"id": rid, "done": True, "frames": n, "render_seconds": round(time.time() - t0, 2)})
        except Exception as e:
            send({"id": rid, "error": str(e)[:300]})


if __name__ == "__main__":
    main()
