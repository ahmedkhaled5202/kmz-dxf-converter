# -*- coding: utf-8 -*-
"""
KMZ <-> DXF/DWG Converter - v2

Drag a file onto this script (or run it and drag files into the window):
  .kmz / .kml  ->  DXF   Folders -> layers, points -> block GIS_POINT (+ attributes + label),
                         lines / polygons -> LWPOLYLINE, KML colours -> entity colours
  .dxf / .dwg  ->  KMZ   Layers -> folders, layer/entity colours, text labels, block attributes,
                         arcs / circles / splines are flattened to polylines. DWG needs ODA File Converter.

Output is never overwritten:  name_v1.kmz, name_v2.kmz ...

Install:  pip install ezdxf pyproj        (nothing else is needed)

Command line (optional):
  python kmz_dxf_converter_v2.py file1.dxf file2.kmz [--epsg 32636] [--yes] [--no-fill] [--explode] [--no-labels]
    --epsg N     coordinate system of the DXF (DXF->KMZ) or of the output DXF (KMZ->DXF); 4326 = lon/lat
    --yes        never ask questions, use the best automatic choice
    --no-fill    polygons are drawn as outlines only in the KMZ
    --explode    DXF->KMZ: explode block references into their geometry instead of one point per block
    --no-labels  KMZ->DXF: do not add TEXT labels next to named points
"""
from __future__ import annotations

import argparse
import glob
import html as _html
import importlib.util
import math
import os
import re
import shutil
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from xml.sax.saxutils import escape as _xml_escape

# =============================================================== settings
VERSION = "2.0"
DXF_VERSION = "R2010"            # DXF written by KMZ->DXF (opens in AutoCAD / GstarCAD 2010+)
POINT_BLOCK = "GIS_POINT"
DEFAULT_LAYER = "KML_IMPORT"
POINT_SIZE_M = 1.0               # size of the point block (metres, or metres converted to degrees)
LABEL_HEIGHT_M = 1.5             # TEXT height for point labels
DEFAULT_RGB = (255, 255, 255)
MAX_EXPLODE_DEPTH = 4
# extra places to look for ODA File Converter (the env var ODA_CONVERTER_PATH is tried first)
ODA_EXTRA_PATHS = [r"D:\Program Files\cad\ODAFileConverter.exe"]

INTERACTIVE = True


class ConvError(Exception):
    """A problem we can explain to the user in one or two lines."""


# =============================================================== small helpers
def need(*pairs):
    """need(("ezdxf","ezdxf"), ("pyproj","pyproj")) -> ConvError if a package is missing."""
    missing = [pip for mod, pip in pairs if importlib.util.find_spec(mod) is None]
    if missing:
        raise ConvError("Missing package(s). Install with:\n      pip install " + " ".join(missing))


def ask(prompt, default=""):
    if not INTERACTIVE:
        return default
    try:
        return input(prompt).strip()
    except EOFError:
        return default


def parse_paths(raw):
    """Turn what the user dragged/pasted into a list of existing file paths."""
    raw = raw.strip()
    if raw.startswith("&"):                      # PowerShell drag & drop
        raw = raw[1:].strip()
    whole = raw.strip("\"'").strip()
    if whole and os.path.isfile(whole):
        return [whole]
    out = []
    for a, b, c in re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', raw):
        out.append(a or b or c)
    return out


_UPLUS_RE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")
_CTRL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]")
_CHARREF_RE = re.compile(r"&#([xX][0-9a-fA-F]+|[0-9]+);")
_MTEXT_CODE_RE =re.compile(r"\\[fFcCCHhQqWwTtAa][^;]*;|\\[LlOoKk]")


def clean_cad_text(s):
    """Plain, single-line text from TEXT / MTEXT / layer names (unicode escapes, %%c, MTEXT codes)."""
    if s is None:
        return ""
    s = _UPLUS_RE.sub(lambda m: chr(int(m.group(1), 16)), str(s))
    s = re.sub(r"%%[cC]", "\u2300", s)
    s = re.sub(r"%%[dD]", "\u00b0", s)
    s = re.sub(r"%%[pP]", "\u00b1", s)
    s = re.sub(r"\\S([^;]*);", r"\1", s)         # stacked text 1/2
    s = _MTEXT_CODE_RE.sub("", s)
    s = s.replace("\\P", " ").replace("\\~", " ").replace("\\\\", "\\")
    s = s.replace("{", "").replace("}", "")
    s = _CTRL_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def xml_text(s):
    return _xml_escape(_CTRL_RE.sub("", str(s)))


def xml_attr(s):
    return _xml_escape(_CTRL_RE.sub("", str(s)), {'"': "&quot;"})


def cdata(s):
    return "<![CDATA[" + _CTRL_RE.sub("", str(s)).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def html_to_text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()


def kml_color(rgb, alpha=255):
    r, g, b = rgb
    return f"{alpha:02x}{b:02x}{g:02x}{r:02x}"


def parse_kml_color(s):
    """'aabbggrr' -> (r, g, b) or None."""
    s = (s or "").strip().lstrip("#")
    if len(s) == 8 and re.fullmatch(r"[0-9a-fA-F]{8}", s):
        return int(s[6:8], 16), int(s[4:6], 16), int(s[2:4], 16)
    return None


def line_width_px(lw):
    if lw is None or lw < 0:
        return 2
    return min(max(round(lw / 20), 1), 6)


def utm_epsg(lon, lat):
    lon = ((lon + 180) % 360) - 180
    zone = min(max(int((lon + 180) // 6) + 1, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


def next_version_path(src, ext):
    """map.dxf -> map_v1.kmz, map_v2.kmz ... next to the source (home folder if it is read-only)."""
    p = Path(src)
    stem = re.sub(r"_v\d+$", "", p.stem)
    folder = p.parent if os.access(p.parent, os.W_OK) else Path.home()
    v = 1
    while (folder / f"{stem}_v{v}{ext}").exists():
        v += 1
    return folder / f"{stem}_v{v}{ext}"


def atomic_write(path, writer):
    """writer(tmp_path) then rename, so a crash never leaves a half-written output."""
    tmp = str(path) + ".part"
    try:
        writer(tmp)
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# =============================================================== feature model
@dataclass
class Feature:
    kind: str                    # point | text | line | polygon
    layer: str
    geom: list                   # list of parts, each a list of (x, y); polygon: outer ring first, then holes
    rgb: tuple | None = None
    width: int | None = None
    text: str = ""
    height: float = 0.0
    block: str = ""
    attrs: list = field(default_factory=list)
    name: str = ""
    desc: str = ""


def bbox_of(feats):
    xs = [x for f in feats for part in f.geom for x, _ in part]
    ys = [y for f in feats for part in f.geom for _, y in part]
    return min(xs), min(ys), max(xs), max(ys)


# =============================================================== coordinate systems
PRESETS = [
    ("2", "Egypt UTM zone 36N  (EPSG:32636)  Cairo, Giza, Suez, Sinai, Upper Egypt", 32636),
    ("3", "Egypt UTM zone 35N  (EPSG:32635)  Alexandria, Matrouh, Siwa", 32635),
    ("4", "Egypt UTM zone 37N  (EPSG:32637)  Red Sea coast, Halayeb", 32637),
    ("5", "Egypt Red Belt      (EPSG:22992)", 22992),
]


def make_transformer(src, dst):
    """src/dst are EPSG ints; None means lon/lat WGS84. Returns (transform_fn | None, dst_is_projected)."""
    if src == dst:
        return None, dst is not None
    need(("pyproj", "pyproj"))
    from pyproj import CRS, Transformer
    try:
        s = CRS.from_epsg(src if src is not None else 4326)
        d = CRS.from_epsg(dst if dst is not None else 4326)
    except Exception:
        raise ConvError(f"Unknown EPSG code ({src if src is not None else 4326} -> {dst if dst is not None else 4326}).")
    tr = Transformer.from_crs(s, d, always_xy=True)
    return tr.transform, bool(d.is_projected)


def check_epsg(n):
    need(("pyproj", "pyproj"))
    from pyproj import CRS
    try:
        CRS.from_epsg(n)
    except Exception:
        raise ConvError(f"EPSG:{n} is not a valid code.")


def choose_crs(title, default_epsg, default_label):
    """Menu. Returns an EPSG int, or None for plain lon/lat. Enter picks the default."""
    if not INTERACTIVE:
        print(f"  {title} -> {default_label}")
        return default_epsg
    print(f"\n{title}")
    print(f"  Enter = {default_label}")
    print("  1     = lon/lat degrees (WGS84, no transform)")
    for key, label, _ in PRESETS:
        print(f"  {key}     = {label}")
    print("  l     = type an approximate lon/lat and let me pick the UTM zone")
    print("  or type any EPSG number")
    while True:
        a = ask("Your choice: ").lower()
        if a == "":
            return default_epsg
        if a == "1":
            return None
        for key, _, e in PRESETS:
            if a == key:
                return e
        if a == "l":
            try:
                lon = float(ask("  Approximate longitude (e.g. 31.2): ").replace(",", "."))
                lat = float(ask("  Approximate latitude  (e.g. 30.0): ").replace(",", "."))
                e = utm_epsg(lon, lat)
                print(f"  -> EPSG:{e}")
                return e
            except ValueError:
                print("  Numbers only.")
                continue
        if a.isdigit():
            try:
                check_epsg(int(a))
                return int(a)
            except ConvError as e:
                print(f"  {e}")
                continue
        print("  Not understood, try again.")


def to_lonlat(points, epsg):
    """[(x, y)] in `epsg` -> [(lon, lat)] or None if anything is invalid."""
    fn, _ = make_transformer(epsg, None)
    xs, ys = zip(*points)
    if fn:
        xs, ys = fn(list(xs), list(ys))
    out = list(zip(xs, ys))
    for lon, lat in out:
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            return None
    return out


def project_features(feats, src, dst, require_lonlat):
    """Transform every feature from src to dst in ONE bulk call. Features with bad coordinates are dropped."""
    fn, projected = make_transformer(src, dst)
    xs, ys = [], []
    for f in feats:
        for part in f.geom:
            for x, y in part:
                xs.append(x)
                ys.append(y)
    if fn and xs:
        xs, ys = fn(xs, ys)
    out, dropped, i = [], 0, 0
    for f in feats:
        new_geom, ok = [], True
        for part in f.geom:
            n = len(part)
            seg = list(zip(xs[i:i + n], ys[i:i + n]))
            i += n
            for x, y in seg:
                if not (math.isfinite(x) and math.isfinite(y)):
                    ok = False
                elif require_lonlat and not (-180 <= x <= 180 and -90 <= y <= 90):
                    ok = False
            new_geom.append(seg)
        if ok:
            f.geom = new_geom
            out.append(f)
        else:
            dropped += 1
    return out, dropped, projected


# =============================================================== DWG -> DXF (ODA File Converter)
def dwg_to_dxf(dwg_path):
    need(("ezdxf", "ezdxf"))
    import ezdxf
    from ezdxf.addons import odafc

    def set_path(p):
        try:
            ezdxf.options.set("odafc-addon", "win_exec_path", p)
            ezdxf.options.set("odafc-addon", "unix_exec_path", p)
        except Exception:
            pass
        for attr in ("win_exec_path", "unix_exec_path"):
            try:
                setattr(odafc, attr, p)
            except Exception:
                pass

    candidates = [os.environ.get("ODA_CONVERTER_PATH", "")] + ODA_EXTRA_PATHS
    for c in candidates:
        if c and os.path.isfile(c):
            set_path(c)
            break
    else:
        for pat in (r"C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe",
                    r"C:\Program Files (x86)\ODA\ODAFileConverter*\ODAFileConverter.exe",
                    r"D:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe"):
            hits = sorted(glob.glob(pat))
            if hits:
                set_path(hits[-1])
                break
    try:
        installed = odafc.is_installed()
    except Exception:
        installed = False
    if not installed:
        raise ConvError(
            "DWG needs the free ODA File Converter, which was not found.\n"
            "      Install it from https://www.opendesign.com/guestfiles/oda_file_converter\n"
            "      (or set the ODA_CONVERTER_PATH environment variable),\n"
            "      or save the drawing as DXF from your CAD program and use the DXF.")
    tmp = tempfile.mkdtemp(prefix="dwg2kmz_")
    dxf = os.path.join(tmp, Path(dwg_path).stem + ".dxf")
    print("  Converting DWG -> DXF with ODA File Converter ...")
    try:
        odafc.convert(dwg_path, dxf, version="R2018", audit=True, replace=True)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ConvError(f"DWG conversion failed: {e}")
    if not os.path.isfile(dxf):
        shutil.rmtree(tmp, ignore_errors=True)
        raise ConvError("DWG conversion produced no file.")
    return dxf, tmp


# =============================================================== DXF reading (ezdxf only)
def load_dxf(path):
    need(("ezdxf", "ezdxf"))
    import ezdxf
    try:
        return ezdxf.readfile(path)
    except Exception as first:
        try:
            from ezdxf import recover
            doc, _auditor = recover.readfile(path)
            print("  Note: the DXF had structural problems; it was repaired while reading.")
            return doc
        except Exception:
            raise ConvError(f"Cannot read this DXF ({first}).")


def read_layers(doc):
    from ezdxf.colors import aci2rgb
    out = {}
    for layer in doc.layers:
        try:
            name = clean_cad_text(layer.dxf.name) or "0"
            rgb = getattr(layer, "rgb", None)
            if rgb is None:
                try:
                    rgb = tuple(aci2rgb(abs(int(layer.dxf.get("color", 7)))))[:3]
                except Exception:
                    rgb = DEFAULT_RGB
            out[name] = {
                "rgb": tuple(rgb)[:3],
                "lineweight": layer.dxf.get("lineweight", -3),
                "linetype": str(layer.dxf.get("linetype", "Continuous")),
                "visible": not (layer.is_off() or layer.is_frozen()),
            }
        except Exception:
            continue
    return out


_PATH_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "ELLIPSE", "SPLINE"}


def _xy(v):
    return float(v[0]), float(v[1])


def _wcs(e, v):
    """OCS -> WCS for entities whose points are stored in OCS (TEXT, INSERT, POINT ...)."""
    try:
        ocs = e.ocs()
        if getattr(ocs, "transform", False):
            return ocs.to_wcs(v)
    except Exception:
        pass
    return v


def flatten_entity(e):
    """-> (points, closed). Curves are flattened with a tolerance of ~0.1% of the entity size."""
    from ezdxf import path as ezpath
    p = ezpath.make_path(e)
    try:
        cv = [_xy(v) for v in p.control_vertices()]
        xs, ys = [c[0] for c in cv], [c[1] for c in cv]
        diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        dist = max(diag * 0.001, 1e-9)
    except Exception:
        dist = 0.01
    pts = [_xy(v) for v in p.flattening(dist, segments=4)]
    try:
        v = p.is_closed
        closed = bool(v() if callable(v) else v)
    except Exception:
        closed = len(pts) > 2 and pts[0] == pts[-1]
    # drop consecutive duplicates
    clean = []
    for pt in pts:
        if not clean or pt != clean[-1]:
            clean.append(pt)
    return clean, closed


def extract_dxf(doc, explode):
    """Walk modelspace -> (features in drawing coordinates, layer styles, skipped types, errors)."""
    from ezdxf.colors import aci2rgb
    layers = read_layers(doc)
    feats, skipped, errors = [], Counter(), Counter()

    def layer_info(name):
        if name not in layers:
            layers[name] = {"rgb": DEFAULT_RGB, "lineweight": -3, "linetype": "Continuous", "visible": True}
        return layers[name]

    def entity_style(e, layer, parent):
        info = layer_info(layer)
        rgb = None
        try:
            if e.dxf.hasattr("true_color"):
                tc = int(e.dxf.true_color)
                rgb = ((tc >> 16) & 255, (tc >> 8) & 255, tc & 255)
            else:
                aci = int(e.dxf.get("color", 256))
                if aci == 256:
                    rgb = info["rgb"]
                elif aci == 0:
                    rgb = parent["rgb"] if parent else info["rgb"]
                else:
                    rgb = tuple(aci2rgb(abs(aci)))[:3]
        except Exception:
            rgb = info["rgb"]
        lw = e.dxf.get("lineweight", -1)
        if lw == -2 and parent:
            lw = parent["lw"]
        if lw in (-1, -2):
            lw = info["lineweight"]
        return rgb or info["rgb"], line_width_px(lw), lw

    def handle(e, parent, depth):
        t = e.dxftype()
        name = clean_cad_text(e.dxf.get("layer", "0")) or "0"
        if name == "0" and parent:
            name = parent["layer"]
        if name.lower() == "defpoints":
            return
        rgb, width, lw = entity_style(e, name, parent)
        ctx = {"layer": name, "rgb": rgb, "lw": lw}

        if t in _PATH_TYPES:
            pts, closed = flatten_entity(e)
            if len(pts) < 2:
                return
            if closed and len(pts) >= 3:
                ring = pts if pts[0] == pts[-1] else pts + [pts[0]]
                if len(ring) >= 4:
                    feats.append(Feature("polygon", name, [ring], rgb, width))
                    return
            feats.append(Feature("line", name, [pts], rgb, width))
        elif t == "POINT":
            feats.append(Feature("point", name, [[_xy(_wcs(e, e.dxf.location))]], rgb))
        elif t in ("TEXT", "MTEXT"):
            if t == "TEXT":
                raw = e.dxf.get("text", "")
                h = e.dxf.get("height", 0) or 0
                pos = e.dxf.insert
                if (e.dxf.get("halign", 0) or e.dxf.get("valign", 0)) and e.dxf.hasattr("align_point"):
                    pos = e.dxf.align_point
            else:
                try:
                    raw = e.plain_text()
                except Exception:
                    raw = e.text
                h = e.dxf.get("char_height", 0) or 0
                pos = e.dxf.insert
            txt = clean_cad_text(raw)
            if txt:
                feats.append(Feature("text", name, [[_xy(_wcs(e, pos))]], rgb, text=txt, height=float(h)))
        elif t == "INSERT":
            if explode and depth < MAX_EXPLODE_DEPTH:
                for sub in e.virtual_entities():
                    try:
                        handle(sub, ctx, depth + 1)
                    except Exception as ex:
                        errors[type(ex).__name__] += 1
            else:
                attrs = []
                for a in getattr(e, "attribs", []) or []:
                    tag = clean_cad_text(a.dxf.get("tag", ""))
                    if tag:
                        attrs.append((tag, clean_cad_text(a.dxf.get("text", ""))))
                bname = clean_cad_text(e.dxf.get("name", "")) or "BLOCK"
                feats.append(Feature("point", name, [[_xy(_wcs(e, e.dxf.insert))]], rgb,
                                     block=bname, attrs=attrs))
        else:
            skipped[t] += 1

    n = 0
    for e in doc.modelspace():
        n += 1
        try:
            handle(e, None, 0)
        except Exception as ex:
            errors[type(ex).__name__] += 1
    print(f"  Read {n} entities -> {len(feats)} features")
    return feats, layers, skipped, errors


# =============================================================== KML writing
_DOT_ICON = "http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png"


class Styles:
    def __init__(self, fill):
        self.fill = fill
        self.ids, self.xml = {}, []

    def get(self, kind, rgb, width=2, scale=0.8):
        key = (kind, rgb, width if kind in ("line", "poly") else 0, scale if kind == "text" else 0)
        if key in self.ids:
            return self.ids[key]
        sid = f"s{len(self.ids)}"
        self.ids[key] = sid
        col = kml_color(rgb)
        icon, label = "", "<LabelStyle><scale>0</scale></LabelStyle>"
        if kind == "text":
            icon = "<IconStyle><scale>0</scale></IconStyle>"
            label = f"<LabelStyle><color>{col}</color><scale>{scale}</scale></LabelStyle>"
        elif kind == "point":
            icon = (f"<IconStyle><color>{col}</color><scale>0.5</scale>"
                    f"<Icon><href>{_DOT_ICON}</href></Icon></IconStyle>")
        poly = (f"<PolyStyle><color>{kml_color(rgb, 0x4d)}</color><fill>{1 if self.fill else 0}</fill>"
                "<outline>1</outline></PolyStyle>")
        self.xml.append(f'<Style id="{sid}">{icon}{label}'
                        f"<LineStyle><color>{col}</color><width>{width}</width></LineStyle>{poly}</Style>")
        return sid


def _coords(seq):
    return " ".join(f"{x:.8f},{y:.8f},0" for x, y in seq)


def geom_xml(f):
    if f.kind in ("point", "text"):
        return f"<Point><coordinates>{_coords(f.geom[0][:1])}</coordinates></Point>"
    if f.kind == "line":
        return f"<LineString><coordinates>{_coords(f.geom[0])}</coordinates></LineString>"
    s = ("<Polygon><outerBoundaryIs><LinearRing><coordinates>"
         f"{_coords(f.geom[0])}</coordinates></LinearRing></outerBoundaryIs>")
    for ring in f.geom[1:]:
        s += f"<innerBoundaryIs><LinearRing><coordinates>{_coords(ring)}</coordinates></LinearRing></innerBoundaryIs>"
    return s + "</Polygon>"


def text_scale(h, med):
    if not h or not med:
        return 0.8
    return round(min(max(0.8 * h / med, 0.5), 1.6), 1)


def build_kml(feats, layers, doc_name, fill=True):
    styles = Styles(fill)
    heights = [f.height for f in feats if f.kind == "text" and f.height > 0]
    med = median(heights) if heights else None
    by_layer = defaultdict(list)
    for f in feats:
        by_layer[f.layer].append(f)
    folders = []
    for layer in sorted(by_layer, key=str.lower):
        info = layers.get(layer, {})
        vis = 1 if info.get("visible", True) else 0
        base_rgb = info.get("rgb", DEFAULT_RGB)
        ltype = info.get("linetype", "")
        marks = []
        for f in by_layer[layer]:
            rgb = f.rgb or base_rgb
            extra = ""
            if f.kind == "point" and f.block:
                sid = styles.get("point", rgb)
                name = f.block
                rows = "".join(f"<tr><td><b>{_html.escape(t)}</b></td><td>{_html.escape(v)}</td></tr>"
                               for t, v in f.attrs)
                body = (f"<b>Block:</b> {_html.escape(f.block)}<br><b>Layer:</b> {_html.escape(layer)}"
                        + (f'<br><table border="1" cellpadding="3">{rows}</table>' if rows else ""))
                desc = cdata(body)
                if f.attrs:
                    extra = "<ExtendedData>" + "".join(
                        f'<Data name="{xml_attr(t)}"><value>{xml_text(v)}</value></Data>' for t, v in f.attrs
                    ) + "</ExtendedData>"
            elif f.kind == "text":
                sid = styles.get("text", rgb, scale=text_scale(f.height, med))
                name = f.text
                desc = xml_text(f"Layer: {layer}")
            elif f.kind == "point":
                sid, name = styles.get("point", rgb), layer or "Unnamed"
                desc = xml_text(f"Layer: {layer}")
            else:
                sid = styles.get("poly" if f.kind == "polygon" else "line", rgb, f.width or 2)
                name = layer or "Unnamed"
                desc = xml_text(f"Layer: {layer}" + (f" | Linetype: {ltype}" if ltype else ""))
            marks.append(f"<Placemark><name>{xml_text(name)}</name><description>{desc}</description>{extra}"
                         f"<styleUrl>#{sid}</styleUrl>{geom_xml(f)}</Placemark>")
        folders.append(f"<Folder><name>{xml_text(layer or 'Unnamed')}</name><visibility>{vis}</visibility>"
                       + "".join(marks) + "</Folder>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f"<name>{xml_text(doc_name)}</name>" + "".join(styles.xml) + "".join(folders) + "</Document></kml>")


def verify_kmz(path):
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:
            raise ConvError("KMZ zip is corrupt.")
        ET.fromstring(z.read("doc.kml"))


# =============================================================== DXF / DWG -> KMZ
def guess_dxf_crs(b):
    """-> (suggested EPSG or None for lon/lat, human note)."""
    minx, miny, maxx, maxy = b
    if -180 <= minx and maxx <= 180 and -90 <= miny and maxy <= 90:
        return None, "The values look like lon/lat degrees."
    if 100000 <= minx and maxx <= 900000 and 0 <= miny and maxy <= 10_000_000:
        return 32636, "The values look like UTM metres."
    return 32636, "Warning: the values look like local coordinates - pick the correct system below."


def resolve_dxf_crs(b, opts):
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    guess, note = guess_dxf_crs(b)
    if opts.forced:
        pt = to_lonlat([(cx, cy)], opts.epsg)
        if pt is None:
            raise ConvError(f"EPSG:{opts.epsg or 4326} cannot place this drawing (values out of range).")
        return opts.epsg
    print(f"\n  Drawing extent: X {b[0]:.3f} -> {b[2]:.3f}   Y {b[1]:.3f} -> {b[3]:.3f}")
    print(f"  {note}")
    label = "lon/lat (WGS84)" if guess is None else f"EPSG:{guess} (Egypt UTM 36N)"
    while True:
        epsg = choose_crs("Coordinate system of the drawing?", guess, label)
        pt = to_lonlat([(cx, cy)], epsg)
        if pt is None:
            print("  The drawing cannot be placed on Earth with that system.")
            if not INTERACTIVE:
                raise ConvError("Cannot place the drawing; run again with --epsg <code>.")
            continue
        lon, lat = pt[0]
        print(f"  Drawing centre -> lat {lat:.5f}, lon {lon:.5f}")
        print(f"  Check it: https://www.google.com/maps?q={lat:.6f},{lon:.6f}")
        if not INTERACTIVE or ask("  Is this the right place? (Enter = yes / n = choose again): ").lower() != "n":
            return epsg


def dxf_to_kmz(path, opts):
    need(("ezdxf", "ezdxf"), ("pyproj", "pyproj"))
    tmp = None
    try:
        work = path
        if Path(path).suffix.lower() == ".dwg":
            work, tmp = dwg_to_dxf(path)
        doc = load_dxf(work)
        feats, layers, skipped, errors = extract_dxf(doc, opts.explode)
        if not feats:
            raise ConvError("Nothing convertible was found in the drawing's model space.")
        epsg = resolve_dxf_crs(bbox_of(feats), opts)
        feats, dropped, _ = project_features(feats, epsg, None, require_lonlat=True)
        if not feats:
            raise ConvError("All coordinates fell outside the valid lon/lat range - wrong coordinate system?")
        out = next_version_path(path, ".kmz")
        kml = build_kml(feats, layers, Path(path).stem, fill=opts.fill)

        def _write(tmp_path):
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("doc.kml", kml.encode("utf-8"))
        atomic_write(out, _write)
        try:
            verify_kmz(out)
        except Exception as e:
            print(f"  WARNING: the KMZ failed its self-check ({e}).")
        kinds = Counter(f.kind for f in feats)
        print(f"\n  DONE: {out}")
        print(f"  {len(feats)} features: " + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
              + f" | {len({f.layer for f in feats})} layers")
        if dropped:
            print(f"  {dropped} feature(s) dropped (coordinates out of range)")
        if skipped:
            print("  Not converted: " + ", ".join(f"{k} x{v}" for k, v in skipped.most_common(6)))
        if errors:
            print("  Entities that could not be read: " + ", ".join(f"{k} x{v}" for k, v in errors.items()))
        return out
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


# =============================================================== KML / KMZ reading
def local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def child_text(el, name):
    for c in el:
        if local(c.tag) == name:
            return (c.text or "").strip()
    return None


def read_kml_bytes(path):
    if Path(path).suffix.lower() == ".kmz":
        try:
            with zipfile.ZipFile(path) as z:
                kmls = [n for n in z.namelist() if n.lower().endswith(".kml")]
                if not kmls:
                    raise ConvError("The KMZ contains no KML file.")
                pick = next((n for n in kmls if n.lower().endswith("doc.kml")), kmls[0])
                return z.read(pick)
        except zipfile.BadZipFile:
            raise ConvError("This is not a valid KMZ (zip) file.")
    with open(path, "rb") as f:
        return f.read()


def _drop_bad_charref(m):
    try:
        n = int(m.group(1)[1:], 16) if m.group(1)[0] in "xX" else int(m.group(1))
    except ValueError:
        return ""
    ok = n in (9, 10, 13) or 0x20 <= n <= 0xD7FF or 0xE000 <= n <= 0xFFFD or 0x10000 <= n <= 0x10FFFF
    return m.group(0) if ok else ""


def parse_xml(raw):
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        pass
    text = raw.decode("utf-8", errors="replace")
    text = _CTRL_RE.sub("", text)
    text = _CHARREF_RE.sub(_drop_bad_charref, text)   # e.g. &#x1; is illegal in XML 1.0
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    try:
        return ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as e:
        raise ConvError(f"The KML is not valid XML ({e}).")


def parse_coords(text):
    text = re.sub(r"\s*,\s*", ",", text or "")
    pts = []
    for tok in text.split():
        p = tok.split(",")
        if len(p) >= 2:
            try:
                x, y = float(p[0]), float(p[1])
            except ValueError:
                continue
            if math.isfinite(x) and math.isfinite(y):
                pts.append((x, y))
    return pts


def parse_style(el):
    d = {}
    for ch in el:
        n = local(ch.tag)
        if n == "LineStyle":
            rgb = parse_kml_color(child_text(ch, "color"))
            if rgb:
                d["line"] = rgb
        elif n == "PolyStyle":
            rgb = parse_kml_color(child_text(ch, "color"))
            if rgb:
                d["poly"] = rgb
        elif n == "IconStyle":
            rgb = parse_kml_color(child_text(ch, "color"))
            if rgb:
                d["icon"] = rgb
            sc = child_text(ch, "scale")
            try:
                d["icon_scale"] = float(sc) if sc is not None else None
            except ValueError:
                pass
        elif n == "LabelStyle":
            rgb = parse_kml_color(child_text(ch, "color"))
            if rgb:
                d["label"] = rgb
    return d


def collect_styles(root):
    styles, maps = {}, {}
    for el in root.iter():
        n = local(el.tag)
        if n == "Style" and el.get("id"):
            styles[el.get("id")] = parse_style(el)
        elif n == "StyleMap" and el.get("id"):
            for pair in el:
                if local(pair.tag) == "Pair" and child_text(pair, "key") == "normal":
                    maps[el.get("id")] = (child_text(pair, "styleUrl") or "").lstrip("#")
    for k, v in maps.items():
        if v in styles and k not in styles:
            styles[k] = styles[v]
    return styles


def placemark_style(pm, styles):
    for ch in pm:
        if local(ch.tag) == "Style":
            return parse_style(ch)
    url = (child_text(pm, "styleUrl") or "").split("#")[-1]
    return styles.get(url, {})


def geoms_of(el):
    n = local(el.tag)
    if n == "Point":
        for c in el.iter():
            if local(c.tag) == "coordinates":
                pts = parse_coords(c.text)
                return [("point", [pts[:1]])] if pts else []
        return []
    if n == "LineString":
        for c in el.iter():
            if local(c.tag) == "coordinates":
                pts = parse_coords(c.text)
                return [("line", [pts])] if len(pts) >= 2 else []
        return []
    if n == "LinearRing":
        for c in el.iter():
            if local(c.tag) == "coordinates":
                return _polygon([parse_coords(c.text)])
        return []
    if n == "Polygon":
        outer, inner = [], []
        for b in el:
            bn = local(b.tag)
            if bn in ("outerBoundaryIs", "innerBoundaryIs"):
                for r in b:
                    if local(r.tag) == "LinearRing":
                        for c in r.iter():
                            if local(c.tag) == "coordinates":
                                (outer if bn == "outerBoundaryIs" else inner).append(parse_coords(c.text))
        return _polygon(outer[:1] + inner)
    if n == "MultiGeometry":
        res = []
        for sub in el:
            res += geoms_of(sub)
        return res
    return []


def _polygon(rings):
    out = []
    for r in rings:
        if len(r) >= 3:
            out.append(r if r[0] == r[-1] else r + [r[0]])
    return [("polygon", out)] if out and len(out[0]) >= 4 else []


def safe_layer_name(name):
    name = str(name or "").strip()
    for c in '<>/\\":;?*|=,`\'':
        name = name.replace(c, "_")
    name = _CTRL_RE.sub("", name).strip()
    return name[:255] or DEFAULT_LAYER


def parse_kml(path):
    raw = read_kml_bytes(path)
    root = parse_xml(raw)
    styles = collect_styles(root)
    feats, stats = [], Counter()

    def walk(el, trail):
        for ch in el:
            n = local(ch.tag)
            if n == "Folder":
                nm = child_text(ch, "name")
                walk(ch, trail + [nm] if nm else trail)
            elif n == "Document":
                walk(ch, trail)
            elif n == "Placemark":
                layer = safe_layer_name("_".join(trail)) if trail else DEFAULT_LAYER
                st = placemark_style(ch, styles)
                name = child_text(ch, "name") or ""
                d = ch.find("{*}description")
                desc = html_to_text(d.text if d is not None else "")
                attrs = []
                for ed in ch.iter():
                    en = local(ed.tag)
                    if en == "Data" and ed.get("name"):
                        v = ed.find("{*}value")
                        attrs.append((ed.get("name"), (v.text or "").strip() if v is not None else ""))
                    elif en == "SimpleData" and ed.get("name"):
                        attrs.append((ed.get("name"), (ed.text or "").strip()))
                found = False
                for g in ch:
                    for kind, geom in geoms_of(g):
                        found = True
                        rgb = None
                        if kind == "point":
                            rgb = st.get("icon") or st.get("label")
                            if st.get("icon_scale") == 0 and name:
                                kind = "text"
                                rgb = st.get("label") or rgb
                        elif kind == "line":
                            rgb = st.get("line")
                        else:
                            rgb = st.get("line") or st.get("poly")
                        feats.append(Feature(kind, layer, geom, rgb, name=name, desc=desc, attrs=list(attrs),
                                             text=name if kind == "text" else ""))
                        stats[kind] += 1
                if not found:
                    stats["no-geometry"] += 1

    walk(root, [])
    return feats, stats


# =============================================================== KMZ / KML -> DXF
def write_dxf(feats, projected, out_path, labels=True):
    import ezdxf
    from ezdxf.colors import rgb2int
    doc = ezdxf.new(DXF_VERSION)
    msp = doc.modelspace()
    try:
        doc.header["$INSUNITS"] = 6 if projected else 0
    except Exception:
        pass
    scale = 1.0 if projected else 1.0 / 111320.0
    size, th = POINT_SIZE_M * scale, LABEL_HEIGHT_M * scale

    blk = doc.blocks.new(POINT_BLOCK)
    blk.add_circle((0, 0), 0.5)
    blk.add_line((-0.75, 0), (0.75, 0))
    blk.add_line((0, -0.75), (0, 0.75))
    has_attdefs = True
    try:
        for tag in ("NAME", "DESCRIPTION"):
            blk.add_attdef(tag, (0.8, 0), "", dxfattribs={"height": 0.5, "flags": 1})
    except Exception:
        has_attdefs = False

    layer_names = {}

    def layer_of(name):
        key = safe_layer_name(name).lower()
        if key not in layer_names:
            real = safe_layer_name(name)
            if real not in doc.layers:
                doc.layers.new(real)
            layer_names[key] = real
        return layer_names[key]

    def attribs(f, layer):
        d = {"layer": layer}
        if f.rgb:
            d["true_color"] = rgb2int(f.rgb)
        return d

    def short(s, n=250):
        return _CTRL_RE.sub("", re.sub(r"\s+", " ", str(s))).strip()[:n]

    stats = Counter()
    for f in feats:
        layer = layer_of(f.layer)
        at = attribs(f, layer)
        if f.kind == "line":
            msp.add_lwpolyline(f.geom[0], format="xy", dxfattribs=at)
            stats["lines"] += 1
        elif f.kind == "polygon":
            for ring in f.geom:
                pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
                if len(pts) >= 3:
                    msp.add_lwpolyline(pts, format="xy", close=True, dxfattribs=at)
            stats["polygons"] += 1
        elif f.kind == "text":
            x, y = f.geom[0][0]
            msp.add_text(short(f.text, 250), dxfattribs=dict(at, insert=(x, y), height=th))
            stats["texts"] += 1
        else:
            x, y = f.geom[0][0]
            extra = "; ".join(f"{k}={v}" for k, v in f.attrs)
            desc = short(f.desc + (" | " + extra if extra else ""))
            values = {"NAME": short(f.name), "DESCRIPTION": desc}
            at2 = dict(at, xscale=size, yscale=size, zscale=size)
            try:
                if not has_attdefs:
                    raise ValueError("no attribute definitions")
                msp.add_auto_blockref(POINT_BLOCK, (x, y), values, dxfattribs=at2)
            except Exception:                      # never lose the point because of attributes
                msp.add_blockref(POINT_BLOCK, (x, y), dxfattribs=at2)
            stats["points"] += 1
            if labels and f.name:
                msp.add_text(short(f.name, 250), dxfattribs=dict(at, insert=(x + size, y), height=th))
                stats["labels"] += 1
    atomic_write(out_path, lambda tmp: doc.saveas(tmp))
    return stats


def kml_to_dxf(path, opts):
    need(("ezdxf", "ezdxf"), ("pyproj", "pyproj"))
    feats, stats = parse_kml(path)
    good, bad = [], 0
    for f in feats:
        if all(-180 <= x <= 180 and -90 <= y <= 90 for part in f.geom for x, y in part):
            good.append(f)
        else:
            bad += 1
    if not good:
        raise ConvError("No placemarks with usable geometry were found in this KML/KMZ.")
    b = bbox_of(good)
    clon, clat = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    auto = utm_epsg(clon, clat)
    if opts.forced:
        epsg = opts.epsg
    else:
        print(f"\n  Found {len(good)} features around lat {clat:.4f}, lon {clon:.4f}")
        epsg = choose_crs("Coordinate system of the output DXF?", auto, f"UTM metres, automatic zone (EPSG:{auto})")
    good, dropped, projected = project_features(good, None, epsg, require_lonlat=False)
    if not good:
        raise ConvError("Coordinate transformation failed for every feature.")
    out = next_version_path(path, ".dxf")
    counts = write_dxf(good, projected, out, labels=opts.labels)
    try:
        import ezdxf
        ezdxf.readfile(str(out))
    except Exception as e:
        print(f"  WARNING: the DXF failed its self-check ({e}).")
    print(f"\n  DONE: {out}")
    print("  " + ", ".join(f"{v} {k}" for k, v in counts.items())
          + f" | CRS: {'EPSG:' + str(epsg) if epsg else 'lon/lat'}")
    skipped = bad + dropped + stats.get("no-geometry", 0)
    if skipped:
        print(f"  {skipped} placemark(s) skipped (no geometry or invalid coordinates)")
    return out


# =============================================================== driver
@dataclass
class Opts:
    epsg: int | None = None
    forced: bool = False
    fill: bool = True
    explode: bool = False
    labels: bool = True


def log_error(path):
    try:
        with open(Path(__file__).with_name("converter_errors.log"), "a", encoding="utf-8") as f:
            f.write(f"--- {path}\n{traceback.format_exc()}\n")
    except Exception:
        pass


def process(path, opts):
    ext = Path(path).suffix.lower()
    print(f"\n>> {path}")
    try:
        if not os.path.isfile(path):
            raise ConvError("File not found.")
        if ext in (".kmz", ".kml"):
            kml_to_dxf(path, opts)
        elif ext in (".dxf", ".dwg"):
            dxf_to_kmz(path, opts)
        else:
            raise ConvError("Unsupported file type (use .kmz .kml .dxf .dwg).")
        return True
    except ConvError as e:
        print(f"  ERROR: {e}")
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print(f"  UNEXPECTED ERROR: {type(e).__name__}: {e}")
        print("  Details were saved to converter_errors.log next to this script.")
        log_error(path)
    return False


def main(argv=None):
    global INTERACTIVE
    ap = argparse.ArgumentParser(description="KMZ <-> DXF/DWG converter")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--epsg", type=int)
    ap.add_argument("--yes", "-y", action="store_true")
    ap.add_argument("--no-fill", action="store_true")
    ap.add_argument("--explode", action="store_true")
    ap.add_argument("--no-labels", action="store_true")
    args = ap.parse_args(argv)
    INTERACTIVE = (not args.yes) and bool(sys.stdin) and sys.stdin.isatty()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    opts = Opts(fill=not args.no_fill, explode=args.explode, labels=not args.no_labels)
    if args.epsg is not None:
        opts.forced = True
        opts.epsg = None if args.epsg == 4326 else args.epsg
        try:
            if opts.epsg:
                check_epsg(opts.epsg)
        except ConvError as e:
            print(f"ERROR: {e}")
            return 2

    print("=" * 44)
    print(f" KMZ <-> DXF/DWG Converter v{VERSION}")
    print("=" * 44)

    files = []
    for a in args.files:
        hits = glob.glob(a) if any(c in a for c in "*?[") else [a]
        files += hits or [a]
    ok = 0
    try:
        if files:
            ok = sum(process(f, opts) for f in files)
            print(f"\nFinished: {ok}/{len(files)} file(s) converted.")
            if INTERACTIVE:
                ask("Press Enter to close ...")
            return 0 if ok == len(files) else 1
        while True:
            raw = ask("\nDrag KMZ / KML / DXF / DWG file(s) here (Enter = quit): ")
            if not raw:
                break
            for p in parse_paths(raw):
                process(p, opts)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
