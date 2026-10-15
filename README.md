# Steam Deck Large Library Optimization Plugin

[![简体中文](https://img.shields.io/badge/简体中文-切换-2563eb?style=for-the-badge)](README.zh-CN.md)

If you have a very large Steam library, logging in, waking your Steam Deck, or browsing your games can mean a long wait and severe stuttering—even when most of those games aren't installed. A major cause is the way Steam syncs account data: it repeatedly filters, sorts, and updates the entire library and its collections, piling work onto the UI thread and slowing down interaction. Common workarounds are to use [offline mode](https://github.com/ValveSoftware/steam-for-linux/issues/9956#issuecomment-1806394364), or to suspend from inside a game so you can at least get back to playing while the Steam UI catches up. This goes back years: [in April 2022, users with large libraries were already reporting startup waits of over ten minutes](https://www.reddit.com/r/Steam/comments/uaxhc4/comment/i60yv91/), followed by [a 2023 report of the UI freezing for 30–60 seconds after waking](https://github.com/ValveSoftware/steam-for-linux/issues/9956). Valve has released [optimizations for large libraries](https://store.steampowered.com/news/app/1675200/view/3220645457134651321), but this kind of stuttering still hasn't been fully resolved. This plugin aims to improve that long-standing experience.

## Download and install

1. Exit your games and switch your Steam Deck to **Desktop Mode**.
2. Download `SteamDeck-LargeLibraryFix-0.4.16.desktop` from **[Releases](https://github.com/dtq1997/steamdeck-large-library-fix/releases)** on GitHub. You can also download it on another computer and copy the file to your Deck's desktop.
3. Open the file. If a security prompt appears, choose **Trust and Launch**, **Continue**, or the equivalent run option. If it opens as text, right-click it → Properties → Permissions, allow it to run as a program, then open it again.
4. Once **Installed** appears, restart Steam normally—or restart your Deck—when convenient to load the patch.

The file includes everything it needs. No extraction, installation-time internet connection, or administrator password is required.

## Improvements

- Reduce repeated computation during library synchronization after waking.
- Reduce filtering, sorting, and rendering work when switching tabs, showing collections, and scrolling down.
- Improve some cases where held navigation stops or scrolling leaves focus outside the visible area.
- Prevent store-data requests from blocking startup for too long, without changing games, saves, or collection definitions.

This is a preview release and some stutter remains. Testing used one Deck with roughly 40,000 games; results may differ on other devices.

## How it works

The plugin patches recognized Steam UI code to avoid repeated work: it reuses unchanged game data, shares filter results within an update, and recalculates collection counts and cover previews only when relevant data changes. It also adjusts parts of navigation and scrolling, and limits how long store-data requests can hold up initialization.

The patch and original backups stay in your user directory. Compatibility is checked by feature group when Game Mode starts and relevant files change. Checks recognize formatting changes and some variable and class renaming. Wake checks cover the relevant model members, reducing disruption from unrelated helper changes. Verified results are reused to avoid repeated parsing at startup. Compatible groups are reapplied after updates; groups whose related logic changed are paused. Check status lists active and paused groups. Unrecognized changes can still require a newer compatible release; old logic is not forced onto them.

## Usage

Open **Steam Library Fix** on your desktop and choose:

- **Check status**: Check whether the current Steam files on disk contain a compatible patch. This does not confirm that an already-running UI has loaded it.
- **Check for updates**: Find the newest GitHub release and install it after confirmation and download verification. Existing backups are kept; a failed download leaves your installation unchanged.
- **Install / repair this version**: Apply the version bundled with this installer. It does not download a newer version.
- **Uninstall and restore**: Remove automatic checks and restore original files that still match this tool's modification records. Then restart Steam normally.
