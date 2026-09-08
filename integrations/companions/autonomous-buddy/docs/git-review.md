# Git review sidebar

The Changes panel is organized in reading order: branch and worktree, commit message, staging actions, changed files, committed branch files, then recent commits. A single scrolling area keeps the file list and history reachable on smaller windows. Dark and light appearances use the shared palette.

**Stage all** stages the currently listed unstaged and untracked paths in the selected worktree. **Unstage all** removes the currently listed staged paths from that worktree's index while retaining working files. Both call the existing explicit-path APIs, including rename handling; they do not issue a repository-wide stage command. The backend accepts 1–1000 selected changed files per action and rejects stale selections. Per-file plus/minus actions remain available on the right of each row.

Git's two-column status distinguishes the index from the working tree. A partially staged file counts in both staged and unstaged totals. Staged deletions remain reviewable.

The commit message appears above the changes list. The commit composer explicitly says **Commit staged changes**. Its button commits the entire existing index, including files staged outside Buddy, using the entered message; it never stages files implicitly. Unstaged edits and untracked files remain on disk. Git's configured identity, hooks and signing behavior still apply, and errors surface to the user. Buddy does not push, bypass hooks, amend, or discard files in this workflow. Success clears the composer and refreshes Git status/history. Commit and staging errors from an earlier workspace are not displayed over a different selected workspace.

The **CHANGES** header shows a file count and expands/collapses the list. Rows display a prominent filename, muted directory or rename origin, available line statistics, Git status, and staging controls. Clicking a row opens its existing working diff in the central preview. Recent commits still expand to their changed file list, and selecting a historical file opens its commit diff. The current sidebar does not add remote push, pull, or PR actions, or an aggregate “View all” diff.

Ordinary and merge commits compare with their first parent; initial commits compare with an empty tree. Rename source/destination paths are included. Binary changes display Git's patch summary rather than fabricated text content.

Commit-message drafts stay scoped to each registered project/worktree while the panel is mounted; switching worktrees does not carry a draft into another index. Async file and history previews ignore replies from an earlier workspace or superseded selection.

## Line statistics

The optional `GitFile.lineStats` carries `added` and `removed` counts. The renderer never substitutes zero when these statistics are absent.

- Tracked files with HEAD use one combined HEAD-to-working-tree numstat calculation, including renames. Staged and unstaged counts are not blindly added together: an edit reversed in the working tree can have a real net count of zero.
- Before the first commit, staged-only changes can use the initial index baseline. Mixed staged/working changes without a single verified baseline remain unknown.
- Untracked regular UTF-8 text files use their current line count, preserving a final line without a trailing newline. This is local text counting rather than a Git diff. Only the first 100 untracked files per refresh are inspected, at most 1 MiB each, with concurrency four.
- Binary data, explicit `-diff` attributes, symlinks, oversized/unreadable files, unsupported or conflicting states, and failed statistics retrieval keep counts unknown where applicable. A symlink's target is not read to produce a line count.

## Boundary and validation

Finite preload methods are `stageFiles(projectId, worktreePath, files)`, `unstageFiles(...)`, `commitStaged(projectId, worktreePath, message)`, `commitFiles(projectId, worktreePath, hash)`, and `commitDiff(projectId, worktreePath, hash, file)`. Manager validates membership in registered project worktrees before Git access. Mutations accept 1–1000 explicitly selected current changed files, expand a rename's original path, and reject directory-wide selections not present as changed files. Absolute paths, traversal, NUL and `.git` segments are rejected. Git runs via argv with global `--literal-pathspecs` and `--` before selected paths; filenames such as `:(glob)*` cannot expand into other files.

Unstage uses selected-path reset against HEAD. On an unborn branch it removes only selected index entries with `git rm --cached`; working files survive. Commit messages must contain 1–10,000 characters after validation. Commit review accepts full 40/64-character hexadecimal hashes and verifies selected files belong to that commit. Patch commands disable external diff drivers, text conversion and color. Existing Git subprocess bounds remain 15 seconds and 4 MiB output; large diffs or slow hooks/signing can fail visibly. Concurrent external Git changes can invalidate a selection; refresh and review before retrying.

`tests/git-review.test.ts` uses temporary repositories to verify literal path selection, unrelated-file isolation, initial commit/unstage, staged rename unstage and history, deletion preview, and committing index contents while preserving newer unstaged edits. These tests never stage or commit the user's repository.

## Source review and validation

The presentation was reviewed against Orca's `right-sidebar/source-control/commit/commit-area.tsx`, `listing/uncommitted-sections.tsx`, `listing/section-header.tsx`, and `listing/row-layout.ts`. Buddy uses its own React components and existing local Git APIs; this is a targeted readability improvement rather than full source-control parity.

Validation includes desktop lint/build, focused backend line-stat tests, and the packaged Electron smoke workflow. The smoke fixture stages/unstages files and commits only README in a temporary repository, checks that unrelated fixture changes remain unstaged, and opens the resulting historical diff. See [workspace UI](workspace-ui.md) and [appearance settings](settings.md).


The implementation was written locally after reviewing Orca at `ba5f708290b72012132fb23a6f016b8fd5601718`: [commit diff](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/git/source-control/commit-diff.ts), [staged commit context](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/git/source-control/staged-commit-context.ts), and [commit changes](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/git/source-control/commit-changes.ts). Full graph, hunk staging, merge tooling, remote operations and AI commit-message generation remain outside this slice.

## Committed on branch

**COMMITTED ON BRANCH** appears expanded between working changes and the collapsed-by-default recent commit history. It lists the net files changed from the comparison base's common ancestor to the current HEAD, with filenames, directories/rename origins, Git status and available added/removed line counts. Working-tree and staged edits are excluded, including when the same file also appears in CHANGES. Selecting a row opens the committed diff against the captured base/HEAD hashes; branch rows have no staging actions. The base name appears under the current branch. Empty comparisons and unavailable bases are stated explicitly. Refresh follows the existing five-second/focus/manual cycle.

The read-only `branchDiff(projectId, worktreePath, baseHash, headHash, file)` preload method uses the registered worktree boundary and full commit hashes. Base selection checks `origin/HEAD`, `origin/main`, `main`, `origin/master`, then `master`, skipping the current local branch. It uses locally available refs and does not fetch remotes or infer a review base from a feature tracking branch. The implementation follows Orca's merge-base-to-HEAD comparison (`src/main/git/source-control/branch-change-entries.ts`), retaining the independent Buddy Git implementation.

The right panel separates tracked `CHANGES` from `UNTRACKED FILES` (Git porcelain `??`), with independent collapse controls and counts; empty groups are omitted. Stage/unstage actions still operate on the explicit file paths. `COMMITTED ON BRANCH` remains expanded by default below these groups, separated by a border. When the working tree is clean, a compact one-line message replaces the large empty-state illustration so committed files stay visible near the top. Tracked rows keep combined HEAD-to-working-tree stats; this change does not pretend they are index-only diffs.
