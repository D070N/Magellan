# Changelog

## Unreleased — 2026-10-11

- Fixed auto-hide behavior so the main panel can hide after modal error dialogs close, even when the search field has focus or the pointer has not moved.
- Fixed the timed tab reveal so it closes after its intended delay when the pointer is outside the panel and tab strip.
- Released animated-image and decoded-image resources before moving a viewed image to the Recycle Bin. Deletion now waits asynchronously for any active background decode to finish.
- Added a visible close button to the file tree and made its auto-hide overlay open at a consistent position.
