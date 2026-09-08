# Trade Check - project rules for Claude

## Git and GitHub: free rein

Claude may perform any git or GitHub operation in this repository without asking first:
commit, push, pull, rebase, merge, branch, tag, force-push, open and merge pull requests,
create releases, re-run or edit CI workflows, change remotes. Just do it and report what was done.

Conventions:
- Bump `__version__` in `app/__init__.py` before tagging a release (`vX.Y.Z` triggers the release workflow).
- Keep the new version close to the newest tag. Check `git tag --sort=-v:refname | head -1` first.
  Allowed: same minor with a higher patch, or the next minor at `.0`. If the latest release is 1.0.2,
  never go past 1.1.x. Never jump a major. Anything further needs explicit approval.
- Keep commit messages descriptive; the first line is what shows in the release notes.

## Project notes

- Remote: https://github.com/Lv100Luca/BN-SP-Trading-Helper (public). HTTPS with `gh` as credential helper.
- Run from source with `run.bat`; build with `build.py --release`; tune OCR with `tools/ocr_tune.py`
  against screenshots in `samples/` (the game's Steam screenshots live on the E: drive).
