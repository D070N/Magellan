# Magellan

A Windows file and media browser built with Python and Qt, with a thumbnail-first interface, persistent tabs, an image/comic viewer, and customizable panels.

## Browse and organize

- Browse folders, ZIP/RAR archives and supported nested archives. Generate thumbnails for images, documents, comics and supported videos.
- Keep tabs across restarts; drag to reorder, scroll through crowded tabs, and color tab outlines. Each location remembers its sorting and personal order.
- Use **Farview** to flatten selected folders, files and supported archive/document pages into one ordered collection. The viewer follows that collection across containers.
- Organize Favorites into groups, including references to files and archive members. Select across folders for batch favorites commands and Farview creation.
- Hold **Ctrl** for organization mode: drag to reorder thumbnails or move selected items into folders. Manage saved personal orders from **VIEW**.
- **Alt+click** reveals a thumbnail item in its containing Explorer location.
- Use the File Tree to navigate drives and favorite locations. It remembers expanded paths and scroll position; large directories load in the background.
- Search filenames, optionally including subfolders. Optional automatic folder refresh updates pages while preserving navigation and deferring disruptive work during interaction.

## View images, comics and PDFs

- Open supported images, animated GIF/WebP, ICO, PDFs and CBZ/CBR comics. ZIP/RAR contents can be browsed as folders.
- Cycle through four layouts: single image, double image, vertical reader and horizontal reader. Reader pages load on demand; per-file layout, zoom and page position are remembered within the session.
- Zoom, pan, rotate, flip, compare two images with a wipe slider, or start a slideshow. Hover near the edges to reveal viewer controls and metadata.
- Search text PDFs, navigate matches and see result markers on the scrollbar.
- Jump to an image/page with the editable position counter. Optional continuous swipe includes supported containers; Farview follows its displayed order.
- Nearby archive/PDF pages in Farview preload in the background and remain in a bounded viewer-session cache for faster swiping.
- Explorer can open supported files directly in the lightweight viewer. **TOOLS** manages default-viewer registration and the optional **Open in Magellan** context command, which opens a location in the browser.

Supported image extensions include JPEG/JFIF, PNG, WebP, GIF, BMP, TIFF, AVIF, HEIC/HEIF, JPEG XL and ICO. Decoding depends on the available Qt/Pillow codecs; damaged files and unsupported variants may fail. Very large images are decoded at bounded resolution while original dimensions remain available in the details.

## Selection and file actions

- **Shift+click** toggles selection; **Shift+drag** draws an inverting selection rectangle with edge auto-scroll. Selections can persist across folder navigation.
- **Ctrl+A** toggles selection of the current page; **Space** or a double-click on empty background clears selection. **Ctrl+S** restores the last deselection.
- **Ctrl+C / Ctrl+X / Ctrl+V** copy, cut and paste. Copying supported archive members extracts the chosen files.
- **Ctrl+E** opens extraction confirmation; operation messages show progress and provide cancellation.
- **Shift+Delete** sends selected filesystem items to the Recycle Bin. **Delete** removes selected Favorites references when browsing Favorites; in the viewer it trashes the current image.
- **Ctrl+Z** undoes recent supported moves/deletions. **Shift+R** renames one selected item; the file-actions menu also offers inline renaming.

See **HELP → Keyboard and mouse controls** for the complete guide and context-dependent behavior. File operations affect original files; Favorites and personal order are references/order metadata.

## Appearance and performance

**VIEW → Appearance settings** controls backgrounds, gradients, accents, transparency and blur for the main panel, buttons, tabs, File Tree and context menus. **Hover and hide settings** groups panel, thumbnail, side-control and scrollbar behavior.

**PERFORMANCE → Cache settings** separates memory and disk thumbnail caches, their limits, cache location and kept-folder thumbnails. The disk-size limiter is off by default. Kept folders include their immediate children; add deeper locations separately. Memory and disk limits are independent.

**PERFORMANCE → GPU settings** explains three optional features:

- **GPU thumbnail rendering:** supports swap-paced smooth scrolling and requests VSync. Requires a restart.
- **GPU panel blur:** accelerates supported blur samples, with regular rendering for unsupported cases.
- **GPU panel composition:** keeps panel backgrounds on the GPU beneath ordinary Qt controls. It can help with substantial visible blur, but may be slower with transparency alone. It does not accelerate dropdown/right-click menus. Requires GPU thumbnail rendering and a restart; takes precedence over standalone panel blur.

The one-time welcome offers GPU thumbnail rendering plus smooth scrolling for the recommended scrolling experience, then restarts the app. Other GPU options stay independently configurable. They are experimental and fall back when unavailable; performance depends on theme, drivers and hardware. AMD/Intel performance has not been certified. **TOOLS → Scroll behavior** sets wheel distance and glide duration.

Thumbnail decoding, folder scans and file operations use background work, with bounded caches and progress feedback. These controls reduce stalls but do not guarantee hitch-free scrolling for every disk or file.

Optional reverse image search supports Google Lens and SauceNAO, including supported images inside archives. These actions send images to the chosen external service; they are separate from local browsing.

## Demos

- [Farview selection and browsing](https://github.com/user-attachments/assets/25239582-d1f0-48bf-8d8b-0d49383d4699)
- [Personal ordering](https://github.com/user-attachments/assets/5fcd3016-2674-4b24-9826-fcd5327a770c)

<img width="421" height="295" alt="Image search example" src="https://github.com/user-attachments/assets/795accee-b1c3-4096-b9b7-213b94993340" />
<img width="679" height="698" alt="Image search results example" src="https://github.com/user-attachments/assets/25226686-339f-444b-8d5f-a8b954b7fadb" />

## Run from source

Windows is the supported platform. Development builds are tested with Python 3.14; use a current Python version compatible with the dependencies in `MediaExplorer/requirements.txt`.

From a Windows checkout:

```powershell
cd MediaExplorer
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

Keep `Alternative Icons/` beside `MediaExplorer/`. The app/build uses `MainIconNew.ico`, `MainIconNewRAR.ico`, `MainIconNewRead.ico`, the welcome PNG and toolbar PNGs from that directory. The old root `MainIcon.ico` is no longer required.

## Build on Windows

Run `MediaExplorer\build_windows.bat`. It installs dependencies and PyInstaller, builds in staging, and publishes `MediaExplorer\dist\Magellan.exe` alongside its `_internal` runtime. **Distribute the entire `dist` folder, not just the EXE.** Existing settings and the thumbnail-cache directory are separate from the bundled runtime.

Build outputs, virtual environments, recordings, caches and `Examples/` are excluded from Git. Executable packages belong in GitHub Releases rather than source commits. A clean-machine build/distribution check is still recommended before publishing binaries.

## Tests and developer mode

From `MediaExplorer`, using the same environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s ..\tests -p "test_*.py"
.\.venv\Scripts\python.exe main.py --developer
```

Developer mode exposes diagnostic controls and a non-mutating welcome preview in HELP. The separate `tests/ui_*.py` scripts exercise real Qt windows; GPU checks need a working graphics context and some need an unobstructed interactive desktop. Unit tests alone do not establish visual correctness or graphics-driver compatibility.

## Settings, backups and privacy

Settings are normally stored in `%APPDATA%\Magellan\settings.json`, with bounded logs in the `logs` subfolder. Changed full-browser sessions create up to ten settings backups in `Backups`, at least one minute apart. Viewer-only sessions do not create backups. Backups include Favorites references and groups, **not copies of the referenced files**.

Thumbnails normally live in `thumbnail_cache` beside the application, unless a custom cache location is chosen. Logs can contain local paths; review them before sharing. Optional SauceNAO credentials are stored locally and are not required for offline browsing. Do not commit settings, keys, logs, caches or personal media.

## License

Magellan's source is licensed under [MIT](LICENSE). Third-party libraries, codecs and bundled native components retain their own licenses and redistribution terms; the project license does not replace them.
