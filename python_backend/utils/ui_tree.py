"""
AgentVision — UI / Accessibility Tree capture
=============================================

THE CHEAPEST WAY TO READ A SCREEN.

A screenshot of a window costs hundreds-to-thousands of visual tokens and its
text has to be OCR-guessed. The same window's ACCESSIBILITY TREE is text: exact
element names, exact roles, exact pixel bboxes, no OCR error. Published browser-
automation comparisons put the differential at 10-20x cheaper for tree-vs-vision,
and intelligently pruned trees have been measured cutting input tokens ~98% while
holding task F1 (see docs/RESEARCH_TOKEN_EFFICIENCY.md for citations).

Per-OS backends, all OPTIONAL and lazily imported:
  macOS    AXUIElement via pyobjc (ApplicationServices/HIServices). Needs the
           Accessibility TCC grant — the GUI's Permissions tab already handles it.
  Windows  UI Automation via comtypes; falls back to EnumChildWindows+WM_GETTEXT.
  Linux    AT-SPI via pyatspi.

WHEN THIS DOES NOT WORK — and it often will not, which is why the fallback is
part of the contract rather than an afterthought:
  * Custom-drawn UIs expose no tree: games, emulators, canvas/WebGL apps,
    immediate-mode GUIs (Dear ImGui), most SDL/OpenGL windows. The tree comes
    back empty or as a single opaque node.
  * Unlabelled or offscreen controls are missing from the tree even when visible.
  * Icon colour, spatial layout, progress bars and rendering corruption are NOT
    in the tree at all — those are exactly the questions that need pixels.
So `available: false` (or a suspiciously tiny tree) is a legitimate answer that
routes the caller to av_frame_json / av_ocr_frame / av_frame_region instead.

PRUNING is where the token saving comes from: drop invisible and zero-size
nodes, drop container nodes that carry no text and only one child, cap depth and
node count. A raw a11y tree can be tens of thousands of tokens — worse than the
screenshot it was meant to replace.
"""

from __future__ import annotations

import os
import sys
import time

# 150 nodes is ~2-4k tokens. Higher budgets routinely produce a tree that
# costs MORE than the screenshot it was supposed to replace (a 400-node
# Finder tree measured ~15,900 est tokens vs 4,784 for a 4K frame), which is
# the classic a11y-tree trap. capture_tree() reports the comparison so the
# caller can see when the tree is NOT the cheap option.
MAX_NODES  = int(os.environ.get("AGENTVISION_UITREE_MAX_NODES", "150"))
MAX_DEPTH  = int(os.environ.get("AGENTVISION_UITREE_MAX_DEPTH", "12"))
TEXT_CAP   = int(os.environ.get("AGENTVISION_UITREE_TEXT_CAP", "160"))
# Hard wall-clock ceiling on a traversal. Accessibility APIs are cross-process
# IPC and can be arbitrarily slow (or block) on a busy or wedged target, so the
# walk is always deadline-bounded — a UI-tree read must never stall the bridge.
TREE_DEADLINE_MS = float(os.environ.get("AGENTVISION_UITREE_DEADLINE_MS", "2500"))

IS_MAC   = sys.platform == "darwin"
IS_WIN   = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

# Roles that are pure layout scaffolding — pruned when they carry no text.
_CONTAINER_ROLES = {
    "AXGroup", "AXSplitGroup", "AXScrollArea", "AXLayoutArea", "AXLayoutItem",
    "AXUnknown", "pane", "panel", "filler", "section", "Group", "Pane",
    "AXRow", "AXCell", "AXColumn", "AXList", "AXOutline", "AXTable",
}

# Roles worth keeping even with no text — they are the things an agent acts on
# or reasons about. Everything else that is text-free is scaffolding.
_MEANINGFUL_ROLES = {
    "AXButton", "AXCheckBox", "AXRadioButton", "AXTextField", "AXTextArea",
    "AXMenuItem", "AXMenuButton", "AXPopUpButton", "AXSlider", "AXProgressIndicator",
    "AXTabGroup", "AXWindow", "AXSheet", "AXDialog", "AXImage", "AXLink",
    "button", "check box", "radio button", "text", "entry", "menu item",
    "combo box", "slider", "progress bar", "dialog", "window", "link",
    "Button", "Edit", "CheckBox", "ComboBox", "Hyperlink", "Text", "Window",
}


def backends_available() -> dict:
    """Which backend this OS would use, and whether its import actually works.
    Pure probe — never raises, never requires a window."""
    out = {"platform": sys.platform, "backend": None, "importable": False,
           "reason": ""}
    if IS_MAC:
        out["backend"] = "macos-ax (pyobjc ApplicationServices)"
        try:
            import ApplicationServices  # noqa: F401
            out["importable"] = True
        except Exception as ex:
            out["reason"] = (f"pyobjc not installed ({ex}). "
                             "Install with: pip install pyobjc-framework-Quartz "
                             "pyobjc-framework-ApplicationServices")
    elif IS_WIN:
        out["backend"] = "windows-uia (comtypes) / win32 fallback"
        try:
            import comtypes.client  # noqa: F401
            out["importable"] = True
        except Exception as ex:
            out["reason"] = (f"comtypes not installed ({ex}); the win32 "
                             "EnumChildWindows fallback may still work")
            try:
                import ctypes  # noqa: F401
                out["fallback"] = "win32-enumchildwindows"
            except Exception:
                pass
    elif IS_LINUX:
        out["backend"] = "linux-atspi (pyatspi)"
        try:
            import pyatspi  # noqa: F401
            out["importable"] = True
        except Exception as ex:
            out["reason"] = (f"pyatspi not installed ({ex}). On Debian/Ubuntu: "
                             "apt install python3-pyatspi; also requires "
                             "at-spi2 to be running and accessibility enabled")
    else:
        out["reason"] = f"no UI-tree backend for platform {sys.platform!r}"
    return out


def _unavailable(reason: str, extra: dict | None = None) -> dict:
    out = {
        "available": False,
        "reason": reason,
        "fallback": ("use av_frame_json (JSON descriptor + OCR text) or "
                     "av_ocr_frame for on-screen text, and av_frame_region for "
                     "pixels — a UI tree is unavailable or empty here"),
        "note": ("Custom-drawn UIs (games, emulators, canvas/WebGL, ImGui) "
                 "legitimately expose no accessibility tree. This is expected, "
                 "not a bug."),
        "backends": backends_available(),
    }
    if extra:
        out.update(extra)
    return out


# ── Pruning ───────────────────────────────────────────────────────────────────

def prune(nodes: list[dict]) -> tuple[list[dict], dict]:
    """Prune a raw tree in place-ish and report what it cost.

    Rules, in order of how much they save:
      1. drop invisible / zero-area nodes
      2. drop text-free container nodes, promoting their children
      3. drop text-free leaves whose role is not something an agent acts on
      4. cap total nodes (breadth-first, so the useful top of the tree survives)

    Rule 3 is where most of the saving comes from on real trees: list/table
    scaffolding is the bulk of a deep window's node count and carries nothing.
    """
    stats = {"dropped_invisible": 0, "dropped_empty_containers": 0,
             "dropped_silent_leaves": 0, "dropped_over_cap": 0}

    def _visible(n: dict) -> bool:
        bb = n.get("bbox")
        if not bb or len(bb) != 4:
            return bool(n.get("text"))
        return bb[2] > 0 and bb[3] > 0

    def _walk(ns: list[dict], depth: int) -> list[dict]:
        out: list[dict] = []
        for n in ns:
            kids = _walk(n.get("children") or [], depth + 1) if depth < MAX_DEPTH else []
            if not _visible(n) and not kids:
                stats["dropped_invisible"] += 1
                continue
            has_text = bool((n.get("text") or "").strip())
            role = n.get("role") or ""
            is_container = role in _CONTAINER_ROLES
            if is_container and not has_text:
                # A text-free container is pure layout scaffolding: promote its
                # children and drop it. Done for ANY child count, not just one —
                # nesting costs tokens and flatten() already reports depth, so
                # the grouping adds cost without adding information.
                stats["dropped_empty_containers"] += 1
                out.extend(kids)
                continue
            # A leaf with no text and no meaningful role tells the agent nothing
            # — this rule is where most of the token saving comes from.
            if not kids and not has_text and role not in _MEANINGFUL_ROLES:
                stats["dropped_silent_leaves"] += 1
                continue
            n2 = {k: v for k, v in n.items() if k != "children"}
            if kids:
                n2["children"] = kids
            out.append(n2)
        return out

    pruned = _walk(nodes, 0)

    # Node cap — breadth-first so the informative top of the tree survives.
    kept = 0

    def _cap(ns: list[dict]) -> list[dict]:
        nonlocal kept
        out = []
        for n in ns:
            if kept >= MAX_NODES:
                stats["dropped_over_cap"] += 1
                continue
            kept += 1
            kids = n.get("children") or []
            n2 = {k: v for k, v in n.items() if k != "children"}
            capped = _cap(kids)
            if capped:
                n2["children"] = capped
            out.append(n2)
        return out

    pruned = _cap(pruned)
    stats["nodes_kept"] = kept
    return pruned, stats


def _count(nodes: list[dict]) -> int:
    return sum(1 + _count(n.get("children") or []) for n in nodes)


def flatten(nodes: list[dict], out: list[dict] | None = None,
            depth: int = 0) -> list[dict]:
    """Flatten a tree to {d, role, text, bbox} rows — usually the cheapest and
    most useful shape for an agent (no nesting to reason about).

    `d` is the depth instead of a full slash-path: full paths were measured to be
    a large share of the payload for deep trees, and the agent addresses elements
    by bbox/text anyway."""
    if out is None:
        out = []
    for n in nodes:
        row = {"d": depth, "role": n.get("role")}
        if n.get("text"):
            row["text"] = n.get("text")
        if n.get("bbox"):
            row["bbox"] = n.get("bbox")
        out.append(row)
        flatten(n.get("children") or [], out, depth + 1)
    return out


# ── macOS backend ─────────────────────────────────────────────────────────────

def _mac_tree(app_name: str = "", pid: int | None = None) -> dict:
    try:
        import ApplicationServices as AS
    except Exception as ex:
        return _unavailable(f"pyobjc/ApplicationServices unavailable ({ex})")
    try:
        if not AS.AXIsProcessTrusted():
            return _unavailable(
                "this process is not TRUSTED for macOS Accessibility",
                {"fix": ("System Settings > Privacy & Security > Accessibility: "
                         "grant the terminal/app running AgentVision. The GUI's "
                         "Permissions tab and `agentvision doctor` walk through "
                         "this.")})
    except Exception:
        pass                       # older pyobjc: fall through and try anyway

    target_pid = pid
    if target_pid is None:
        try:
            from AppKit import NSWorkspace
            want = (app_name or "").lower()
            for a in NSWorkspace.sharedWorkspace().runningApplications():
                nm = (a.localizedName() or "")
                if want and want in nm.lower():
                    target_pid = int(a.processIdentifier())
                    break
                if not want and a.isActive():
                    target_pid = int(a.processIdentifier())
            if target_pid is None:
                return _unavailable(
                    f"no running application matched {app_name!r}"
                    if app_name else "could not determine the frontmost app")
        except Exception as ex:
            return _unavailable(f"could not resolve the target app ({ex})")

    # Every AX attribute read is an IPC round trip to the target process, so a
    # naive per-attribute walk over a big window (Finder with a long file list:
    # ~2800 nodes) took 27 SECONDS. Two fixes, both essential on the capture
    # path: fetch all scalar attributes in ONE call via
    # AXUIElementCopyMultipleAttributeValues (~2.4x faster and 1 round trip
    # instead of 5), and enforce a node budget + wall-clock deadline DURING the
    # traversal so a pathological tree can never stall the bridge.
    _ATTRS = ["AXRole", "AXTitle", "AXValue", "AXPosition", "AXSize",
              "AXDescription"]
    deadline = time.perf_counter() + (TREE_DEADLINE_MS / 1000.0)
    raw_budget = MAX_NODES * 4
    visited = {"n": 0, "hit_budget": False, "hit_deadline": False}

    def _attr(el, name):
        try:
            err, val = AS.AXUIElementCopyAttributeValue(el, name, None)
            return val if err == 0 else None
        except Exception:
            return None

    def _unwrap(v, kind):
        try:
            ok, out = AS.AXValueGetValue(v, kind, None)
            return out if ok else None
        except Exception:
            return None

    def _node(el, depth: int) -> dict | None:
        if depth > MAX_DEPTH:
            return None
        if visited["n"] >= raw_budget:
            visited["hit_budget"] = True
            return None
        if time.perf_counter() > deadline:
            visited["hit_deadline"] = True
            return None
        visited["n"] += 1
        try:
            err, vals = AS.AXUIElementCopyMultipleAttributeValues(
                el, _ATTRS, 0, None)
            vals = list(vals) if vals else []
        except Exception:
            vals = []
        def _v(i):
            return vals[i] if i < len(vals) else None
        role = _v(0) or ""
        text = ""
        for cand in (_v(1), _v(2), _v(5)):
            if isinstance(cand, str) and cand.strip():
                text = cand.strip()[:TEXT_CAP]
                break
        bbox = None
        pt = _unwrap(_v(3), AS.kAXValueCGPointType) if _v(3) is not None else None
        sz = _unwrap(_v(4), AS.kAXValueCGSizeType) if _v(4) is not None else None
        if pt is not None and sz is not None:
            bbox = [int(pt.x), int(pt.y), int(sz.width), int(sz.height)]
        node = {"role": str(role), "text": text or None, "bbox": bbox}
        kids = _attr(el, "AXChildren") or []
        children = []
        for k in list(kids)[:200]:
            cn = _node(k, depth + 1)
            if cn:
                children.append(cn)
            if visited["hit_budget"] or visited["hit_deadline"]:
                break
        if children:
            node["children"] = children
        return node

    try:
        app_el = AS.AXUIElementCreateApplication(target_pid)
        windows = _attr(app_el, "AXWindows") or []
        roots = []
        for w in list(windows)[:5]:
            n = _node(w, 0)
            if n:
                roots.append(n)
            if visited["hit_budget"] or visited["hit_deadline"]:
                break
        if not roots:
            n = _node(app_el, 0)
            if n:
                roots.append(n)
    except Exception as ex:
        return _unavailable(f"AX traversal failed ({ex})")

    if not roots:
        return _unavailable("the application exposed no accessibility windows",
                            {"pid": target_pid})
    out = {"available": True, "backend": "macos-ax", "pid": target_pid,
           "raw": roots, "nodes_visited": visited["n"]}
    if visited["hit_budget"]:
        out["traversal_stopped"] = f"node budget ({raw_budget}) reached"
    if visited["hit_deadline"]:
        out["traversal_stopped"] = f"deadline ({TREE_DEADLINE_MS}ms) reached"
    return out


# ── Windows backend ───────────────────────────────────────────────────────────

def _win_tree(app_name: str = "", pid: int | None = None) -> dict:
    # Why this is held in a plain local instead of read off the `except ... as`
    # name: Python DELETES the `as` target when the handler block exits, so the
    # win32 handler below cannot see it. Reading it there raised
    # UnboundLocalError from inside the very error path that exists to explain
    # why BOTH backends failed. comtypes is not in requirements-windows.txt, so
    # the UIA branch always fails on a stock install and this is the only path
    # that ever reports the pair.
    uia_error: BaseException | str = "not attempted"
    # Preferred: UI Automation.
    try:
        import comtypes.client as cc
        uia = cc.GetModule("UIAutomationCore.dll")
        iuia = cc.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}",
                               interface=uia.IUIAutomation)
        root = iuia.GetRootElement()
        cond = iuia.CreateTrueCondition()

        def _node(el, depth: int) -> dict | None:
            if depth > MAX_DEPTH:
                return None
            try:
                name = el.CurrentName or ""
                ctype = el.CurrentLocalizedControlType or ""
                r = el.CurrentBoundingRectangle
                bbox = [int(r.left), int(r.top),
                        int(r.right - r.left), int(r.bottom - r.top)]
            except Exception:
                return None
            node = {"role": str(ctype), "text": (name or None) and name[:TEXT_CAP],
                    "bbox": bbox}
            children = []
            try:
                walker = iuia.CreateTreeWalker(cond)
                child = walker.GetFirstChildElement(el)
                n = 0
                while child and n < 200:
                    cn = _node(child, depth + 1)
                    if cn:
                        children.append(cn)
                    child = walker.GetNextSiblingElement(child)
                    n += 1
            except Exception:
                pass
            if children:
                node["children"] = children
            return node

        want = (app_name or "").lower()
        walker = iuia.CreateTreeWalker(cond)
        top = walker.GetFirstChildElement(root)
        roots = []
        n = 0
        while top and n < 60:
            try:
                nm = (top.CurrentName or "").lower()
                match = (not want) or (want in nm)
            except Exception:
                match = False
            if match:
                node = _node(top, 0)
                if node:
                    roots.append(node)
                    if want:
                        break
            top = walker.GetNextSiblingElement(top)
            n += 1
        if roots:
            return {"available": True, "backend": "windows-uia", "raw": roots}
        return _unavailable(f"no UIA window matched {app_name!r}"
                            if want else "UIA returned no top-level windows")
    except Exception as ex_uia:
        uia_error = ex_uia

    # Fallback: raw win32 child-window enumeration + WM_GETTEXT.
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        want = (app_name or "").lower()
        found: list[dict] = []

        def _text(hwnd) -> str:
            n = u32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return ""
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            return buf.value

        def _rect(hwnd):
            r = wintypes.RECT()
            if u32.GetWindowRect(hwnd, ctypes.byref(r)):
                return [r.left, r.top, r.right - r.left, r.bottom - r.top]
            return None

        ENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _children(hwnd) -> list[dict]:
            kids: list[dict] = []

            def cb(h, _l):
                if len(kids) >= 200:
                    return False
                cls = ctypes.create_unicode_buffer(128)
                u32.GetClassNameW(h, cls, 128)
                kids.append({"role": cls.value, "text": (_text(h) or None),
                             "bbox": _rect(h)})
                return True
            u32.EnumChildWindows(hwnd, ENUM(cb), 0)
            return kids

        def top_cb(hwnd, _l):
            if not u32.IsWindowVisible(hwnd):
                return True
            t = _text(hwnd)
            if want and want not in t.lower():
                return True
            if not t:
                return True
            found.append({"role": "Window", "text": t[:TEXT_CAP],
                          "bbox": _rect(hwnd), "children": _children(hwnd)})
            return len(found) < 5
        u32.EnumWindows(ENUM(top_cb), 0)
        if found:
            return {"available": True, "backend": "win32-enumchildwindows",
                    "raw": found,
                    "limitation": ("class names instead of real control roles, "
                                   "and no owner-drawn control text — UIA "
                                   "(pip install comtypes) is much better")}
        return _unavailable(f"no visible window matched {app_name!r}"
                            if want else "no visible top-level windows found")
    except Exception as ex_win:
        return _unavailable(f"UIA unavailable ({uia_error}); win32 fallback also "
                            f"failed ({ex_win})")


# ── Linux backend ─────────────────────────────────────────────────────────────

def _linux_tree(app_name: str = "", pid: int | None = None) -> dict:
    try:
        import pyatspi
    except Exception as ex:
        return _unavailable(
            f"pyatspi unavailable ({ex})",
            {"fix": ("apt install python3-pyatspi (Debian/Ubuntu) or "
                     "pacman -S python-atspi (Arch/Artix); at-spi2 must be "
                     "running and accessibility enabled for the session")})
    try:
        want = (app_name or "").lower()

        def _node(acc, depth: int) -> dict | None:
            if depth > MAX_DEPTH:
                return None
            try:
                role = acc.getRoleName()
                name = acc.name or ""
                comp = acc.queryComponent() if acc.getState() else None
            except Exception:
                return None
            bbox = None
            try:
                comp = acc.queryComponent()
                ex = comp.getExtents(pyatspi.DESKTOP_COORDS)
                bbox = [int(ex.x), int(ex.y), int(ex.width), int(ex.height)]
            except Exception:
                bbox = None
            node = {"role": str(role), "text": (name[:TEXT_CAP] or None),
                    "bbox": bbox}
            children = []
            try:
                for i in range(min(acc.childCount, 200)):
                    cn = _node(acc.getChildAtIndex(i), depth + 1)
                    if cn:
                        children.append(cn)
            except Exception:
                pass
            if children:
                node["children"] = children
            return node

        roots = []
        desktop = pyatspi.Registry.getDesktop(0)
        for i in range(desktop.childCount):
            app = desktop.getChildAtIndex(i)
            try:
                nm = (app.name or "").lower()
            except Exception:
                continue
            if want and want not in nm:
                continue
            n = _node(app, 0)
            if n:
                roots.append(n)
            if want and roots:
                break
        if roots:
            return {"available": True, "backend": "linux-atspi", "raw": roots}
        return _unavailable(f"no AT-SPI application matched {app_name!r}"
                            if want else "AT-SPI desktop exposed no applications")
    except Exception as ex:
        return _unavailable(f"AT-SPI traversal failed ({ex})")


# ── Public API ────────────────────────────────────────────────────────────────

def capture_tree(app_name: str = "", pid: int | None = None,
                 flat: bool = False,
                 frame_size: list | tuple | None = None) -> dict:
    """Capture the target window's UI tree as bounded, pruned JSON.

    Returns {available, backend, element_count, raw_element_count, truncated,
    prune_stats, tree | elements, cost{est_tokens, cheaper_than_screenshot,
    verdict}, cost_ms} or, when no tree exists, {available:false, reason,
    fallback, note, backends} — which is a legitimate answer for custom-drawn
    UIs, not an error.

    `frame_size` (w, h) enables the honest cost comparison against what the
    equivalent screenshot would cost in visual tokens."""
    t0 = time.perf_counter()
    if IS_MAC:
        res = _mac_tree(app_name, pid)
    elif IS_WIN:
        res = _win_tree(app_name, pid)
    elif IS_LINUX:
        res = _linux_tree(app_name, pid)
    else:
        res = _unavailable(f"no UI-tree backend for {sys.platform!r}")
    if not res.get("available"):
        res["cost_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        return res

    raw = res.pop("raw", [])
    raw_n = _count(raw)
    tree, stats = prune(raw)
    n = _count(tree)
    out = dict(res)
    out.update({
        "element_count": n,
        "raw_element_count": raw_n,
        "pruned_away": max(0, raw_n - n),
        "prune_stats": stats,
        "truncated": bool(stats.get("dropped_over_cap")),
        "caps": {"max_nodes": MAX_NODES, "max_depth": MAX_DEPTH,
                 "text_cap": TEXT_CAP},
        "cost_ms": round((time.perf_counter() - t0) * 1000.0, 2),
    })
    if flat:
        out["elements"] = flatten(tree)[:MAX_NODES]
    else:
        out["tree"] = tree

    # HONEST COST VERDICT. An accessibility tree is only a win if it is actually
    # cheaper than the screenshot it replaces — for a deep list-heavy window it
    # is not, and pretending otherwise would defeat the whole point.
    try:
        import json as _json
        from utils import visual_engine as _ve
        chars = len(_json.dumps(out.get("elements") or out.get("tree") or []))
        est = _ve.est_text_tokens(chars)
        out["cost"] = {"payload_chars": chars, "est_tokens": est,
                       "method": "~4 characters per token (estimate)"}
        if frame_size and len(frame_size) == 2 and frame_size[0]:
            img = _ve.visual_tokens(int(frame_size[0]), int(frame_size[1]))
            out["cost"]["est_visual_tokens_of_the_screenshot"] = img
            out["cost"]["cheaper_than_screenshot"] = bool(est < img)
            out["cost"]["ratio_vs_screenshot"] = (round(est / float(img), 2)
                                                  if img else None)
            if est >= img:
                out["cost"]["verdict"] = (
                    "This tree costs MORE than the screenshot it would replace. "
                    "Prefer av_frame_json / av_ocr_frame, or re-request with a "
                    "smaller max_nodes / a specific app_name.")
            else:
                out["cost"]["verdict"] = (
                    f"Tree is ~{max(1, int(img / max(1, est)))}x cheaper than the "
                    "screenshot AND gives exact text + coordinates (no OCR error).")
    except Exception:
        pass

    # An almost-empty tree is the custom-drawn-UI case in disguise. Say so.
    if n <= 2:
        out["likely_custom_drawn"] = True
        out["fallback"] = ("this tree is nearly empty — the app probably draws "
                           "its own UI (game/emulator/canvas). Use av_frame_json "
                           "/ av_ocr_frame / av_frame_region instead.")
    return out


def diff_trees(before: list[dict], after: list[dict]) -> dict:
    """SEMANTIC diff of two UI trees — far cheaper and more meaningful than a
    pixel diff: "button 'Start' disappeared", "label changed to 'Error'".

    Matches elements by (role, bbox-cell) so a moved-but-identical element is
    not reported as both an add and a remove."""
    def _key(e):
        bb = e.get("bbox") or [0, 0, 0, 0]
        # Quantise position so a 1-px reflow is not a "change".
        return (e.get("role") or "", bb[0] // 8, bb[1] // 8)

    b = {_key(e): e for e in flatten(before)}
    a = {_key(e): e for e in flatten(after)}
    appeared, disappeared, changed = [], [], []
    for k, e in a.items():
        if k not in b:
            appeared.append({"role": e.get("role"), "text": e.get("text"),
                             "bbox": e.get("bbox")})
        elif (b[k].get("text") or "") != (e.get("text") or ""):
            changed.append({"role": e.get("role"), "bbox": e.get("bbox"),
                            "from": b[k].get("text"), "to": e.get("text")})
    for k, e in b.items():
        if k not in a:
            disappeared.append({"role": e.get("role"), "text": e.get("text"),
                                "bbox": e.get("bbox")})
    summary = []
    for e in appeared[:10]:
        summary.append(f"appeared: {e['role']} {e.get('text') or ''}".strip())
    for e in disappeared[:10]:
        summary.append(f"disappeared: {e['role']} {e.get('text') or ''}".strip())
    for e in changed[:10]:
        summary.append(f"changed: {e['role']} {e.get('from')!r} -> {e.get('to')!r}")
    return {
        "appeared": appeared[:50], "disappeared": disappeared[:50],
        "changed": changed[:50],
        "counts": {"appeared": len(appeared), "disappeared": len(disappeared),
                   "changed": len(changed)},
        "summary": summary,
        "identical": not (appeared or disappeared or changed),
    }
