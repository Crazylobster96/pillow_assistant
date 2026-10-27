---
name: excel
description: Change an Excel workbook the user gave you (.xlsx) and keep its formatting - reorder rows or blocks (for example put one branch's counties in a given order), find and replace text, set one font and size, renumber a 序号 column. Use for 表格 / Excel 修改, 排序, 调整顺序, 替换, 改字体, 重新编号. Not for analysis reports or charts.
tools: excel_inspect, excel_edit, ask_user
---

# Excel: do exactly what was asked, in one edit

The user wants specific changes to their own spreadsheet. Do those changes and
nothing else: no data-quality audit, no statistics, no report, no checks of
columns the request does not mention.

## Steps

1. **Look once.** Call `excel_inspect` on the attached file (its absolute path
   from the context). When the request is about an order of groups ("合肥的顺序调为
   瑶海、肥西…"), pass `group_by` with the column holding those names (here
   县支机构) and `within` for the enclosing group (`{"column": "中支机构",
   "value": "合肥中支"}`). The result shows the header row, the columns and the
   blocks with their rows. Do not write Python to probe the sheet.
2. **Map the request to operations.** One `excel_edit` call carries all of them,
   in the order the user listed:
   - order of groups → `reorder_blocks` (`column`, `order`, `within`). Names in
     `order` may be short: 瑶海 matches 瑶海支公司.
   - "A 改为 B" → `replace` (limit `columns` to where A lives when you know it).
   - "字体统一为宋体 10 号" → `set_font` with `name` 宋体 and `size` 10.
   - "重新编号" → `renumber` on the 序号 column; use `skip_if_blank` with a
     name column if blank spacer rows must not get a number.
   If the request is ambiguous (which column, which group), ask once with
   `ask_user` instead of guessing.
3. **Save beside the original.** Leave `output` out to get `<name>_修改.xlsx`
   next to the source. Only pass `overwrite: true` when the user asked to change
   the original file itself; the tool backs it up first.
4. **Report briefly.** Say which file was written and what each step changed,
   using the tool's report (blocks before and after, cells replaced, number
   range). If `checks` shows a problem, say so.

If the tool says the file is open in another program, ask the user to close
it in Excel and run the same `excel_edit` call again. If an operation is
refused (a value not found, a group split in several places), read the message,
fix the arguments and retry once; if it still fails, tell the user what is in
the way.

Only `.xlsx` / `.xlsm` are supported. For `.xls` or `.csv`, ask the user to
save the file as `.xlsx` first.
