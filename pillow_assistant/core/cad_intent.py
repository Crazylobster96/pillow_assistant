"""Does a chat message talk about CAD / concept-diagram work?

Pure keyword heuristics, no model call. The text-to-cad edition uses it to
route CAD requests into projects; the main edition uses it to suggest the
text-to-cad-anatomy edition. Keep this file identical on both branches (the
daily sync merges main into text-to-cad).
"""

from __future__ import annotations

import re

_CAD_PATTERNS = (
    # Chinese
    r"(?<!数据)(?<!数学)(?<!统计)(?<!业务)建模|三维模型|3\s*[dD]\s*模型|立体模型|零件|装配体|装配图|工程图|图纸|机械设计|钣金",
    r"概念图|爆炸图|剖视图|剖面图|轴测图|等轴测",
    r"数控|刀路|刀具路径|[gG]\s*代码|铣削|车削|3\s*[dD]\s*打印",
    # Names, formats and English terms (ASCII word boundaries)
    r"(?<![a-z])(?:cad|cam|cnc|cadgen|freecad|autocad|solidworks|fusion\s*360|openscad|cadquery|build123d)(?![a-z])",
    r"(?<![a-z])(?:step|stp|stl|dxf|3mf|iges|igs|glb)\s*(?:文件|格式|模型|file|format|model)",
    r"\.(?:step|stp|stl|dxf|3mf|iges|igs)(?![a-z])",
    r"(?<![a-z])(?:3d\s*model|3d\s*print|exploded\s*view|cross[-\s]?section|isometric|concept\s*(?:art|diagram|drawing|sketch)"
    r"|g-?code|toolpath|sheet\s*metal|mechanical\s*design|technical\s*drawing)",
)
_CAD_RE = re.compile("|".join(f"(?:{p})" for p in _CAD_PATTERNS), re.IGNORECASE)


def mentions_cad(prompt: str) -> bool:
    """True when the message is about CAD modelling, CAM or concept diagrams."""
    return bool(prompt) and _CAD_RE.search(prompt) is not None
