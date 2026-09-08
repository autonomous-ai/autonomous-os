# Local Git review

The Changes panel shows Git's two-column status, staged and unstaged file counts, and explicit per-file Stage (+) and Unstage (−) actions. A partially staged file counts in both groups. Clicking a working file opens its combined HEAD-to-working-tree diff in the existing preview; untracked text is previewed read-only. Rename rows retain original paths. Staged deletions remain reviewable.

The commit composer explicitly says **Commit staged changes**. Its button commits the entire existing index, including files staged outside Buddy, using the entered message; it never stages files implicitly. Unstaged edits and untracked files remain on disk. Git's configured identity, hooks and signing behavior still apply, and errors surface to the user. Buddy does not push, bypass hooks, amend, or discard files in this workflow. Success clears the composer and refreshes Git status/history.

Click a recent commit to expand changed files, then select a file to inspect its patch. Ordinary and merge commits compare with their first parent; initial commits compare with an empty tree. Rename source/destination paths are included. Binary changes display Git's patch summary rather than fabricated text content.

Commit-message drafts stay scoped to each registered project/worktree while the panel is mounted; switching worktrees does not carry a draft into another index. Async file and history previews ignore replies from an earlier workspace or superseded selection.

## Boundary and validation

Finite preload methods are `stageFiles(projectId, worktreePath, files)`, `unstageFiles(...)`, `commitStaged(projectId, worktreePath, message)`, `commitFiles(projectId, worktreePath, hash)`, and `commitDiff(projectId, worktreePath, hash, file)`. Manager validates membership in registered project worktrees before Git access. Mutations accept 1–1000 explicitly selected current changed files, expand a rename's original path, and reject directory-wide selections not present as changed files. Absolute paths, traversal, NUL and `.git` segments are rejected. Git runs via argv with global `--literal-pathspecs` and `--` before selected paths; filenames such as `:(glob)*` cannot expand into other files.

Unstage uses selected-path reset against HEAD. On an unborn branch it removes only selected index entries with `git rm --cached`; working files survive. Commit messages must contain 1–10,000 characters after validation. Commit review accepts full 40/64-character hexadecimal hashes and verifies selected files belong to that commit. Patch commands disable external diff drivers, text conversion and color. Existing Git subprocess bounds remain 15 seconds and 4 MiB output; large diffs or slow hooks/signing can fail visibly. Concurrent external Git changes can invalidate a selection; refresh and review before retrying.

`tests/git-review.test.ts` uses temporary repositories to verify literal path selection, unrelated-file isolation, initial commit/unstage, staged rename unstage and history, deletion preview, and committing index contents while preserving newer unstaged edits. These tests never stage or commit the user's repository.

## Source audit

The implementation was written locally after reviewing Orca at `ba5f708290b72012132fb23a6f016b8fd5601718`: [commit diff](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/git/source-control/commit-diff.ts), [staged commit context](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/git/source-control/staged-commit-context.ts), and [commit changes](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/git/source-control/commit-changes.ts). Full graph, hunk staging, merge tooling, remote operations and AI commit-message generation remain outside this slice.
