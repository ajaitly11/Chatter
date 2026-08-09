# Choose models in three steps

Chatter has one live speech model and one optional local cleanup model. You do
not need a catalog of models to start dictating.

## 1. Check your Mac's memory

| Mac | Recommended starting point |
| --- | --- |
| 8 GB | Nemotron 3.5 streaming; keep AI cleanup off initially |
| 16 GB | Nemotron 3.5 streaming + a small 2B–4B cleanup model |
| 24 GB+ | The same setup; try a larger cleanup model only if latency stays low |

## 2. Download only what you need

- **Live dictation:** [Nemotron 3.5 streaming GGUFs](https://huggingface.co/models?search=nemotron%203.5%20streaming%20gguf). Choose a Q8_0 file when memory allows.
- **Optional cleanup:** [small instruct GGUFs](https://huggingface.co/models?pipeline_tag=text-generation&search=2b%20instruct%20gguf). Chatter uses this only for punctuation, corrections, and formatting.
- **File transcription:** [Whisper large-v3 Turbo](https://huggingface.co/models?search=whisper%20large%20v3%20turbo%20gguf) or [Parakeet TDT](https://huggingface.co/models?search=parakeet%20tdt%20gguf).

## 3. Import them in Chatter

Open **Models** → choose the job → **Import .gguf file…**. Chatter checks
that a live model supports streaming before accepting it. Start with cleanup
off, confirm the raw dictation feels fast, then enable **Clean up with local
AI** in Settings.

The push-to-talk path uses one ASR model from the first syllable to the final
transcript. Cleanup is optional, runs locally, and never replaces the speech
model. If anything feels slow, turn cleanup off first. Gemma MTP is an
experimental accelerator for compatible cleanup models, not a second ASR
model.

Model files and prompts stay on this Mac; Chatter does not upload them.
