# Gemma 4 audio evaluation

## Decision

Chatter keeps Nemotron as its single low-latency streaming ASR model. Gemma 4 is not wired into the microphone path yet. It is a promising local audio-language model, but it is not currently a drop-in replacement for a streaming dictation recognizer.

## What the official material says

Google documents Gemma 4 E2B, E4B, and 12B Unified as multilingual automatic speech recognition models. The documented interface passes an audio clip to a multimodal text-generation model and asks it to transcribe the segment. Audio is mono, 16 kHz float audio, with a documented maximum clip length of 30 seconds:

- [Google Gemma 4 audio guide](https://ai.google.dev/gemma/docs/capabilities/audio)
- [Google Gemma 4 model card](https://ai.google.dev/gemma/docs/core/model_card_4)
- [Google Gemma model selection guide](https://ai.google.dev/gemma/docs/get_started)

The official examples use batch-style `generate`/pipeline calls. They do not describe an incremental microphone-ASR protocol or stable partial-transcript behavior. Streaming generated text is not the same thing as streaming audio recognition: it still requires a complete audio segment before the model can generate the result.

## Local files inspected

The Mac already has the Gemma 4 E2B GGUF stack:

| Component | Approximate disk size |
| --- | ---: |
| `gemma-4-E2B-it-UD-Q4_K_XL.gguf` | 3.0 GB |
| `mmproj-BF16.gguf` audio/vision projector | 941 MB |
| `MTP/gemma-4-E2B-it-BF16-MTP.gguf` | 162 MB |

The E2B weights fit on this Mac’s storage and can load with the local llama.cpp multimodal runtime. Loading the model is not the same as meeting dictation latency or accuracy requirements; the projector must also stay resident.

## Smoke-test result

The official short Google audio sample was run locally through `llama-mtmd-cli` with the E2B GGUF and projector. The model loaded, but this runtime path emitted reasoning text and did not return a clean, reliable transcription-only answer. The llama.cpp multimodal documentation also labels audio input experimental and warns that it may have reduced quality:

- [llama.cpp multimodal documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md)

That is enough evidence to avoid putting Gemma 4 in the hotkey path now. A future evaluation should use the server’s OpenAI-compatible `input_audio` request with `enable_thinking: false`, short rolling audio windows, and a word-error-rate test set before it can replace Nemotron.

## Practical recommendation

Keep the fast path as:

`microphone → Nemotron streaming ASR → optional local text cleanup`

Evaluate Gemma 4 E2B separately as an opt-in experimental recognizer or cleanup model. Do not run it in parallel with Nemotron for every keystroke until the end-to-end latency and word-error rate beat the current pipeline.
