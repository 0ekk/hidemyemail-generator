# AGENTS.md

Rules for working in this repository. See [CLAUDE.md](CLAUDE.md) for what the
project is and how it is built; this file covers how to change it.

## This is a fork

`0ekk/hidemyemail-generator` is a fork of `rtunazzz/hidemyemail-generator`, and
upstream is active. Every change here has to survive being merged with upstream
work on the same files, so the cost of a change is not only its own complexity
— it is also how much upstream code it makes unmergeable later.

```bash
git remote add upstream https://github.com/rtunazzz/hidemyemail-generator.git
git fetch upstream && git rebase upstream/main
```

Keep `main` tracking upstream and develop on branches. Rebase onto upstream
often; a fork's diff replayed one commit at a time is far easier to keep honest
than a `main` that has drifted for months.

## The metric that matters: deleted upstream lines

New files never conflict. Added lines rarely conflict. **Deleted and modified
upstream lines are the entire merge cost.** Judge a change by `git diff
--numstat`'s second column, not its size.

At the time of writing the fork's whole footprint is ~43 deleted upstream lines,
41 of them one deliberate extraction. Keep it that way.

Rules, in order of preference:

1. **Put new capability in new files.** The web UI is ~2,000 lines across
   `webui.py`, `static/index.html`, and `tests/test_webui.py`, and none of it can
   ever conflict.
2. **Prefer additive edits to existing files.** An inserted line merges; a
   rewritten line conflicts.
3. **Never renumber or re-sequence existing lists.** Menus, option orders, and
   numbered docs are the classic trap: upstream adds an entry, git auto-merges
   the surrounding lines, and you silently end up with two items numbered 6.
   Claim a letter instead — `main_menu` in `launcher.py` uses `W` for the web UI
   for exactly this reason, and that keeps the dispatch block auto-merging
   correctly when upstream adds a numbered entry.
4. **When you must touch upstream code, extract rather than duplicate.**
   `quota_snapshot()` and `inbox_counts()` were lifted out of `quotacommand` and
   `inbox_status` so the CLI and the web UI share one implementation. That earns
   a conflict when upstream edits those bodies, which is the point: the
   alternative — copying the logic into `webui.py` — turns an upstream change
   into a silent behaviour drift instead of a loud merge conflict. Prefer the
   loud failure.

## Known conflict hotspots

Ordered by how often upstream touches them:

| Location | Resolution |
| --- | --- |
| `cli.add_command(...)` block at the bottom of `main.py` | Touched by nearly every upstream feature commit. Always "keep both". |
| `README.md` and its `zh-CN` / `ru` translations | Highest churn in the repo. Pure text; resolve by hand. |
| `quotacommand` / `inbox_status` bodies in `main.py` | The code moved to `quota_snapshot()` / `inbox_counts()`. Apply upstream's edit at the new location — do not paste the old body back. |
| `main_menu` print block in `launcher.py` | Take upstream's version and re-add the `W.` line. The `elif` dispatch below merges on its own. |
| PyInstaller flag lists in `scripts/build-macos-app.sh` and `.github/workflows/release.yml` | One added `--add-data` line each. Keep both. |

## Deliberate divergences from upstream

Do not "fix" these; they are choices, and each is a small, reversible diff.

- **The container image is the only published artifact.** `docker-image.yml`
  builds and pushes `ghcr.io/0ekk/hidemyemail-generator` on `main` and on `v*`
  tags. `release.yml` still builds and tests the macOS app and the Windows
  binary, but its `push.tags` trigger is removed, so it only runs on
  `workflow_dispatch` and publishes nothing. Restoring those two lines brings
  desktop releases back.
- **`deploy/k8s/` targets the web UI**, one replica on a ReadWriteOnce volume
  because the database is SQLite. Its README documents behaviour that has been
  verified on a cluster — Secret rotation without a restart, `/healthz` skipping
  the token check, uid 1000 reading the `0440` Secret mount. Re-verify before
  changing any of it.

## Conventions to match

- Bilingual English / Simplified Chinese strings in every user-facing CLI,
  launcher, and web UI message, in the existing `English / 中文` shape.
- Commands and API handlers return the `{"ok", ..., "error"}` envelope that
  `write_result_json` and `bridge_error` produce, so the CLI, the macOS app
  bridge, and the web UI all read the same results.
- Documentation changes land in all three READMEs, not just `README.md`.
- `uv run ruff check` must pass. The repo is not `ruff format` clean, so format
  only the files you add — reformatting existing files would bury a real change
  in hundreds of unrelated lines and conflict with every upstream edit to them.

## Before handing off

```bash
uv run pytest -q
uv run ruff check
git diff --numstat        # check the deletion column
```

If a change deletes upstream lines, say so in the handoff and explain why the
deletion was necessary. To sanity-check a risky edit, simulate the merge: clone
the repo to a scratch directory, commit your change on one branch, apply a
plausible upstream change on another, and merge them.
