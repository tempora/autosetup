# Contributing

Thanks for helping with autosetup. This page covers how to send a change.

## Before you start

- For anything bigger than a small fix, open an issue first and describe what you'd like to change, so we can agree on it before you spend time on it.
- Keep each pull request to one change. Send an unrelated fix you find along the way as a separate pull request.

## Branches

Fork the repository and work on a branch named `type/short-description`, in kebab-case.

| Type | For |
|---|---|
| `feature` | a new capability, including new UI |
| `fix` | wrong behaviour |
| `refactor` | the same behaviour with the code restructured |
| `perf` | speed or memory |
| `docs` | documentation only |
| `test` | tests only |
| `chore` | tooling and build |

Use a few plain words for the description, and leave issue numbers and version numbers out of it.

Examples: `fix/gt3-wheelbases`, `refactor/balance-band`, `feature/analysis-time-ledger`.

## Commits

Follow the seven rules of [How to Write a Git Commit Message](https://cbea.ms/git-commit/):

1. Separate the subject from the body with a blank line.
2. Keep the subject to 50 characters; 72 is the hard limit.
3. Capitalise the subject.
4. Don't end the subject with a period.
5. Write the subject in the imperative mood. It must complete "If applied, this commit will …".
6. Wrap the body at 72 characters.
7. Use the body to explain what and why, not how.

A simple change needs only the subject. Don't use `feat:` or `fix:` prefixes. If a subject needs "and … and …", split the commit.

Squash "fix typo" and "try again" commits into the ones they belong to before you open the pull request.

## Pull requests

### Title

Write the title like a commit subject: imperative, capitalised, 50 characters or fewer, with no period and no prefix. For example, "Keep balance inside a two-sided band", not "Balance rework".

### Description

The template fills in these sections. Keep them short.

- **What changes:** two to four bullets on what's different in the app, in a driver's terms where possible.
- **Testing:** the `pytest` result, what you checked in the game, and what you didn't check.
- **Screenshots:** before and after, for anything you can see.
- **Issue:** end with `Closes #42` if the pull request resolves an issue.

### Before you open it

- [ ] `python -m pytest -q` passes.
- [ ] The version number is unchanged; it's bumped when a release is made.
- [ ] No local files are included: virtual environments, databases, logs, `build/` or `dist/`.
- [ ] Nothing in your tests or scripts writes to a Le Mans Ultimate install.

Open it as a draft while it's unfinished or still needs checking in the game, and mark it ready when it's done.
