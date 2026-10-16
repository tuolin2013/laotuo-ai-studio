---
skill_id: official_h3_prompt_writing
name: MiniMax 官方 H3 提示词（h3-prompt-writing · I2VA / Ref2VA）
description: 官方 MiniMax-H3 h3-prompt-writing 技能原文 + 本项目适配。I2VA 首帧模式按 references/base-en.txt 三段式输出；Ref2VA 全参考模式按 references/ref-en.txt 六段式输出。
version: "1.0"
default: true
source_url: https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing
target_models: [claude-opus-5-5, minimax-h3]
references: [references/base-en.txt, references/ref-en.txt]
variables: [h3_mode, shot_json, category_json, aspect_ratio, duration_seconds, picture_labels]
---
<!-- 以下「Official Skill」一节为 MiniMax-AI/MiniMax-H3 skills/h3-prompt-writing/SKILL.md 原文（去掉了其 YAML 头），请勿修改；项目适配写在后面。 -->

# Official Skill (verbatim)

# H3 Prompt Writing

## Workflow

1. Identify the input mode: T2VA, I2VA, FL2VA, L2VA, or full-reference Ref2VA.
2. For base text/keyframe modes, read `references/base-en.txt` and follow its final prompt structure.
3. For full-reference mode, read `references/ref-en.txt` and follow its six-section rewrite format.
4. Preserve the exact field names, section order, labels, and timing notation from the selected guide.

## Base Modes

- T2VA: build the full audiovisual timeline from text.
- I2VA: start from the first frame and develop forward from it.
- FL2VA: describe the continuous path between the first and last frames.
- L2VA: infer a plausible opening and converge to the supplied last frame.

Use `integrated_multimodal_description`, `overall_soundscape`, and `non_diegetic_music` in the order shown in `references/base-en.txt`.

## Full-Reference Mode

Ref2VA rewrites use `subject_definitions`, `summary`, `retention_analysis`, `detailed_description`, `overall_soundscape`, and `non_diegetic_music` in that order. Reference labels stay consistent across all sections.

Read `references/ref-en.txt` for label rules, retention analysis, and complete examples.

## Output Rules

- Write rewrite sections in English; preserve dialogue, lyrics, and visible scene text in their original language.
- Describe each shot by composition, subjects, environment, actions, camera, sound, and the exact point where referenced content appears.
- Avoid plot summaries, unresolved reference labels, and timing that does not match the requested duration.
## Tips for Better Results
- Always match the total duration of the description to the requested video length (4–15 seconds).
- Keep reference labels consistent (e.g. `<Picture 1>`, `<Video 1>`, `<Audio 1>`) across every section.
- Prefer concrete visual and audio details over abstract words like "cinematic" or "beautiful".
- When using keyframes (I2VA / FL2VA / L2VA), clearly state how the first and/or last frame connects to the timeline.

# Project Adaptation（电商带货短视频）

You are executing the official skill above for one storyboard shot of an e-commerce product video. The selected reference guide is attached after this instruction inside `<reference_file>` tags; follow it exactly.

## Input

- Selected input mode: `{{ h3_mode }}`
  - `i2va` → I2VA mode, follow `references/base-en.txt`: first line is exactly the I2VA instruction `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.`, one blank line, then `integrated_multimodal_description:`, `overall_soundscape:`, `non_diegetic_music:` in that order.
  - `ref2va` → full-reference mode, follow `references/ref-en.txt`: output the six sections `subject_definitions`, `summary`, `retention_analysis`, `detailed_description`, `overall_soundscape`, `non_diegetic_music` in that order, each section name on its own line followed by a colon.
- Target video duration: `{{ duration_seconds }}` seconds (already clamped to the official 4–15 second range). The timeline must end exactly at this duration.
- Aspect ratio: `{{ aspect_ratio }}` (identical to the locked first frame; do not change it).
- Picture labels (fixed by the rendering backend, keep them identical in every section):

{{ picture_labels }}

- Storyboard shot (Chinese, written by the scriptwriter skill):

```json
{{ shot_json }}
```

- Category config (selling points, forbidden words, ambient sound library):

```json
{{ category_json }}
```

## Rules

1. Write every section in precise English. Keep `character_dialogue` lines and visible package text in their original Chinese, verbatim: dialogue goes inside `<d>[Chinese] ...</d>` with a stable speaker ID such as `(S1)`; visible text such as the brand name on the package goes inside English double quotes, e.g. `"张家界莓茶"`.
2. The video has native sound. Never write voice-over or narration unless the shot's dialogue clearly comes from an off-screen person; follow the guide's off-screen voice rules in that case.
3. `audio_mode` mapping: `native_dialogue` → on-screen synchronized speech; `ambient_sfx` → diegetic ambient and physical action sounds only, no speech; `dialogue_and_ambient` → speech first, ambience lower than the voice; `silent` → `overall_soundscape: N/A`.
4. `non_diegetic_music` is `N/A` unless the shot explicitly asks for background music.
5. Describe the timeline with explicit second marks inside the shot (for example `From 0.00 to 1.20 seconds`, `At 2.40 seconds`), covering composition, subject, action, camera motion (type + amplitude + speed), and the exact moment each sound starts.
6. The product packaging's shape, colors, logo, and printed Chinese text stay unchanged for the whole video; do not invent new packaging text.
7. No abstract words such as "cinematic", "beautiful", "stunning", "amazing", "epic"; turn them into concrete visual and audio details. Do not use any of the category's `forbidden_words` and make no efficacy or medical claims.

## Output

Return only one JSON object, no explanation and no Markdown fence:

{"mode": "{{ h3_mode }}", "prompt": "<the complete H3 prompt text in the selected official structure, using \n for line breaks>"}
