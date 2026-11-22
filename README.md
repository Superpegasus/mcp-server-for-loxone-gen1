# loxone-mcp-gen1 · v0.9

**Loxone MCP for Miniserver Gen 1**

An MCP server that lets an AI assistant (e.g. Claude) **read, analyze and safely edit copies of Loxone Config project files** (`.Loxone`) — including on a **Miniserver Gen 1**, which the official Loxone MCP does not support.

> **Unofficial.** Not affiliated with or endorsed by Loxone Electronics GmbH. "Loxone" is a trademark of its owner. Use at your own risk.

## Why

Most Loxone MCP servers can only *control* devices (switch lights, read states). This one works on the **program**: it understands pages, blocks, connections and placeholders, and can add and wire logic — always into a **new file** that you open in Loxone Config, check, and load into the Miniserver yourself. Loxone Config stays the final safety check.

## Features

| Area | Tools |
|---|---|
| Read | `project_list_files`, `project_open` (with automatic comparison to the last Miniserver backup and the Config autosave), `project_list_pages`, `project_get_page`, `project_find`, `project_get_block`, `project_trace`, `project_text` (readable text view) |
| Catalog | `catalog_learn` (learns block types and templates from your own projects), `catalog_list_types`, `catalog_get_type` |
| Edit logic | `block_add`, `block_clone` (also large blocks: lighting controller, room controller, automatic blinds), `ref_add`, `block_connect`, `block_disconnect`, `block_set_params`, `block_set_inverted`, `block_set_position`, `block_remove`, `page_add` (new program page) |
| Build plan | `build_plan`: many steps in one call, **all or nothing**, names via `als`/`$name`, missing placeholders created automatically |
| Status with icons | `state_add` (text, icon and colour per state), `icon_find` (searches the Loxone icon library) |
| Peripherals | `io_add_virtual_input` (incl. text inputs), `io_set_display`, `io_set_statistics`, `io_add_command` (e.g. Home Assistant webhook), `rs232_add`, `periphery_remove`, `notification_add` (Loxone app notification) |
| App display | `state_style` (colour/icon per state of an existing status block), `app_set` (favourite, order, icon), `structure_favorite`, `block_set_room_category`, `page_color_by_function` |
| Schedules | `heating_schedule_set` (room controller V2), `schedule_set` (DayTimer per weekday) |
| Layout | `layout2.py` (layered layout: inputs left, logic in columns, outputs right; feedback loops ignored), `layout_opt.py` (local search so connections do not run across blocks), `layout_all.py` (command line) |
| Network (read only) | `net_check_outputs` (ping/port of all virtual outputs with an address), `net_find_device` (by MAC), `yeelight_find`, `mijia_find` (miIO discovery) |
| Rooms/categories | `structure_list`, `structure_remove`, `structure_reassign` |
| Working copy | `project_changes`, `project_validate`, `project_lint`, `project_undo`, `project_discard`, `project_save` (always a new file, never overwrites) |
| Miniserver (read only) | `live_status`, `live_page`, `live_block`, `live_history`, `backup_miniserver` (FTP download of the running program into a local Git repo, with a plain-text diff), `verify_load` (after loading: waits for the Miniserver, backs up, compares with the expected file) |
| Analysis & recipes | `project_analyze`, `recipe_list`, `recipe_apply` |

Tool descriptions are in **English**; result messages and build-plan keys are still German.

## Requirements

- Windows recommended (the icon library path defaults to Loxone Config's install folder; set `LOXONE_ICON_LIBRARY` elsewhere). On other systems old versions are moved to a `.loxone-papierkorb` folder instead of the recycle bin
- Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/)
- Loxone Config installed (for the icon library) and your project files
- Optional: Git (for `backup_miniserver`)

## Setup

Add to your MCP client configuration (example for Claude Desktop):

```json
{
  "mcpServers": {
    "loxone-mcp-gen1": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\path\\to\\loxone-mcp-gen1", "server.py"],
      "env": {
        "LOXONE_PROJECTS_DIR": "C:\\Users\\<you>\\Documents\\Loxone\\Loxone Config\\Projects",
        "LOXONE_HOST": "http://<miniserver-ip>",
        "LOXONE_USER": "<user>",
        "LOXONE_PASS": "<password>"
      }
    }
  }
}
```

Then, in the assistant: run `catalog_learn` once (it learns the block types from your project files) and `project_set_main` with a pattern for your project, e.g. `my_home_v*.Loxone`.

### Settings

| What | How |
|---|---|
| Project folder | `LOXONE_PROJECTS_DIR` (default: `Documents\Loxone\Loxone Config\Projects`) |
| Main project | `settings.json` → `hauptprojekt_muster` (see `settings.example.json`), set via `project_set_main` |
| Catalog file | `LOXONE_CATALOG` (default: `catalog.json` next to the server; created by `catalog_learn`) |
| Miniserver access (read only) | `LOXONE_HOST`, `LOXONE_USER`, `LOXONE_PASS` — use a dedicated user with minimal rights |
| Icon library | `LOXONE_ICON_LIBRARY` (path to an `IconLibrary.zip`) or `LOXONE_CONFIG_DATA` (default `C:\ProgramData\Loxone`) |
| Backup folder | `LOXONE_BACKUP_DIR` (default `Documents\Loxone\Miniserver-Backup`) |

## Safe workflow

1. `project_open` — check the `abgleich` field: does the file match the Miniserver?
2. Edit (ideally with `build_plan`), then `project_validate` / `project_lint`.
3. `project_save("my_home_v23.Loxone")` — always a new file. The project timestamp is set to *now*; otherwise Config would treat the Miniserver program as newer when loading.
4. Open the new file in Loxone Config, **decline the "automatic backup" prompt**, check it, load it into the Miniserver.
5. `verify_load(cleanup=True)` — confirms the Miniserver runs exactly this file.

## Known pitfalls (learned the hard way)

- Loxone Config **silently drops invalid connections**. Input placeholders must use output `AQ` (digital memories: `Q`).
- Config replaces `<` and `%` in block titles with `_`.
- Querying `/dev/sps/io/<name>/state` on a **virtual text input overwrites** its text with "state". The live tools never query text inputs.
- System variables are fed via an output placeholder of the system variable, not via the `source` attribute.
- Pages have no size in the file; the visible sheet is about 23,400 × 16,300 units. New blocks are placed inside it.
- After loading, a Gen 1 Miniserver is unreachable (HTTP 503) for about a minute.
- Status thresholds: `Cond0` 1 means "greater than", 2 "greater or equal" – use 2 for thresholds, otherwise a value exactly on the threshold falls through.
- DayTimer entries sit directly in the object; `Ix` is the position in the timer's *mode list*, not a fixed weekday. Config keeps only as many entries as the timer has modes. Room controller schedules live in `<Timer N=…>` and `N` must equal the number of entries.
- Config drops `Def` values on connected inputs and zero geometry values when loading; the comparison tools ignore both.
- Analogue Tree valve actuators report 0 % via `astate`; `state` returns the input value, which `live_block` scales with SourceValHigh → DestValHigh.
- Zehnder ComfoAir (RS232) sends a data byte `0x07` as `07 07`, which shifts all following bytes for fixed-position RS232 sensors.

## What it deliberately does *not* do

- It never uploads to the Miniserver and never overwrites your files. Loading stays in Loxone Config.
- It sends no switching commands. Live access is a fixed whitelist of read-only requests (plus an FTP download for backups).

## Tests

```bash
uv run python tests/test_units.py          # no project needed
uv run python test_smoke.py "C:\path\to\your\project.Loxone"
```

## License

MIT — see [LICENSE](LICENSE).

## Changelog

- **0.9.1** – App display tools (`state_style`, `app_set`, `structure_favorite`, `block_set_room_category`, `page_color_by_function`); schedules (`heating_schedule_set`, `schedule_set`, build-plan ops `heizplan`, `schaltuhr`); build-plan op `verschieben` (move whole independent groups to another page); layered layout and crossing optimizer (`layout2.py`, `layout_opt.py`, `layout_all.py`); read-only network tools (`net_check_outputs`, `net_find_device`, `yeelight_find`, `mijia_find`); `block_set_position` also sets missing geometry; `block_remove` removes user-permission references; live values of Tree valve actuators scaled correctly; `verify_load` ignores default values on connected inputs; unit test for DayTimer entries and permission references.
- **0.9.0** – `page_add`, `notification_add`, English tool descriptions, unit tests, recycle fallback outside Windows, `build_plan` ops `trennen`, `seite`, `mitteilung`, `kategorie_umhaengen`, `kategorie_loeschen`; empty virtual outputs can be removed; parser accepts attributes starting with a digit (newer Config files).
- **0.8.0** – first public release.
