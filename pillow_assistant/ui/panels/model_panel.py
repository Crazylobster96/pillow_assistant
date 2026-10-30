"""3D model panel: STL / STEP shown in a window the user can rotate and zoom.

Pillow has no CAD application of its own, so the mesh is drawn by a small,
self-contained WebGL page (no external scripts, works offline) in Qt WebEngine:
drag to rotate, wheel to zoom, buttons for the standard views. STEP has no mesh;
the panel uses an STL of the same part when one exists (beside it, in ../STL, or
the one Pillow's reviewer made in .pillow/review). Without one, the CAD edition
tessellates the STEP in the background (cadgen runtime); otherwise FreeCAD is offered.
"""

from __future__ import annotations

import base64
import hashlib
import subprocess
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.panels.base_panel import FilePanel

try:  # PySide6-Addons
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView

    HAVE_WEB = True
except ImportError:  # pragma: no cover
    HAVE_WEB = False

MAX_STL = 80 * 1024 * 1024  # larger meshes are left to FreeCAD


def mesh_for(path: Path) -> Optional[Path]:
    """The STL to draw for ``path``: itself, or an STL of the same part for a STEP."""
    if path.suffix.lower() == ".stl":
        return path
    stem = path.stem
    candidates = [path.with_suffix(".stl"), path.parent.parent / "STL" / f"{stem}.stl"]
    for parent in [path.parent, *path.parents]:
        review = parent / ".pillow" / "review" / f"{stem}.stl"
        candidates.append(review)
        if (parent / ".pillow").is_dir():
            break
    found = next((c for c in candidates if c.is_file()), None)
    if found is not None:
        return found
    made = converted_stl(path)  # one this panel tessellated earlier, if the STEP has not changed since
    try:
        return made if made.stat().st_mtime >= path.stat().st_mtime else None
    except OSError:
        return None


def converted_stl(path: Path) -> Path:
    """Where the panel keeps the STL it makes of a STEP: the project's review folder, else a cache."""
    home = Path.home()
    for parent in path.parents:
        if parent == home:
            break  # ~/.pillow is Pillow's own data, not a project's
        if (parent / ".pillow").is_dir():
            return parent / ".pillow" / "review" / f"{path.stem}.stl"
    key = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    return Path.home() / ".pillow" / "cache" / "model_view" / f"{key}-{path.stem}.stl"


def can_tessellate() -> bool:
    """Whether a STEP can be turned into a mesh here (the CAD edition's cadgen runtime)."""
    try:
        from pillow_assistant.capabilities import text_to_cad
        from pillow_assistant.core import cad_review  # noqa: F401
    except ImportError:
        return False
    return text_to_cad.find_uv() is not None


class _Done(QObject):
    done = Signal(bool)


def viewer_html(stl_bytes: bytes, title: str) -> str:
    """A self-contained page that draws the STL with WebGL and lets the user turn it."""
    data = base64.b64encode(stl_bytes).decode("ascii")
    return VIEWER.replace("__TITLE__", title.replace("<", "&lt;")).replace("__DATA__", data)


VIEWER = r"""<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>html,body{margin:0;height:100%;background:#f4f6f8;font:13px "Microsoft YaHei",sans-serif;overflow:hidden}
canvas{display:block;width:100%;height:100%;cursor:grab}#bar{position:absolute;left:10px;top:10px;display:flex;gap:6px}
#bar button{border:1px solid #c9d0d8;background:#fff;border-radius:6px;padding:4px 10px;cursor:pointer}
#hint{position:absolute;left:10px;bottom:8px;color:#6b7684;font-size:12px}#size{position:absolute;right:10px;bottom:8px;color:#6b7684;font-size:12px}</style></head>
<body><canvas id="c"></canvas><div id="bar"><button data-v="iso">等轴测</button><button data-v="front">正视</button>
<button data-v="top">俯视</button><button data-v="right">右视</button></div><div id="hint">拖动旋转 · 滚轮缩放 · 双击复位</div><div id="size"></div>
<script>
const raw=atob("__DATA__"),buf=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)buf[i]=raw.charCodeAt(i);
function parse(b){const dv=new DataView(b.buffer);if(b.length>=84){const n=dv.getUint32(80,true);if(84+n*50===b.length){const v=new Float32Array(n*9);
for(let i=0;i<n;i++)for(let k=0;k<9;k++)v[i*9+k]=dv.getFloat32(84+i*50+12+k*4,true);return v;}}
const txt=new TextDecoder().decode(b),m=txt.match(/vertex\s+[-+\deE.]+\s+[-+\deE.]+\s+[-+\deE.]+/g)||[],v=new Float32Array(m.length*3);
m.forEach((s,i)=>{const p=s.trim().split(/\s+/);v[i*3]=+p[1];v[i*3+1]=+p[2];v[i*3+2]=+p[3];});return v;}
const pos=parse(buf),nrm=new Float32Array(pos.length);let lo=[1e30,1e30,1e30],hi=[-1e30,-1e30,-1e30];
for(let i=0;i<pos.length;i+=9){const a=[pos[i],pos[i+1],pos[i+2]],b=[pos[i+3],pos[i+4],pos[i+5]],c=[pos[i+6],pos[i+7],pos[i+8]];
const u=[b[0]-a[0],b[1]-a[1],b[2]-a[2]],w=[c[0]-a[0],c[1]-a[1],c[2]-a[2]];let n=[u[1]*w[2]-u[2]*w[1],u[2]*w[0]-u[0]*w[2],u[0]*w[1]-u[1]*w[0]];
const l=Math.hypot(...n)||1;n=n.map(x=>x/l);for(let k=0;k<3;k++){nrm.set(n,i+k*3);for(let j=0;j<3;j++){const x=pos[i+k*3+j];lo[j]=Math.min(lo[j],x);hi[j]=Math.max(hi[j],x);}}}
const ctr=lo.map((x,j)=>(x+hi[j])/2),rad=Math.hypot(hi[0]-lo[0],hi[1]-lo[1],hi[2]-lo[2])/2||1;
document.getElementById("size").textContent=`${(hi[0]-lo[0]).toFixed(1)} × ${(hi[1]-lo[1]).toFixed(1)} × ${(hi[2]-lo[2]).toFixed(1)} mm · ${pos.length/9} 面`;
const cv=document.getElementById("c"),gl=cv.getContext("webgl",{antialias:true});
const vs=`attribute vec3 p;attribute vec3 n;uniform mat4 m;uniform mat3 r;varying vec3 vn;void main(){vn=r*n;gl_Position=m*vec4(p,1.0);}`;
const fs=`precision mediump float;varying vec3 vn;void main(){vec3 n=normalize(vn);float d=abs(dot(n,normalize(vec3(0.4,0.6,0.7))));
gl_FragColor=vec4(vec3(0.42,0.52,0.64)*(0.35+0.65*d)+0.08,1.0);}`;
function sh(t,s){const o=gl.createShader(t);gl.shaderSource(o,s);gl.compileShader(o);return o;}
const pr=gl.createProgram();gl.attachShader(pr,sh(gl.VERTEX_SHADER,vs));gl.attachShader(pr,sh(gl.FRAGMENT_SHADER,fs));gl.linkProgram(pr);gl.useProgram(pr);
function attr(name,data){const b=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,b);gl.bufferData(gl.ARRAY_BUFFER,data,gl.STATIC_DRAW);
const l=gl.getAttribLocation(pr,name);gl.enableVertexAttribArray(l);gl.vertexAttribPointer(l,3,gl.FLOAT,false,0,0);}
attr("p",pos);attr("n",nrm);gl.enable(gl.DEPTH_TEST);
const views={iso:[-0.62,0.55],front:[0,0],top:[0,1.5707],right:[-1.5707,0]};let yaw=views.iso[0],pitch=views.iso[1],zoom=1;
function draw(){const W=cv.clientWidth*devicePixelRatio,H=cv.clientHeight*devicePixelRatio;if(cv.width!==W||cv.height!==H){cv.width=W;cv.height=H;}
gl.viewport(0,0,W,H);gl.clearColor(0.957,0.965,0.973,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
const cy=Math.cos(yaw),sy=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch);
// view rows (model is z-up): screen right X, screen up Y, toward the viewer D
const X=[cy,-sy,0],Y=[sp*sy,sp*cy,cp],D=[-cp*sy,-cp*cy,sp];
const s=zoom/rad,a=W>H?H/W:1,b=W>H?1:W/H,rows=[X.map(v=>v*s*a),Y.map(v=>v*s*b),D.map(v=>-0.5*v*s)];
const M=new Float32Array(16);for(let r=0;r<3;r++){for(let c=0;c<3;c++)M[c*4+r]=rows[r][c];M[12+r]=-(rows[r][0]*ctr[0]+rows[r][1]*ctr[1]+rows[r][2]*ctr[2]);}M[15]=1;
const N=new Float32Array(9);[X,Y,D].forEach((row,r)=>row.forEach((v,c)=>N[c*3+r]=v));
gl.uniformMatrix4fv(gl.getUniformLocation(pr,"m"),false,M);gl.uniformMatrix3fv(gl.getUniformLocation(pr,"r"),false,N);
gl.drawArrays(gl.TRIANGLES,0,pos.length/3);}
let drag=null;cv.onmousedown=e=>{drag=[e.clientX,e.clientY];cv.style.cursor="grabbing";};
window.onmouseup=()=>{drag=null;cv.style.cursor="grab";};
window.onmousemove=e=>{if(!drag)return;yaw-=(e.clientX-drag[0])*0.01;pitch=Math.max(-1.57,Math.min(1.57,pitch+(e.clientY-drag[1])*0.01));drag=[e.clientX,e.clientY];draw();};
cv.onwheel=e=>{e.preventDefault();zoom=Math.max(0.2,Math.min(20,zoom*(e.deltaY<0?1.12:1/1.12)));draw();};
cv.ondblclick=()=>{[yaw,pitch]=views.iso;zoom=1;draw();};
document.querySelectorAll("#bar button").forEach(b=>b.onclick=()=>{[yaw,pitch]=views[b.dataset.v];zoom=1;draw();});
window.onresize=draw;draw();
</script></body></html>"""


class ModelPanel(FilePanel):
    TITLE = t("panel.model")

    def build_preview(self, layout: QVBoxLayout) -> None:
        self._box = QWidget(self)
        self._box_layout = QVBoxLayout(self._box)
        self._box_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._box, 1)
        self._fill(self._box_layout, convert=True)

    def _fill(self, layout: QVBoxLayout, convert: bool) -> None:
        path = Path(self.paths[0])
        mesh = mesh_for(path)
        if mesh is None and convert and HAVE_WEB and path.suffix.lower() in {".step", ".stp"} and can_tessellate():
            layout.addWidget(QLabel(t("panel.model_converting"), self))
            layout.addStretch(1)
            self._convert(path)
            return
        if mesh is None or not HAVE_WEB or mesh.stat().st_size > MAX_STL:
            reason = t("panel.model_no_mesh") if mesh is None else (
                t("panel.web_unavailable") if not HAVE_WEB else t("panel.model_too_big"))
            layout.addWidget(QLabel(reason, self))
            self._freecad_button(layout, path)
            layout.addStretch(1)
            return
        page = self._page(mesh)
        row = QHBoxLayout()
        row.addStretch(1)
        self._freecad_button(row, path)
        layout.addLayout(row)
        self.web = QWebEngineView(self)
        self.web.settings().setAttribute(QWebEngineSettings.WebGLEnabled, True)
        self.web.page().setBackgroundColor(QColor("#F4F6F8"))
        self.web.load(QUrl.fromLocalFile(str(page)))
        layout.addWidget(self.web, 1)

    def initial_size(self) -> tuple[int, int]:
        return (760, 640)

    def _convert(self, path: Path) -> None:
        """Tessellate the STEP off the UI thread, then draw it (or say why not)."""
        from pillow_assistant.core import cad_review

        self._done = _Done(self)
        self._done.done.connect(self._converted)
        signal = self._done.done
        target = converted_stl(path)

        def work() -> None:
            try:
                ok = cad_review.tessellate(path, target)
            except Exception:  # noqa: BLE001 - no mesh is the FreeCAD fallback, not a crash
                ok = False
            try:
                signal.emit(ok)
            except RuntimeError:  # the window was closed meanwhile
                pass

        threading.Thread(target=work, name="pillow-step-mesh", daemon=True).start()

    def _converted(self, _ok: bool) -> None:
        layout = self._box_layout
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._fill(layout, convert=False)

    @staticmethod
    def _page(mesh: Path) -> Path:
        data = mesh.read_bytes()
        cache = Path.home() / ".pillow" / "cache" / "model_view"
        cache.mkdir(parents=True, exist_ok=True)
        page = cache / (hashlib.sha1(data).hexdigest()[:16] + ".html")
        if not page.is_file():
            page.write_text(viewer_html(data, mesh.name), "utf-8")
        return page

    def _freecad_button(self, layout, path: Path) -> None:
        try:
            from pillow_assistant.capabilities import cad_apps

            app = cad_apps.locate("freecad")
        except Exception:  # noqa: BLE001
            app = None
        if app is None:
            return
        gui = Path(app).with_name("FreeCAD.exe")
        exe = gui if gui.is_file() else Path(app)
        button = QPushButton(t("panel.model_open_freecad"), self)
        button.clicked.connect(lambda: subprocess.Popen([str(exe), str(path)]))  # noqa: S603
        layout.addWidget(button)
