# Steam game names

Offline Steam name resolution (Linux and Windows). Back to the [README](../README.md).

On **Linux**, Proton/Wine games show up as a window class `steam_app_<AppID>` with no
`.desktop` file, which used to display as "Steam App 2620". Names are now
resolved **entirely offline** from Steam's own files (no Steam Web API, no
network; `tests/test_steam_library.py` fails if any network module is ever
imported):

1. A `.desktop` entry (e.g. a Steam shortcut with `steam://rungameid/<id>`)
   still wins if one exists.
2. Otherwise `<library>/steamapps/appmanifest_<AppID>.acf` (`name`, falling
   back to `installdir`). Libraries come from every Steam root
   (`~/.local/share/Steam`, `~/.steam/steam|root`, Flatpak
   `~/.var/app/com.valvesoftware.Steam/...`, Snap; symlinked duplicates
   collapsed) plus each path in `libraryfolders.vdf` (current and legacy
   formats). Libraries on unmounted drives are skipped.
3. Native Linux Steam games (whose window class doesn't say "steam") are named
   via the `SteamGameId`/`SteamAppId` environment Steam sets on the game's
   process; their app key is unchanged.

The app *key* never changes, so existing history keeps grouping under the same
app. If nothing is found (uninstalled game, unknown AppID, no Steam) it falls
back to "Steam App <id>" and tracking is unaffected; a good name already stored
is never overwritten by that fallback. On daemon start, rows still holding the
old "Steam App <id>" name are renamed if the game is now resolvable.

Performance: results, including "unknown", are cached; the daemon's per-poll
lookups do no disk I/O. The cache is dropped when `libraryfolders.vdf` or a
library's `steamapps` directory changes (checked at most every 30 s), so newly
installed games appear without a restart.

Diagnosing a name that still shows as "Steam App <id>": run
`python -m screentime.steam_library` (lists the Steam locations searched and
every game found; pass AppIDs to test specific ones). The daemon also logs once
per unresolved AppID which locations it searched (`journalctl --user -u
screentime-daemon`), and `scripts/diagnose.sh` includes both. If the name is
still old right after an upgrade, the running daemon is probably pre-upgrade;
see "Upgrades".

Limitations: only *installed* games can be named (uninstalled games have no
manifest; Steam's binary `appinfo.vdf` is not parsed). Names come from the
manifest, so they are in the game's default language.


### Windows

Native Windows Steam is supported with the same offline rules (no Steam Web API, no network):

* Steam is located from its registry entry (`HKCU\Software\Valve\Steam`) and the default `Program Files` folders, then every library in `libraryfolders.vdf` (paths like `D:\\SteamLibrary` are handled).
* A game is recognised by **where its executable lives**: anything under `<library>\steamapps\common\<installdir>\` is matched to the `appmanifest_<AppID>.acf` with that `installdir`. It is tracked under the same key Linux uses, `steam_app_<AppID>`, and shown with the installed game's name. This works even for games whose process cannot be inspected.
* If a game exposes `SteamAppId`/`SteamGameId` in its environment, that is used as well where Windows allows reading it.
* Uninstalled games, or no Steam at all, fall back to the executable's name; tracking is unaffected.
