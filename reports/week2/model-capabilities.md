# Native cloning integration: source audit

Verifier: Codex, 24 October 2026. This is source evidence, not inference, parity,
quality, performance, or license qualification. No model weights were downloaded.

The inspected speech-swift revision is
[`e98478435b40ac0fe2d2d75c02462a6a6122abaf`](https://github.com/soniqo/speech-swift/tree/e98478435b40ac0fe2d2d75c02462a6a6122abaf).
It was resolved from the upstream GitHub API during this implementation pass.

| Candidate API | Observed source behavior | Integration consequence |
| --- | --- | --- |
| Qwen3 `synthesizeStream` | Emits audio asynchronously and checks cooperative cancellation, but accepts text, language, a named speaker, and instructions; its public signature has no reference-audio or prepared-clone conditioning argument. | The generic streaming API alone does not establish streaming voice cloning. |
| Qwen3 `synthesizeWithVoiceClone` | Accepts reference PCM and caches the speaker embedding; returns a complete `[Float]` synchronously. | Requires a cancellable clone generation path and measurement before enabling it in the live pipeline. Splitting the finished array into chunks would not establish streaming latency. |
| CosyVoice `synthesizeStream` | Calls synchronous `synthesize(text:language:)`, then yields one final chunk containing the whole result. The implementation comments explicitly describe this behavior. | This wrapper does not demonstrate incremental audio generation or clone conditioning. |
| CosyVoice reference extraction | `extractVoiceProfile` prepares reference conditioning and supports a reference transcript. | Useful for the reference-acquisition adapter, but does not by itself satisfy the streaming/cancellation contract. |

Sources: [Qwen3TTS.swift](https://github.com/soniqo/speech-swift/blob/e98478435b40ac0fe2d2d75c02462a6a6122abaf/Sources/Qwen3TTS/Qwen3TTS.swift),
[CosyVoiceTTS.swift](https://github.com/soniqo/speech-swift/blob/e98478435b40ac0fe2d2d75c02462a6a6122abaf/Sources/CosyVoiceTTS/CosyVoiceTTS.swift),
[VoiceCloning.swift](https://github.com/soniqo/speech-swift/blob/e98478435b40ac0fe2d2d75c02462a6a6122abaf/Sources/CosyVoiceTTS/VoiceCloning.swift).

SHA-256s of the inspected source bytes:

```text
Qwen3TTS.swift      24caa3d519caf0ad75ca3fbc9ba9844fed21519a1efa37097548b33af766665e
CosyVoiceTTS.swift  4996a2973c9f8ae095db8cfc43b4a991879dd2bc611d57e24be3f18c18628811
VoiceCloning.swift 9d24c46a534dd8f0b685d45ceb345b577451fc734861b9a12200d4b6a0404746
```

These are code hashes, not weight hashes. They do not enable any entry in
`models/manifest.yaml`. Required next work remains: select and verify model/codec
artifacts and licenses; implement and test the clone streaming/cancellation path;
probe Metal/model loading in the bundled inference worker; then run M1–M3 and the
independent listener study. The source audit is not evidence that either candidate
cannot meet the product requirements after that work.
