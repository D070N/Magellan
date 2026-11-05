# Changelog

## Unreleased

- Remove repository test and audit artifacts, retaining application source and user-facing documentation.
- Count rapid double-clicks on viewer side-navigation controls as a second swipe, without triggering image zoom or duplicating the release action.

## Update — 2026-11-04

This update includes the accumulated browser, viewer and performance work since the previous published source update. Historical entries below retain implementation details and dates.

### Browser, archives and file management

- Browse ZIP/RAR and supported nested archives, including flattened wrapper folders, archive thumbnails and selected-member copying/extraction. Preserve return navigation and scroll position across archive browsing.
- Extend Farview to archive/document contents and selections across locations, retaining selection order and the displayed viewer sequence. Allow duplicate Farview tabs, refresh in place, personal ordering and restored first/last swipe wrapping.
- Add copy/cut/paste, extraction confirmation and cancellation, inline rename, folder creation, recent move/delete undo and feedback for clipboard/operation state. Harden cross-volume recovery and archive extraction path validation.
- Support archive-member Favorites, grouped favorites, batch selection actions, drag organization and drop-into-folder moves. Preserve personal orders and expand their management tools.
- Preserve File Tree navigation and scroll state, fix unintended favorite-path expansion, improve large-directory loading, add middle-click tabs, and stabilize docked/floating/auto-hide transitions.
- Improve tab controls, restore recently closed tabs, add empty tabs, and stabilize appearance and background sampling across tab scrolling and closing.

### Viewer and Explorer integration

- Unify single, double, vertical-reader and horizontal-reader layouts with per-file session state; add image comparison, separate page/compare zoom and pan controls, slideshow, position editing and hover metadata.
- Add full PDF/comic page reading, PDF text search with match navigation/markers, and fixes for rapid search navigation, page fitting, zoom anchoring and reader scrollbars.
- Reduce Explorer viewer startup work, open viewer-only sessions without the browser, improve focus/fullscreen/minimize handling, and maintain the optional Open in Magellan context command.
- Use separate application/archive/reading-format icons. Supported viewer image formats include ICO and animated GIF/WebP.
- Preload nearby Farview archive/PDF pages in displayed order, with a bounded cache retained across archive transitions and cleared on viewer close.

### Appearance and settings

- Add independently configurable backgrounds, gradients, hues, brightness, accents, transparency and blur for panels, tabs, buttons, Tree and context menus, with live previews and custom color swatches.
- Consolidate hover/auto-hide controls, improve panel/menu hit areas, preserve fullscreen window-control placement, and repair transparency seams, texture orientation, tab-gap inheritance and docking resize updates.
- Keep button text independent of interface font color; clarify that zero gradient brightness produces black.
- Group GPU and cache controls into dedicated Performance windows with explanations. Welcome artwork introduces navigation and HELP; opt-in enables only GPU thumbnail rendering and smooth scrolling, followed by restart. Developer HELP can preview welcome without changing settings.
- Persist settings through a coalesced ordered background writer with atomic replacement, unchanged-snapshot skipping and final-save-before-backup ordering. Retain bounded logs and settings backups.

### Performance, safety and validation

- Make GPU thumbnail rendering optional; add context-preserving fullscreen handling, swap-paced glide, fractional wheel distance and duration controls. Hide scroll diagnostics from normal HELP while retaining developer access.
- Add optional GPU panel blur and GPU panel composition with bounded resources and automatic fallback. Composition helps tested blur-heavy themes but can regress transparency-only themes; GPU settings explains this distinction.
- Reuse a bounded blur atlas, avoid unscaled temporary crop images, batch Tree blur, and optimize regular blur buffers without lowering text/icon resolution.
- Reduce folder-heavy scroll work through damaged-region painting, cached card composition, adaptive construction budgets, background kept-thumbnail reads and memoized cache lookups. Keep filename/icon layout stable when cached cards appear.
- Reduce drag-selection work through indexed membership, chunked/deferred size requests and reduced redundant counter updates. Reuse folder-size metadata for sorting and avoid tooltip resets during updates.
- Add archive image-search support, safer decoder selection, file-operation regression tests and GPU appearance/fallback/geometry checks. Update dependencies including the tested Pillow 13 migration. Dependency checks are point-in-time evidence, not a guarantee about native libraries or every machine.
- Evaluate and reject experiments without reliable gains rather than retaining complexity: direct BGRA uploads, extra snapshot caching and several thumbnail scheduling/rendering variants. Detailed local profiling traces and personal test media are not published.

## Unreleased — 2026-11-04

- Preload nearby Farview archive/PDF pages in displayed order using one background request at a time, retaining a bounded viewer-session cache across archive swipes and clearing it on close.

- Reuse the bounded GPU blur atlas and draw unscaled samples directly, reducing temporary image copies while preserving existing scaled-blur sampling and clearing stale pixels every frame.

- Refresh appearance after dock-induced central-area resizes in both directions, and expose a non-mutating welcome preview in developer HELP.

- Refresh appearance after docking layout transitions, add illustrated welcome/navigation help with restart after GPU opt-in, and add the current folder from the kept-thumbnails manager.

- Group GPU and thumbnail cache controls into dedicated Performance windows with explanatory text; add a one-time GPU welcome choice and repaint compositor parents on geometry transitions.

- Correct GPU composition texture orientation and main-panel crop alignment, preventing inverted thumbnail backgrounds and seams across panel sections. Add directional-pattern and tab-gap continuity checks.

- Add optional experimental GPU panel composition, combining GPU-resident backplates with existing Qt controls, bounded shared textures, automatic regular-renderer recovery, and isolated viewer-only startup. Requires GPU thumbnail rendering and a full restart.

- Include stretched File Tree backdrops in the existing experimental GPU blur batch, retaining current-frame sampling and CPU fallback.

- Keep fullscreen window controls anchored and reachable when showing or hiding the docked File Tree resizes the central area.

- Add an off-by-default experimental GPU panel-blur toggle, using bounded batched OpenGL samples, reusable textures and automatic regular-blur fallback. Menus and unsupported samples retain regular blur; shader output may look slightly different.

- Reduce transparency blur processing with read-only pixel buffers, format-based opaque handling, and direct conversion to Qt-owned output, preserving exact backdrop pixels, clipping and update frequency.

## Unreleased — 2026-11-03

- Support reverse image search for selected images inside ZIP/RAR/CBZ/CBR and nested archives, preparing bounded uploads in a background worker for Google Lens and SauceNAO.

- Keep deferred and cached thumbnail filename/icon layouts identical and repaint the complete vertical card when a preview arrives, preventing label shifts and stale text.

- Coalesce settings updates and persist independent snapshots with one ordered background writer, skipping unchanged content and preserving atomic saves and final-save-before-backup ordering.

- Start the first GPU scroll-motion tick before requesting its first frame, reducing stop/start latency while retaining subsequent swap-paced scheduling and glide behavior.

- Add adaptive first-time thumbnail-card construction: keep cached cards immediate and defer costly new cards briefly, with automatic retries and low-memory safeguards.

- Resolve and decode initial kept-folder previews in existing background workers, memoizing legacy cache paths without changing filenames or regenerating valid cached images.

- Throttle repeated kept-thumbnail validation during painting, retaining periodic missing-cache detection with bounded bookkeeping.

- Reuse directory-entry metadata for creation dates instead of issuing a separate filesystem lookup for every item during folder scans.

## Unreleased — 2026-11-02

- Restore first/last image swipe wrapping, preserving container skipping when continuous swipe is off.

- Fix partial cross-drive move cleanup that could delete a completed recovery copy.
- Reject unsafe Windows archive names and restrict Pillow loading to the intended JPEG XL decoder.
- Remove confirmed unused code, add file-safety regression tests and update dependencies and improve release safety.

- Avoid full-page selection-order scans and repeated unchanged-membership/counter work during drag selection.
- Defer and chunk uncached selection-size requests until release; pause queued size scans during gestures.
- Verify selection inversion, persistence, undo, Shift-click and auto-scroll.

- Tested adaptive thumbnail worker limits and progressive JPEG folder previews separately and together. Neither showed a reliable overall improvement, so both experiments were reverted.

- Prepare ordinary thumbnail pixel formats in existing decoder workers, avoiding display-thread format conversion without changing rendered pixels or high-bit-depth handling.
- Tested stricter promotion budgets and full-card background preparation; neither demonstrated a reliable end-to-end benefit, so those experiments were removed and existing scheduling retained.

- Use the standard yellow Main Panel accent when no custom color is selected.
- Validate ordinary, smooth and uncached-folder GPU scrolling against the previous transparency readback path; no sustained regression found in the measured scenarios. Coalesce redundant refreshes to display cadence, with a lower refresh budget during active thumbnail loading.

- Update transparency from displayed thumbnail-card pixels on each changed frame, removing delayed GPU framebuffer captures and duplicate card rendering.

- Refresh visible transparent backdrops during scrolling without a trailing debounce; adapt capture frequency to its measured cost.
- Restore docked button/tab transparency cutouts and fill File Tree effects to the bottom.
- Refresh appearance immediately on hide-mode changes and move detached tabs below a revealed main panel.
- Reduce broad background-blur processing cost without reducing text, icon or thumbnail resolution.

- Keep appearance backgrounds behind controls when auto-hide is off, including resize and visibility transitions; restore File Tree content stacking after docking.

- Hide scroll diagnostics from normal HELP; launch with `--developer` to access them.
- Lock File Tree width and title dragging, refine appearance previews, and refresh the grouped controls guide.

- Merged the GPU rendering test into the main application. GPU thumbnail rendering remains optional under PERFORMANCE, with standard rendering available as the default for new profiles.
- Added desktop-composited fullscreen for GPU rendering, preserving the native window and OpenGL context across fullscreen and minimize/restore transitions.
- Improved top and left edge detection for hidden panels, including native borders and zero-millisecond hide timers.
- Reused appearance captures and bounded blur results during normal use, and corrected logical-coordinate sampling on scaled displays.
- Added independent wheel distance and glide-duration controls, retaining automatic duration as the default, plus GPU guidance in the scroll test settings.
- Made settings loading explicitly UTF-8 compatible, including BOM-prefixed files, while preserving protection against overwriting unreadable settings.
- Removed the separate GPU test executable and filename-dependent preference override from the main build.

## Unreleased — 2026-10-18

- Added a configurable automatic folder refresh in **View**. It scans at low priority, defers applying results while the user is scrolling or selecting, and preserves the current navigation state.
- Added scroll diagnostic logging, later restricted to developer-mode **Help**. Pressing `7` now selects Personal order.
- Reworked smooth wheel scrolling to keep its initial momentum and extend the slide before easing to a stop.
- Improved image-viewer preloading and moved file-size reads off the UI thread; retained original file metadata in the viewer cache.
- Made viewer shutdown cleanup more defensive to reduce lingering image locks and close-time errors.

## Unreleased — 2026-10-15

- Organization mode now works in regular folders, Farview, and Favorites.
- Added the Personal order sorting button.
- Added a management window for personal orders in the VIEW menu to review and exclude saved folder orders.
- Ctrl+A and the right-side Select All button now select missing items, or deselect the current page when all its items are already selected.
- Fixed the Refresh button behavior in Farviews and in the favorites menu.
- Improved the looks of the "Keep thumbs" window.
- Reduced smooth-scroll stalls by avoiding repeated thumbnail-card composition, painting only damaged cards, and deferring new thumbnail work until scroll motion settles; fixed cached placeholders so deferred folder thumbnails still load afterward. Smooth scrolling can still show some tearing on the user’s display.
- Added a bounded cache for composed thumbnail cards that shares the configured memory budget with decoded thumbnails.
- Improved Farview and personal ordering, including support for inheriting the source folder’s custom order.
- Preserved file-tree expansion, selection, and scroll state more reliably, and added clearer progress and selection-size feedback for longer operations.
- Added tab reorganization via dragging, and thumbnail-cache location/size controls.
- TOOLS > Scroll behavior now lets you set rows per wheel notch and toggle smooth momentum. (Smooth scrolling is still experimental)
- Updated HELP to match the new commands and behavior.
- Many minor fixes.

## Unreleased — 2026-10-12

- Improved folder-size sorting responsiveness by reusing cached sizes, prioritizing visible folders, and batching resorting until background size updates finish.
- Prevented repeated thumbnail refreshes during size calculation, eliminating the tooltip blinking that occurred after scrolling stopped.
- Reduced folder-heavy scrolling stalls by caching normalized pinned-thumbnail roots and using constant-time path checks.
- Avoided full-grid scans and repaints for each asynchronous folder-size result by indexing items by path and invalidating only affected visible thumbnails.
- Limited grid repainting to rows intersecting the invalidated area.
- Kept tab context menus reachable in auto-hide mode, closed them when the pointer leaves, and restored Ctrl-based tab range selection.

## Unreleased — 2026-10-11

- Kept favorite groups at the top of Favorites across sorting modes.
- Enabled holding Ctrl to enter Favorites organization mode; dragging a selected item into a group now assigns the full selection.
- Persisted folder sizes for pinned thumbnail-cache folders so size sorting can reuse calculated values.
- Opened folders and revealed files in Explorer when their thumbnail name is clicked; group names can be renamed by double-clicking.
- Added Q/E previous/next navigation in the image viewer with the same hold-to-accelerate behavior as the arrow keys.
- Added a Help menu with keyboard and mouse controls.
- Released viewer image/movie resources and cleared viewer caches when closing the viewer to reduce lingering file locks.

## Previous

- Fixed auto-hide behavior so the main panel can hide after modal error dialogs close, even when the search field has focus or the pointer has not moved.
- Fixed the timed tab reveal so it closes after its intended delay when the pointer is outside the panel and tab strip.
- Released animated-image and decoded-image resources before moving a viewed image to the Recycle Bin. Deletion now waits asynchronously for any active background decode to finish.
- Added a visible close button to the file tree and made its auto-hide overlay open at a consistent position.
