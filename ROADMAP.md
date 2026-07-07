# 🎬 Roadmap — what takes this to the next level

An audit of where Auto Movie Director stands at v0.4.0 and the ordered plan forward.
The theme of every item: **close the gap between "clips stitched together" and "a film."**

## Where we are (v0.4.0)

✅ idea → three-act script with shot grammar & sound design (any Ollama LLM, auto-pulled)
✅ storyboard approval loop with live per-scene boxes, thumbnails streaming in as they render
✅ full-res storyboard frames guiding img2vid renders · STG prompt adherence · Crisp Enhance
✅ one project folder per movie · versioned releases with workflow assets

**Honest gaps:** scenes are islands (world/character drift between cuts) · re-rendering one bad
scene means re-queueing the film · no music/narration · no titles · hard cuts only ·
character faces drift · single-pass sampling below the official two-stage quality ceiling.

---

## v0.5 — "It feels like a movie" *(quick wins, days)*

| # | Feature | How |
|---|---|---|
| 1 | **Per-scene re-roll** | Override-box syntax `[seed 123]` per scene + docs. ComfyUI's cache already skips unchanged scenes — change one scene, only that scene re-renders, stitcher reuses the rest. Iterate one bad scene in ~2 min instead of re-rendering the film. |
| 2 | **Title & end cards** | LLM names the film; PIL-rendered title card (2 s, styled like the storyboard header) and end card enter the stitch. Instant production feel. |
| 3 | **Transitions** | ffmpeg `xfade`: cut / crossfade / dip-to-black. The LLM assigns one per scene boundary in the plan; stitcher applies. |
| 4 | **Scene continuity chaining ("flow mode")** | Scene N+1's first frame = scene N's LAST frame (img2vid), for boundaries the LLM marks as continuous. Locations and light stop teleporting between cuts. |

## v0.6 — "It sounds like a movie"

| # | Feature | How |
|---|---|---|
| 5 | **Narration** | LLM writes a narrator line per scene; local TTS renders the voiceover; mixed under the scene's generated audio (ducked). |
| 6 | **Music bed** | One continuous music cue across the film: generated (ACE-Step / Stable Audio if installed) or a user-supplied track folder, ducked under effects & narration. |

## v0.7 — "Faces stay the same"

| # | Feature | How |
|---|---|---|
| 7 | **IC-LoRA character lock** | Generate one canonical portrait per character from the character sheet, feed it to every scene via LTX's IC-LoRA guide nodes. The real fix for identity drift. |
| 8 | **Two-stage quality pipeline** | Official LTX recipe: base render → latent upsample ×2 → short refine pass. "Max quality" mode beside the current fast mode. |

## v0.8 — Director's chair

| # | Feature | How |
|---|---|---|
| 9 | Re-roll / pin buttons on each scene row (no syntax needed) | JS + per-scene seed state |
| 10 | Render progress % on the node; per-scene audio prompt field | progress websocket events |
| 11 | Project browser: reopen any past movie's plan and continue | reads project folders |

## Ship & share

- Publish to the ComfyUI Registry (`comfy-cli publish`) → installable by name in Manager
- 30-second demo reel at the top of the README
- Civitai / community post once v0.5 lands
