# Changelog

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
