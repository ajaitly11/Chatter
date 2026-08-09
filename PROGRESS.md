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
- **Tabbed main window** (`main_window.py`) — Live Dictation, Files, Models,
  Dictionary, History, Settings. Resizable/maximizable (was fixed-size),
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
- **Models tab** now uses progressive disclosure: active slots first, a short
  in-app chooser second, and external model links only when the user asks for
  them.
- **MTP acceptance** remains optional and machine-specific: compare a short
  cleanup phrase with the toggle off/on before considering it a default.
