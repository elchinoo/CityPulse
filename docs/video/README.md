# Tutorial video: build and review cut

`docs/VIDEO-SCRIPT.md` is the script, `record.spec.ts` the recorder (real Chrome, 1920x1080, Playwright video), `build.py` turns both into the
finished file with narration and captions.

```bash
cd docs/video
npm install                                   # the project's own devDependencies (@playwright/test); needs Google Chrome
python3 build.py --deliver ../../../videos/review   # script + recorder -> out/citypulse.mp4 and .srt, copied to the review folder
```

Needs: the local Kokoro text-to-speech tool `../../../videos/tts` (`narrate.py` and its `.venv`; override with `TTS_DIR`), `ffmpeg`/`ffprobe`
on PATH (Homebrew is fine), Node, PostgreSQL 17 binaries on PATH (or `RECORD_PG_BIN`) and `../../.venv` with CityPulse installed. Nothing is
downloaded; no live service or real credential is used. A take lasts about 5 minutes of warm-up (the fleet must have history) plus the video
itself, so allow about 15 minutes.

What `build.py` does:

1. **Narration.** `narration.txt` is the spoken text, written for listening (short sentences, one `## <scene id> <voice>` section per scene of
   VIDEO-SCRIPT.md). Blank line = pause, a line `...` = short beat, `[pause 18]` = deliberate long pause over something happening on screen
   (the burst building up, the recovery). `videos/tts/narrate.py scenes` speaks it with Kokoro (local neural voices; nothing leaves the machine) and
   writes `out/audio/scene-<id>.wav` plus `narration.json` (scene durations and per-sentence cues). The voice changes with the topic, British
   accent throughout, alternating female and male:

   | Topic | Scenes | Voice |
   |---|---|---|
   | Introduction and start | 1, 2 | `bf_isabella` (female) |
   | Reading the dashboard | 3, 4 | `bm_george` (male) |
   | Feeding it data | 5, 6 | `bf_emma` (female) |
   | Breaking it and recovering | 7, 8, 9 | `bm_lewis` (male) |
   | Watching it, recap | 10, 11 | `bf_isabella` (female) |

   Pronunciation respellings live in the shared `videos/tts/pronounce.json` and the project's `pronounce.json` (spoken text only; captions keep
   the written form). Every build rewrites the NARRATION column of VIDEO-SCRIPT.md from `narration.txt`, so the script and the voice never
   diverge: edit `narration.txt`, not the table.
2. **Recording.** The recorder runs with `SCENE_INFO` (cue start offsets) and `SCENE_DURATIONS` (audio + 0.7 s per scene). Each on-screen action
   starts when the narration sentence it illustrates begins (`at(k)` = the k-th sentence of the scene, counted from 0 across paragraphs) and a
   scene never ends before its audio plus 0.7 s. A fresh throwaway Postgres and CityPulse (role `all`, city `paris`) are created for every take
   (ports from `CITYPULSE_PORT_RANGE`, default 41500-41599). Tokens (`cp_...`) are blurred the moment they are rendered. The burst scenes run at
   real speed: nothing is fast-forwarded. The narration pauses (`[pause 18]` in scene 8, `[pause 26]` in scene 9) are sized for the measured
   timings (degraded about 30 s after the button, recovery 15 to 36 s after Stop) and the recorder checks the live verdict at those moments
   (printed as `CHECK` lines; `FAIL` means the narration does not match the picture: lengthen the pause and rebuild).
3. **Assembly.** Scene 2 must be filmed before scene 1 (the Overview needs a signed-in session), so the build cuts the recording back into script
   order using the recorder's wall-clock marks and a calibration frame, places each scene's audio at its start time and encodes H.264 yuv420p +
   AAC + a soft subtitle track (mov_text) with `+faststart` to `out/citypulse.mp4`.
4. **Captions.** `out/citypulse.srt` is timed from the measured cue times (long sentences are split in two cues).

Options: `--skip-record` re-assembles from the last recording in `out/pw`, `--narration-only` only synthesises audio and prints durations; `BUILD_OUT`
moves `out/`, `--name` changes the output name. Environment: `BUILD_TMP` (scratch for the throwaway Postgres and the ffmpeg shim),
`PLAYWRIGHT_BROWSERS_PATH`, `CITYPULSE_PORT_RANGE`, `RECORD_WARMUP_S` (shorter warm-up for a rehearsal only), `RECORD_PG_BIN`, `RECORD_CITYPULSE_BIN`.

If Playwright's own ffmpeg build is not installed, `build.py` creates a private browsers directory under `BUILD_TMP` (default `out/tmp`) that
links the existing browsers and points Playwright's ffmpeg slot at the system ffmpeg (no download).
If a scene gains, loses or reorders sentences, adjust the `at(k)` anchors in `record.spec.ts` (the recorder fails loudly when it asks for a
sentence that does not exist). Scene 3 ends with the heatmap click that opens Sensors (there is no separate scene 3b in the narrated cut).

`out/` is git-ignored; do not commit media.
