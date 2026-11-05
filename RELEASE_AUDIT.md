# Release audit â€” 2026-11-02

## Verdict: local fixes validated; distribution checks still required

The local build has meaningful safety fixes and passed the checks below. The reported Python dependency security gate has been resolved by a tested Pillow 13 migration; native-library and clean-machine distribution checks remain open. This audit does not establish that the app is bug-free or that every feature combination has been exercised.

## Architecture and feature inventory

Windows desktop application: Python 3.14 on this test host, PySide6/Qt Widgets, optional QOpenGLWidget thumbnail viewport. Entry point is MediaExplorer/main.py. PyInstaller Magellan.spec produces dist/Magellan.exe plus its _internal runtime and assets; the executable alone is not a portable distribution. FFmpeg is supplied as a compressed helper for video thumbnails. Pillow/JPEG XL, PDFium, Qt PDF search and bundled UnRAR implement media support.

Explorer owns folder/archive/Farview/favorites tabs, per-folder sorting and manual orders, persistent cross-folder selections, file operations, search, file-tree state, keyboard navigation and configurable appearance/auto-hide. MediaViewer/MediaCanvas implement image/GIF viewing, four layouts, zoom/pan/transforms, comparison, slideshow, PDF search and reader navigation. Settings and favorites/groups are JSON references, not backed-up file contents; atomic replacement and bounded backups protect configuration. LRU memory/disk thumbnails and background priority pools support browsing. QThread scans use generation identifiers to reject stale results. QLocalServer/QLocalSocket route Explorer requests to one application instance. Optional external reverse-image searches and HKCU file associations/context commands are integrations.

There is no web server, web routing, database or mobile UI. Browser/mobile tests and migrations are not applicable. No mandatory API credentials are required for offline use; optional search credentials were not inspected or exercised.

## Confirmed fixes

1. HIGH â€” Cross-volume move cleanup could delete the completed destination after partially deleting the source. Reproduced with disposable files and an injected deletion failure. Completed copies now survive, and the error identifies their location. The same preservation rule avoids deleting shutil.move output after junction/cross-volume fallback failures.
2. MEDIUM â€” Archive extraction accepted alternate-data-stream names, reserved Windows devices and trailing-dot/space aliases. Validation now rejects them. Individual archive-file copying uses the same validation. Existing traversal/symlink escape checks remain.
3. MEDIUM â€” JPEG XL paths permitted unrelated Pillow decoders through content sniffing. Explicit JXL-only selection now rejects disguised inputs while retaining genuine JXL support.
4. LOW â€” Removed confirmed unused animation/effect imports and redundant locals, including an unused filesystem query in the favorites picker. JPEG XL registration imports were intentionally retained and documented for linting.
5. Build staging and local build logs are now excluded from version control. README was not changed.

## Baseline and verification

- No pre-existing repository test runner, formatter or type-check configuration was present. No mass style rewrite or unconfigured type-check pass is claimed.
- pip check: passed; no incompatible installed requirements reported.
- Added tests/test_release_safety.py. Before fixes: simulated move lost the destination; four unsafe-name cases were accepted. After fixes: all six tests passed (move recovery, names, collision-safe ordinary copy, ZIP extraction/traversal rollback, archive cancellation, true/disguised JXL).
- Targeted Ruff F401/F841/F821/F822/F823: passed after cleanup. This is not a full style lint or static type proof.
- Isolated settings checks passed for legacy ASCII, Unicode UTF-8, BOM, favorites/tabs/appearance data, renderer preference persistence and protection against overwriting malformed settings.
- UI: five image-viewer lifecycle cycles through all four layouts and comparison toggle; generated two-page CBZ with a wrapper directory; 1,676-page dhammapada_illustrated.pdf opened, query "one" found matches, and 100 rapid next-result calls completed without an unhandled exception. Initial PDF harness waits starved Python imports; allowing worker execution corrected the harness, not production code.
- Selection regression checks passed: inversion, persistence/order, undo, select-all, deferred cross-folder sizes, real Shift-click and auto-scroll.
- UI regression suite passed 80 tab switches and 30 menu/File Tree cycles with zero-ms hide settings.
- Fullscreen suite passed 12 normal/maximized/fullscreen cycles, native maximize and restore, menus/tree, stable native window/context.
- PyInstaller production packaging passed in the isolated Pillow 13 release environment, without the temporary audit-tools import path. Embedded source was verified against the audited source, the embedded Pillow version is 13.0.0, and every installed runtime file matches the staged package. The obsolete Pillow _imagingmath binary was removed; existing thumbnail cache and FFmpeg helper were preserved. GUI checks above exercised source/runtime; a packaged cold-start and clean-machine installer test remain unverified.

## Dependency security findings — Python dependency gate resolved

The baseline scan reported 39 advisory entries against Pillow 12.3.0; retained in audit/dependency-advisories-baseline.json. The patched release uses Pillow 13.0.0, with requirements updated to >=13.0,<14. Migration was tested in an isolated release environment before packaging: genuine JPEG XL decoding, pixel output, PDF rendering/search, image layouts, selection, appearance and fullscreen checks passed. pip check reports no broken requirements.

A fresh pip-audit scan of the exact Python packages in audit/tested-dependencies.txt reports no known vulnerabilities; the complete result is audit/dependency-advisories.json. This is an audit snapshot, not a lockfile or a guarantee of safety. pip-audit does not independently clear bundled Qt/PDFium/UnRAR/JPEG XL native libraries, OS codecs, or future advisories. The JXL-only decoder restriction remains as defense in depth.

## Follow-up fixes and verification

- Farview swiping called a missing set_farview_index method. Implemented source/member/page switching, global position tracking and safe handling of early archive opens before File Tree creation. Delayed page restoration now rejects stale navigation generations.
- Added tests/ui_farview_smoke.py: ordinary images, two CBZ members, an explicit PDF page, previous/next/wrap, position jumps and rapid repeated navigation passed under the patched runtime.
- Copying now checks cancellation between 1 MiB reads and directory boundaries. Exclusive file/directory creation prevents cleanup from overwriting or removing a destination created by another process. Completed cross-drive recovery copies remain protected.
- Ten safety tests pass, including destination races, partial-copy cancellation, directory copying, move/undo and nested ZIP member reading. Cross-drive source-removal failure remains simulated; real user files were not deleted.
- Settings checks passed for ASCII, Unicode and BOM input, renderer persistence and corrupt-file protection. The test expectation was updated to account for existing tab normalization; no settings reset was needed.
- Repeated selection, appearance, 80-tab-switch/30-menu-cycle and 12-fullscreen-cycle checks passed with Pillow 13. Viewer tests included all four layouts, comparison, CBZ and 100 rapid PDF search-result jumps.

## Remaining issues and explicit unverified areas

- Cancellation is cooperative. File copies and extraction check between chunks; a directory enumeration, filesystem call, same-volume move or undo/source-deletion phase can still delay interruption.
- Not re-exercised end to end: real/nested RAR variations, animated WEBP/GIF timing, JXL plugin edge cases, every Farview/favorites/manual-order combination, external Lens/SauceNAO services, Windows default-app/context registration, focus from actual Explorer launches, real Recycle Bin deletion/restore, permissions across actual drives/junctions, and all 10-backup rotation/time-interval combinations.
- No destructive test touched real user data. Cross-drive failure used a simulation rather than deleting files across physical volumes. No clean Windows VM, alternate GPU, multi-monitor/DPI matrix or screen-reader audit was available in this pass. Small desktop layouts and every appearance setting were not exhaustively visually checked.

## Launch requirements

Test the complete packaged directory on clean supported Windows machines; validate Explorer associations and Recycle Bin recovery with disposable files; review third-party/native redistribution notices. Preserve existing settings/cache locations. No database migration or production deployment is required. No GitHub push or release publication was performed.
