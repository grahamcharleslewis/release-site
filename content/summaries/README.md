# Release summaries

Each release on the site shows a short "What's new" list. The lists are written
by hand (or drafted with an AI assistant such as Claude Code and checked by a
person) and committed here. A release without a summary file shows
"Summary not available yet" plus its original notes. Pushing a summary file
publishes it straight away.

## Where files go

    content/summaries/<owner>__<repo>/<tag>.md

- `<owner>__<repo>` is the repo name with `/` replaced by `__`,
  e.g. `example-org__example-android-app`.
- `<tag>` is the release tag exactly as on GitHub (with any `/` replaced by `_`),
  e.g. `v1.11` for Android and `1.11` for iPhone.

To see which releases still need a summary, and the exact file path for each:

```sh
python3 scripts/build.py --list-missing
```

## File format

One item per line. `- ` bullets are optional; lines starting with `#` are
comments and never shown on the site. `` `code` `` is shown as code.

```markdown
- Save items to read later, even when you're offline
- Dark mode that follows your device settings
# Unclear: * Feat/app-408 sync settings part2 by @dev-one in https://github.com/example-org/example-android-app/pull/876
```

### Unclear items

When a change in the notes is too vague to describe to a user, don't guess and
don't drop it silently. Copy its line from the release notes **exactly as it
is** into a comment starting with `# Unclear: `. It isn't shown on the site, and
the original line (with its PR link) makes it easy to look up and turn into a
proper item later. Leave clearly internal changes (version bumps, CI, analytics…)
out entirely; `Unclear` is only for changes that might matter to users.

To find every unclear item across all summaries:

```sh
grep -rn --exclude=README.md "^# Unclear:" content/summaries
```

To resolve one, replace the comment with a proper item, or delete it if the
change turns out not to matter to users.

## Writing guidelines

- Write for people who use the app, not for developers.
- One item per change a user would actually notice: a new feature, a visible
  improvement, or a fixed problem.
- Plain language, about 12 words at most. Start with a capital letter; no full stop.
- Combine closely related changes into one item. Aim for 1–8 items, most
  important first.
- Leave out dependency and version bumps, CI or build changes, refactors, tests,
  analytics, performance monitoring, A/B tests, feature switches, survey tooling,
  branch merges ("Main -> release updates") and anything only developers would care about.
- Never include PR numbers, ticket IDs (APP-1234, PROJ-405), commit hashes or
  usernames in items. (`# Unclear:` comments keep the original line, since they're
  never shown.)
- Don't guess. If a change is too vague to describe to a user, keep it as a
  `# Unclear:` comment (see above).
- If nothing in the release is user-facing, write
  `- Maintenance and internal improvements`.

## Example

Android `v2.4` notes (PR titles, abridged):

```
* Feat/app-408 sync settings part2 by @dev-one in …/pull/876
* APP-4004: Search multiple filters bug by @dev-two in …/pull/873
* APP-3996 Saved items component by @dev-three in …/pull/863
* APP-4046 Bump version name to 2.4 by @dev-three in …/pull/878
* fix: Android | Suggested searches only partially tappable (landscape) by @dev-four in …/pull/883
* APP-4051 : Search screen not announced by TalkBack by @dev-five in …/pull/880
* APP-3412 Add Firebase Performance by @dev-six in …/pull/887
* Feat/offline saved items by @dev-one in …/pull/889
* APP-3975: API call on thumbs up/down by @dev-five in …/pull/882
* …
```

`content/summaries/example-org__example-android-app/v2.4.md`:

```markdown
- Save items to read later, even when you're offline
- Fixed a problem when combining more than one search filter
- Suggested searches are now fully tappable in landscape
- Better TalkBack support on the search screen
# Unclear: * Feat/app-408 sync settings part2 by @dev-one in https://github.com/example-org/example-android-app/pull/876
# Unclear: * APP-3975: API call on thumbs up/down by @dev-five in https://github.com/example-org/example-android-app/pull/882
```

Left out as internal: the version bump and Firebase Performance.

## Drafting with Claude Code

To draft many at once, run `scripts/draft-summaries.sh [N]` from the repo root.
It drafts the newest N missing summaries without committing anything (see
"Writing summaries" in the main README).

To do it interactively instead, ask Claude Code something like:

> Run `python3 scripts/build.py --list-missing`. For each release listed, read
> its notes on GitHub and write the summary file at the path shown, following
> `content/summaries/README.md`. Don't commit.

Then review the drafts before committing. Check especially for anything
guessed from a vague PR title.
