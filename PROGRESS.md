# Chatter — Progress & Roadmap

A running record of what's been built and what's next, covering the redesign
from a bare-bones push-to-talk tool into the current app: a tabbed native
window, a notch-docked HUD, and a Warm Terracotta design system, aimed at
being something worth open-sourcing rather than a vibe-coded prototype.

## Done so far

### Design system
- **Warm Terracotta** palette (`theme.py`, `style.qss`) — one set of colors
  shared between Qt stylesheets and hand-painted widgets (mascot, HUD),
  converted from the original Claude Design mockup's OKLCH values.
- **Mascot** (`mascot.py`) — a blob character with one consistent silhouette
  reused at any size (large on the Live Dictation tab, small in the HUD),
  with distinct listening / processing / done / error poses and animations
  (bounce, tilt, settle-squash). Geometry unified across states after early
  versions drifted in size between them.
- **Tabbed main window** (`main_window.py`) — Live Dictation, Transcribed Files,
  Dictionary, History, Insights, and Settings. Resizable/maximizable (was fixed-size),
  native titlebar recolored to match the app's own background instead of
  system gray.
- **Squiggle tab underline** — hand-drawn wavy underline instead of a plain
  line, animates smoothly between tabs instead of snapping.
- **Onboarding flow** (`onboarding.py`) — first-run microphone, Input
  Monitoring, and Accessibility permission walkthrough, matching the design
  system and explaining that processing stays on-device.

### Notch HUD (`overlay.py`)
- Docks at the physical notch on notched MacBook displays (falls back to a
  bottom-right pill on external monitors / non-notched Macs), detected via
  `NSScreen.auxiliaryTopLeftArea/auxiliaryTopRightArea`.
- Appears immediately on hotkey press with a short fade; on a real notch the
  overlay now starts at the measured physical notch width and sweeps outward
  on both sides while growing down into the HUD, so it reads as the hardware
  extending rather than a separate panel appearing.
- The notch window now starts at the top of the display and includes the
  menu-bar/notch plane in the same black surface; the status content is laid
  out below that plane so the expanded HUD no longer reads as two stacked
  rectangles.
- Iterated repeatedly on making it read as *the notch itself expanding*
  rather than a separate panel: true black fill in notch mode, shape
  simplified down to a plain rectangle (square top, rounded bottom) after a
  more elaborate concave-corner shape didn't read as cleaner in practice.
- System notifications removed entirely — the HUD is the only feedback
  surface for the press/hold/release cycle now, per direct feedback that
  notification banners (fixed top-right, auto-dismissing) were the wrong
  fit for a hold-to-talk interaction.
- Multiple passes on showing up over **other apps' fullscreen Spaces**:
  corrected an over-high window level (was reaching for
  `NSScreenSaverWindowLevel`, actually needed only `NSMainMenuWindowLevel +
  3`, matching boring.notch's reference implementation), added
  `hidesOnDeactivate=False` and `isFloatingPanel=True` (confirmed via
  direct PyObjC introspection that neither was set by default, and that
  setting `isFloatingPanel` has an undocumented side effect of resetting
  the window level — reordered the calls so the explicit level wins).
  **Confirmed by user:** the notch HUD is visible over a genuinely fullscreen
  Codex app on the built-in MacBook display.

### Push-to-talk reliability (`hotkey.py`, `audio_capture.py`)
- Simplified push-to-talk to one ASR model: the streaming-capable local model
  shows committed/tentative text while speaking and finalizes that same
  transcript on release. The optional local language model is the only
  additional pass in this workflow.
- Added non-blocking warm-up for the streaming session so model loading does
  not land on the first hotkey press. File transcription retains a separate
  batch-model slot because it is a different workflow.
- Moved Quartz hotkey callbacks onto Qt's event thread and added event-tap
  recovery/error feedback instead of mutating controller state directly from
  the native callback thread.
- Reduced microphone handoff chunks to 200ms for more responsive streaming
  updates and added an explicit microphone selector in Settings.
- Upgraded the default streaming ASR to NVIDIA Nemotron 3.5
  (`nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf`), with locale-aware English
  configuration and a balanced 240ms lookahead setting.
- Persisted microphone names instead of PortAudio indexes, so reconnecting a
  headset cannot silently select a different device. An empty selection still
  follows macOS's system default.
- Fixed trailing-word truncation with a short release grace period before
  the mic stream actually closes.
- Fixed Whisper's well-documented hallucination on silence ("Thank you.",
  "Thanks for watching!") with an upfront RMS-based silence check, rather
  than trusting Whisper's own no-speech detection.
- Added diagnostic logging (peak volume, input device name) for the
  "recording was real speech but got treated as silence" case, so a
  recurrence is diagnosable from the log instead of guessed at.
- Lowered the speech-energy gate for quiet microphones and now records the
  peak RMS level on every completed utterance, so a weak real signal is not
  discarded before streaming finalization and a silent device is actionable.
- Fixed a major accuracy confounder found in the live log: an older Chatter
  process was still listening alongside the moved app, causing duplicate
  recordings and conflicting pasted results. Added an Application Support
  instance lock and `LSMultipleInstancesProhibited` bundle setting.
- English decoding is the default instead of letting short/quiet English
  clips auto-detect as an unrelated language. The Settings tab can switch
  back to Auto-detect for multilingual dictation.
- The streaming transcript is gated until real speech energy appears,
  preventing the model's silence hypothesis from surfacing as live text.
- The press sound is requested before HUD geometry work, so it is triggered at
  key-down just like the visual listening state.
- The MacBook notch remains the stable anchor, with a synchronized fallback
  HUD mirrored onto the active external display when the user is working
  there.
- The HUD now receives the complete streaming draft, follows the newest
  words instead of permanently showing only the opening fragment, and can
  grow modestly for longer phrases before eliding. Both notch and mirror
  surfaces use a true black background.
- The optional local cleanup model is debounced on a background worker while
  Nemotron continues streaming; stale cleanup results are discarded so they
  cannot overwrite newer speech.
- Reworked the cleanup prompt around transcript-preserving rules: punctuation
  follows grammar rather than pauses, fused words and segment-overlap repeats
  are repaired, verbal corrections keep the final version, and introductory
  phrases/list items are preserved. The request token budget is now sized to
  the utterance, deterministic, and transport-level reasoning/labels are
  stripped before paste.
- Added a conservative local self-correction pass for explicit spoken
  walk-backs (`no`, `wait`, `I mean`, `actually`, `sorry`) before Gemma sees the
  input, plus stable bullet formatting for explicit shopping/list requests.
  This keeps the common correction/list cases reliable even when a small
  cleanup model returns a comma run or repeats the walked-back phrase.
- Added an experimental Gemma MTP setting with automatic matching-head
  detection. The exact local E2B target/head pair starts successfully, but a
  short Apple M3 benchmark was slower than the existing path, so MTP is
  explicitly opt-in and does not affect ASR by default.
- The static bundle icon reuses the mascot's shared silhouette geometry, and
  fused-word corrections such as `goodmorning` → `good morning` are eligible
  for the auto-learning dictionary.
- Added unit tests for silence trimming, persisted SRT words, and a real local
  streaming-session smoke test when the Nemotron model is available.
- Never touches the system clipboard on paste (`paste_action.py`) — types
  directly via simulated keystrokes — so whatever the user had copied
  isn't clobbered by a dictation.
- Auto-learning correction dictionary (`correction_watcher.py`,
  `dictionary.py`) — watches the field after a paste for manual edits and
  learns them as future corrections.

### History (`history.py`, History tab)
- Persistent JSONL log of every push-to-talk result *and* every file
  transcription, kept even if a dictation only got copied (not pasted).
- Copy and Clear actions per the Files/History tabs; the History tab now
  auto-refreshes when a new dictation lands instead of requiring a manual
  Refresh click.

### Models tab
- Went through two designs. First: a live-queried, searchable Hugging Face
  catalog (transcribe.cpp ASR models by tag, chat GGUF models by search
  query) with inline download. Direct feedback: the list read as confusing
  and the search gave bad results (e.g. searching "Qwen" surfaced only one
  hit despite many existing repos). **Replaced** with a minimal per-slot
  view: the currently active transcription/cleanup model, an "Import .gguf
  file…" picker (for a file already downloaded via the browser), and a
  link out to Hugging Face to go find one.
- **llama-server runtime** (`llama_runtime.py`) — the text-cleanup pass
  needs a separate native binary this app doesn't bundle. Added detection
  (PATH, Homebrew locations, prior download) and a one-click download of
  the official prebuilt binary from llama.cpp's GitHub releases, picked
  dynamically from the release's actual asset list (not a hardcoded
  filename) so an upstream naming change doesn't silently break it.
- Added separate Accuracy model and Live preview model slots. Imported live
  preview models are checked for `supports_streaming=True` before being saved.

### Live Dictation tab
- Redesigned layout: compact mascot+title header instead of a large centered
  block, freeing up room for real content below.
- State text is explicit and literal ("Listening…", "Finishing audio…",
  "Transcribing…", "Cleaning up…", "Done!") across both the HUD and main
  tab; the mascot supplies the personality while the state remains clear.
- Added a **practice text box** — click in, hold the hotkey, watch the
  dictation land directly in the box (works with no special-casing, since
  paste just types at whatever has keyboard focus). Doubles as a
  first-run "try it out" tool.
- The notch HUD is the live transcript preview surface; the main tab keeps a
  clean practice box for the committed result instead of duplicating the same
  streaming text in a second card.

### Latest character and motion polish
- Slightly lowered and spread the ears in the shared mascot geometry so the
  in-app character, Dock/Launchpad icon, and menu-bar icon use one larger,
  vertically centered silhouette.
- Done now resizes the HUD to its compact notch width and moves it immediately
  to the physical notch center, clearing the long-phrase right-hand extent.
- The first tab underline is initialized after tab layout and its wave phase
  travels across the target tab during a switch, avoiding the malformed
  first-frame squiggle.
- The icon generator now emits only valid macOS iconset sizes, avoiding stale
  icon files that newer `iconutil` versions reject.
- The HUD now stays at one fixed 520px width—the previous maximum—for the
  entire listening/processing/Done cycle. Live text is centered in the stable
  surface, and the mascot remains visible and gently animated through
  listening, processing, Done, and error states.
- Fixed a clipping bug where the listening-state bounce animation could
  cut the mascot's bottom off the widget on the downward half of the cycle.
- Added an in-app three-step model guide with links, replacing the external
  Markdown guide flow; added clearer Models, History search/empty states,
  Dictionary learning guidance, and Settings section hierarchy.
- Strengthened cleanup with deterministic post-processing for fused words,
  explicit shopping/list bullets, spoken self-corrections, and obvious pause
  punctuation. The correction watcher now waits and retries after paste so
  Accessibility-backed edits have time to settle before learning.
- Rebuilt the app with PyInstaller as a real arm64 Chatter executable and
  installed it at `/Applications/Chatter.app`; the bundle now identifies as
  Chatter in macOS privacy settings instead of Python 3.
- Added an AVFoundation microphone preflight and visible permission status;
  a newly packaged Chatter now requests microphone access as Chatter before
  PortAudio opens the selected device, preventing silent zero-RMS captures
  when the old Python permission entry is stale. Push-to-talk now stays
  disabled until the Chatter-specific microphone, Input Monitoring, and
  Accessibility permissions are ready.
- Reordered onboarding so Accessibility is granted before Input Monitoring;
  the latter now probes the same listen-only event tap used by the hotkey and
  no longer reopens System Settings on every click when macOS has not refreshed
  its permission state yet.
- Fixed the recurring onboarding trap: each permission pane now opens only
  once, the dialog polls macOS and changes to Continue when the toggle is
  recognized, and Set up later lets a new user reach the app without being
  forced into a reopening loop.
- Accessibility onboarding now validates both AX trust and Core Graphics
  post-event access, actively refreshes the TCC state when Check is pressed,
  resumes at the first missing permission, and offers a restart/recheck when
  macOS has not refreshed the current process. The stale Accessibility entry
  for `com.chatter.app` was reset during live verification so the current
  `/Applications/Chatter.app` can register cleanly.

## Acceptance checks still needed

- **Confirm the notch HUD over a genuinely fullscreen app.** The latest fix
  (level + `isFloatingPanel` + `hidesOnDeactivate`) is applied and the app has
  been relaunched, but this remains a live acceptance check because the agent
  cannot reliably drive a user's fullscreen/Spaces setup without changing
  their active workspace.
- **Run the microphone acceptance check.** The Settings dropdown and Test
  button now make the selected input explicit; verify the intended device has
  a non-zero RMS level before testing transcription.
- **Optional feature-tour pass.** The practice box and permission walkthrough
  cover first-run use; a richer tour of every tab is intentionally deferred
  until the core dictation path has been validated in real use.
- **Open-source readiness pass.** Stated goal is to publish this — worth a
  dedicated pass on licensing, first-run experience on a machine that's
  never had this configured, and making sure nothing macOS-version- or
  hardware-specific (Apple Silicon vs. Intel, notch vs. no notch) breaks
  silently on someone else's Mac.
- **Dictionary / correction UX** now has an explicit learning explanation and
  a searchable, high-confidence auto-learning path; a deeper visual pass can
  still follow real usage.
- **Advanced model settings** now use progressive disclosure: active slots
  first, a short in-app chooser second, and external model links only when the
  user asks for them.
- **MTP acceptance** remains optional and machine-specific: compare a short
  cleanup phrase with the toggle off/on before considering it a default.

### Local Insights dashboard
- Added an in-app **Insights** tab that summarizes Chatter's own dictation
  history without sending analytics anywhere: words dictated, speaking pace,
  today's volume, personal vocabulary, daily rhythm, active-day streaks,
  foreground-app context, cleanup usage, paste success, and average finishing
  time.
- Added a small time-range selector (this week, last 30 days, all time) and a
  refresh action. The dashboard is a view over the existing local history and
  dictionary files, so clearing history also removes the source for these
  summaries.
- New dictation records retain only the local metadata needed for those
  summaries: word count, captured audio duration, context app/mode, cleanup
  usage, paste result, and pipeline finishing time. No window contents or
  transcript analytics leave the Mac.
- Added the Insights destination to the Chatter menu-bar/native application
  menu and unit coverage for the summary calculations.
- Reworked Insights around a visual “voice rhythm” story: a single speaking
  pace meter with clear Slow / Conversational / Fast anchors, daily rhythm
  bars, active-day signals, named-app destination bars, and a local-pipeline
  health row. Empty states now say when there is not enough data rather than
  drawing misleading zero-height charts. Sessions without recorded audio
  duration are excluded from the pace calculation.
- Added an always-visible Live Dictation setup card whenever Microphone,
  Input Monitoring, or Accessibility is missing. It resumes the existing
  one-time permission flow and the background permission watcher starts the
  hotkey listener as soon as the final toggle is recognized.

### First UX/UI pass
- Kept **Dictionary** as a first-class destination and renamed **Files** to
  **Transcribed Files** so the two core product workflows are explicit.
- Removed **Models** from the everyday top navigation. Model files, runtime
  downloads, and experimental Gemma MTP controls now open from a calmer
  **Advanced settings** dialog.
- Added a plain-language local setup summary to Settings and enlarged the
  default window slightly so the six core destinations have room to breathe.
- Renamed the Dictionary experience to **Your growing vocabulary** and made
  its local learning behavior more visible without claiming that Chatter
  retrains the speech model.
- Added **Dictations** as a first-class Insights metric alongside words,
  speaking pace, daily volume, and learned vocabulary.
- Applied the first UX cleanup pass to History and Settings: grouped cards,
  shorter guidance, a discoverable push-to-talk toggle, a Writing card for
  local cleanup/context, and a separate Advanced settings entry point for
  technical model controls. History now searches the full local store while
  keeping the screen readable by showing the latest 100 results.
- History rows now use a vertical text/metadata structure, and clearing
  history refreshes Insights immediately. Insights shows only named foreground
  apps (it does not guess or surface a noisy “Unclassified” bucket) and
  rebuilds its context rows cleanly on refresh.
- Organized the menu-bar status item into Dictation, Writing, Open, and
  Permissions groups, while the native Chatter application menu exposes the
  same core destinations with a standard Settings… command.
- Added restrained native Qt motion: a one-time window entrance fade, a short
  tab-content fade, and a small press response for buttons and toggles. These
  run on the UI thread and do not add work to audio capture or transcription.
- Added a second responsive UX pass for full-screen windows: Insights and
  Settings now use centered max-width content instead of stretching across
  the display, and scrollable pages stay aligned to the top. Removed the
  machine-specific “recommended setup” banner from everyday Settings.
- Replaced the raw Insights bar chart with a local speaking calendar. Each
  date uses a terracotta intensity gradient based on words spoken, keeps the
  date number legible, and shows words, dictations, and top destination on
  hover. The unused local pipeline metrics were removed from the main story.
- Fixed Insights destination-row duplication by rebuilding each row inside a
  dedicated widget; rows now show a count and percentage without stale labels
  or overlap after refresh/maximize. History now supports app/date filters and
  uses a compact copy icon for secondary actions.
- Replaced the barely visible global opacity pulse with a visible native Qt
  button sheen/outline and themed animated switches for push-to-talk and local
  cleanup. The effects are paint-only and stay outside the audio path.
- Expanded the native macOS menu into familiar File, Edit, Dictation,
  Insights, View, Window, and Help sections while keeping the status-item menu
  useful for background quick actions.
- Rewrote `docs/index.html` around the product experience: hotkey, notch HUD,
  local processing, optional cleanup, and guided model setup. The landing page
  now has several download CTAs, so `update_release_page.py` updates every
  tagged DMG link instead of requiring exactly one.

### Current reliability and interaction pass
- Fixed file transcription failures caused by requesting word timestamps from
  models that only expose segment timestamps (or no timestamps). Chatter now
  reads the loaded model capability, downgrades safely, and retries once with
  the native automatic default if a provider reports status 12.
- Transcribed Files now keeps model/backend selection in the background and
  offers a compact Export menu: TXT for every result, plus SRT and WebVTT when
  word- or segment-level timings are available.
- Added Command+Backspace line clearing to the live practice editor without
  changing ordinary word deletion or the behavior of other text fields.
- Added an optional Tap-to-toggle persistent dictation mode. Hold-to-talk
  remains the default and both modes use the same recorder and streaming ASR
  pipeline, so the option does not add a second transcription model.
- Consolidated the native Mac menu into app, file, edit, dictation,
  permissions, view, window, and help sections; Edit actions target the
  focused field, with Copy falling back to the latest transcript when no field
  is focused.
- Applied a consistent Avenir Next hierarchy, bordered themed menus, combobox
  popups, and calendar tooltips. Insights destination percentages now use the
  total named sessions and group smaller destinations as Other apps, avoiding
  misleading bar lengths and duplicate-looking labels.
- File exports now distinguish word-level from phrase-level subtitles. Token
  timing is grouped into real word rows when a model exposes tokens; a
  segment-only model keeps word-level export disabled instead of generating
  inaccurate synthetic word timings.
- Replaced the prototype tap-to-toggle activation with an opt-in double-tap
  gesture. It starts and stops one persistent session while leaving the
  default hold-to-talk path immediate and unchanged.
- Added both Command+Backspace and Command+Delete handling to the Live
  Dictation editor, including native Qt shortcuts as a fallback for macOS key
  event variations.
- Made hands-free behavior explicit in Settings and the HUD: double-tap the
  selected hotkey to start, double-tap it again to stop, and releasing the
  key alone does not end the persistent session. The persistent stop grace
  period is shorter so the finish action feels immediate.
- Added a visible Word level / Phrase level selector to Transcribed Files.
  Word level is now the default and automatically prefers the bundled
  Parakeet TDT model, which was verified locally to return real per-word
  timings. Whisper remains available for phrase-level exports.
- Hardened Command+Backspace/Delete by claiming the macOS ShortcutOverride
  event before QTextEdit's own shortcut handling can consume it.
- Added a local update checker that reads the installed bundle version,
  compares it with the public GitHub latest release, shows the version in
  Settings, and delivers a native macOS update notification once per release.
- The compact menu-bar surface now shows local words/dictations for today,
  links to Insights, and can be hidden or restored from Settings or its own
  menu. Its QMenu styling uses Chatter's terracotta surface and orange accent
  instead of a generic Qt menu treatment.
- File history records the exact local model selected for each transcription
  and whether the result actually contains word, phrase, or no timing. The
  Files tab shows that provenance so a Whisper phrase-only result cannot be
  mistaken for a word-level export.
- The live practice editor also exposes Delete Current Line in Chatter's
  native Edit menu, alongside direct Command+Backspace/Delete handling.
