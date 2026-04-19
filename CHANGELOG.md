# Changelog

All notable changes to this project will be documented in this file.

## [1.0.0] - 2025-12-14

### 🚀 New Features
- **Hide Answers Until Submit**: Added a "Study Mode" where answers are hidden until explicitly submitted.
- **Strikeout Options**: Users can now visually cross out incorrect answers (persists per session).
- **New Themes**: Added Sunset, Lavender, and Terminal themes.
- **Timed Mode Toggle**: Option to disable the timer completely in settings.
- **Deployment Ready**: Added `runtime.txt`, `Procfile`, and Docker support for platforms like Koyeb.

### 🐛 Bug Fixes
- Fixed PDF parsing issues for multi-column layouts and footer text merging.
- Fixed 54+ specific test files to ensure accurate question counts and option parsing.
- Fixed `SECRET_KEY` startup handling in production by requiring explicit configuration.
- Fixed session storage on read-only filesystems (fallback to `/tmp`).
- Fixed "undefined" correct answer bug in explanations.

### 💅 UI/UX Improvements
- Updated "Choose File" button styling.
- Improved Theme Selector in settings.
- Added "About" modal with credits and tips.
- responsive improvements for mobile views.
