# Changelog

All notable changes to this project will be documented in this file.

## [1.1.0] - 2026-04-19

### 🔒 Security
- Enforced production `SECRET_KEY` requirement and hardened session cookie settings.
- Added CSRF protection for state-changing API routes.
- Added security response headers (CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, HSTS in production).
- Sanitized API error responses to avoid leaking internal exception details.
- Added upload parsing safeguards (PDF page cap and per-page parse timeout).
- Added quiz-session answer access controls to reduce unauthorized answer disclosure.
- Reduced log-injection risk by sanitizing user-controlled log values.

### 🧱 Infrastructure & Supply Chain
- Removed tracked runtime session artifact from the repository.
- Pinned Python dependencies to exact versions and remediated known dependency CVEs.
- Pinned Docker base image by immutable digest and switched container runtime to non-root.
- Reduced Docker build context and expanded `.dockerignore` to exclude local/sensitive artifacts.
- Pinned GitHub Actions to commit SHAs.
- Split CI build/publish flows with least-privilege permissions and added `pip-audit` checks.

### 🖥️ Frontend Security
- Removed inline HTML event handlers and replaced with explicit event listeners.
- Added centralized CSRF-aware API fetch helper in client code.
- Added SRI + `crossorigin` to external CDN scripts.
- Added `rel="noopener noreferrer"` on external `_blank` links.
- Updated UI copy to accurately describe local browser persistence behavior.

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
