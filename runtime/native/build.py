"""Build the portable runtime's native library (ADR-0007).

The library is the shared real-time shim, compiled from apple/Sources/A2ARTShim exactly as
the Swift package uses it (rings, timing records, two-bus mixer), plus the WASAPI host on
Windows. The Python host loads it through ctypes (a2a_runtime/native.py).

    uv run --project runtime python runtime/native/build.py [--debug] [--out DIR]

Windows: Visual Studio 2022 with the C++ workload (its bundled clang-cl and the Windows
SDK, 10.0.20348 or newer for process loopback). macOS/Linux: any C11 compiler (`cc`).
Output: .local/native/<platform>/ (git-ignored); A2A_NATIVE_LIB overrides the load path.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHIM = ROOT / "apple" / "Sources" / "A2ARTShim"
NATIVE = Path(__file__).resolve().parent
C_SOURCES = [SHIM / "rings.c", SHIM / "engine.c"]
HEADERS = [SHIM / "include" / "A2ARTShim.h", NATIVE / "a2a_wasapi.h"]
APPLE_ONLY = {"a2a_engine_ioproc"}


def platform_dir() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)


def library_name() -> str:
    return {"win32": "a2a_rt.dll", "darwin": "liba2a_rt.dylib"}.get(sys.platform, "liba2a_rt.so")


def default_out() -> Path:
    return ROOT / ".local" / "native" / platform_dir()


def exported_symbols(windows: bool) -> list[str]:
    names: list[str] = []
    for header in HEADERS if windows else HEADERS[:1]:
        text = header.read_text(encoding="utf-8")
        for name in re.findall(r"\b(a2a_[a-z0-9_]+)\s*\(", text):
            if name not in APPLE_ONLY and name not in names:
                names.append(name)
    return names


def visual_studio() -> Path:
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.exists():
        raise SystemExit("vswhere.exe not found: install Visual Studio 2022 with the 'Desktop development with C++' workload")
    path = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires",
                           "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
                          capture_output=True, text=True, check=True).stdout.strip()
    if not path:
        raise SystemExit("no Visual Studio installation with the C++ x64 tools was found")
    return Path(path)


def build_windows(out: Path, debug: bool) -> Path:
    vs = visual_studio()
    vcvars = vs / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    clang = vs / "VC" / "Tools" / "Llvm" / "x64" / "bin" / "clang-cl.exe"
    compiler = str(clang) if clang.exists() else "cl.exe"
    opt = ["/Od", "/Zi"] if debug else ["/O2"]
    includes = [f"/I{SHIM / 'include'}", f"/I{NATIVE}"]
    common = ["/nologo", "/c", "/MD", "/W3", "/D_CRT_SECURE_NO_WARNINGS", *opt, *includes]
    if compiler == "cl.exe":
        c_flags = [*common, "/std:c11", "/experimental:c11atomics"]
    else:
        c_flags = [*common, "/std:c11", "-Wno-nullability-completeness"]
    cpp_flags = [*common, "/std:c++17", "/EHsc"]
    with tempfile.TemporaryDirectory(prefix="a2a-native-") as tmp:
        tmpdir = Path(tmp)
        defs = tmpdir / "a2a_rt.def"
        defs.write_text("LIBRARY a2a_rt\nEXPORTS\n" + "".join(f"    {n}\n" for n in exported_symbols(True)))
        commands = []
        objects = []
        for src in C_SOURCES:
            obj = tmpdir / (src.stem + ".obj")
            commands.append(subprocess.list2cmdline([compiler, *c_flags, f"/Fo{obj}", str(src)]))
            objects.append(obj)
        cpp = NATIVE / "a2a_wasapi.cpp"
        obj = tmpdir / "a2a_wasapi.obj"
        commands.append(subprocess.list2cmdline([compiler, *cpp_flags, f"/Fo{obj}", str(cpp)]))
        objects.append(obj)
        dll = tmpdir / library_name()
        link = ["link.exe", "/nologo", "/DLL", f"/DEF:{defs}", f"/OUT:{dll}", *(str(o) for o in objects),
                "ole32.lib", "mmdevapi.lib", "avrt.lib"]
        if debug:
            link.append("/DEBUG")
        commands.append(subprocess.list2cmdline(link))
        script = tmpdir / "build.cmd"
        script.write_text("@echo off\r\n" + f'call "{vcvars}" >nul || exit /b 1\r\n'
                          + "".join(f"{c} || exit /b 1\r\n" for c in commands))
        result = subprocess.run(["cmd", "/d", "/c", str(script)], capture_output=True, text=True)
        if result.returncode != 0:
            sys.stderr.write(result.stdout + result.stderr)
            raise SystemExit(f"native build failed ({compiler})")
        out.mkdir(parents=True, exist_ok=True)
        target = out / library_name()
        shutil.copy2(dll, target)
        if debug and (tmpdir / "a2a_rt.pdb").exists():
            shutil.copy2(tmpdir / "a2a_rt.pdb", out / "a2a_rt.pdb")
        return target


def build_posix(out: Path, debug: bool) -> Path:
    cc = os.environ.get("CC", "cc")
    out.mkdir(parents=True, exist_ok=True)
    target = out / library_name()
    flags = ["-O0", "-g"] if debug else ["-O2"]
    shared = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared"]
    cmd = [cc, "-std=c11", "-fPIC", *flags, *shared, f"-I{SHIM / 'include'}", *(str(s) for s in C_SOURCES),
           "-o", str(target)]
    if sys.platform != "darwin":
        cmd.append("-lm")
    subprocess.run(cmd, check=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    out = args.out or default_out()
    target = build_windows(out, args.debug) if sys.platform == "win32" else build_posix(out, args.debug)
    print(target)


if __name__ == "__main__":
    main()
