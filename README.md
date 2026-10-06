# Copium Timer

A small Windows app for sim racers. Copium Timer tracks your best time in each sector of a lap and shows the **theoretical best lap**: the sum of your best sectors, i.e. the lap you *could* do if you put it all together. It runs as two windows:

- **Green Timer**: floating green digits (HH:MM:SS) of a stopwatch that starts when the app opens. The elapsed time is saved, so it continues across sessions.
- **Copium timer**: a terminal window with the COPIUM TIMER banner, the theoretical best in big purple digits, and a sector table. Each sector shows when it was set and what the Green Timer read at that moment.

New sector times are only accepted if they beat your current best and aren't faster than the sector's limit (a time nobody could realistically do).

## Requirements

- Windows 10 or 11
- Python 3.8+ (standard library only, nothing to install)

## Running

Double-click `Copium Timer.bat`, or run:

```
python copium_timer.py          # both windows
python copium_timer.py --cli    # terminal only, in the current console
python copium_timer.py --gui    # Green Timer only
```

Closing either window closes the other.

## Commands (Copium timer window)

| Command | What it does |
|---|---|
| `s1 1:30.999` | Submit a new best for sector 1. Exact format: `s<N> M:SS.mmm` |
| `undo` | Revert the last accepted sector time |
| `reset` | Reset all sectors to the track's defaults (asks to confirm with `YES`) |
| `stop` / `start` | Stop or start the Green Timer |
| `restart` | Close and reopen both windows, picking up changes to the code or `sectors.json` |
| `help` | Show the command list |
| `quit` | Close both windows |

## Green Timer controls

- **Drag** the digits to move them (the position is remembered).
- **Scroll** over the digits to resize.
- **Right-click** for Stop/Start timer, Reset timer, Always on top, Restart windows and Quit.

## Setting up a different track

Sector data lives in `sectors.json` next to `copium_timer.py`. Without a `track` name, the file follows the built-in sectors in `DEFAULT_SECTORS` in the code. To use your own track, give the file a `track` name and list its sectors:

1. Close the app.
2. If you want to keep your current times, rename the existing `sectors.json` (e.g. to `sectors-default.json`).
3. Create a new `sectors.json`:

   ```json
   {
     "track": "Spa-Francorchamps",
     "sectors": [
       { "name": "Sector 1", "default": "0:42.512", "limit": "0:36.000" },
       { "name": "Sector 2", "default": "1:12.304", "limit": "1:01.000" },
       { "name": "Sector 3", "default": "0:31.870", "limit": "0:27.000" }
     ]
   }
   ```

4. Start the app (or type `restart` if it's running).

| Field | Required | Meaning |
|---|---|---|
| `track` | yes | Track name, shown next to THEORETICAL BEST. Marks the file as a custom track so the app won't overwrite it with the built-in sectors |
| `name` | no | Sector name (first 10 characters are shown). Defaults to `Sector 1`, `Sector 2`, ... |
| `default` | yes | Starting time for the sector, `M:SS.mmm` |
| `limit` | yes | Fastest allowed time; anything quicker is rejected as impossible. Must not be slower than `default` |
| `best` | no | An existing personal best to start from. Defaults to `default` |

Tips:

- Times are always `M:SS.mmm` with exactly three decimals: `0:42.512`, `1:12.304`.
- A track can have any number of sectors; they're entered as `s1`, `s2`, `s3`, `s4`, ...
- A reasonable limit is about 85% of the default time (the built-in sectors use ~84.4%). For example, a `1:11.069` sector gets a `1:00.000` limit.
- Once the app runs, it rewrites the file with the times in milliseconds (`default_ms`, `floor_ms`, `best_ms`) and adds its own fields (`achieved_at`, `history`, ...). That's expected.
- If the file has a mistake, the terminal shows what's wrong, the file is moved to `sectors.json.invalid`, and the built-in sectors are used. Fix the file, rename it back to `sectors.json`, and type `restart`.
- To switch tracks, swap `sectors.json` for another track's file and `restart`.

## Files

| File | Contents |
|---|---|
| `copium_timer.py` | The whole app |
| `Copium Timer.bat` | Launcher without an extra console window |
| `sectors.json` | Sector bests, defaults, limits and undo history (created on first run) |
| `green_timer.json` | Green Timer elapsed time, running state and window position |

The `.json` and `.png` files are git-ignored, so your times stay local.

## Customizing

Settings are constants near the top of the sections in `copium_timer.py`; type `restart` after changing them:

- `DEFAULT_SECTORS`: built-in sector names, default times and limits
- `GREEN_HEX`, `EDGE_HEX`, `WINDOW_ALPHA`: Green Timer color, outline and opacity
- `PURPLE`: color of the theoretical best
- `CONSOLE_FONT`, `CONSOLE_FONT_PX`: Copium timer font and maximum font size
