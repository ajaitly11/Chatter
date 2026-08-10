# Chatter

A small native macOS transcription app built on
[transcribe.cpp](https://github.com/handy-computer/transcribe.cpp) (see also
[this write-up](https://workshop.cjpais.com/projects/transcribe-cpp)) instead
of whisper.cpp directly. Two ways to use it:

- **File transcription** — open an audio/video file, pick a model, transcribe,
  export `.txt` or `.srt`.
- **Push-to-talk** — hold your chosen key (default **Right Shift**, pick from
  the "Push-to-talk key" dropdown in the main window — Right/Left Shift,
  Option, Control, Command, or Caps Lock) anywhere on your Mac and speak.
  One streaming-capable local ASR model transcribes the audio while you speak,
  finalizes its own transcript on release, and inserts that result. There is
  no second ASR pass in the push-to-talk path. The optional local cleanup model
  is the only additional model used there.

The current push-to-talk model is
`nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf`. The older Nemotron and Moonshine
files remain available as fallbacks. File transcription still has a separate
batch-model slot because it is a different workflow.

An optional local-LLM pass (any Gemma/Llama-family GGUF model served by
`llama-server`) cleans up filler words, punctuation, grammar, repeated words,
and verbal self-corrections before pasting. It runs entirely on the Mac.

## 1. Install ffmpeg

Needed to decode mp3/mp4/mov/whatever into the raw PCM transcribe.cpp expects
(only used for file transcription — the push-to-talk path records mic audio
directly in the right format).

```bash
brew install ffmpeg
```

## 2. Set up Python and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`transcribe-cpp` ships prebuilt native wheels for macOS (Apple Silicon), so
`pip install` should just work. If it doesn't find a wheel for your platform,
see "Building from source" below.

## 3. Download models

Use the short [model guide](docs/model-guide.md), or open **Models → How to
choose** inside Chatter. Start with one live model:

- **Live dictation:** Nemotron 3.5 streaming Q8_0.
- **Optional cleanup:** a small 2B–4B instruct GGUF.
- **File transcription:** Whisper large-v3 Turbo or Parakeet TDT.

The guide recommends a setup by unified memory, explains what each model does,
and links directly to download searches. Chatter rejects a live model that
does not support streaming.

## 4. Run it

```bash
python main.py
```

Or build a double-clickable app once your venv/models are set up:

```bash
./packaging/build_app.sh
open Chatter.app
```

`Chatter.app` is a frozen arm64 bundle with a real `Chatter` executable. The
large GGUF model directory stays outside the bundle and is linked into the
app at build time, so rebuilding does not duplicate the models. Rebuild it
any time with `./packaging/build_app.sh`.

To make it appear in Launchpad and the Applications folder, copy the generated
app once:

```bash
ditto Chatter.app /Applications/Chatter.app
open -a /Applications/Chatter.app
```

## Push-to-talk setup

Push-to-talk needs three macOS permissions, granted to Chatter:

1. **System Settings → Privacy & Security → Accessibility** — add Chatter,
   needed to simulate the Cmd+V paste.
2. **System Settings → Privacy & Security → Input Monitoring** — add Chatter,
   needed to detect the global Right Shift hold.
3. **System Settings → Privacy & Security → Microphone** — grant when
   prompted, needed to record your voice.

The permission entry must be the installed app at `/Applications/Chatter.app`.
Enable Chatter when macOS asks for permission; if Chatter is missing, click
**+**, choose `/Applications/Chatter.app`, and turn Chatter on. Do not change
permissions for unrelated apps. Chatter's onboarding checks macOS's actual
cross-application authorization rather than accepting a temporary test
listener. After enabling Chatter, return to the app and continue once;
quitting and reopening the same installed release should not require you to
repeat setup.

The Settings tab lets you choose and test the input device, toggle local AI
cleanup, and choose an automatic writing context. Automatic context uses only
the foreground app and window title to distinguish email, notes, coding/AI,
and social writing; it never reads the page or document.

The **Insights** tab is a local activity view over Chatter's saved dictation
history. It shows word volume, speaking pace, active-day streaks, foreground
app context, dictionary/cleanup activity, paste success, and finishing time.
It does not use an account or network analytics; clearing dictation history
also removes the source used for these summaries.

If push-to-talk is unavailable, the Live Dictation tab shows exactly which
macOS permission is missing and a **Finish setup** button. Once the final
toggle is enabled, Chatter starts the listener automatically without a
relaunch.

Once granted, choose the microphone in Settings, hold Right Shift anywhere,
speak, and release — a status HUD appears at the MacBook notch and shows live
partial text while you talk. When you are working on an attached display, the
same HUD is mirrored there so it remains in your field of view. The final
(optionally cleaned-up) text pastes
wherever your cursor is focused. The compact HUD follows the newest words
of a long live transcript; the full draft remains visible in the Live
Dictation tab. Even without
Accessibility granted, the result is always copied to the clipboard, so
manual Cmd+V works as a fallback. Toggle push-to-talk on/off from the
menu-bar (tray) icon; closing the main window does not quit the app, only
"Quit Chatter" from the tray menu does.

### Custom dictionary + auto-learning

Chatter keeps a personal dictionary of words the ASR consistently mishears
(accents, names, jargon) — manage it from the "Custom Dictionary" table in
the main window, or let Chatter learn automatically: after a push-to-talk
paste, it watches the field you pasted into (via the same Accessibility
trust used for paste simulation — nothing else is monitored) for about a
minute. If you correct exactly one word — or insert a missing space in a
fused word — it's saved to the dictionary and applied to every transcript
from then on, both as a direct substitution and as a hint to the AI cleanup
pass. This is word-level personalization, not
model retraining: `transcribe.cpp` is inference-only, so the ASR's actual
recognition of your voice/accent doesn't change, but confirmed corrections
do get remembered and reapplied.

## AI cleanup (optional)

Chatter can run raw transcripts through a small local LLM to fix punctuation,
strip filler words, recognize explicit self-corrections, and clean the latest
live preview. Clear spoken shopping/list requests are rendered with stable
item boundaries. Long transcripts get a dynamic output budget instead of an
arbitrary 512-token ceiling. Toggle it with the
"Clean up with local AI (parallel) ✨" checkbox. It runs on a background
thread while Nemotron continues transcribing, so the streaming path and HUD
do not wait for cleanup; the final paste uses a ready cleanup result when one
is available. The final transcript is pasted as one operation rather than
typed character by character, while Chatter restores the clipboard it
temporarily uses for that paste:

1. Have a `llama-server` binary (from
   [llama.cpp](https://github.com/ggml-org/llama.cpp)) and a chat-capable
   GGUF model (Gemma/Llama/Qwen family) somewhere on disk.
2. Set `llama_server_bin` and `llama_model_path` in
   `~/Library/Application Support/Chatter/config.json` (created after first
   run) to their absolute paths.

If unconfigured or the server fails to start, Chatter silently falls back to
the raw transcript — cleanup is never required for either flow to work.

The Settings tab also exposes an experimental Gemma multi-token prediction
toggle. It auto-detects a matching `MTP/` head beside the configured GGUF,
but is off by default because short cleanup requests can be slower on some
Apple Silicon/llama.cpp combinations. It never changes the one-model
Nemotron ASR path.

## Notes

- SRT export only works for models that return segment timestamps (Whisper
  family does; some Parakeet/Canary variants don't expose them yet — the
  button is greyed out when that's the case).
- transcribe.cpp serializes one run per model session; Chatter keeps one
  persistent session per flow (file transcription and push-to-talk each have
  their own), so within a flow only one transcription runs at a time.
- Gemma's chat template defaults to emitting a chain-of-thought block before
  its answer, which can eat the whole token budget and return an empty
  cleanup result — Chatter disables it via `chat_template_kwargs:
  {enable_thinking: false}`. Worth checking for if you swap in a different
  reasoning-capable model for the formatting pass.
- If you hit a `transcribe_cpp` import error, check
  `transcribe_cpp.backends()` in a Python shell to see what's registered on
  your machine — the API surface is still evolving (library is v0.1.x).

## Known limitations

- **Permission reset after repackaging**: macOS privacy grants are tied to the
  executable identity. The public DMG uses a stable release signing identity,
  but local ad-hoc builds, development certificates, or moving a locally
  rebuilt app can require granting Chatter access again; this does not affect
  the model or configuration files.
- **Not portable**: `Chatter.app` hardcodes this checkout's absolute paths at
  build time. Cloning the repo elsewhere requires rebuilding it there with
  `./packaging/build_app.sh`.

## Building transcribe.cpp from source (only if no prebuilt wheel exists)

```bash
git clone https://github.com/handy-computer/transcribe.cpp
cd transcribe.cpp
cmake -B build-shared -DTRANSCRIBE_BUILD_SHARED=ON
cmake --build build-shared --target transcribe
```

Then point Chatter at it before running:

```bash
export TRANSCRIBE_LIBRARY=/path/to/transcribe.cpp/build-shared/src/libtranscribe.dylib
```
