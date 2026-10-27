**Final plan review — 24 October 2026**

The implementation baseline is [IMPLEMENTATION_PLAN.md](../../IMPLEMENTATION_PLAN.md), revision 2.1. The architecture proposal remains linked as rationale. This review changes planning documents and records two decisions; it does not bootstrap the application, download weights, or claim measured pipeline performance.

The first work is T0.1 (minimal core/trace/harness scaffold), T0.3 (runtime preflights), and A1/A2 (FixturePlayer capture/replay). A3 must establish reliable sound restoration before playback takeover is used in the model pipeline. This sequence works with one implementer; the two-week calendar estimate assumes three staffed workstreams.

Concrete corrections in this pass:

- Selected Swift-first for the Mac alpha and isolated inference behind bounded XPC messages; documented a helper fallback if the XPC model probe fails.
- Made closed source units the initial translation policy. Removed the assumption that assistant prefill establishes forced-prefix decoding. Retained contextual/continuation experiments behind explicit capabilities.
- Required playable PCM before muting the original bus for translation. Added source-age monitoring, bounded retry, generation cancellation, and explicit handoff/abandoned-unit accounting.
- Separated delivered translation coverage from original-audio fallback and semantic quality. Required paced streaming without future audio at the feasibility gate.
- Kept same-speaker identity and the ≤2 s product target. A ≤2.5 s feasibility result supports further engineering while leaving that target unmet.
- Separated ordinary CI from interactive audio/permission/model tests, reduced mandatory download scope, and made repository/hosting infrastructure optional for local progress.
- Corrected privacy/transparency wording: ephemeral voice storage reduces retained data but is not itself a complete legal determination; applicable output-marking requirements depend on scope and exceptions.

Evidence obtained during the review:

| Check | Result | Limit |
| --- | --- | --- |
| Local toolchain | macOS reports 27.0.1, build 26A434; Swift 6.4, arm64; selected developer directory `/Library/Developer/CommandLineTools` | Full Xcode, signing identity, and a macOS 26 test machine remain setup checks |
| Basic installed-language initializer | Local SDK marks `TranslationSession(installedSource:target:)` available from macOS 26.0 | Installed/missing language behavior and zh→en availability were not executed |
| Strategy overload | Local SDK marks the `preferredStrategy:` overload available from macOS 26.4 | Optional; do not require it on the 26.0 deployment target |
| Core Audio declarations | SDK includes unmuted, muted, and muted-when-tapped behaviors; process-tap create/destroy APIs marked macOS 14.2 | No capture permission, muting, or crash recovery test performed |
| Swift API probe | A temporary source importing Foundation/CoreAudio/Translation typechecked successfully for `arm64-apple-macos26.0`, using `.mutedWhenTapped` and the basic initializer | Compile-time evidence only; it used the installed newer SDK, not a macOS 26 runtime |
| Swift speech dependencies | Public repository/package lists the required candidate model products | No dependency build, streamed inference, identity, parity, memory, ANE placement, or latency measured |
| Qwen translation license | Upstream Qwen3-4B-Instruct-2507 model card identifies Apache-2.0 | Exact converted artifact and dependencies still need manifest provenance |

The temporary API probe used `swiftc -typecheck -target arm64-apple-macos26.0 -module-cache-path <temporary-cache>`. It produced no executable and all probe files were removed. SDK evidence was read from `Translation.framework/Modules/Translation.swiftmodule/arm64e-apple-macos.swiftinterface`, `CoreAudio.framework/Headers/CATapDescription.h`, and `CoreAudio.framework/Headers/AudioHardwareTapping.h` under the selected MacOSX SDK. All five planning documents passed local-link, fenced-block, and table-column consistency checks.

Day-1 runtime work remains essential: identify the actual reference hardware/RAM; confirm supported speech locales and installed language assets; compile pinned model adapters; test signed FixturePlayer capture and playback control; qualify true streaming versus repeated-window inference; and run the bundled MLX worker probe. These are scheduled experiments, not unresolved choices that prevent scaffolding.

Sources checked for the reviewed claims: [Apple TranslationSession](https://developer.apple.com/documentation/translation/translationsession), [Apple process taps](https://developer.apple.com/documentation/coreaudio/capturing-system-audio-with-core-audio-taps), [speech-swift package](https://github.com/soniqo/speech-swift/blob/main/Package.swift), [Qwen model card](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507), [HY-MT license](https://github.com/Tencent-Hunyuan/HY-MT/blob/main/License.txt), [Seed LiveInterpret report](https://arxiv.org/abs/2507.17527), [Windows ML requirements](https://learn.microsoft.com/en-us/windows/ai/new-windows-ml/supported-execution-providers), [European Commission transparency guidance](https://digital-strategy.ec.europa.eu/en/factpages/quick-facts-transparency-rules-ai-systems).
