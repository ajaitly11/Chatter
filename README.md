# Chatter

A small native macOS transcription app built on
[transcribe.cpp](https://github.com/handy-computer/transcribe.cpp) (see also
[this write-up](https://workshop.cjpais.com/projects/transcribe-cpp)) instead
of whisper.cpp directly. Two ways to use it:

- **File transcription** — open an audio/video file, pick a model, transcribe,
  export `.txt` or `.srt`.
- **Push-to-talk** — hold **Right Option (⌥)** anywhere on your Mac, speak,
  release, and the transcribed (optionally AI-cleaned-up) text is pasted at
  your cursor. Wispr-Flow-style.

Both flows share one persistent, Metal-accelerated `transcribe.cpp` session so
push-to-talk stays fast — the model is loaded once, not on every hotkey press.

An optional local-LLM pass (any Gemma/Llama-family GGUF model served by
`llama-server`) cleans up filler words and punctuation before pasting.

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

## 3. Download at least one model

Models are GGUF files hosted under the `handy-computer` org on Hugging Face.
Drop them into `models/`. Two solid starting points:

- `whisper-large-v3-turbo-Q8_0.gguf` — great general-purpose accuracy, 100+ languages
- `parakeet-tdt-0.6b-v2-Q8_0.gguf` — fast, English-only, no length cap per call

Grab these from `https://huggingface.co/handy-computer` — look for the
`<model>-gguf` repos and download the quant you want (Q8_0 is a good default).

## 4. Run it

```bash
python main.py
```

Or build a double-clickable app once your venv/models are set up:

```bash
./packaging/build_app.sh
open Chatter.app
```

`Chatter.app` is a thin wrapper around this checkout's venv — it isn't a
portable/frozen bundle, so it only runs from this machine/folder. Rebuild it
any time with `./packaging/build_app.sh`.

## Push-to-talk setup

Push-to-talk needs two macOS permissions, both granted to the running Python
process (macOS shows it as **"Python"** in the permission list, since this
isn't a signed/frozen app — see "Known limitations" below):

1. **System Settings → Privacy & Security → Accessibility** — add Python
   (or Terminal, if you're running via `python main.py`), needed to simulate
   the Cmd+V paste.
2. **System Settings → Privacy & Security → Input Monitoring** — add the same,
   needed to detect the global Right Option hold.
3. **System Settings → Privacy & Security → Microphone** — grant when
   prompted, needed to record your voice.

Once granted, hold Right Option anywhere, speak, and release — the text
pastes wherever your cursor is focused. Toggle push-to-talk on/off from the
menu-bar (tray) icon; closing the main window does not quit the app, only
"Quit Chatter" from the tray menu does.

## AI cleanup (optional)

Chatter can run raw transcripts through a small local LLM to fix punctuation
and strip filler words. It's on by default (toggle with the "Clean up with
AI ✨" checkbox) but does nothing until configured:

1. Have a `llama-server` binary (from
   [llama.cpp](https://github.com/ggml-org/llama.cpp)) and a chat-capable
   GGUF model (Gemma/Llama/Qwen family) somewhere on disk.
2. Set `llama_server_bin` and `llama_model_path` in
   `~/Library/Application Support/Chatter/config.json` (created after first
   run) to their absolute paths.

If unconfigured or the server fails to start, Chatter silently falls back to
the raw transcript — cleanup is never required for either flow to work.

## Notes

- SRT export only works for models that return segment timestamps (Whisper
  family does; some Parakeet/Canary variants don't expose them yet — the
  button is greyed out when that's the case).
- transcribe.cpp serializes one run per model session; Chatter keeps a single
  persistent session for both the file-open flow and push-to-talk, so only
  one transcription runs at a time.
- If you hit a `transcribe_cpp` import error, check
  `transcribe_cpp.backends()` in a Python shell to see what's registered on
  your machine — the API surface is still evolving (library is v0.1.x).

## Known limitations

- **Permission churn**: because `Chatter.app` just execs the venv's Python
  rather than being a signed, frozen bundle, macOS ties Accessibility/Input
  Monitoring grants to the Python interpreter binary itself, not to
  "Chatter". Switching Python versions may mean re-granting permissions.
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
