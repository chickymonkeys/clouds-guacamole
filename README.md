# Clouds Guacamole

An Omarchy bar widget and background service, built on [rclone](https://rclone.org), for your cloud drives:

- **Stream** whole drives without using disk space. Every drive shows up as a folder in `~/Cloud/Stream`, and files download when you open them.
- **Keep chosen folders on this device.** They are real local folders in `~/Cloud/Sync`, synced both ways: instant, available offline, and safe for apps that read many small files (Obsidian, git, IDEs).

It works with Google Drive, Dropbox, Microsoft OneDrive, Box, pCloud, Nextcloud and ownCloud, WebDAV, S3-compatible storage (AWS, MinIO, Cloudflare R2), Proton Drive, and any remote you have already set up with rclone.

<p align="center">
  <img src="docs/images/panel.png" width="430" alt="The Clouds Guacamole panel: Google Drive and Dropbox streaming with their quotas, Drive's two local folders listed with one syncing (progress, current file and speed), Dropbox's folder closed to one line, two files uploading, Nextcloud not streaming, and recent files">
</p>

```
~/Cloud/
├── Stream/                  one folder per drive, streamed on demand
│   ├── Drive/
│   └── Dropbox/
└── Sync/                    folders kept on this device, synced both ways
    └── Drive/Documents/Notes/
```

Both locations can be changed in Settings, and each drive or folder can also live anywhere you like.

## Streaming

Turn on a drive's switch and it is mounted at `~/Cloud/Stream/<Drive>`. Browsing is instant once a folder has been listed. Files you open are cached, so reopening them is instant too. The cache is 20 GB by default; the least recently used files are evicted first. Edits upload a few seconds after you save. The card shows files still uploading, and unmounting waits for them; you can also choose _Unmount anyway_.

## Keep folders on this device

On a drive card, open **Local folders** and choose **Keep a folder on this device** (a drive with none shows it right away), browse to a folder and confirm. It is downloaded to `~/Cloud/Sync/<Drive>/<path>`, or anywhere else you pick. From then on:

- **Local edits reach the cloud within about 5 seconds.** The service watches the folder and syncs once you stop typing.
- **Cloud edits arrive on a timer** (every 5 minutes by default). They also arrive when you click 󰑐, right after resume from suspend, and when the network comes back.
- **Conflicts never lose data.** If a file changed on both sides, the newer copy wins and the other is kept next to it as `name.conflict1.ext`. The folder lists its conflicts and you get a notification.
- **Safety stop.** If most files disappear on one side (a wiped disk, an unmounted drive, a sync gone wrong), nothing is propagated. The folder asks you to choose between **Resync**, which merges both sides again and never deletes, and **Sync anyway**.
- **Replaced local files are kept** for 30 days under `~/.local/state/guacamole/backups/` (Settings → Engine → Backups).
- **Pause a folder** to keep just that one offline, or while you change many files and don't want each save uploaded. Nothing syncs and no rclone runs for it until you resume. A sync already under way finishes first. The folder still notices your edits and says _paused, changes waiting_. Resuming runs one ordinary sync at once that brings both sides together, with the same conflict copies and safety stop as any other sync.
- **Losing the network needs nothing from you.** A folder that can't reach the cloud says _waiting for network_ and the bar icon turns yellow, not red. It tries again by itself, every 30 seconds to 10 minutes, and soon after the network comes back. _Sync now_ tries at once. Edits made meanwhile stay marked as waiting, also across restarts.
- Editor temp files, `.DS_Store`, `Thumbs.db`, Obsidian's per-device `workspace.json` and similar files are never synced, either way, and changing them never starts a sync. Edit the list in Settings → Engine → Filters: add or remove patterns (`*.log`, `build/`, `/Exports/**`; each one is explained as you type, and nothing changes until you save), or open `~/.config/guacamole/filters.txt` itself, which stays the source and keeps any rule the widget doesn't manage.
- **A folder can have its own never-synced list** instead of the global one: hover it, click 󰈲 and choose _Just this folder_ (it starts from a copy of the global list); a 󰈲 next to its name marks it. The folder's list **replaces** the global one, it is not added to it: patterns you later add to the global list don't reach that folder, and neither do any other rules in `filters.txt` (include rules, for instance). Emptying it, or turning it off, goes back to the global list. Whenever the rules a folder syncs with change, its next sync is a safe resync: nothing is deleted, the newer copy wins, and replaced local files still go to the backups. Files newly excluded stay where they are; files no longer excluded are brought over.

<table>
  <tr>
    <td valign="top" width="50%"><img src="docs/images/keep-folder.png" width="400" alt="Keep a folder on this device: browsing Drive, Documents, Papers, with its size (2.7 GB in 1284 files) and the local folder it will be kept in"></td>
    <td valign="top" width="50%"><img src="docs/images/conflicts.png" width="400" alt="With both drives' folder lists closed, the folders that need a look stay listed: one with two conflicts, both versions kept, and a Dropbox folder stopped by the safety stop, offering Resync, Sync anyway and Dismiss"></td>
  </tr>
  <tr>
    <td align="center">Choosing a folder to keep</td>
    <td align="center">Conflicts and the safety stop</td>
  </tr>
</table>

Under the hood this is `rclone bisync` with `--resilient --recover`, `--conflict-resolve newer`, a 50% max-delete threshold and a local backup directory. Google Docs files are skipped, since they have no real size and can't round-trip.

Missing sync listings trigger one automatic safe resync. If that repair fails, or the next ordinary sync still needs a repair, the folder stops and offers **Resync** in the widget. The repair state survives restarting the daemon. Network and rate-limit failures keep their delayed retries and never force a resync: a connection lost during a transfer only sets the listings aside, and `--recover` restores them on the next ordinary sync, so files deleted meanwhile stay deleted. On a backend without atomic uploads, a partly uploaded file can come back as a conflict, with both versions kept. Expired sign-in offers **Reconnect account**. Conflicts remain listed even if a later part of the sync fails, and **Clear** forgets the notice while keeping the files.

Streaming and keeping folders are independent: a drive can be streamed, have local folders, both, or neither. A folder you keep is also visible inside the streamed drive. Work in the local copy; edits made through the stream reach it on the next sync.

## Work offline

The _Cloud Drives_ switch at the top of the panel covers everything. Turn it off when you won't have a network, or don't want one used, and it stays off until you turn it back on.

<p align="center"><img src="docs/images/work-offline.png" width="400" alt="Working offline: the Cloud Drives switch is off, the header says Working offline with 2 with changes, drives say offline, Nextcloud says not streaming, and every local folder says offline, two with changes waiting. Sync now is dimmed"></p>

- **Nothing reaches the cloud by itself.** Every drive disconnects, no rclone runs for any folder, and quotas stop refreshing, also across restarts. Only something you start yourself, like adding a drive, still connects. Streamed files aren't reachable meanwhile, not even cached ones: keep what you'll need as a local folder.
- **Off means off within seconds.** A running sync winds down at once, through bisync's graceful shutdown, so nothing is lost and the next sync is an ordinary one. A drive stays connected until its uploads are out, then disconnects by itself. If a file is open in it, or there's no network to finish on, its card says so and offers _Unmount anyway_. Uploads left over stay in the cache and continue when the drive reconnects.
- **Folders keep noticing your edits** and say _offline, changes waiting_, and the header counts how many have some.
- **Turning it back on brings back exactly what was on.** The drives you were streaming reconnect, each folder catches up with one ordinary sync, with the same conflict copies and safety stop as any other sync. A drive you weren't streaming stays off and a folder you paused stays paused. Each drive's own switch is dimmed while you're offline, and `guac stream DRIVE off` still takes one out of what comes back.

With nothing to go offline from, no drive streaming and no local folder, the same switch streams every drive.

## Your own OAuth client

Google Drive, Dropbox, OneDrive, Box and pCloud sign in through rclone's built-in OAuth client unless you give them your own. That built-in client is shared by every rclone user, so the providers throttle it. For Google Drive it is also being retired: **it stops working during 2026, so Google Drive needs a client of your own.**

Guacamole walks you through creating one, step by step, the same way for every one of these providers. The steps follow rclone's own documentation (pCloud's developer site for pCloud, which rclone doesn't cover). Each step has a button that opens the right console page and a copy button for the values to paste. At the end you paste the client ID and secret and sign in. The widget checks the format of what you paste, so a secret in the ID field or a OneDrive _Secret ID_ instead of its _Value_ is caught before signing in.

<table>
  <tr>
    <td valign="top" width="50%"><img src="docs/images/client-guide.png" width="400" alt="Step 4 of 6 of the Google client guide: a button that opens the Data access page and the Drive scopes with a Copy button"></td>
    <td valign="top" width="50%"><img src="docs/images/client-paste.png" width="400" alt="The last step: the pasted client ID and secret, a note about Google's unverified-app screen, and Sign in with my client"></td>
  </tr>
  <tr>
    <td align="center">Each step opens the right page and copies the values</td>
    <td align="center">Paste the client, then sign in</td>
  </tr>
</table>

- When adding any of these drives, the own-client setup is on by default (it can be switched off, except that Google Drive then warns it will stop working).
- On an existing drive, use **Set up** on the card's notice, or the drive's 󰇘 menu (_Set up my own client_). A drive that gets rate-limited offers it too.
- From a terminal: `guac set-client-id Drive` runs the same guided setup, and `guac client-guide dropbox` prints the steps.

| Provider     | Own client                                             | What you create                                                                 | Redirect URI              | rclone docs                                                                                                    |
| ------------ | ------------------------------------------------------ | ------------------------------------------------------------------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Google Drive | **Needed** (shared client retiring, heavily throttled) | A _Desktop app_ OAuth client in Google Cloud, about 5 minutes                   | none (loopback)           | [Making your own client_id](https://rclone.org/drive/#making-your-own-client-id)                               |
| Dropbox      | Recommended                                            | A scoped app with Full Dropbox access, about 3 minutes                          | `http://localhost:53682/` | [Get your own Dropbox App ID](https://rclone.org/dropbox/#get-your-own-dropbox-app-id)                         |
| OneDrive     | Recommended                                            | An app registration in Microsoft Entra ID with a client secret, about 5 minutes | `http://localhost:53682/` | [Getting your own Client ID and Key](https://rclone.org/onedrive/#getting-your-own-client-id-and-key)          |
| Box          | Recommended                                            | A custom app with OAuth 2.0 user authentication, about 3 minutes                | `http://127.0.0.1:53682/` | [Get your own Box App ID](https://rclone.org/box/#get-your-own-box-app-id)                                     |
| pCloud       | Recommended                                            | An app with access to all folders, about 3 minutes                              | `http://localhost:53682/` | [pCloud options](https://rclone.org/pcloud/); app console: [My applications](https://docs.pcloud.com/my_apps/) |

Nextcloud, WebDAV, S3 and Proton Drive have no OAuth client to create. Except on Google Drive, the card's notice can be hidden with ×. pCloud accounts in the EU and the US both work: after sign-in Guacamole asks pCloud which server holds the account, which `rclone authorize` alone can't tell.

## Why it stays fast

- **One long-running service.** `guacd` runs one `rclone rcd` that hosts every mount, and gives each running sync its own short-lived rclone process. The widget keeps one socket open and the service **pushes** changes, so nothing is polled and no process starts when the panel opens: it shows current data instantly.
- **Opening the panel never touches the network.** Recent files come from rclone's local cache records and from your local folders. Quotas refresh in the background.
- **Nothing blocks on a stalled mount.** The service never probes a mount on the main path; health comes from rclone's API and `/proc`, and the few unavoidable filesystem calls run on separate threads with timeouts. rclone itself gives up on a dead connection after 60 seconds.
- **Drives mount in parallel**, and mounting returns as soon as the mount is up.
- **Folders sync in parallel**, up to three at once, including several folders of the same drive. Each sync runs in its own rclone process, so its log, conflicts and errors are never mixed up with another folder's.
- **Tuned caching.** On backends that report changes (Google Drive, Dropbox, OneDrive, Box), folder listings stay cached for days and are refreshed by change notifications every 30 seconds. After mounting, the first two folder levels are prefetched in the background, so the file manager opens instantly.

## Install

Requirements: Omarchy 4 (Quickshell shell), `rclone`, `fuse3`, Python 3.10 or newer (standard library only).

```bash
omarchy plugin add https://github.com/chickymonkeys/clouds-guacamole --enable
```

This installs the widget and its background service code; it does not install the dependencies or run `install.sh`. Install missing dependencies with `sudo pacman -S rclone fuse3 python`.

The CLI is included at `~/.config/omarchy/plugins/chickymonkeys.guacamole/bin/guac`. When following the `guac` examples below, use that full path (or `./bin/guac` from a checkout). The local installer below also creates a `~/.local/bin/guac` shortcut, if that name is free.

Or from a checkout:

```bash
./install.sh           # copy into ~/.config/omarchy/plugins and enable the bar widget
./install.sh --link    # symlink instead, for development
./install.sh --uninstall
```

Running it again updates an existing install and restarts the shell and `guacd` so the new version loads (the shell only reloads a changed widget on restart); `--no-restart` skips that. `--no-enable` installs without adding the widget to the bar. `guacd` is left alone while files are uploading.

The widget starts `guacd` on demand as a transient systemd user unit (`guacamole.service`). It survives shell restarts and plugin reloads and stops cleanly at logout, and drives you were streaming are mounted again when it starts. To start it at login even without the bar widget, run `guac service install`.

Remotes already in your rclone config appear as drives right away, not streamed until you turn them on. If another program already mounted something at a drive's folder, the card says **in use elsewhere** and offers **Take over**.

### Update

For an installation made with `omarchy plugin add`:

```bash
omarchy plugin update chickymonkeys.guacamole
```

Wait for uploads and local-folder syncs to finish, then stop `guacd` with the installed CLI and restart the shell with `omarchy restart shell` so both the service and widget load the new code. The widget starts `guacd` again. For a local installation, update your checkout and rerun `./install.sh` instead.

### Remove

**Wait for uploads and local-folder syncs to finish first.** Removing the widget alone does not stop the background service.

For an installation made with `omarchy plugin add`, first disable the widget so it cannot restart the service:

```bash
omarchy plugin disable chickymonkeys.guacamole
```

If you previously ran `guac service install` to start it at login, remove that optional login service **before removing the plugin files**:

```bash
~/.config/omarchy/plugins/chickymonkeys.guacamole/bin/guac service uninstall
```

Then stop the daemon, wait for its systemd user unit to finish stopping, and remove the plugin:

```bash
~/.config/omarchy/plugins/chickymonkeys.guacamole/bin/guac stop
systemctl --user stop guacamole.service
omarchy plugin remove chickymonkeys.guacamole
```

If the service is already stopped, systemctl may report that the unit is not loaded. If you installed from a local checkout, use `./install.sh --uninstall` instead (remove the optional login service first, if enabled). Do not use the local uninstaller to remove an installation made with `omarchy plugin add`.

Removal keeps your cloud files, folders kept on this device, rclone accounts and credentials, Guacamole settings, backups, sync state, and rclone cache. It does not delete `~/Cloud/Sync`, `~/.config/guacamole`, `~/.local/state/guacamole`, `~/.config/rclone`, or `~/.cache/rclone` (or their configured/XDG locations). If you manually created a `guac` shortcut, remove only that shortcut once you have confirmed it points to this plugin.

## The widget

<p><img src="docs/images/bar-states.png" width="660" alt="The bar icon: a green filled cloud when all is good, a spinner while files move, an amber outline with no network or nothing streaming, an amber crossed-out cloud while working offline, and red with a dot when something needs you"></p>

- **Bar icon.** Green when everything you asked for is healthy. Yellow when nothing is streaming, there's no network, or a folder waits for the network. A yellow crossed-out cloud while you work offline. Red with a dot when something needs you. It spins while files move. Left-click opens the panel, right-click syncs local folders now, middle-click flips the panel's _Cloud Drives_ switch.
- **Panel.** The header says the one thing worth knowing now: what needs you, syncing with its speed, offline, or up to date. The _Cloud Drives_ switch next to it works offline. The footer has _Add drive_ and buttons to sync local folders now, open `~/Cloud` and open Settings. Drives and recent files scroll as one list, and opening a dropdown scrolls it into view. Keys: `a` add a drive, `s` settings, `r` sync now, `m` work offline or go back online, `o` open `~/Cloud`, `↑` `↓` or `j` `k` scroll, `Esc` back or close.
- **Drive cards.** Each card shows:
  - a streaming switch, and one line with the drive's quota and files still uploading, or its state when it isn't streaming;
  - problems, with a one-click fix: _Retry_, _Reconnect account_, _Take over_, _Set up my own client_;
  - rename, move, your own client and remove, behind 󰇘;
  - its local folders, in a dropdown.
- **Local folders.** A drive's folders sit behind one line with how many there are and what they are doing: _all synced_, _2 syncing_, _1 needs you_. Click it to list them, one line each with its state. Hover a folder for _Sync now_, _Open_, _Pause_, _Rename_ and _Stop_, and its name for where it syncs. _Rename_ gives a folder a display name of your own; leave it empty for the folder's own name. Progress, conflicts and anything that needs you show under it. Folders with conflicts or a problem stay listed while the list is closed, so nothing that needs you is behind a click. The panel remembers which lists you opened, and adding a folder opens its drive's list.
- **Settings.** Stream and sync locations, cache size, sync interval, backup retention and notifications. The **Engine** row has:
  - _Restart_ (rclone);
  - _Log_, an in-panel viewer for Guacamole's and rclone's logs, with a problems-only filter, copy and open;
  - _Filters_, the never-synced patterns of every folder without its own list, editable in place, with a link to open `filters.txt` in your editor;
  - _Backups_, the local files syncs replaced or deleted (it says so when there are none yet).

<table>
  <tr>
    <td valign="top" width="50%"><img src="docs/images/folder-list.png" width="400" alt="Drive keeping nine folders with its list closed: the folder needing attention remains visible with its conflict notice. Dropbox's folder is closed to one line too"></td>
    <td valign="top" width="50%"><img src="docs/images/folder-list-open.png" width="400" alt="The same list open: nine folders one line each, two of them syncing with their progress, current file and speed, then Keep a folder on this device"></td>
  </tr>
  <tr>
    <td align="center">Local folders, closed</td>
    <td align="center">Local folders, open</td>
  </tr>
</table>

<table>
  <tr>
    <td valign="top" width="50%"><img src="docs/images/add-drive.png" width="400" alt="Connect a cloud drive: Google Drive, Microsoft OneDrive, Dropbox, Nextcloud or ownCloud, S3, MinIO or R2, Box, pCloud, Proton Drive and WebDAV"></td>
    <td valign="top" width="50%"><img src="docs/images/settings.png" width="400" alt="Settings: stream and sync locations, cache size, sync interval, backup retention, notifications, and the Engine row with Restart, Log, Filters and Backups"></td>
  </tr>
  <tr>
    <td align="center">Add Drive</td>
    <td align="center">Settings</td>
  </tr>
</table>

<p align="center"><img src="docs/images/filters.png" width="400" alt="Settings with the Engine filter editor open: saved never-synced patterns, a draft build/ pattern with a plain-language explanation, and Save and Cancel controls"></p>

## Command line

```bash
guac status [--json]                    # drives, local folders, state
guac stream Drive on|off [--force]      # mount / unmount (also: guac mount|unmount Drive)
guac stream-all on|off
guac keep Drive:Documents/Notes [~/Notes]   # keep a folder on this device
guac unkeep Drive:Documents/Notes [--delete-local]
guac sync [FOLDER]                      # sync now
guac pause|resume FOLDER...             # keep just these offline; resume catches up at once
guac offline [on|off]                   # drives disconnect, folders stop; off brings back what was on
guac alias FOLDER "Team notes"          # display name; empty: the folder's own name
guac exclude [FOLDER] [--add P] [--remove P] [--global]  # never-synced patterns: global, or a folder's own
guac resolve FOLDER resync|force|dismiss
guac browse Drive:Documents             # list folders
guac size Drive:Documents/Notes
guac add-oauth Work drive --own-client  # guided own client, then browser sign-in
guac add-credentials Home nextcloud < creds.json   # {"url": ..., "user": ..., "pass": ...}
guac set-client-id Drive                # guided; or --client-id ID (secret on stdin)
guac client-guide drive|dropbox|onedrive|box|pcloud
guac reconnect Drive
guac retry|takeover Drive
guac label Drive "Google Drive" ; guac set-path Drive ~/GDrive
guac config [KEY [VALUE]]               # mount_root, local_root, cache_max_size_gb, sync_interval_min, …
guac log [-n 200] [--daemon]
guac open [Drive]
guac daemon [--ensure] | guac stop | guac watch
guac service install|uninstall
```

`FOLDER` is a folder id, its alias, `REMOTE:PATH`, or the local path.

## Files

| Path                               | What                                                                                                           |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `~/Cloud/Stream/`, `~/Cloud/Sync/` | Streamed drives and folders kept on this device (both configurable)                                            |
| `~/.config/guacamole/config.json`  | Settings, and per drive: streaming, mount path, local folders                                                  |
| `~/.config/guacamole/filters.txt`  | Global never-synced patterns (editing it makes the next syncs safe resyncs)                                    |
| `~/.local/state/guacamole/`        | `state.json`, `daemon.log`, `rclone.log`, bisync work files, `backups/`                                        |
| `$XDG_RUNTIME_DIR/guacamole/`      | Private sockets: `daemon.sock` (widget, CLI), `rclone.sock` (rclone rc), `sync-<id>.sock` while a folder syncs |
| `~/.cache/rclone/vfs*`             | rclone's stream cache                                                                                          |

Accounts and credentials stay in rclone's own config (`~/.config/rclone/rclone.conf`). Secrets travel only over the private sockets and through the environment of `rclone authorize`, never on a command line, since every local user can read other processes' arguments.

The CLI and widget authenticate the daemon's user ID using Linux peer credentials before exchanging messages. The widget keeps one Python bridge running for this check and uses private pipes to talk to it. Socket directories must belong to your user and have mode `0700`; symlinks and unsafe parent directories are rejected. If `XDG_RUNTIME_DIR` is absent or its socket paths would be too long, `/tmp/guacamole-<uid>` is used with the same checks. An unsafe existing directory causes an error; Guacamole does not take it over or change its permissions.

Human-readable CLI output renders control characters visibly, including in cloud paths, status, errors, logs and confirmation prompts. A shared folder's name cannot send terminal escape commands such as OSC 52 clipboard writes. Bidirectional text controls and Unicode line separators are also shown explicitly. The original paths stay intact in requests and JSON output, so browsing and syncing still address the actual folders. Guacamole's daemon log keeps each message on one line and marks traceback and stack continuations with `    | `, so untrusted text cannot masquerade as a new log entry. Terminal controls are escaped too; rclone's own log is escaped when read through `guac log`.

Settings, state and filter updates use exclusively created temporary files with mode `0600`, then replace the destination atomically. A pre-existing predictable `.tmp` file or symlink is left alone. Filter files managed through a dotfiles symlink still update the linked target.

## Development

```bash
tests/test_config.py   # settings, provider guides and client checks, path rules
python3 tests/test_install.py  # local installer lifecycle, ownership checks, retained user data
tests/test_daemon.py   # end-to-end: real FUSE mounts and bisync on throwaway local remotes
python3 tests/test_recovery.py  # failure transitions, real local bisync jobs and the widget off-screen
python3 tests/test_ipc.py  # runtime ownership, daemon authentication, credential non-delivery and widget reconnects
python3 tests/test_security.py  # hostile terminal text, JSON preservation and atomic-write safety
```

The end-to-end suite runs `guacd` against two local rclone `alias` remotes in temporary directories, with its own rclone config. It covers:

- streaming and write-through, busy unmount, cleanup of empty mount folders;
- keeping folders local: syncing in both directions, deletions, conflicts, the safety stop and resync, and folders of one drive syncing in parallel;
- pausing a folder: no rclone runs while paused, edits on both sides wait, and resuming catches up with one normal sync;
- working offline: a drive waits for its uploads, then disconnects and keeps its own switch; no rclone runs for any folder, also after a restart; a new folder waits for its first sync; turning it off brings back only the drives that were on, catches up with one normal sync and leaves a paused folder paused;
- recent files;
- moving the stream and sync roots;
- recovery from an rclone crash, and taking over a foreign mount;
- path and client validation, logs, hostile cloud folder names on terminal and redirected output, and clean shutdown.

The recovery suite needs no FUSE mounts. It covers stopping a sync mid-transfer without losing its listings, also with the network gone or while shutting down, safety-stop Resync and Sync anyway, missing listings, failed and repeated repairs across restart, transient backoff, conflicts from failed jobs, working offline, a real HTTP remote losing its connection, keeping deletions and whole files after a transfer is cut, and the widget's controls, theme colors, offline switch and visibility while folder lists are closed. Real-job tests need rclone; the widget check needs Quickshell and `/usr/share/omarchy/shell`. Those sections are reported as skipped if their dependencies are unavailable.

The Python code is formatted and checked with [ruff](https://docs.astral.sh/ruff/) and [ty](https://docs.astral.sh/ty/) (`uv tool install ruff ty`; settings in `pyproject.toml`):

```bash
ruff format && ruff check && ty check
```

The screenshots in `docs/images` come from `docs/screenshots/render.py`, which runs the real widget off-screen against a demo `guacd` with made-up drives and folders (it needs Quickshell and the Omarchy shell).

## License

MIT, © 2026 chickymonkeys. See [LICENSE](LICENSE).

Parts of the bar widget are derived from [ODrive](https://github.com/TiniTinyTerminator/ODrive), © 2026 TiniTinyTerminator, released under the MIT License.
