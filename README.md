# herdr-pr

A [Herdr](https://herdr.dev) plugin that shows the pull request for each
workspace's current branch in the sidebar, and opens it in the browser on a key.

```
● api
  feature/login · #42 merged
● web
  fix/navbar · #57 changes requested
```

Works with **GitHub** and **Forgejo** (Codeberg and self-hosted), including PRs
opened from a fork to its parent. The label is one of `#N open`, `#N approved`,
`#N changes requested`, `#N draft`, `#N merged` or `#N closed`. Workspaces
without a PR, or not on a supported forge, show nothing.

## Requirements

- Herdr 0.9.1 or later, on Linux or macOS
- Python 3.9 or later (standard library only) and `git`
- Optional: [`gh`](https://cli.github.com) and/or
  [`fj`](https://codeberg.org/forgejo-contrib/forgejo-cli) logged in, for
  private repos and for hosts beyond github.com and codeberg.org

## Install

```sh
herdr plugin install twiking/herdr-pr
```

Then add `$pr` to the sidebar's Space rows and bind the `open` action in
`~/.config/herdr/config.toml`:

```toml
[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace"],
  ["branch", "git_status", { token = "$pr", rules = [
    { contains = "merged", fg = "#c678dd" },
    { contains = "approved", fg = "#98c379" },
    { contains = "changes requested", fg = "#e5c07b" },
    { contains = "open", fg = "#61afef" },
    { contains = "draft", dim = true },
    { contains = "closed", fg = "#e06c75" },
  ] }],
]

[[keys.command]]
key = "prefix+m"
type = "plugin_action"
command = "twiking.pr.open"
description = "Open this workspace's pull request in the browser"
```

Reload with `prefix+shift+r` (or `herdr server reload-config`). The first two
rows are Herdr's defaults; put `$pr` wherever you like. `prefix+m` is free in
Herdr's default keymap; pick another key if you've used it. The browser is
opened with `xdg-open` (`open` on macOS).

Custom key bindings aren't listed in Herdr's prefix-mode bar; they show up in
the `prefix+?` help panel with their `description`.

## How it works

A background watcher (one per Herdr session) refreshes every 60 seconds, and
right away when you switch workspace. For each workspace it takes the focused
pane of the active tab, reads the branch and the remote it tracks, and asks that
forge for the PR whose head is the branch.

- **Review state** comes from each reviewer's latest verdict; comments don't
  count, dismissed reviews are ignored, and one "request changes" outranks any
  approvals. Drafts (or titles starting with `WIP`/`Draft`) show as `draft`.
- **Forks:** the remote's repo is searched, then its parent.
- **Forgejo** deletes merged branches and rewrites the PR's head ref, so PRs are
  matched on the head label and then the head commit.
- **Merged/closed** results are remembered per branch, so they keep showing
  after the PR ages out of the forge's recent list.

## Hosts and logins

No setup is needed for public repos on github.com and codeberg.org. Otherwise
hosts come from your existing CLI logins:

- **GitHub:** github.com plus every host `gh auth login` knows. Token from
  `GH_TOKEN`/`GITHUB_TOKEN`, else `gh auth token`. Without a token GitHub allows
  60 requests an hour, so lookups slow to every 5 minutes and back off when the
  limit is hit.
- **Forgejo:** codeberg.org plus every host `fj auth login` knows, including
  ones served on a port or under a subpath. Token from `FORGEJO_TOKEN`, else
  fj's login file. An expired OAuth login is renewed by running `fj whoami`.

SSH remotes that use a host alias from `~/.ssh/config` (e.g.
`git@github-work:org/repo`) are resolved to the real host.

## Settings

All optional, in `$(herdr plugin config-dir twiking.pr)/pr.conf`:

```
forgejo = host,...   # extra Forgejo hosts (host[:port][/path])
github = host,...    # extra GitHub hosts, e.g. GitHub Enterprise
interval = 60        # seconds between refreshes (min 15)
```

Changes apply on the next refresh.

## Limitations

- GitLab, Bitbucket and other forges aren't supported. Gitea uses the same API as
  Forgejo and should work if listed under `forgejo`, but is untested.
- The PR is looked up under the owner of the remote the branch tracks. If you
  track upstream but push to a fork (`pushRemote`), the PR isn't found.
- On Forgejo only the 50 most recently updated PRs per repo are searched, so a
  PR that has sat untouched while 50 others changed may not show until it's
  updated.
- Self-hosted forges must serve their API over HTTPS.

## Troubleshooting

- Logs: `watch-*.log` (one per session) in the plugin's state dir, e.g.
  `~/.local/state/herdr/plugins/twiking.pr/` on Linux.
- `python3 -I pr.py check` (from the plugin dir, inside Herdr) prints what every
  workspace resolves to. `python3 -I pr.py check HOST OWNER/REPO BRANCH` looks up
  one branch.
- `herdr plugin action invoke twiking.pr.refresh` forces a refresh.
