---
name: cam
description: Turn a text-to-cad part into a machine program (CAM). Use when the user wants G-code for a laser cutter or plasma table (from a DXF), or for a 3-axis or 3+2 indexed (4th-axis / trunnion) CNC mill or router (from a STEP part). Turning and simultaneous 5-axis toolpaths are not supported yet.
tools: text_to_cam, cam_mill, file_write, file_read, apply_skill
---

# CAM: from CAD output to a machine program

The tools write the program and a preview. They never connect to a machine.
The user loads the program on the machine themselves.

## What is supported

- `text_to_cam` with `action="cut2d"`: 2D laser or plasma cutting from a DXF.
  It compensates for the kerf, cuts holes before the outer profile, adds
  lead-ins on the scrap side, and checks the bed size. Controllers: `grbl`,
  `linuxcnc`, `fanuc` and `generic`.
- `cam_mill`: 3-axis milling of a STEP part with FreeCAD's CAM workbench.
  FreeCAD is downloaded on first use, which is several hundred MB, so tell the
  user before the first run. Operations are facing, adaptive roughing,
  drilling, adaptive finishing, an outline profile and 3D surfacing with a
  ball end mill. Controllers: `grbl`, `linuxcnc`, `fanuc`, `mach3`, `mach4`,
  `centroid`, `uccnc` and `heidenhain`.
- `cam_mill` with `job.setups`: 3+2 indexed milling on a 4th axis (`A` or
  `B`) or a trunnion (`AC` or `BC`). Each setup is machined as a 3-axis job
  with the part turned, and gets its own program.
- Not yet supported: turning and simultaneous 4/5-axis work (the tool moving
  while the axes turn). Say so plainly. Do not write G-code by hand as a
  substitute.

## Laser and plasma steps

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

## Milling steps

1. **Get the part as a STEP file**, in mm, oriented so that it is machined
   from the top (+Z). If it was modelled lying another way, rotate it in the
   text-to-cad model first. One setup machines the top side only: features on
   the sides or the bottom need another setup, so tell the user which ones.
2. **Check that it can be made.** Apply `dfm` and read its CNC reference:
   look at inside corner radii against the tool radius, deep narrow pockets
   against the flute length, and thin walls.
3. **Get the machine and tools from the user.** Never invent spindle speeds,
   feeds or depths of cut: a wrong value breaks tools or ruins the part.
   - machine: `controller`, `travel` [x, y, z], `max_rpm`, `max_feed`, and
     `tool_change: manual` when there is no tool changer (always the case
     with GRBL). Save it as `cam/machines/<name>.json`.
   - stock: `xy` (extra material on each side), `top` (above the part; with
     more than 0 a facing pass is added) and `bottom` (below the part). With
     `bottom: 0` the cuts stop `skin` mm (0.3 by default) above the bottom so
     the part stays attached.
   - tools: for each tool its number, `type`, `diameter`, `rpm`, `feed`,
     `plunge`, `stepdown` (not for drills) and ideally `flute_length`. If the
     user only knows the material and the tool, they can take the values from
     the tool maker's chart; do not make them up.
4. **Run `cam_mill`.** Leave `operations` out to get the standard plan: face,
   rough with the largest end mill, drill with each drill (only holes of that
   exact diameter), finish with the smallest end mill, then surface with any
   ball end mill. Give `operations` to change the order, tools, `stepover`
   (percent of the tool diameter) or `stepdown`.
5. **Review it with the user.** Show the `.preview.svg` (top and front views,
   one colour per operation) and the `.setup.md` sheet. Read out every
   warning, especially a tool cutting deeper than its flutes and cuts below
   the stock. For GRBL there is one file per tool change, run in order.
6. **Hand it over** with this reminder: open the `.FCStd` project in FreeCAD
   and run the CAM simulator (or simulate the G-code in CAMotics), set work
   zero at the stock's front-left corner with Z0 on the stock top, set the
   tool length after every tool change, and dry run above the part first.

## 3+2 indexed steps

1. **Find out which rotary axes the machine has.** Ask; never assume.
   - `machine.rotary.axes`: `A` (4th axis along X), `B` (along Y), `AC` or `BC`
     (trunnion: a C table on a tilting A or B axis).
   - `limits` such as `{"A": [-120, 120]}` for each axis that cannot turn freely.
   - `signs` such as `{"A": -1}` if the axis turns the other way from the
     right-hand rule turning the part. If the user does not know, say the
     first run must check it with a slow test move.
   - `center_height` (trunnion only): from the C table top to the tilt axis.
   - `retract`: the line that lifts the tool before turning, `G53 G0 Z0` by
     default. It must lift the tool clear of the swinging stock.
   - `mode`: `table` (default) writes `G0 A..` moves. `tilted_plane` writes a
     Fanuc `G68.2` plane, for Fanuc controls with that option or head-tilting
     machines.
   - Standard GRBL has only 3 axes; the A/B/C moves need grblHAL.
2. **Plan the setups with the user.** Each setup is a direction the tool comes
   from, in the part's own frame: `+Z` is the top, `+Y` the back, `-Z` the
   bottom, `+X` the right end, or a vector such as `[0, 1, 1]`. On an A axis
   only directions without an X part are reachable (B: without a Y part). You
   can also give the angles directly (`{"A": 90}`). Give per-setup
   `operations` when a side only needs, for example, drilling.
3. **How the part is held.** Work zero is the rotary centre.
   - A or B axis: the part is centred on the axis. `job.hold` is the stub
     length in the chuck; X0 (B: Y0) is the jaw face and nothing is cut beyond
     it. Each setup cuts down to the axis (`floor` 0) by default, so opposite
     setups meet in the middle.
   - Trunnion: the part sits centred on the C table and nothing is cut within
     `skin` of the table.
   - `job.offset` [x, y, z] moves the part off the centre when it is not
     centred.
4. **Run `cam_mill`** with `machine.rotary` and `job.setups`. It writes
   `<name>_S1.nc`, `<name>_S2.nc` … (GRBL: one file per tool per setup), one
   preview and one `.FCStd` project per setup, and one setup sheet.
5. **Review it with the user.** Read out the swing radius: before every
   rotation the tool tip must be higher than that above the rotary centre.
   Read the other warnings too, especially flute length on side setups.
6. **Hand it over** with this reminder: set work zero on the rotary centre,
   check the rotary direction with a slow test move, simulate each setup's
   `.FCStd` project, and dry run each setup with the spindle off.
