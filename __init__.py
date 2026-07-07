"""Auto Movie Director by AdamGman — plan a movie with an LLM, preview it as a
storyboard, then render every scene with LTX 2.3 (video + audio) and stitch one
finished MP4. https://github.com/AdamGman/ComfyUI-AutoMovieDirector

Workflow: queue once in 'editor preview' (fast, low-res single frame per scene ->
storyboard saved to the output folder), approve, flip mode to 'full movie', queue
again — the saved storyboard frames become first-frame guides so the movie matches
what you approved.
"""

import base64
import hashlib
import io as _io
import json
import math
import os
import re
import subprocess
import time
import urllib.request

from fractions import Fraction

import folder_paths

_PLANNER_SYSTEM = (
    "You are an award-winning film director and screenwriter writing shot prompts for a "
    "text-to-video model (LTX) that renders VIDEO AND SOUND together from each prompt. "
    "The model renders each scene INDEPENDENTLY with no memory between scenes.\n"
    "Rules for the scene prompts:\n"
    "1. STRUCTURE: follow three-act film structure across the scenes — Act 1: establishing "
    "world + inciting incident (first ~25%), Act 2: rising action and complications (middle), "
    "CLIMAX at roughly 3/4 through, Act 3: resolution and a closing image that echoes the opening.\n"
    "2. SHOT VARIETY: give every scene an explicit shot type and camera move (establishing wide, "
    "medium tracking, slow push-in close-up, low-angle, aerial, static tableau...). Never use the "
    "same shot type twice in a row. Wide shots to open and close; close-ups at emotional peaks.\n"
    "3. CONTINUITY: each scene must clearly follow from the previous one (weather, time of day, "
    "damage, objects persist and progress). Time of day should progress across the film.\n"
    "4. SOUND: every scene prompt MUST end with one sentence starting exactly 'Audio:' describing "
    "the soundscape concretely (ambience, effects, and how it evolves; e.g. 'Audio: steady rain on "
    "tin, distant thunder rolls, servo motors whirring softly.'). The model generates audio from this.\n"
    "5. Each scene prompt is 50-90 words, concrete and visual. No numbering, no markdown, no camera jargon "
    "the model can't see.\n"
    "6. Do NOT restate the character sheet inside scene prompts — it is prepended automatically.\n"
    "7. CHARACTER SHEET must pin down COUNTABLE anatomy so the subject looks identical in every scene: body "
    "plan (biped/quadruped/wheeled/treaded...), exact number of limbs, head shape, number/color of eyes or "
    "lenses, torso shape, size next to a familiar object, and 2-3 unmistakable marks. If the movie brief "
    "already describes the subject physically, the character sheet MUST repeat those exact physical facts "
    "word-for-word — never contradict, replace, or omit them.\n"
    "8. IMAGE HYGIENE: full-bleed frames only — never letterboxing, black bars, borders, picture frames, "
    "windows framing the subject, split screens, or text overlays. The word 'lightning' (any form) is "
    "FORBIDDEN, as are 'sparks', 'glitter', 'film grain', 'flickering': storms read as sheets of streaking "
    "rain, wet reflections, and a soft glow inside the clouds. Rain is always streaks in motion, never specks.\n"
    "9. Verbs must match the subject's stated anatomy: a robot on treads ROLLS or SITS PARKED — it never "
    "stands, walks, kneels, or grows legs. Never give the subject body parts the character sheet doesn't list.\n"
    "10. The FIRST sentence of every scene prompt must name the subject and its single main action plainly "
    "(the video model weights early words most). Atmosphere, background and camera detail come after."
)


def _ollama_chat(url, model, messages, seed=0, images_b64=None, timeout=600):
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "format": "json",
        "keep_alive": 0,
        "think": False,
        "options": {"temperature": 0.8, "seed": int(seed) & 0x7FFFFFFF, "num_ctx": 8192},
    }
    if images_b64:
        body["messages"][-1]["images"] = images_b64

    def _post(b):
        req = urllib.request.Request(
            url.rstrip("/") + "/api/chat",
            data=json.dumps(b).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def _pull(name):
        print(f"[AutoMovieDirector] model '{name}' not installed - asking Ollama to download it (this can take a while)...")
        preq = urllib.request.Request(
            url.rstrip("/") + "/api/pull",
            data=json.dumps({"name": name, "stream": False}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(preq, timeout=3600) as r:
            status = json.load(r).get("status", "")
        print(f"[AutoMovieDirector] pull '{name}': {status or 'done'}")

    try:
        data = _post(body)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        low = detail.lower()
        if "think" in low:
            body.pop("think", None)
            data = _post(body)
        elif "not found" in low or e.code == 404:
            _pull(model)
            data = _post(body)
        else:
            raise RuntimeError(f"ollama HTTP {e.code}: {detail}")
    msg = data.get("message", {})
    content = (msg.get("content") or "").strip()
    if not content:
        content = (msg.get("thinking") or "").strip()
    return content


def _extract_json(text):
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in LLM reply")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unbalanced JSON in LLM reply")


def _scene_text(s):
    if isinstance(s, dict):
        for k in ("prompt", "description", "text", "scene", "content"):
            if k in s and str(s[k]).strip():
                return str(s[k]).strip()
        return " ".join(str(v).strip() for v in s.values() if str(v).strip())
    return str(s).strip()


def _fallback_scenes(global_prompt, style, n):
    beats = [
        ("establishing wide shot", "introducing the world and main subject in daylight"),
        ("medium tracking shot", "the subject begins their goal, first signs of trouble"),
        ("slow push-in", "a discovery or complication raises the stakes"),
        ("dynamic low-angle shot", "the most dramatic confrontation, peak intensity"),
        ("static wide tableau", "the aftermath settles at dusk, quiet closing image"),
    ]
    scenes = []
    for i in range(n):
        shot, beat = beats[min(i * len(beats) // max(n, 1), len(beats) - 1)]
        scenes.append(
            f"{shot} of {global_prompt}; {beat}. {style} "
            f"Audio: ambient environment sounds matching the scene, subtle movement effects."
        )
    return scenes


def _plan_hash(scenes, seed):
    payload = json.dumps([s["prompt"] for s in scenes]) + f"|{seed}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:10]


def _project_rel(plan, phash):
    """One folder per movie project: output/auto_movie/<title-slug>_<planhash>."""
    slug = re.sub(r"[^\w]+", "_", (plan.get("global_prompt") or "movie").lower()).strip("_")[:40].rstrip("_")
    return os.path.join("auto_movie", f"{slug or 'movie'}_{phash}")


class AMD_MoviePlanner:
    CATEGORY = "AdamGman/🎬 Auto Movie Director"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("scene_plan", "plot_text")
    FUNCTION = "plan"
    OUTPUT_NODE = True  # emits per-scene texts to the UI so the scene boxes fill live

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "global_prompt": ("STRING", {"multiline": True, "default": "A lone lighthouse keeper discovers a tiny glowing whale stranded in a tide pool and helps it return to the sea during a storm."}),
                "num_scenes": ("INT", {"default": 4, "min": 1, "max": 24}),
                "seconds_per_scene": ("FLOAT", {"default": 5.0, "min": 1.0, "max": 10.0, "step": 0.5}),
                "style": ("STRING", {"multiline": False, "default": "cinematic, photorealistic, shallow depth of field, film grain"}),
                "use_ollama": ("BOOLEAN", {"default": True}),
                "ollama_model": ("STRING", {"default": "qwen3.6:latest", "tooltip": "Any Ollama model name. If it isn't installed, it is downloaded automatically on first use."}),
                "ollama_url": ("STRING", {"default": "http://127.0.0.1:11434"}),
                "scene_overrides": ("STRING", {"multiline": True, "default": "", "tooltip": "One line per scene. A non-empty line REPLACES that scene's auto prompt. Leave a line blank to let the AI write it."}),
                "llm_seed": ("INT", {"default": 7, "min": 0, "max": 2**31 - 1}),
            },
            "optional": {
                "image": ("IMAGE", {"tooltip": "Optional reference image. If the Ollama model is multimodal it is described and woven into the plot."}),
            },
        }

    def _caption(self, image, url, model, seed):
        try:
            import numpy as np
            from PIL import Image

            arr = (image[0].cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
            im = Image.fromarray(arr)
            im.thumbnail((512, 512))
            buf = _io.BytesIO()
            im.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()
            reply = _ollama_chat(
                url, model,
                [{"role": "user", "content": 'Describe this image in one dense sentence (subject, setting, lighting, mood). Reply as JSON: {"caption": "..."}'}],
                seed=seed, images_b64=[b64], timeout=180,
            )
            return _extract_json(reply).get("caption", "")
        except Exception as e:
            print(f"[AutoMovieDirector] image caption skipped: {e}")
            return ""

    def plan(self, global_prompt, num_scenes, seconds_per_scene, style, use_ollama,
             ollama_model, ollama_url, scene_overrides, llm_seed, image=None):
        n = int(num_scenes)
        overrides = [ln.strip() for ln in scene_overrides.splitlines()]
        overrides += [""] * (n - len(overrides))

        plot, sheet, caption = "", "", ""
        scenes = None
        if image is not None and use_ollama:
            caption = self._caption(image, ollama_url, ollama_model, llm_seed)

        need_auto = any(not overrides[i] for i in range(n))
        if use_ollama and need_auto:
            brief = global_prompt if not caption else f"{global_prompt}\n\nVisual reference to honor: {caption}"
            user_msg = (
                f"Movie brief: {brief}\n\nVisual style: {style}\n\n"
                f"Write a cohesive short-film plot for exactly {n} scenes of ~{seconds_per_scene:.0f} seconds each, "
                f"following three-act structure with the climax about three quarters of the way through. "
                f'Also write a "character_sheet": ONE sentence physically describing the recurring subject(s) and '
                f"location palette in exact reusable words (colors, materials, size, distinguishing marks). "
                f'Reply as JSON: {{"plot": "...", "character_sheet": "...", "scenes": ["scene 1 prompt", ...]}} '
                f'with exactly {n} entries in "scenes". Remember: every scene prompt ends with an "Audio:" sentence.'
            )
            reply = ""
            for attempt in range(2):
                try:
                    reply = _ollama_chat(
                        ollama_url, ollama_model,
                        [{"role": "system", "content": _PLANNER_SYSTEM},
                         {"role": "user", "content": user_msg}],
                        seed=llm_seed + attempt,
                    )
                    parsed = _extract_json(reply)
                    got = [t for t in (_scene_text(s) for s in parsed.get("scenes", [])) if t]
                    if not got:
                        raise ValueError("LLM returned no scenes")
                    while len(got) < n:
                        got.append(got[-1])
                    scenes = got[:n]
                    sheet = str(parsed.get("character_sheet", "")).strip()
                    plot = str(parsed.get("plot", "")).strip()
                    break
                except Exception as e:
                    print(f"[AutoMovieDirector] Ollama planning attempt {attempt + 1} failed: {e} | reply head: {reply[:220]!r}")
        if scenes is None:
            scenes = _fallback_scenes(global_prompt, style, n)
            if need_auto:
                plot = plot or "(Ollama unavailable or disabled - used built-in act-structure fallback.)\n" + global_prompt

        final = []
        for i in range(n):
            core = overrides[i] if overrides[i] else scenes[i]
            if "audio:" not in core.lower():
                core = f"{core} Audio: ambient sounds of the scene, natural movement effects."
            full = f"{sheet} {core}".strip() if sheet and sheet.lower() not in core.lower() else core
            if style and style.lower() not in full.lower():
                full = f"{full} {style}"
            final.append({"index": i, "prompt": full, "core": core, "seconds": float(seconds_per_scene)})

        plan = {"plot": plot, "sheet": sheet, "global_prompt": global_prompt, "scenes": final}
        pretty = (f"PLOT\n{plot}\n\nCHARACTER SHEET\n{sheet}\n\n" if plot or sheet else "") + \
            "\n\n".join(f"[Scene {s['index'] + 1} - {s['seconds']:.1f}s]\n{s['core']}" for s in final)
        return {"ui": {"scene_texts": [s["core"] for s in final]},
                "result": (json.dumps(plan), pretty)}


class AMD_MovieRenderer:
    CATEGORY = "AdamGman/🎬 Auto Movie Director"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("movie_path",)
    FUNCTION = "render"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        import comfy.samplers
        return {
            "required": {
                "model": ("MODEL",),
                "clip": ("CLIP",),
                "video_vae": ("VAE",),
                "audio_vae": ("VAE",),
                "scene_plan": ("STRING", {"forceInput": True}),
                "mode": (["1) storyboard preview", "2) full movie (img2vid from storyboard)", "full movie (pure text2vid)"], {"default": "1) storyboard preview", "tooltip": "1) fast storyboard: one preview frame per scene (these become the movie's first frames). 2) full render where each scene starts from its approved storyboard frame (image-to-video). Or skip the storyboard entirely: pure text-to-video."}),
                "width": ("INT", {"default": 1536, "min": 256, "max": 2048, "step": 32}),
                "height": ("INT", {"default": 864, "min": 256, "max": 2048, "step": 32}),
                "fps": ("INT", {"default": 24, "min": 8, "max": 60}),
                "steps": ("INT", {"default": 10, "min": 1, "max": 60}),
                "cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 15.0, "step": 0.1}),
                "sampler": (comfy.samplers.SAMPLER_NAMES, {"default": "euler"}),
                "scheduler": (comfy.samplers.SCHEDULER_NAMES, {"default": "linear_quadratic"}),
                "seed": ("INT", {"default": 42, "min": 0, "max": 2**63 - 1, "control_after_generate": False}),
                "apply_stg": ("BOOLEAN", {"default": True, "tooltip": "Spatio-temporal guidance: better prompt adherence and detail at cfg 1 for a small speed cost."}),
                "preview_size": ("INT", {"default": 0, "min": 0, "max": 1536, "step": 32, "tooltip": "Width of the storyboard preview frames. 0 = match the final width (best: full-quality frames that also guide img2vid). Lower for faster, rougher boards."}),
                "storyboard_strength": ("FLOAT", {"default": 0.75, "min": 0.1, "max": 1.0, "step": 0.05, "tooltip": "How strongly each scene sticks to its storyboard frame in img2vid mode. Lower = more natural/free, higher = more faithful to the exact frame."}),
                "filename_prefix": ("STRING", {"default": "auto_movie"}),
                "output_dir": ("STRING", {"default": "", "tooltip": "Optional extra folder for the final movie. Always also saved to the ComfyUI output folder."}),
            },
            "optional": {
                "upscale_model": ("LATENT_UPSCALE_MODEL", {"tooltip": "Connect the LTX spatial upscaler to sharpen low-res storyboard frames before they guide the full render. Strongly recommended."}),
            },
        }

    def render(self, model, clip, video_vae, audio_vae, scene_plan, mode, width, height,
               fps, steps, cfg, sampler, scheduler, seed, preview_size,
               storyboard_strength, filename_prefix, output_dir="",
               upscale_model=None, use_storyboard_frames=True, apply_stg=True, **_legacy):
        m = str(mode).lower()
        is_preview = m.startswith("1") or "preview" in m or "editor" in m
        want_i2v = (not is_preview) and ("text2vid" not in m) and bool(use_storyboard_frames)
        return self._render(model, clip, video_vae, audio_vae, scene_plan, is_preview, want_i2v,
                            width, height, fps, steps, cfg, sampler, scheduler, seed,
                            preview_size, storyboard_strength, filename_prefix, output_dir,
                            upscale_model, apply_stg)

    def _render(self, model, clip, video_vae, audio_vae, scene_plan, is_preview, want_i2v,
                width, height, fps, steps, cfg, sampler, scheduler, seed, preview_size,
                storyboard_strength, filename_prefix, output_dir, upscale_model, apply_stg=True):
        from comfy_execution.graph_utils import GraphBuilder

        plan = json.loads(scene_plan)
        scenes = plan["scenes"]
        if not scenes:
            raise ValueError("scene_plan contains no scenes")

        run_id = time.strftime("%Y%m%d_%H%M%S")
        width = max(256, (int(width) // 32) * 32)
        height = max(256, (int(height) // 32) * 32)
        phash = _plan_hash(scenes, seed)
        proj_rel = _project_rel(plan, phash)
        board_dir = os.path.join(folder_paths.get_output_directory(), proj_rel, "storyboard")

        def stg_model(g):
            if apply_stg:
                return g.node("LTXVApplySTG", model=model, block_indices="14, 19").out(0)
            return model

        def scene_sampling(g, the_model, sc, i, w, h, frames, latent_ref, pos_ref, neg_ref):
            noise = g.node("RandomNoise", noise_seed=(int(seed) + i) & (2**63 - 1))
            ks = g.node("KSamplerSelect", sampler_name=sampler)
            sch = g.node("BasicScheduler", model=the_model, scheduler=scheduler, steps=int(steps), denoise=1.0)
            gd = g.node("CFGGuider", model=the_model, positive=pos_ref, negative=neg_ref, cfg=float(cfg))
            return g.node("SamplerCustomAdvanced", noise=noise.out(0), guider=gd.out(0),
                          sampler=ks.out(0), sigmas=sch.out(0), latent_image=latent_ref)

        if is_preview:
            pw = int(preview_size) if int(preview_size) > 0 else int(width)
            pw = max(256, (pw // 32) * 32)
            ph = max(256, int(round(pw * height / width / 32)) * 32)
            g = GraphBuilder()
            the_model = stg_model(g)
            frames_ref = None
            for sc in scenes:
                i = int(sc["index"])
                enc = g.node("CLIPTextEncode", clip=clip, text=sc["prompt"])
                zero = g.node("ConditioningZeroOut", conditioning=enc.out(0))
                cond = g.node("LTXVConditioning", positive=enc.out(0), negative=zero.out(0), frame_rate=float(fps))
                vlat = g.node("EmptyLTXVLatentVideo", width=pw, height=ph, length=1, batch_size=1)
                samp = scene_sampling(g, the_model, sc, i, pw, ph, 1, vlat.out(0), cond.out(0), cond.out(1))
                vdec = g.node("VAEDecode", samples=samp.out(0), vae=video_vae)
                g.node("PreviewImage", images=vdec.out(0))  # fires per-scene so the UI fills live
                frames_ref = vdec.out(0) if frames_ref is None else g.node("ImageBatch", image1=frames_ref, image2=vdec.out(0)).out(0)
            board = g.node("AMD_Storyboard", images=frames_ref, scene_plan=scene_plan, columns=4, save_dir=board_dir)
            g.node("PreviewImage", images=board.out(0))
            g.node("PreviewImage", images=frames_ref)
            msg = (f"STORYBOARD saved to: {board_dir}\\storyboard.png (+ scene frames + scenes.txt). "
                   f"Approve it, then set mode='2) full movie (img2vid from storyboard)' and queue again - "
                   f"each scene will START FROM these frames. (Keep seed and prompts unchanged.)")
            return {"result": (msg,), "expand": g.finalize()}

        # ---- full movie ----
        frame_files = [os.path.join(board_dir, f"scene_{int(sc['index']):02d}.png") for sc in scenes]
        use_i2v = want_i2v and all(os.path.isfile(p) for p in frame_files)
        if want_i2v and not use_i2v:
            print(f"[AutoMovieDirector] no matching storyboard frames for plan {phash} - rendering text-to-video. "
                  f"(Run '1) storyboard preview' first with the same prompts+seed to lock compositions.)")

        g = GraphBuilder()
        the_model = stg_model(g)
        paths_ref = None
        for sc in scenes:
            i = int(sc["index"])
            frames = max(9, int(round((float(sc["seconds"]) * fps - 1) / 8.0)) * 8 + 1)
            enc = g.node("CLIPTextEncode", clip=clip, text=sc["prompt"])
            zero = g.node("ConditioningZeroOut", conditioning=enc.out(0))
            cond = g.node("LTXVConditioning", positive=enc.out(0), negative=zero.out(0), frame_rate=float(fps))
            if use_i2v:
                img = g.node("AMD_LoadFrame", path=frame_files[i])
                img_ref = img.out(0)
                if upscale_model is not None:
                    glat = g.node("VAEEncode", pixels=img_ref, vae=video_vae)
                    gup = g.node("LTXVLatentUpsampler", samples=glat.out(0),
                                 upscale_model=upscale_model, vae=video_vae)
                    img_ref = g.node("VAEDecode", samples=gup.out(0), vae=video_vae).out(0)
                i2v = g.node("LTXVImgToVideo", positive=cond.out(0), negative=cond.out(1), vae=video_vae,
                             image=img_ref, width=width, height=height, length=frames,
                             batch_size=1, strength=float(storyboard_strength))
                pos_ref, neg_ref, vlat_ref = i2v.out(0), i2v.out(1), i2v.out(2)
            else:
                vlat = g.node("EmptyLTXVLatentVideo", width=width, height=height, length=frames, batch_size=1)
                pos_ref, neg_ref, vlat_ref = cond.out(0), cond.out(1), vlat.out(0)
            alat = g.node("LTXVEmptyLatentAudio", frames_number=frames, frame_rate=fps, batch_size=1, audio_vae=audio_vae)
            av = g.node("LTXVConcatAVLatent", video_latent=vlat_ref, audio_latent=alat.out(0))
            samp = scene_sampling(g, the_model, sc, i, width, height, frames, av.out(0), pos_ref, neg_ref)
            sep = g.node("LTXVSeparateAVLatent", av_latent=samp.out(0))
            vdec = g.node("VAEDecode", samples=sep.out(0), vae=video_vae)
            adec = g.node("LTXVAudioVAEDecode", samples=sep.out(1), audio_vae=audio_vae)
            wr = g.node("AMD_SceneWriter", images=vdec.out(0), audio=adec.out(0),
                        fps=fps, scene_index=i,
                        run_id=run_id, out_rel=os.path.join(proj_rel, "takes", run_id))
            paths_ref = wr.out(0) if paths_ref is None else g.node("AMD_PathJoin", a=paths_ref, b=wr.out(0)).out(0)

        script_text = ((plan.get("plot", "") + "\n\nCHARACTER SHEET\n" + plan.get("sheet", "")).strip() + "\n\n" +
                       "\n\n".join(f"[Scene {s['index'] + 1} - {s['seconds']:.1f}s]\n{s.get('core', s['prompt'])}"
                                   for s in scenes)).strip()
        st = g.node("AMD_Stitcher", paths=paths_ref, filename_prefix=filename_prefix,
                    plot_text=script_text, run_id=run_id, output_dir=output_dir,
                    proj_rel=proj_rel)
        return {"result": (st.out(0),), "expand": g.finalize()}


class AMD_LoadFrame:
    CATEGORY = "AdamGman/🎬 Auto Movie Director"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "load"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"path": ("STRING", {"default": ""})}}

    def load(self, path):
        import numpy as np
        import torch
        from PIL import Image

        im = Image.open(path).convert("RGB")
        arr = np.asarray(im).astype("float32") / 255.0
        return (torch.from_numpy(arr).unsqueeze(0),)


class AMD_SceneWriter:
    CATEGORY = "AdamGman/🎬 Auto Movie Director"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("scene_path",)
    FUNCTION = "write"
    OUTPUT_NODE = True  # writes a file + emits a thumbnail; must count as a graph output standalone

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "audio": ("AUDIO",),
                "fps": ("INT", {"default": 24, "min": 1, "max": 120}),
                "scene_index": ("INT", {"default": 0, "min": 0, "max": 9999}),
                "run_id": ("STRING", {"default": "run"}),
                "out_rel": ("STRING", {"default": ""}),
            }
        }

    def write(self, images, audio, fps, scene_index, run_id, out_rel=""):
        import numpy as np
        from PIL import Image
        from comfy_api.latest import InputImpl, Types

        rel = out_rel.strip() or os.path.join("auto_movie", run_id)
        out_dir = os.path.join(folder_paths.get_output_directory(), rel)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"scene_{scene_index:02d}.mp4")
        video = InputImpl.VideoFromComponents(
            Types.VideoComponents(images=images, audio=audio, frame_rate=Fraction(fps))
        )
        video.save_to(path, format=Types.VideoContainer.MP4, codec=Types.VideoCodec.H264)
        print(f"[AutoMovieDirector] wrote {path}")

        # emit a mid-frame thumbnail so the scene's box in the UI fills as each scene finishes
        ui = {}
        try:
            mid = (images[images.shape[0] // 2].cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
            tdir = folder_paths.get_temp_directory()
            os.makedirs(tdir, exist_ok=True)
            tname = f"amd_{run_id}_scene_{scene_index:02d}.png"
            Image.fromarray(mid).save(os.path.join(tdir, tname))
            ui = {"images": [{"filename": tname, "subfolder": "", "type": "temp"}]}
        except Exception as e:
            print(f"[AutoMovieDirector] scene thumbnail skipped: {e}")
        return {"ui": ui, "result": (path,)}


class AMD_PathJoin:
    CATEGORY = "AdamGman/🎬 Auto Movie Director"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("paths",)
    FUNCTION = "join"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"a": ("STRING", {"forceInput": True}), "b": ("STRING", {"forceInput": True})}}

    def join(self, a, b):
        return (a + "\n" + b if a else b,)


class AMD_Stitcher:
    CATEGORY = "AdamGman/🎬 Auto Movie Director"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("movie_path",)
    FUNCTION = "stitch"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "paths": ("STRING", {"forceInput": True}),
                "filename_prefix": ("STRING", {"default": "auto_movie"}),
                "plot_text": ("STRING", {"default": ""}),
                "run_id": ("STRING", {"default": "run"}),
                "output_dir": ("STRING", {"default": ""}),
                "proj_rel": ("STRING", {"default": ""}),
            }
        }

    def stitch(self, paths, filename_prefix, plot_text, run_id, output_dir="", proj_rel=""):
        import imageio_ffmpeg

        files = [p for p in paths.splitlines() if p.strip()]
        files.sort()
        if not files:
            raise ValueError("no scene files to stitch")

        out_root = folder_paths.get_output_directory()
        safe_prefix = re.sub(r"[^\w\-]+", "_", filename_prefix) or "auto_movie"
        final_name = f"{safe_prefix}_{run_id}.mp4"
        sub = proj_rel.strip().replace("\\", "/")
        final_dir = os.path.join(out_root, proj_rel.strip()) if sub else out_root
        os.makedirs(final_dir, exist_ok=True)
        final_path = os.path.join(final_dir, final_name)

        list_path = os.path.join(os.path.dirname(files[0]), "concat.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            for p in files:
                f.write("file '" + p.replace("\\", "/").replace("'", "'\\''") + "'\n")

        ff = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ff, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
               "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", final_path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {r.stderr[-1200:]}")

        if plot_text.strip():
            with open(os.path.join(final_dir, "plot.txt"), "w", encoding="utf-8") as f:
                f.write(plot_text.strip() + "\n")

        result_path = final_path
        if output_dir.strip():
            try:
                import shutil
                dest_dir = output_dir.strip()
                os.makedirs(dest_dir, exist_ok=True)
                result_path = os.path.join(dest_dir, final_name)
                shutil.copy2(final_path, result_path)
                print(f"[AutoMovieDirector] copied movie to: {result_path}")
            except Exception as e:
                print(f"[AutoMovieDirector] copy to output_dir failed ({e}); movie is at {final_path}")
                result_path = final_path

        print(f"[AutoMovieDirector] finished movie: {final_path}")
        preview = {"filename": final_name, "subfolder": sub, "type": "output"}
        return {"ui": {"images": [preview], "animated": (True,)}, "result": (result_path,)}


class AMD_Storyboard:
    CATEGORY = "AdamGman/🎬 Auto Movie Director"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("storyboard",)
    FUNCTION = "compose"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "scene_plan": ("STRING", {"forceInput": True}),
                "columns": ("INT", {"default": 4, "min": 1, "max": 8}),
                "save_dir": ("STRING", {"default": ""}),
            }
        }

    @staticmethod
    def _font(size, bold=False):
        from PIL import ImageFont
        for name in (("segoeuib.ttf",) if bold else ("segoeui.ttf",)) + ("arialbd.ttf" if bold else "arial.ttf", "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                continue
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    @staticmethod
    def _wrap(draw, text, font, max_w, max_lines):
        words = text.split()
        lines, cur = [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if draw.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = w
                if len(lines) == max_lines:
                    break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        if len(lines) == max_lines and draw.textlength(" ".join(words), font=font) > max_w * max_lines * 0.98:
            lines[-1] = lines[-1][:max(0, len(lines[-1]) - 1)] + "…"
        return lines

    def compose(self, images, scene_plan, columns, save_dir=""):
        import numpy as np
        import torch
        from PIL import Image, ImageDraw

        plan = json.loads(scene_plan)
        scenes = plan["scenes"]
        n = images.shape[0]
        cols = max(1, min(int(columns), n))
        rows = math.ceil(n / cols)

        tile_w = 460
        src_h, src_w = images.shape[1], images.shape[2]
        tile_h = int(round(tile_w * src_h / src_w))
        bar_h = 92
        gap = 18
        header_h = 96
        BG, CARD, ACCENT = (16, 16, 20), (33, 33, 41), (255, 165, 44)
        board_w = gap + cols * (tile_w + gap)
        board_h = header_h + gap + rows * (tile_h + bar_h + gap)
        board = Image.new("RGB", (board_w, board_h), BG)
        draw = ImageDraw.Draw(board)

        f_title = self._font(30, bold=True)
        f_sub = self._font(15)
        f_chip = self._font(16, bold=True)
        f_time = self._font(14)
        f_body = self._font(14)

        total = sum(float(s.get("seconds", 0)) for s in scenes)
        title = (plan.get("global_prompt") or "Untitled").strip()
        title = title[:70] + ("…" if len(title) > 70 else "")
        draw.rectangle([0, 0, board_w, header_h], fill=(22, 22, 28))
        draw.rectangle([0, header_h - 3, board_w, header_h], fill=ACCENT)
        draw.text((gap + 2, 16), title, fill=(240, 240, 245), font=f_title)
        draw.text((gap + 2, 58), f"STORYBOARD   ·   {n} scenes   ·   {total:.0f}s total   ·   approve, then render the full movie",
                  fill=(150, 150, 162), font=f_sub)

        pil_frames = []
        t_cursor = 0.0
        for i in range(n):
            arr = (images[i].cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
            full = Image.fromarray(arr)
            pil_frames.append(full)
            tile = full.resize((tile_w, tile_h), Image.LANCZOS)
            cx = gap + (i % cols) * (tile_w + gap)
            cy = header_h + gap + (i // cols) * (tile_h + bar_h + gap)
            sc = scenes[i] if i < len(scenes) else {"prompt": "", "core": "", "seconds": 0}
            secs = float(sc.get("seconds", 0))
            draw.rectangle([cx - 1, cy - 1, cx + tile_w + 1, cy + tile_h + bar_h + 1], outline=(58, 58, 70), width=1)
            board.paste(tile, (cx, cy))
            draw.rectangle([cx, cy + tile_h, cx + tile_w, cy + tile_h + bar_h], fill=CARD)
            chip = f"SC {i + 1:02d}"
            chip_w = draw.textlength(chip, font=f_chip) + 16
            draw.rounded_rectangle([cx + 10, cy + tile_h + 10, cx + 10 + chip_w, cy + tile_h + 34], radius=5, fill=ACCENT)
            draw.text((cx + 18, cy + tile_h + 13), chip, fill=(20, 20, 24), font=f_chip)
            tc = f"{int(t_cursor // 60):02d}:{t_cursor % 60:04.1f} – {int((t_cursor + secs) // 60):02d}:{(t_cursor + secs) % 60:04.1f}"
            draw.text((cx + tile_w - draw.textlength(tc, font=f_time) - 12, cy + tile_h + 15), tc,
                      fill=(150, 150, 162), font=f_time)
            text = str(sc.get("core") or sc.get("prompt") or "").replace("\n", " ")
            for li, line in enumerate(self._wrap(draw, text, f_body, tile_w - 24, 2)):
                draw.text((cx + 12, cy + tile_h + 42 + li * 20), line,
                          fill=(214, 214, 224) if li == 0 else (168, 168, 180), font=f_body)
            t_cursor += secs

        if save_dir.strip():
            try:
                os.makedirs(save_dir, exist_ok=True)
                for i, fr in enumerate(pil_frames):
                    idx = int(scenes[i]["index"]) if i < len(scenes) else i
                    fr.save(os.path.join(save_dir, f"scene_{idx:02d}.png"))
                board.save(os.path.join(save_dir, "storyboard.png"))
                with open(os.path.join(save_dir, "scenes.txt"), "w", encoding="utf-8") as f:
                    f.write("NOTE: scene_XX.png are fast low-res composition previews - faces and fine "
                            "detail refine in the full render. Judge framing and story here, not skin.\n\n")
                    f.write((plan.get("plot", "") + "\n\nCHARACTER SHEET\n" + plan.get("sheet", "")).strip() + "\n\n")
                    for s in scenes:
                        f.write(f"[Scene {s['index'] + 1} - {s['seconds']:.1f}s]\n{s.get('core', s['prompt'])}\n\n")
                print(f"[AutoMovieDirector] storyboard saved to {save_dir}")
            except Exception as e:
                print(f"[AutoMovieDirector] storyboard save failed: {e}")

        out = torch.from_numpy(np.asarray(board).astype(np.float32) / 255.0).unsqueeze(0)
        return (out,)


WEB_DIRECTORY = "./js"

NODE_CLASS_MAPPINGS = {
    "AMD_MoviePlanner": AMD_MoviePlanner,
    "AMD_MovieRenderer": AMD_MovieRenderer,
    "AMD_LoadFrame": AMD_LoadFrame,
    "AMD_SceneWriter": AMD_SceneWriter,
    "AMD_PathJoin": AMD_PathJoin,
    "AMD_Stitcher": AMD_Stitcher,
    "AMD_Storyboard": AMD_Storyboard,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "AMD_MoviePlanner": "🎬 Movie Planner (Ollama)",
    "AMD_MovieRenderer": "🎬 Movie Renderer (LTX scenes)",
    "AMD_LoadFrame": "🎬 Load Storyboard Frame",
    "AMD_SceneWriter": "🎬 Scene Writer",
    "AMD_PathJoin": "🎬 Path Join",
    "AMD_Stitcher": "🎬 Movie Stitcher (final MP4)",
    "AMD_Storyboard": "🎬 Storyboard",
}
