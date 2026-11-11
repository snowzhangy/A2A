# Windows 11 development build

The Windows 11 x64 archive contains the experimental portable desktop runtime, its
source, `A2A-Windows.bat`, and a compiled WASAPI/shared-mixer library at
`.local/native/windows/a2a_rt.dll`. `BUILD-INFO.json` records the source commit and
native library hash. It is a Python desktop preview, not a standalone `.exe` installer.

Extract the whole archive into a writable folder. Install Python 3.12 or 3.13 and uv,
then open PowerShell in the extracted A2A directory:

```powershell
uv sync --project runtime --locked --extra gpu --extra video
uv sync --project runtime/separator --locked
```

The prebuilt DLL removes the need to compile the audio host. It requires the x64
[Microsoft Visual C++ runtime](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist).
An NVIDIA GPU is recommended; the CPU path is much slower. Model downloads and the
translation-model conversion remain explicit setup steps in
[runtime/README.md](https://github.com/snowzhangy/A2A/blob/v0.2.0-alpha.2/runtime/README.md). No models are bundled or fetched by
starting a translation session.

After setup, double-click `A2A-Windows.bat`. The Windows preview uses Whisper,
CTranslate2 translation and OmniVoice rather than the Mac app's MLX model path.
Use voice matching only with speakers who consented. System audio captures and ducks
other apps too; start with one selected source app, System voice or Captions only.
If a source remains quiet after an interrupted run, follow the recovery instructions
in `runtime/README.md`.

The release workflow builds the DLL and checks deterministic runtime/harness behavior
on a Windows runner without downloading models. Installed Windows voices, real model
inference, devices, capture permissions, crash recovery and A/V quality require
interactive Windows 11 qualification. This preview does not claim Mac production parity.
