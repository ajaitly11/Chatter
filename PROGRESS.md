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
- **Onboarding flow** (`onboarding.py`) — first-run mic + Accessibility
  permission walkthrough, matching the design system.

### Notch HUD (`overlay.py`)
- Docks at the physical notch on notched MacBook displays (falls back to a
  bottom-right pill on external monitors / non-notched Macs), detected via
  `NSScreen.auxiliaryTopLeftArea/auxiliaryTopRightArea`.
- Appears instantly on hotkey press with a quick fade, no slide animation
  (the slide was the main source of felt lag).
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
  **Not yet confirmed working over a genuinely fullscreen app** — see
  Roadmap.

### Push-to-talk reliability (`hotkey.py`, `audio_capture.py`)
- Simplified to a single accurate Whisper pass on release (an earlier
  version also streamed live through a second model for word-by-word
  captions — doubled GPU load and added lag for a caption that wasn't even
  what got pasted).
- Fixed trailing-word truncation with a short release grace period before
  the mic stream actually closes.
- Fixed Whisper's well-documented hallucination on silence ("Thank you.",
  "Thanks for watching!") with an upfront RMS-based silence check, rather
  than trusting Whisper's own no-speech detection.
- Added diagnostic logging (peak volume, input device name) for the
  "recording was real speech but got treated as silence" case, so a
  recurrence is diagnosable from the log instead of guessed at.
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

### Live Dictation tab
- Redesigned layout: compact mascot+title header instead of a large centered
  block, freeing up room for real content below.
- State text is now explicit and literal ("Listening…", "Transcribing…",
  "Cleaning up…", "Done!") instead of the cute rotated phrase — the HUD
  keeps the rotated phrases (fits a small space), the main tab needed to be
  unambiguous.
- Added a **practice text box** — click in, hold the hotkey, watch the
  dictation land directly in the box (works with no special-casing, since
  paste just types at whatever has keyboard focus). Doubles as a
  first-run "try it out" tool.
- Fixed a clipping bug where the listening-state bounce animation could
  cut the mascot's bottom off the widget on the downward half of the cycle.

## Roadmap / not yet done

- **Confirm the notch HUD over a genuinely fullscreen app.** The latest
  fix (level + `isFloatingPanel` + `hidesOnDeactivate`) is applied but
  unverified live — if it's still not showing, the blocker is likely
  something structural rather than a property tweak, and needs a fresh
  angle (possibly a from-scratch NSPanel instead of Qt's Tool-window
  NSPanel).
- **First-time-user guided tour.** The practice box covers "try it
  yourself"; a proper step-by-step walkthrough of the tabs/features is
  still just the onboarding permission flow, not a feature tour.
- **README is stale** — it still describes the earlier dual-model live-
  streaming push-to-talk architecture that's since been simplified to a
  single accurate pass, and doesn't mention the current tab layout, notch
  HUD, or Models tab redesign.
- **Open-source readiness pass.** Stated goal is to publish this — worth a
  dedicated pass on licensing, first-run experience on a machine that's
  never had this configured, and making sure nothing macOS-version- or
  hardware-specific (Apple Silicon vs. Intel, notch vs. no notch) breaks
  silently on someone else's Mac.
- **Dictionary / correction UX** hasn't had a dedicated design pass — it's
  functional (table + add/remove) but not yet held to the same design bar
  as the rest of the app.
- **Models tab** may need to grow back some browsing capability if the
  minimal link-out version proves too manual in practice — worth
  revisiting once there's real usage to react to, rather than guessing
  upfront.
