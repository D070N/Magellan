# Magellan

Magellan is a Windows desktop file and media browser built with Python and Qt. The application source and Windows build files are in [`MediaExplorer/`](MediaExplorer/); the root icon and `Alternative Icons/` assets are required by the app and its executable build.

## Features

- Browse folders and media with tabs, natural sorting, favorites, and Farview collections.
- Generate thumbnails asynchronously and cache them for smoother scrolling.
- Search the current folder, with optional recursive search under **Performance**.
- Browse folder trees with background loading and paged child lists.
- View images, animated images, PDFs, and supported video formats; very large images are decoded at a bounded resolution while retaining their original dimensions in the viewer details.
- Save preferences and tabs locally. Application logs are bounded by the configured limit in **Performance**.

## Run from source

Install Python 3.10 or newer, then run these commands from `MediaExplorer`:

```powershell
py -m pip install -r requirements.txt
py main.py
```

The application expects `MainIcon.ico` and `Alternative Icons/` in the repository root.

## Build on Windows

Run `MediaExplorer\build_windows.bat` from a Windows checkout. It installs the Python dependencies and PyInstaller, then writes `MediaExplorer\dist\Magellan.exe`. Build output and thumbnail caches are intentionally excluded from Git; distribute executables through GitHub Releases rather than committing them to the source repository.

## Local settings and privacy

Settings and logs are stored in the per-user application-data directory. The optional SauceNAO API key is stored in the local settings file and is not included in this repository. `Examples/` contains local sample images and is excluded; it is not needed to run the application.

## License

Magellan is distributed under the MIT License. See [LICENSE](LICENSE).
