---
name: cam
description: Turn a text-to-cad part into a machine program (CAM). Use when the user wants G-code for a laser cutter or plasma table from a DXF or a CAD part they made. CNC milling, turning and multi-axis toolpaths are not supported yet.
tools: text_to_cam, file_write, file_read, apply_skill
---

# CAM: from CAD output to a machine program

The `text_to_cam` tool writes the program and a preview. It never connects to a
machine. The user loads the program on the machine themselves.

## What is supported

- `cut2d`: 2D laser or plasma cutting from a DXF. It compensates for the kerf,
  cuts holes before the outer profile, adds lead-ins on the scrap side, and
  checks the bed size. Controllers: `grbl`, `linuxcnc`, `fanuc` and `generic`.
- Not yet supported: CNC milling (2.5D or 3-axis), turning, and 4- or 5-axis
  work. Say so plainly. Do not write milling G-code by hand as a substitute.

## Steps

1. **Get the geometry as a DXF at 1:1, in mm.** If the user only has a
   text-to-cad model, apply the `dxf` skill and export the cutting profile:
   every closed loop is either a part outline or a hole. Put marking or scoring
   lines on their own layer, for example `ENGRAVE`.
2. **Check that it can be made.** Apply `dfm` and read its sheet-metal
   reference. The usual problems are holes smaller than the sheet thickness,
   slots narrower than the kerf, and inside corners tighter than half the kerf.
3. **Get the machine profile from the user.** Ask for anything you do not
   know. Never invent feeds, power, pierce delay or kerf: a wrong value can
   start a fire, damage the machine or ruin the material.
   - machine: `controller`, `process` (`laser` or `plasma`), `bed` [x, y] in
     mm. Laser: `max_power` (GRBL `$30`, often 1000) and `laser_mode`
     (`dynamic` M4 is the default, GRBL needs `$32=1`). Plasma with a Z axis:
     `z_axis: true`, `safe_height`, `pierce_height`, `cut_height`. Plasma
     controllers that set heights themselves (for example THC or QtPlasmaC):
     `z_axis: false`, plus `torch_on` such as `"M3 $0 S1"` if their docs say so.
   - job, which depends on the material and thickness: `kerf` (mm), `feed`
     (mm/min), `power` (laser S value), `pierce_delay` (s), `lead_in` (mm,
     usually 0 to 1 for laser and 3 to 5 for plasma), `overcut` (mm), `origin`
     (`lower_left` puts the part's lower-left corner at work zero).
   - Save the machine profile with `file_write` as
     `cam/machines/<name>.json` and pass it with `machine_file` next time.
4. **Generate the program.** Call `text_to_cam` with `action="cut2d"`, the
   `input` DXF, `machine` or `machine_file`, and `job`. Use `layers` to cut only
   some layers and `engrave_layers` for lines that are cut on the line without
   compensation.
5. **Review it with the user.** Show the `.preview.svg`. Cut paths are red for
   outlines and orange for holes, lead-ins are blue, rapids are dashed and the
   numbers give the cut order. Read every warning out to the user: open paths,
   skipped holes, shortened lead-ins and offsets that cross themselves.
6. **Hand it over** with this reminder: set work zero, do a dry run with the
   laser or torch off, and test on scrap first.
