# SparkGrid Web Client Installer

Private repo for building the SparkGrid Web Client installer.

## Structure
- `SparkGrid_Installer.iss` — Inno Setup script
- `SparkGrid.bat` — Launcher (sets env vars, opens browser)
- `log_config.py` — Centralized logging system
- `patches/` — Patched app modules with logging
- `python/` — Portable Python 3.12.13
- `lib/tqdm/` — tqdm stub module

## Build
```
iscc SparkGrid_Installer.iss
```

## Download installer
See [Releases](https://github.com/sifferee/sparkgrid-web-client-installer/releases)
