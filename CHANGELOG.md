# Changelog

## Unreleased — 2026-10-18

- Added a configurable automatic folder refresh in **View**. It scans at low priority, defers applying results while the user is scrolling or selecting, and preserves the current navigation state.
- Added a **Help** toggle for scroll diagnostic logging; it remains disabled by default. Pressing `7` now selects Personal order.
- Reworked smooth wheel scrolling to keep its initial momentum and extend the slide before easing to a stop.
- Improved image-viewer preloading and moved file-size reads off the UI thread; retained original file metadata in the viewer cache.
- Made viewer shutdown cleanup more defensive to reduce lingering image locks and close-time errors.

## Unreleased — 2026-10-15

- Organization mode now works in regular folders, Farview, and Favorites.
- Added the Personal order sorting button.
- Added a managegement window for personal orders in the VIEW menu to review and exclude saved folder orders.
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
