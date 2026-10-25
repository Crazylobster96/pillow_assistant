"""Project lineage: which project inherited whose outputs.

Each project records the projects it built on in ``project.json``
(``inherited``: id, name, copied files, time; see ``ProjectStore.inherit``).
This module turns those records into a graph and renders it as a text tree
(for the Agent) or an SVG / HTML page (for the projects panel and browser).
Pure functions apart from ``build_graph`` reading the store.
"""

from __future__ import annotations

import html
from typing import Any, Optional

from pillow_assistant.core.i18n import t

Graph = dict[str, Any]  # {"nodes": {id: node}, "edges": [edge]}


def build_graph(store: Any) -> Graph:
    """Nodes for every project (plus deleted parents still named in records), edges parent -> child."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for p in store.list():
        nodes[p.id] = {"id": p.id, "name": p.name, "deleted": False, "updated_at": p.updated_at}
    for p in store.list():
        for entry in getattr(p, "inherited", None) or []:
            pid = str(entry.get("id") or "")
            if not pid or pid == p.id:
                continue
            if pid not in nodes:
                nodes[pid] = {"id": pid, "name": entry.get("name") or pid, "deleted": True, "updated_at": 0.0}
            edges.append({"from": pid, "to": p.id, "files": len(entry.get("files") or []),
                          "at": entry.get("at") or 0.0})
    return {"nodes": nodes, "edges": edges}


def component(graph: Graph, focus: Optional[str]) -> Graph:
    """The connected part of the graph around ``focus`` (the whole graph's linked projects when None)."""
    linked = {e["from"] for e in graph["edges"]} | {e["to"] for e in graph["edges"]}
    if focus is None:
        keep = linked
    else:
        keep, todo = {focus}, [focus]
        while todo:
            cur = todo.pop()
            for e in graph["edges"]:
                for a, b in ((e["from"], e["to"]), (e["to"], e["from"])):
                    if a == cur and b not in keep:
                        keep.add(b)
                        todo.append(b)
    return {"nodes": {k: v for k, v in graph["nodes"].items() if k in keep},
            "edges": [e for e in graph["edges"] if e["from"] in keep and e["to"] in keep]}


def parents(graph: Graph, node_id: str) -> list[dict]:
    return [graph["nodes"][e["from"]] for e in graph["edges"] if e["to"] == node_id and e["from"] in graph["nodes"]]


def children(graph: Graph, node_id: str) -> list[dict]:
    return [graph["nodes"][e["to"]] for e in graph["edges"] if e["from"] == node_id and e["to"] in graph["nodes"]]


def _depths(graph: Graph) -> dict[str, int]:
    """Longest distance from a root, so every parent sits left of its children (cycles cut)."""
    depth = {n: 0 for n in graph["nodes"]}
    for _ in range(len(depth)):
        changed = False
        for e in graph["edges"]:
            if e["from"] in depth and e["to"] in depth and depth[e["to"]] < depth[e["from"]] + 1 <= len(depth):
                depth[e["to"]] = depth[e["from"]] + 1
                changed = True
        if not changed:
            break
    return depth


def _label(node: dict) -> str:
    return node["name"] + (t("lineage.deleted") if node.get("deleted") else "")


def render_text(graph: Graph, focus: Optional[str] = None) -> str:
    """An indented tree from each root; a project reached twice is marked instead of repeated."""
    if not graph["edges"]:
        return t("lineage.none")
    files = {(e["from"], e["to"]): e["files"] for e in graph["edges"]}
    kids: dict[str, list[str]] = {}
    for e in graph["edges"]:
        kids.setdefault(e["from"], []).append(e["to"])
    has_parent = {e["to"] for e in graph["edges"]}
    depth = _depths(graph)
    roots = [n for n in graph["nodes"] if n not in has_parent] or [min(graph["nodes"], key=lambda n: depth[n])]
    seen: set[str] = set()
    lines: list[str] = []

    def walk(node_id: str, prefix: str, parent: Optional[str], last: bool) -> None:
        node = graph["nodes"][node_id]
        mark = " ◀" if node_id == focus else ""
        via = f" ← {t('lineage.files', n=files[(parent, node_id)])}" if parent else ""
        branch = "" if parent is None else ("└─ " if last else "├─ ")
        if node_id in seen:
            lines.append(f"{prefix}{branch}{_label(node)}{via} {t('lineage.seen_above')}")
            return
        seen.add(node_id)
        lines.append(f"{prefix}{branch}{_label(node)}  (id={node_id}){via}{mark}")
        child_prefix = prefix if parent is None else prefix + ("   " if last else "│  ")
        ordered = sorted(kids.get(node_id, []), key=lambda k: graph["nodes"][k]["name"])
        for i, child in enumerate(ordered):
            walk(child, child_prefix, node_id, i == len(ordered) - 1)

    for root in sorted(roots, key=lambda r: graph["nodes"][r]["name"]):
        walk(root, "", None, True)
    return "\n".join(lines)


_W, _H, _GAP_X, _GAP_Y, _PAD = 190, 50, 90, 24, 24


def render_svg(graph: Graph, focus: Optional[str] = None) -> str:
    """Left-to-right layered drawing: parents left, children right, arrows labelled with copied file counts."""
    nodes = graph["nodes"]
    if not nodes:
        return ""
    depth = _depths(graph)
    columns: dict[int, list[str]] = {}
    for node_id in sorted(nodes, key=lambda n: (nodes[n]["name"], n)):
        columns.setdefault(depth[node_id], []).append(node_id)
    pos: dict[str, tuple[int, int]] = {}
    for col, ids in columns.items():
        for row, node_id in enumerate(ids):
            pos[node_id] = (_PAD + col * (_W + _GAP_X), _PAD + row * (_H + _GAP_Y))
    width = _PAD * 2 + (max(columns) + 1) * _W + max(columns) * _GAP_X
    height = _PAD * 2 + max(len(ids) for ids in columns.values()) * (_H + _GAP_Y) - _GAP_Y

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" font-family="Microsoft YaHei, PingFang SC, sans-serif">',
           '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
           'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#7B8AA0"/></marker></defs>',
           f'<rect width="{width}" height="{height}" fill="#F7F9FC"/>']
    for e in graph["edges"]:
        if e["from"] not in pos or e["to"] not in pos:
            continue
        (x1, y1), (x2, y2) = pos[e["from"]], pos[e["to"]]
        sx, sy, ex, ey = x1 + _W, y1 + _H / 2, x2, y2 + _H / 2
        if ex <= sx:  # same or earlier column (cycle): route below
            sx, sy, ex, ey = x1 + _W / 2, y1 + _H, x2 + _W / 2, y2 + _H
            path = f"M{sx},{sy} C{sx},{sy + 40} {ex},{ey + 40} {ex},{ey}"
        else:
            mid = (sx + ex) / 2
            path = f"M{sx},{sy} C{mid},{sy} {mid},{ey} {ex - 2},{ey}"
        out.append(f'<path d="{path}" fill="none" stroke="#7B8AA0" stroke-width="1.6" marker-end="url(#arrow)"/>')
        label = html.escape(t("lineage.files", n=e["files"]))
        # Next to the arrowhead: edges fanning out of one parent stay readable.
        lx, ly, anchor = (ex - 8, ey - 7, "end") if ex > sx else ((sx + ex) / 2, max(sy, ey) + 34, "middle")
        out.append(f'<text x="{lx}" y="{ly}" font-size="11" fill="#667282" '
                   f'text-anchor="{anchor}">{label}</text>')
    for node_id, (x, y) in pos.items():
        node = nodes[node_id]
        is_focus = node_id == focus
        fill = "#4A7CC8" if is_focus else ("#EEF1F5" if node.get("deleted") else "#FFFFFF")
        stroke = "#37699E" if is_focus else "#B8C2CF"
        dash = ' stroke-dasharray="5,4"' if node.get("deleted") else ""
        color = "#FFFFFF" if is_focus else ("#8A94A0" if node.get("deleted") else "#18202A")
        name = _label(node)
        if len(name) > 16:
            name = name[:15] + "…"
        out.append(f'<g><title>{html.escape(node["name"])} (id={html.escape(node_id)})</title>'
                   f'<rect x="{x}" y="{y}" width="{_W}" height="{_H}" rx="10" fill="{fill}" stroke="{stroke}"'
                   f' stroke-width="1.4"{dash}/>'
                   f'<text x="{x + _W / 2}" y="{y + 22}" font-size="14" font-weight="bold" fill="{color}" '
                   f'text-anchor="middle">{html.escape(name)}</text>'
                   f'<text x="{x + _W / 2}" y="{y + 39}" font-size="10" fill="{color}" opacity="0.75" '
                   f'text-anchor="middle">{html.escape(node_id)}</text></g>')
    out.append("</svg>")
    return "\n".join(out)


def render_html(graph: Graph, focus: Optional[str] = None) -> str:
    title = html.escape(t("lineage.title"))
    body = render_svg(graph, focus) or f"<p>{html.escape(t('lineage.none'))}</p>"
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>'
            '<style>body{margin:0;padding:20px;background:#F7F9FC;color:#18202A;'
            'font-family:"Microsoft YaHei","PingFang SC",sans-serif}h1{font-size:18px}'
            f'pre{{font-size:13px}}</style></head><body><h1>{title}</h1>{body}'
            f'<pre>{html.escape(render_text(graph, focus))}</pre></body></html>')
