# PyInstaller spec for the frozen FastAPI backend (onedir).
#
# Build:  pyinstaller desktop/bambu-backend.spec  (run from the repo root)
# Output: dist/bambu-backend/ containing the launcher executable + _internal/
#
# onedir (not onefile): faster startup and no per-launch temp extraction, which
# also makes the Electron packaging (extraResources) straightforward.
import importlib.util
import pathlib

from PyInstaller.utils.hooks import collect_submodules

# SPECPATH is the directory containing this spec file (desktop/); the repo root
# is its parent. Everything is resolved from there so the build is CWD-agnostic.
REPO = pathlib.Path(SPECPATH).parent


def _present(*names):
    """Keep only importable modules. uvicorn[standard]'s speedups (uvloop,
    httptools) exist on Linux but not Windows; listing an absent module as a
    hidden import is a needless warning, so filter to what's actually installed
    in THIS build environment."""
    return [n for n in names if importlib.util.find_spec(n) is not None]


# uvicorn loads its protocol/loop/lifespan implementations by string at runtime,
# so PyInstaller's static analysis misses them without help.
hiddenimports = collect_submodules("uvicorn") + _present(
    "uvloop", "httptools", "websockets", "wsproto", "h11", "anyio",
    "multipart", "python_multipart",
)

datas = [
    # The built React app the backend serves at /. resource_path("frontend/dist")
    # in launcher.py resolves to _MEIPASS/frontend/dist to match this.
    (str(REPO / "frontend" / "dist"), "frontend/dist"),
]

# Heavy, out-of-scope deps. Excluding torch/ultralytics is what keeps the bundle
# small; the detector path is never reached (launcher passes detection=None).
excludes = [
    "torch", "torchvision", "ultralytics", "matplotlib", "pandas", "scipy",
    "pytest", "IPython", "notebook", "tkinter",
]

a = Analysis(
    [str(REPO / "desktop" / "launcher.py")],
    pathex=[str(REPO)],  # so `import server` and root modules resolve
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="bambu-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # keep a console so backend logs are visible if launched raw
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="bambu-backend",
)
