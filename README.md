# Seagate Backup Plus Hub — LED & Sleep Control

A small Windows tool to turn the LED on/off and change the sleep (standby) timer on a
**Seagate Backup Plus Hub** USB drive.

It talks to the drive directly through SCSI pass-through commands — no Seagate Dashboard
or Toolkit installation required. The commands were derived by reverse-engineering
Seagate Dashboard 4.9.1.

## Features

- Auto-detects the Backup Plus Hub (by SCSI `INQUIRY` vendor/product)
- Shows the current LED state and standby timer
- Turns the LED on / off
- Reads and changes the standby timer (Never, 3/4/5/10/15/30 min, 1–5 h, or custom seconds)
- Saves the standby setting to the drive itself (survives unplug / reboot)
- Simple menu-driven interface, single Python file

## Requirements

- Windows 10 / 11
- Python 3.8+ in `PATH`
- Administrator privileges (the script requests UAC automatically)
- A connected Seagate Backup Plus Hub

## Files

| File | Description |
| --- | --- |
| `BackupPlusHub-Tool.cmd` | Double-click launcher (finds Python, starts the tool) |
| `BackupPlusHub-Tool.py`  | Main tool (detection, SCSI commands, menu) |

## Usage

1. Double-click `BackupPlusHub-Tool.cmd`.
2. Approve the UAC prompt.
3. Pick an option from the menu:

```
  1) Turn LED on
  2) Turn LED off
  3) Change sleep timer
  0) Exit
```

The tool prints the detected device, the current LED state and the current sleep timer
before showing the menu.

## How it works (brief)

The tool opens `\\.\PhysicalDriveN` and sends SCSI commands with
`IOCTL_SCSI_PASS_THROUGH_DIRECT`:

- **LED**: vendor command `F9` (set) / `FA` (read), 4 bytes of data
  (`02` = on, `01` = off).
- **Sleep timer**: `MODE SENSE(6)` / `MODE SELECT(6)`, mode page `0x1A` (Power Condition).
  The standby timer is stored in 100 ms units; `SP=1` saves the setting to the drive.

Only the settings above are touched — the tool never reads or writes your files.

## Notes / Limitations

- The LED state is a runtime setting: after the drive sleeps or is re-plugged, the LED may
  fall back to its default behaviour. The sleep timer itself is persisted on the drive.
- If Windows' power plan has a short "Turn off hard disk after" timeout, Windows may put
  the drive to standby before the timer set by this tool. Check the power plan if the drive
  spins down earlier than expected.
- `LED: Unknown (0x00)` means the drive reported a non-static LED state (e.g. during an
  animation/transition); it is not an error.
- Tested on a `Seagate Backup+ Hub BK` (firmware `D785`). Other models may behave differently.

## Disclaimer

Unofficial project, not affiliated with Seagate. Use it at your own risk. It sends
vendor-specific SCSI commands to the selected disk, so make sure the right drive is
selected before applying changes.
