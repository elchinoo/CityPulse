# Tutorial video: recorder and build

One command goes from `docs/VIDEO-SCRIPT.md` and the recorder to the finished review cut:

```bash
cd CityPulse/docs/video
npm install                                   # @playwright/test only; uses the machine's Google Chrome
python3 build.py --deliver ../../../videos/review   # narration + recording + mux -> out/build/citypulse.mp4 and .srt, copied to the review folder
```

Output: `out/build/citypulse.mp4` (1920x1080, H.264 yuv420p, AAC, faststart, captions as a soft `mov_text` track) and `out/build/citypulse.srt`.
Everything stays local: a throwaway Postgres on 127.0.0.1, a real PagerLens and CityPulse on free ports (from `CITYPULSE_PORT_RANGE`, default
`41500-41599`). A fresh database on every take. `out/` is git-ignored; do not commit media. Allow about 15 minutes (5.5 minutes of warm-up before
the camera starts, so the charts and the roll-up have history, plus the video itself).

Needs: macOS, `ffmpeg`/`ffprobe`, Node 22+, Google Chrome, Postgres 17 binaries with pgvector and `pg_trgm` on `PATH` (or `RECORD_PG_BIN`), the
`CityPulse/.venv` (CityPulse installed) and `pagerlens/.venv` next to it (the alert hand-off of scene 5 runs the real PagerLens), and the local
Kokoro tool folder `videos/tts` (its own `.venv` and models; nothing is downloaded; shared and never edited from here).

Structure (problem first, about 5 minutes; story: *my data is time series, JSON, geo and vectors*; landing moment: "look how much easier this is"):

| Scene | Topic | Voice | Local or captured |
|---|---|---|---|
| 1 | The problem (a city, sensors from many vendors, "the usual answer is four databases") and who is who: PLATFORM = EDB Postgres on AgentPG, REFERENCE SCAFFOLDING = identity, gateway, vault (swap any of it, said at second 18), SAMPLE APP = CityPulse and its simulator | `am_liam` | diagram |
| 2 | The map: sensors with lat/lon, outlines derived from the positions, health colours, "what is near this point, in metres" (`ST_DWithin`) | `af_heart` | sensor detail and "Map not available" local; the map captured; the query is labelled source text |
| 3 | One SQL statement across five models (the Explain tab) | `am_eric` | relational, time series, JSON and the SQL local; all five models captured |
| 4 | Live data: the simulator, a new vendor's JSON + position batch, the JSONB query, the burst, the verdict falling | `af_heart` | local |
| 5 | The alert hand-off: PagerLens opens an incident | `am_liam` | local (a real PagerLens) |
| 6 | Insights: edb_stat_monitor, edb_pg_tuner, query_advisor | `af_heart` | "Not available" local; everything else captured |
| 7 | A read-only user: reads the fleet, is denied the secret tables | `am_eric` | local (real `psql`) |
| 8 | Recap: look how much easier this is; try it yourself | `af_heart` | cards |

**Real versus captured.** A local Postgres has no PostGIS and no `edb_*` extensions (`CREATE EXTENSION postgis` fails here: "extension is not
available"), so those parts are never faked. The Map, the geo and vector part of the Explain card and the Insights come from real captures of the
live AgentPG fleet (`videos/live-captures/`: `citypulse-map-gotham-*.jpg`, `citypulse-explain-*.jpg`, `paybroker-insights-*.jpg`,
`extensions-installed.txt`, `ecoctl-insights.txt`), shown as stills with a slow zoom and highlight boxes under the banner *CAPTURED from the live
AgentPG fleet, 28 Oct 2026*. Local scenes carry a green badge *RUNNING LOCALLY*. CityPulse has no AI model, so there is no stand-in model. Nothing
is shown for high availability, failover or AIDB. The live fleet is never touched by the build.

What `build.py` does (same shape as the TrustDesk and PayBroker builds):

1. Checks `narration.txt` against the NARRATION column of the scene table of `VIDEO-SCRIPT.md` (`python3 build.py --sync-script` rewrites the
   column and the word counts from `narration.txt`; edit `narration.txt`, never the table).
2. Speaks `narration.txt` with `videos/tts/narrate.py scenes` (Kokoro, local, speed 1.08): one wav per scene and `narration.json` (scene durations and
   the start and end of every sentence). The heading of every section names the voice, `## 3 am_eric`. Respellings for the voice live in
   `pronounce.json` (spoken text only; captions keep the written form), on top of the shared `videos/tts/pronounce.json`.
3. Runs `record.spec.ts` (Playwright, real Chrome) with `NARRATION_JSON`: every scene lasts at least 0.2 s lead + narration + 0.5 s tail (audio
   + 0.7 s) and each on-screen action waits for the sentence that illustrates it (`await at(page, 'words of the sentence')`), so a click, a
   callout or a page change starts as that sentence begins. The recorder writes `scene-times.json` with a `sync` list (cue time against actual time,
   `lateBy`), the `checks` made on the live burst and run notes.
4. ffmpeg places each scene's audio at its start time, encodes, and adds the captions. It stops with an error if any scene is shorter than its
   narration + 0.7 s, and prints how many cues were more than 1 s late.

The recorder asserts what the script claims (the new vendor's batch is accepted, the vendor appears in the `psql` grouping, five sections on the
Explain card, the Map and Insights tabs say they are not available locally, the burst makes the verdict fall, PagerLens opens an incident that
names the city, the read-only user is denied `api_token`, `/readyz` reports masking ok), so a take that drifts from the script fails instead of
recording something false. The burst scene prints `CHECK` lines (verdict at the moment of "There it is", seconds since the press, a timeline);
`FAIL` means the narration does not match the picture: move the cue or lengthen the narration and rebuild.

Writing narration: one `## <scene id> <voice>` section per scene; short sentences, one idea each; a blank line is a pause, `...` a short beat,
`[pause 1.5]` a deliberate pause (the shared tool halves it); never change voice inside a scene; voices only `am_liam`, `af_heart`, `am_eric`,
male and female alternate, no voice twice in a row, never `af_nicole`. Acronyms are respelled hyphenated in `pronounce.json` (`PostGIS` is
post-gee-eye-ess, `JSONB` jay-son-bee). If you change a sentence the recorder anchors an action to, update the matching `at(...)` text in
`record.spec.ts` (it fails loudly when no sentence contains the text).

Options and environment: `--deliver DIR` copies the mp4 and srt; `--work DIR` moves `out/build`; `--keep-recording` re-mixes a previous take;
`--reuse-audio` keeps the narration already spoken; `--narration-only` prints scene durations and cue times; `--tts-dir` points to another
Kokoro folder. `RECORD_WARMUP_S` (default 330) shortens the warm-up for a rehearsal (`RECORD_WARMUP_S=60 HOLD=0.6 npx playwright test`, which is not
the final timing), `CITYPULSE_PORT_RANGE` limits the ports, `RECORD_PG_BIN`, `RECORD_CITYPULSE_BIN`, `RECORD_SIM_SPEED`, `E2E_SCRATCH` (where the
throwaway data lives), `HEADED=1` shows the browser. Playwright's own ffmpeg build is not required: `build.py` creates a private
`PLAYWRIGHT_BROWSERS_PATH` under the work folder that runs the system `ffmpeg` (no download).

Never in the video: tokens (the terminal cards use shell variables, the browser never opens Settings), the one-time setup code and the demo
password (used off camera only; the databases are deleted at the end).
