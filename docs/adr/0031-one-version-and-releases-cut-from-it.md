# 31. One version, written in `pyproject.toml`, and Releases cut from it

Date: 2026-09-16 · Status: Accepted

The product had three version numbers and no Releases. The app said 0.1.0, from a literal in
`shell/Resources/Info.plist`. The engine said 0.0.0, from `pyproject.toml` and again from a literal
in `gpt_voicecoding/__init__.py`, and that is what Diagnostics, the control plane and codex
app-server's `clientInfo` were told. There was no version tag. `scripts/install.sh` built whatever
`main` held, so the number a user saw named no published state of the code.

## Decision

**`pyproject.toml`'s `version` is the only place the version is written.**

- The engine reads it at import (`gpt_voicecoding/_version.py`): a source checkout reads its own
  `pyproject.toml`, because an editable install's metadata keeps the version from its last
  `pip install -e`; an installed tree, the app bundle among them, reads the metadata pip wrote from
  that same file.
- The app build writes it into `CFBundleShortVersionString` (`app_bundle`), beside the git revision
  it already wrote into `CFBundleVersion`. `Info.plist` has no version of its own.

**A Release is cut by a workflow, never by hand.** Releasing is a PR that changes the version.
`.github/workflows/release.yml` runs when CI has passed on a push to `main`, and
`scripts/cut_release.py` creates `v<version>` at that commit with GitHub's generated notes if the
tag does not exist yet. A push that leaves the version alone finds the tag and does nothing. The
version must be plain `MAJOR.MINOR.PATCH`. A tag whose Release was deleted is reported and left
alone.

**The installer builds the latest Release.** It asks GitHub's API for the latest Release's tag and
builds that tree. `GPT_VOICECODING_REF` names another tag or branch instead (`main` for the owner
and testers). The installer is always `main`'s copy and builds the tree the ref names.

Rejected: deriving the version from git tags (hatch-vcs). Between Releases it reads
`1.0.1.dev3+g1a2b3c4`, and CI, the installer and the build would all need full git history to
compute it.

## Consequences

- The first Release is `v1.0.0`, cut when this change lands. No earlier commit can carry it: that
  tree would build an app at 0.1.0 around an engine at 0.0.0.
- A fix reaches users only when it is released, so a Release should follow soon after a fix that
  users need. Cutting one is a one-line PR.
- The installer now needs `api.github.com` as well as `github.com`, and stops with a message when no
  Release can be found.
- The existing checkout under `~/Library/Application Support/GPT-VoiceCoding/source` moves from the
  `main` branch to a detached tag on its next install; a local edit that conflicts stops the
  install with a message.
- The Control Plane protocol version (ADR 0016) is a separate number and is unchanged.
