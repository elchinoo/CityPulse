# Video script: CityPulse in six minutes

**Title:** CityPulse: break a smart city on purpose, and watch Postgres keep score

**Length:** about 6 minutes 30 seconds (about 800 words of narration, roughly 5 minutes 45 seconds at 140 words a minute, plus pauses while actions run on screen).

**Audience:** customers and EDB colleagues who know what a database is but not necessarily what an ingest gateway or a status engine is. No prior knowledge of the ecosystem is assumed.

**Goal:** by the end the viewer can (1) start CityPulse, (2) read the dashboard, (3) send their own readings, (4) press the burst button and understand what each fault does, and (5) see where CityPulse sits next to PagerLens, the identity provider and the CityPulse Simulator.

Companion document: [GUIDE.md](GUIDE.md). Every behaviour named below is described and verified there.

---

## Prerequisites and seed data (reproducible locally, nothing live)

Do the setup off camera, except where a scene says to show it. This script was rehearsed end to end with the Playwright recorder in [video/record.spec.ts](video/record.spec.ts) against a local build (throwaway Postgres, no Docker); the corrections found are folded in below. To record automatically instead of by hand, see "Recording notes" at the end.

1. Folder layout: `CityPulse` and `agentpg-kit` side by side (only needed if you run without Docker; the Docker route does not need the kit checkout).
2. Docker running. In the `CityPulse` folder:
   ```bash
   cp .env.example .env
   python3 -c "import secrets; print(secrets.token_urlsafe(24))"     # paste the result as POSTGRES_PASSWORD in .env
   grep CITYPULSE_CITY .env                                          # must read CITYPULSE_CITY=paris
   docker compose up --build -d
   docker compose logs api | grep -A2 "first-run setup"              # shows the setup code (scene 2)
   ```
   The app is at `http://127.0.0.1:8002`. The database is on `127.0.0.1:5434`, loopback only.
3. A throwaway admin password for the recording, at least 12 characters (for example `citypulse-demo-2026`). It protects a local container that is deleted afterwards.
4. Warm-up: after the first sign-in wait **at least 5 minutes** so the Overview charts have history and the heatmap is full (3,000 simulated sensors, each reporting every 30 to 60 seconds or so).
5. Create an ingest token (scene 5 does it on camera; do it once beforehand too so the curl command is ready). Save it in a shell variable in the recording terminal:
   ```bash
   export CITYPULSE_URL=http://127.0.0.1:8002
   export CITYPULSE_INGEST_TOKEN=<the cp_ token shown once in Settings>
   ```
6. A second city, pushed live in scene 5 (one command, saved in a text file `lyon.sh` so it can be pasted):
   ```bash
   curl -sS -X POST "$CITYPULSE_URL/api/readings" \
     -H "Authorization: Bearer $CITYPULSE_INGEST_TOKEN" -H "Content-Type: application/json" \
     -d '{"readings":[
       {"sensor":"lyon.temperature-presqu-0001","type":"temperature","district":"presqu","city":"lyon","value":21.4},
       {"sensor":"lyon.humidity-presqu-0002","type":"humidity","district":"presqu","city":"lyon","value":58.0},
       {"sensor":"lyon.rain-vaise-0003","type":"rain","district":"vaise","city":"lyon","value":0.2},
       {"sensor":"lyon.wind-vaise-0004","type":"wind_speed","district":"vaise","city":"lyon","value":3.1},
       {"sensor":"lyon.temperature-vaise-0005","type":"temperature","district":"vaise","value":19.0}
     ]}'
   ```
   The fifth reading carries a district but no city, so the gateway rejects it. Expected answer: `accepted` 4, `rejected` 1, `"errors":["1 x city is required"]` and a `hint` that starts "Every reading needs a city". (A reading missing the district too would be rejected first as "bad district", which is why this variant keeps it.)
   The four Lyon sensors stop reporting after the push, so after about 1.5 minutes they turn **stale** and after about 6 minutes **offline**, which makes Lyon appear under "Cities needing attention". Push the Lyon readings roughly 2 minutes before the take where you want it to look unhealthy, or accept that it looks healthy at first and degrades while you talk.
7. Browser: a clean window at 1920x1080 (what the recorder uses; 1440x900 also works for a manual take), zoom 100%, light theme, a single tab at `http://127.0.0.1:8002`. Terminal: large font, two panes (one for curl, one for `docker compose logs -f api`).
8. Optional PagerLens (only for a fuller take of scene 10; the script works without it): a local PagerLens and its ingest token, then add `PAGERLENS_URL` (a localhost URL) and `PAGERLENS_TOKEN` to the CityPulse container's environment. The reporter refuses plain `http` unless the host is localhost or 127.0.0.1.

Reset between takes: `docker compose down -v && docker compose up -d` (removes the volume, so the setup code and password flow can be filmed again).

---

## Scenes

Timestamps are cumulative. Narration is spoken text; read it plainly.

| # | Time | ON-SCREEN ACTION | NARRATION | On-screen callouts |
|---|---|---|---|---|
| 1 | 0:00 - 0:27 | Title card, then the Overview already full of data (all green, "All systems operational", heatmap, two charts). Slow mouse sweep over the banner and the heatmap. **Shooting order:** the Overview only exists after sign-in, so film this scene right after scene 2 and cut it to the front. | "This is CityPulse. It is a sample smart city, running entirely on Postgres. Thousands of sensors report in. The app decides whether the city is healthy. And a big red button lets you break things on purpose. We built it so a monitoring tool can practise finding a real incident. You will start it, read it, and feed it your own data. Then you will break it." | Title: "CityPulse on Postgres". Lower third: "Sample project on Postgres, extensions optional." |
| 2 | 0:27 - 0:58 | Terminal pane: `docker compose up --build -d`, then `docker compose logs api` with the setup box visible; highlight the code. Switch to the browser, a fresh window at `http://127.0.0.1:8002`: type the code, type the demo password twice if asked, click the button to finish. | "Starting it takes Docker, and one command. The first time, the app has no users. So it denies everything, and prints a one-time setup code in its log. Here is the code. Open the page, type the code, and choose an admin password. It needs at least twelve characters. The code lives only in memory, and changes on every restart. So nobody can guess a way in. In a company, you would sign in through an identity provider instead." | Highlight the setup code box. Callout: "One-time code, memory only." Callout: "Password: 12+ characters." |
| 3 | 0:58 - 2:06 | Overview. Point at: the banner and percentage; the KPI tiles (Sensors, OK, Stale, Offline, Fault, Stuck, Simulator buffer); the heatmap (hover one cell to show its title, for example "Harbor · Rain gauge: 9 of 9 healthy"; **clicking a cell opens Sensors filtered to that district and type**, and the Sensors tab then shows that type with State "All sensors"); the two charts. | "Now the dashboard. This is the Overview, the page you land on after signing in. The banner at the top is the verdict for the whole city. Operational means at least ninety-seven percent of sensors are healthy. Below that, it is degraded. Below eighty-five percent, a partial outage. Below fifty, a major outage. The tiles count sensors by state. A sensor is stale after three missed reports, and offline after twelve. It is faulty when its value is physically impossible. And stuck when it repeats the same number. The heatmap crosses districts with sensor types. Hover over a cell, to see how many of its sensors are healthy. The two charts show health, and incoming readings, for the last hour. Nothing is smoothed. The status engine re-judges every sensor every ten seconds. Click a heatmap cell, and the Sensors tab opens for that district and type." | Callout on thresholds: "97% / 85% / 50%". Callout on tiles: "stale 3, offline 12 missed reports". Callout: "Re-judged every 10 s." |
| 4 | 2:06 - 2:29 | Click **Sensors**. The State filter shows "Problems only" and the empty message "No sensor has a problem right now." (verified when the fleet is healthy). Change State to **All sensors**, type `temperature` in the search box, click the first row to open the detail with the last readings. | "The Sensors tab is where you drill down. By default, it shows only sensors with a problem. And right now, there are none. Switch the filter to all sensors, and search for part of a sensor name. Open one sensor, to see its last sixty readings. And the reason it was judged the way it was." | Callout: "Default filter: problems only." |
| 5 | 2:29 - 3:11 | Click **Connect**; scroll to the push example. Click **Settings**, create a token named `demo sender` with scope **Ingest only**; show the token once and click Copy. Switch to the terminal and paste the `lyon.sh` command (the fifth reading has no city). Show the JSON answer: `accepted 4`, `rejected 1`, the `hint`. | "Next, your own data. We start with the Connect tab. It shows ready-made examples for pushing readings. Now, in Settings, we create an ingest token. It is shown once, and only a hash is stored. It can push readings, and nothing else. Now we send a batch, with curl. Every reading names its sensor, its type, and its district. And it names a city. On purpose, I left the city off one reading. Look at the answer. The app accepted four, and rejected one. It counted the bad one, and the hint tells you how to fix the sender. Bad data never fails the whole batch. And it is never silently lost." | Callout: "Ingest token: push only." Callout on the response: "accepted 4, rejected 1". Highlight the `hint` text. |
| 6 | 3:11 - 3:32 | Back in the browser, open the **City** menu in the top bar, type `ly`, pick **lyon**. Show the Overview scoped to Lyon (chip "lyon"); open the City menu again and choose all cities; point at "Cities needing attention" if Lyon is listed. | "Every sensor belongs to one city, fixed by its first reading. The City menu in the top bar filters every tab. Type a few letters, pick Lyon, and the Overview shows only that city. Choose all cities, and you are back to the whole picture. The card called cities needing attention lists them by severity, worst on top." | Callout: "City is required on every reading." |
| 7 | 3:32 - 4:17 | Click **Burst**. Read the big card. Leave duration at 3 minutes. Click **BURST**. Toast "Burst started" appears; the Active table lists seven rows with "Ends in" (for example "3 min"). Use CityPulse with role `all` (the compose default) for this scene: only then do all seven faults act, see GUIDE 7.3. | "Now the fun part. This is the burst tab. One press starts seven faults at once. A flood of readings. A slow database. And corrupt messages. Noisy sensors. Stuck sensors. And late data. And one whole district going dark. They stop by themselves after the time you choose, from one to ten minutes. Bursts only act on the built-in simulator, and on the gateway. Real sensors you connect are never altered. We keep three minutes. Now, press burst. Seven faults are active, and each one counts down. Nothing is visible yet. So let us go and watch the city." | Callout: "7 faults, auto-expire." Callout on duration: "1 to 10 minutes". |
| 8 | 4:17 - 5:43 | Click **Overview**. Show the amber "Burst active" banner (it names the faults and the time left). **Timing measured at normal speed:** nothing visible for the first 25 s or so, then "Degraded" at about 30 s, "Partial outage" at about 50 s and "Major outage" at about 90 s after the press. Reasons name the worst sensor types and, later, districts. The Stale tile climbs first, Offline after about 2 minutes; Fault and Stuck stay at zero or a handful, so do not promise them. Open **Sensors** (filter "Problems only"); the reason seen is "no reading for 3+ intervals". Show the terminal pane pushing the batch in parallel (12 curls at once: one curl on its own often squeezes through) to catch a `429`. Press Stop at about 75 s to stay inside the scene. | "Back on the Overview, an amber banner says a burst is active. Nothing shows for the first half minute. So watch the verdict. [pause 5] The stale tile is the first to move. [pause 7] There it is. The verdict has slipped to degraded. The flood and the slow database fill the gateway's write slots. Readings queue up, and sensors start missing reports. Stale sensors climb first. Offline ones come later. The banner now names the worst sensor types, and later, the districts. Open Sensors, with the problems filter. The reason is simple: no reading for three or more intervals. Meanwhile, we push readings in parallel, twelve at once. The full gateway answers with a 429, with a Retry-After header. That is how it tells senders to back off, instead of running out of memory. Back on the Overview, the city keeps sliding. Corrupt messages show up as rejected readings. And late data lands in older chart buckets, which is itself a clue. Every symptom has a visible cause." | Callout: "Verdict follows the sensors." Callout on terminal: "HTTP 429 + Retry-After = back-pressure." |
| 9 | 5:43 - 6:33 | Click **Burst**, then **Stop everything**. Return to Overview and wait; the banner returns to operational and the Burst active banner disappears. **Measured:** about 15 to 20 s after Stop when stopped at 40 to 75 s into the burst, about 35 s when stopped after 170 s (the simulator first has to drain its backlog). Speed this part up in the edit. | "To end it early, open the Burst tab, and press Stop everything. Now the backlog has to drain. That can take up to forty seconds. Watch the Overview. [pause 9] The simulator is draining its backlog. [pause 9] There. The verdict is operational again, and the burst banner is gone. Once readings flow again, sensors turn healthy on the next pass. Nothing is latched. So the verdict never lies about the present." | Callout: "Recovery in under a minute." |
| 10 | 6:33 - 7:20 | Terminal: `curl -s $CITYPULSE_URL/healthz` then `curl -s -H "Authorization: Bearer $METRICS_TOKEN" $CITYPULSE_URL/metrics \| grep citypulse_fleet` (use a **metrics** token created beforehand). Then show the Connect tab's last two snippets (pagerlens-agent and PAGERLENS_URL). If PagerLens is running, cut to its incident for 10 seconds; otherwise show only the snippets. | "Last topic: how CityPulse is watched, and where it fits. Create one more token, with the metrics scope only. First, the health endpoint. It needs no login. Then the metrics endpoint, with that token. It shows the fleet health ratio, the sensor counts, and the ingest counters. A monitoring tool such as PagerLens can scrape those. Or CityPulse can report its own health, and raise an alert for each degraded city. The Connect tab shows the snippets for both. In the full ecosystem, the CityPulse Simulator feeds it sensors. The identity provider signs people in, as admin, operator, or viewer. And PagerLens opens, and investigates, the incident." | Callout: "No login: /healthz. Metrics token: /metrics." Diagram card (3 seconds): Simulator -> CityPulse -> PagerLens, identity beside them. |
| 11 | 7:20 - 7:51 | Return to the Overview, all green. Final card with three bullets and the repository path. | "To recap. CityPulse keeps sensor data, judgement, and fault injection, in Postgres. You start it with one command. You feed it with scoped tokens. You read it on one dashboard. And you break it with one button. Try it yourself. Clone the project, and run docker compose up. Press the red button. Then read the guide in the docs folder, to connect it to PagerLens, and the rest of the fleet." | Final card: "docker compose up", "Press BURST", "docs/GUIDE.md". |

### Word budget check

Narration per scene is roughly 75, 85, 100, 50, 85, 40, 60, 90, 45, 105 and 70 words: about 800 in total, about 5 minutes 45 seconds spoken at 140 words a minute. The remaining 45 seconds or so are silent action (typing the code, waiting for the verdict to fall, waiting for recovery). If the take runs long, cut the last sentence of scenes 2 and 10.

---

## Recap and call to action (as spoken in scene 11)

Recap: Postgres, one command to start, scoped tokens, one dashboard, one burst button. Call to action: clone, `docker compose up`, press the red button, read `docs/GUIDE.md`.

---

## Recording notes: the Playwright recorder

[video/record.spec.ts](video/record.spec.ts) performs every browser scene of this script in the installed Google Chrome at 1920x1080 with Playwright video on, slow human pacing (visible cursor, typed text, pauses), and visible callouts (an amber frame and a caption) that match the "On-screen callouts" column. It starts its own throwaway stack (PostgreSQL on 127.0.0.1 with a database named `citypulse_recording_test`, CityPulse role `all`, city `paris`), so it needs neither Docker nor a running CityPulse, and deletes everything afterwards. Terminal scenes (2, 5, 8, 10) are drawn as an on-page terminal card that runs the real `curl` commands against the local app; swap them for a screen capture of a real terminal if you prefer.

```bash
cd docs/video
npm install                          # @playwright/test only; uses the machine's Google Chrome
python3 build.py --deliver ../../../videos/review   # narration (Kokoro) + recording + mux; see docs/video/README.md
```

Output goes to `docs/video/out/` (git-ignored): the `.webm` video and `scenes.json` (scene start times in seconds, plus measured facts such as the time the verdict fell). Knobs: `RECORD_WARMUP_S` (default 300), `RECORD_PACE` (multiplies every pause), `RECORD_BURST_WATCH_S` (default 75), `RECORD_NO_VIDEO=1` (rehearse without ffmpeg), `RECORD_PG_BIN`, `RECORD_CITYPULSE_BIN`, `HEADED=1`. Prerequisites: PostgreSQL 17 binaries on `PATH` (or `RECORD_PG_BIN`) and the project's virtualenv (`.venv`) with CityPulse installed.

Order in the video: the recorder films scene 2 first (the Overview needs a session), then scene 1; cut scene 1 to the front. In the narrated cut the heatmap click that opens Sensors is the last beat of scene 3.

Selectors verified in a real browser (all from `static/`):

| Step | Locator |
|---|---|
| Setup | `#setup-code`, `#setup-pw`, `#setup-pw2`, the card's submit button; then `#nav` is visible |
| Tabs | `a[data-tab="overview" \| "sensors" \| "burst" \| "connect" \| "settings"]` |
| Verdict | `.status-banner`, KPI tiles `.kpis` |
| Heatmap | cells are `<button>` elements, `.heat td button` (class `h0`..`h4` by health, `none` for an empty cell with `aria-hidden`; use `:not(.none)`); `title` and `aria-label` read "District · Type: n of m healthy"; a click goes to `#sensors/<district>/<type>` |
| Sensors | `#f-state` (values `problem`, `all`, `ok`, `stale`, ...), `#f-type`, `#f-district`, `#f-q`; rows `tbody tr.clickable`; detail `.facts`; button "back to the list" |
| Token | `#tk-name`, `#tk-scope` (`ingest`, `metrics`, `read`, `full`), button "Create token"; the token is in `#view-settings .codewrap code` (scope it: the hidden Connect tab has `.codewrap code` too), shown once |
| City picker | `#city-picker button.city-btn` (reads "City: All cities"), `#city-q`, options `#city-panel li.city-opt`; chip `.scope-chip` |
| Burst | `#storm-secs` (default 180), `button.burst`, button "Stop everything", toast `#toast`, Active table `#view-burst table tbody tr` (7 rows), overview banner `.banner` containing "Burst active" |

Notes for the run:

- The UI sends the CSRF header itself. Direct API calls with a bearer token do not need it; calls that rely on the session cookie must send `X-CityPulse-CSRF: 1` on writes.
- The Overview redraws every 5 seconds, and a tab redraws once when you arrive: wait a moment after clicking a tab before typing (the recorder does), or the typed text can be lost.
- Never use a real password or a real URL in the recording; the stack is local and disposable.
- Cut the PagerLens segment of scene 10 if PagerLens is not running; the narration still holds.

## Changes since the recording (not re-recorded)

The Map and Insights tabs, the optional `lat`/`lon` and the read-only hardening were added after this script was recorded. Nothing below was re-recorded; the script text was edited only where a fact became wrong ("plain Postgres" became "Postgres", and the scene 1 lower third now says extensions are optional, in this file, in `video/narration.txt` and in the two callout strings of `video/record.spec.ts`).

- **Selectors.** The recorder reaches tabs through `a[data-tab="<tab>"]` and views through `#view-<tab>`, so the new tabs break none of them. The top bar now has seven tabs (Overview, **Map**, Sensors, Burst, Connect, **Insights**, Settings) instead of five: every frame that shows the bar looks different, and at narrow widths it may wrap. A heatmap cell still opens `#sensors/<district>/<type>`; the Sensors tab also accepts `#sensors/<district>/-` (from the Map) and then starts on "All sensors".
- **Scenes whose frames changed.** 1, 3, 4 (nav bar; Overview unchanged), 5 (the Connect tab is unchanged, but a sensor detail now shows a Position row when the sensor has one, and the built-in simulator's sensors do), 6 (Settings unchanged), 9 and 10 (nav bar). Scene 3 is the natural place to add the Map (a district click opening Sensors); that needs new narration and a recording.
- **Narration facts still true.** "Thousands of sensors", "12 districts", seven bursts, tokens, the PagerLens segment. The recap sentence "keeps sensor data, judgement, and fault injection, in Postgres" is still true; the new capabilities are not mentioned.
- **Needs PostGIS to look right.** The Map is empty ("Map not available") on a database without PostGIS, which is what the recorder's throwaway Postgres 17 is; a take that shows the Map must use a PostGIS-enabled database.

