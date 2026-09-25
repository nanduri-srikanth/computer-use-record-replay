"""PlaywrightSurface: the Surface protocol over a real Chromium session."""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urlparse

from playwright.sync_api import Dialog, Frame, Locator, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from ..contracts import ActionType, LocatorCandidate, LocatorKind
from ..redactor import SENSITIVE_LABELS, Redactor
from . import ActionFailed, ElementInfo, FrameView, HumanEvent, Match, NativeDialog, Observation, Snapshot

_OBSERVE_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const roleOf = el => { const t = el.tagName.toLowerCase(), ty = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'a') return 'link'; if (t === 'select') return 'combobox'; if (t === 'textarea') return 'textbox';
    if (t === 'button' || ['submit','button','reset'].includes(ty)) return 'button';
    if (ty === 'checkbox') return 'checkbox'; if (ty === 'radio') return 'radio';
    if (t === 'td') return 'cell'; return 'textbox'; };
  document.querySelectorAll('[data-cua-ref]').forEach(e => e.removeAttribute('data-cua-ref'));
  const out = []; let n = 0;
  const push = (el, text) => {
    const tag = el.tagName.toLowerCase(), role = roleOf(el);
    let name = role === 'link' || tag === 'button' ? norm(el.innerText) : role === 'button' ? norm(el.value) : '';
    let label = '';
    if (el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) { label = norm(l.innerText); if (!name) name = label; } }
    const td = tag === 'td' ? el : el.closest('td');
    let column = null, rowCells = [];
    if (td) {
      const prev = td.previousElementSibling;
      if (!label && prev && prev.tagName === 'TD') label = norm(prev.innerText);
      const row = td.parentElement; column = Array.from(row.children).indexOf(td) + 1;
      rowCells = Array.from(row.children).map(c => norm(c.innerText));
    }
    const r = el.getBoundingClientRect(); const ref = 'e' + (n++);
    el.setAttribute('data-cua-ref', ref);
    out.push({ref, tag, role, name, label, column, rowCells, x: r.x + r.width / 2, y: r.y + r.height / 2, text: text || ''});
  };
  for (const el of document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea'))
    if (vis(el)) push(el);
  // readable "Label: value" cells, for EXTRACT targets
  for (const td of document.querySelectorAll('td')) {
    const prev = td.previousElementSibling;
    if (vis(td) && prev && prev.tagName === 'TD' && /:\s*$/.test(norm(prev.innerText)) && !td.querySelector('input,select,textarea,a'))
      push(td, norm(td.innerText));
  }
  const headings = Array.from(document.querySelectorAll('h1,h2,h3')).filter(vis).map(h => norm(h.innerText));
  return {elements: out, headings};
}
"""

_ELEMENT_AT_JS = r"""
([x, y]) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  let el = document.elementFromPoint(x, y);
  if (!el || el === document.body || el === document.documentElement) return null;
  el = el.closest('a,button,input,select,textarea,td') || el;
  const tag = el.tagName.toLowerCase(), ty = (el.getAttribute('type') || '').toLowerCase();
  const role = tag === 'a' ? 'link' : tag === 'select' ? 'combobox' : tag === 'textarea' ? 'textbox'
    : (tag === 'button' || ['submit','button','reset'].includes(ty)) ? 'button' : tag === 'td' ? 'cell' : 'textbox';
  const name = role === 'link' || tag === 'button' ? norm(el.innerText) : role === 'button' ? norm(el.value) : '';
  const td = tag === 'td' ? el : el.closest('td'); let label = '', column = null, rowCells = [];
  if (td) { const prev = td.previousElementSibling; if (prev && prev.tagName === 'TD') label = norm(prev.innerText);
    const row = td.parentElement; column = Array.from(row.children).indexOf(td) + 1;
    rowCells = Array.from(row.children).map(c => norm(c.innerText)); }
  const r = el.getBoundingClientRect();
  el.setAttribute('data-cua-ref', 'pt');
  return {tag, role, name, label, column, rowCells, x: r.x + r.width / 2, y: r.y + r.height / 2,
          text: tag === 'td' ? norm(el.innerText) : ''};
}
"""

_SUBMITS_POST_JS = ("el => { const f = el.form; const t = (el.getAttribute('type') || '').toLowerCase();"
                    " return !!f && (f.getAttribute('method') || 'get').toLowerCase() === 'post'"
                    " && (t === 'submit' || el.tagName === 'BUTTON'); }")

_TARGET_URL_JS = ("el => { if (el.tagName === 'A' && el.href) return el.href;"
                  " const t = (el.getAttribute('type') || '').toLowerCase();"
                  " if (el.form && (t === 'submit' || el.tagName === 'BUTTON')) return el.form.action || '';"
                  " return ''; }")

_CAPTURE_JS = r"""
(() => {
  if (window.__cuaCapture) return; window.__cuaCapture = true;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const describe = el => {
    const tag = el.tagName.toLowerCase(), ty = (el.getAttribute('type') || '').toLowerCase();
    const role = tag === 'a' ? 'link' : tag === 'select' ? 'combobox'
      : (tag === 'button' || ['submit','button'].includes(ty)) ? 'button' : tag === 'input' ? 'textbox' : tag;
    const name = role === 'link' || tag === 'button' ? norm(el.innerText) : role === 'button' ? norm(el.value) : '';
    const td = el.closest('td'); const prev = td && td.previousElementSibling;
    return {tag, role, name: name.slice(0, 60), label: prev ? norm(prev.innerText).slice(0, 60) : ''};
  };
  const send = (type, el) => { try { window.__cuaEvent({type, frame: window.name || null, ...describe(el)}); } catch (e) {} };
  document.addEventListener('click', e => { const el = e.target.closest('a,button,input,select,textarea'); if (el) send('click', el); }, true);
  document.addEventListener('change', e => send('change', e.target), true);  // values are never sent
  document.addEventListener('submit', e => send('submit', e.target), true);
})();
"""


def _xpath_literal(s: str) -> str:
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in s.split("'")) + ")"


class PlaywrightSurface:
    def __init__(self, base_url: str, headless: bool = True, action_timeout: float = 3.0,
                 redactor: Redactor | None = None):
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.action_timeout_ms = int(action_timeout * 1000)
        self.redactor = redactor or Redactor()
        self._guard: Callable[[], None] = lambda: None
        self._event_sink: Callable[[HumanEvent], None] | None = None
        self._dialog_policy: Callable[[str, str], bool] = lambda kind, message: False  # default: dismiss
        self._dialogs: list[NativeDialog] = []
        self._pw = self._browser = self._context = None
        self.page: Page | None = None

    # ---- lifecycle -------------------------------------------------------------

    def start(self) -> "PlaywrightSurface":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(viewport={"width": 1100, "height": 750})
        self._context.expose_binding("__cuaEvent", self._on_event)
        self._context.add_init_script(_CAPTURE_JS)
        self.page = self._context.new_page()
        self.page.set_default_timeout(self.action_timeout_ms)
        self.page.on("dialog", self._on_dialog)
        return self

    def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                closer and closer.close()
            except Exception:
                pass
        try:
            self._pw and self._pw.stop()
        except Exception:
            pass

    def __enter__(self) -> "PlaywrightSurface":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def authenticate(self, username: str, password: str) -> None:
        """Session bootstrap: sign in over HTTP so credentials never touch the UI, logs, or the model."""
        resp = self._context.request.post(f"{self.base_url}/login", form={"u": username, "p": password, "next": "/nav"})
        if not resp.ok:
            raise RuntimeError(f"bootstrap sign-in failed ({resp.status})")

    # ---- control token + events ----------------------------------------------------

    def set_act_guard(self, guard: Callable[[], None]) -> None:
        self._guard = guard

    def set_event_sink(self, sink: Callable[[HumanEvent], None] | None) -> None:
        self._event_sink = sink

    def _on_event(self, source: Any, payload: dict) -> None:
        if self._event_sink:
            self._event_sink(HumanEvent(type=payload.get("type", ""), frame=payload.get("frame"),
                                        tag=payload.get("tag", ""), role=payload.get("role", ""),
                                        name=payload.get("name", ""), label=payload.get("label", "")))

    # ---- native dialogs --------------------------------------------------------------

    def set_dialog_policy(self, accept: Callable[[str, str], bool]) -> None:
        self._dialog_policy = accept

    def _on_dialog(self, dialog: Dialog) -> None:
        accept = bool(self._dialog_policy(dialog.type, dialog.message))
        self._dialogs.append(NativeDialog(kind=dialog.type, message=self.redactor.text(dialog.message), accepted=accept))
        dialog.accept() if accept else dialog.dismiss()

    def pop_native_dialogs(self) -> list[NativeDialog]:
        out, self._dialogs = self._dialogs, []
        return out

    def human_page(self) -> Page:
        """Raw page handle for the human (or a scripted operator in tests). Not guarded."""
        return self.page

    def pump(self, ms: int = 200) -> None:
        """Let the browser process events while waiting on a human."""
        self.page.wait_for_timeout(ms)

    # ---- frames ------------------------------------------------------------------

    def _frame(self, name: str | None) -> Frame:
        if name is None:
            return self.page.main_frame
        f = self.page.frame(name=name)
        if f is None:
            raise LookupError(f"frame {name!r} not present")
        return f

    def frame_url(self, frame: str | None) -> str:
        try:
            return self._frame(frame).url
        except LookupError:
            return ""

    def frame_text(self, frame: str | None) -> str:
        try:
            return self._frame(frame).locator("body").inner_text(timeout=1000)
        except Exception:
            return ""

    def dialog_visible(self, frame: str | None, selector: str) -> str | None:
        try:
            loc = self._frame(frame).locator(selector)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    return loc.nth(i).inner_text(timeout=1000)
        except Exception:
            return None
        return None

    # ---- observe -----------------------------------------------------------------

    def _frame_names(self) -> list[str | None]:
        names = [f.name for f in self.page.frames if f is not self.page.main_frame and f.name]
        return names or [None]

    def observe(self, with_screenshot: bool = False, dialog_selector: str = ".modal") -> Observation:
        frames, elements = [], []
        for name in self._frame_names():
            f = self._frame(name)
            try:
                data = f.evaluate(_OBSERVE_JS)
            except Exception:
                data = {"elements": [], "headings": []}
            for e in data["elements"]:
                elements.append(ElementInfo(ref=f"{name or 'top'}:{e['ref']}", frame=name, tag=e["tag"], role=e["role"],
                                            name=e["name"], label=e["label"], column=e["column"],
                                            row_cells=e["rowCells"], x=e["x"], y=e["y"], text=e["text"]))
            frames.append(FrameView(name=name, url=f.url, route=urlparse(f.url).path or "/",
                                    headings=data["headings"], text=self.frame_text(name),
                                    dialog_open=self.dialog_visible(name, dialog_selector) is not None))
        png = self.snapshot().png if with_screenshot else None
        return Observation(frames=frames, elements=elements, screenshot_png=png)

    # ---- resolve -------------------------------------------------------------------

    def _locator(self, f: Frame, c: LocatorCandidate) -> Locator:
        if c.kind == LocatorKind.ROLE_NAME:
            return f.get_by_role(c.role, name=c.name, exact=True)
        tail = "" if c.element == "td" else f"//{c.element}"
        if c.kind == LocatorKind.LABEL_PROXIMITY:
            return f.locator(f"xpath=//td[normalize-space(.)={_xpath_literal(c.label)}]/following-sibling::td[1]{tail}")
        if c.kind == LocatorKind.TABLE_ANCHOR:
            return f.locator(f"xpath=//tr[td[normalize-space(.)={_xpath_literal(c.anchor_text)}]]/td[{c.column}]{tail}")
        raise ValueError(c.kind)

    def resolve(self, frame: str | None, candidate: LocatorCandidate) -> Match:
        try:
            f = self._frame(frame)
        except LookupError:
            return Match(count=0)
        route = urlparse(f.url).path or "/"
        if candidate.kind == LocatorKind.COORDINATES:
            text = f.evaluate("([x, y]) => { const el = document.elementFromPoint(x, y);"
                              " return el && el !== document.body ? (el.innerText || el.value || '').trim() : null; }",
                              [candidate.x, candidate.y])
            return Match(count=0 if text is None else 1, handle=("point", frame, candidate.x, candidate.y),
                         text=text or "", frame_route=route)
        loc = self._locator(f, candidate)
        visible = [loc.nth(i) for i in range(loc.count()) if loc.nth(i).is_visible()]
        if len(visible) != 1:
            return Match(count=len(visible), frame_route=route)
        return Match(count=1, handle=visible[0], text=self._text_of(visible[0]), frame_route=route,
                     submits_post=self._submits_post(visible[0]), target_url=self._target_url(visible[0]))

    def resolve_ref(self, ref: str) -> Match:
        """Discovery only: bind an observation ref. Never used by replay."""
        fname, eref = ref.split(":", 1)
        frame = None if fname == "top" else fname
        f = self._frame(frame)
        loc = f.locator(f"[data-cua-ref='{eref}']")
        n = loc.count()
        return Match(count=n, handle=loc.first if n == 1 else None, text=self._text_of(loc.first) if n == 1 else "",
                     frame_route=urlparse(f.url).path or "/", submits_post=n == 1 and self._submits_post(loc.first),
                     target_url=self._target_url(loc.first) if n == 1 else "")

    def element_at(self, x: float, y: float) -> tuple[ElementInfo, Match] | None:
        """Screenshot-coordinate path (no DOM refs needed): which frame and element is at page point (x, y)?"""
        for name in self._frame_names():
            f = self._frame(name)
            box = f.frame_element().bounding_box() if name else {"x": 0, "y": 0, "width": 1e9, "height": 1e9}
            if not box or not (box["x"] <= x < box["x"] + box["width"] and box["y"] <= y < box["y"] + box["height"]):
                continue
            f.evaluate("() => document.querySelectorAll('[data-cua-ref=pt]').forEach(e => e.removeAttribute('data-cua-ref'))")
            e = f.evaluate(_ELEMENT_AT_JS, [x - box["x"], y - box["y"]])
            if not e:
                return None
            info = ElementInfo(ref=f"{name or 'top'}:pt", frame=name, tag=e["tag"], role=e["role"], name=e["name"],
                               label=e["label"], column=e["column"], row_cells=e["rowCells"], x=e["x"], y=e["y"],
                               text=e["text"])
            return info, self.resolve_ref(info.ref)
        return None

    def _target_url(self, loc: Locator) -> str:
        try:
            return str(loc.evaluate(_TARGET_URL_JS) or "")
        except PlaywrightError:
            return ""

    def _submits_post(self, loc: Locator) -> bool:
        try:
            return bool(loc.evaluate(_SUBMITS_POST_JS))
        except PlaywrightError:
            return False

    @staticmethod
    def _text_of(loc: Locator) -> str:
        try:
            return loc.evaluate("el => (el.tagName === 'INPUT' ? (el.type === 'submit' || el.type === 'button' ? el.value : '')"
                                " : (el.innerText || '')).replace(/\\s+/g, ' ').trim()")
        except Exception:
            return ""

    # ---- act (guarded) -----------------------------------------------------------------

    def goto(self, route: str) -> None:
        self._guard()
        try:
            self.page.goto(self.base_url + route, wait_until="load")
        except PlaywrightError as e:
            raise ActionFailed(f"open {route}: {str(e).splitlines()[0][:150]}") from None

    def act(self, action: ActionType, match: Match, value: str | None = None) -> str | None:
        if action != ActionType.EXTRACT:
            self._guard()
        try:
            return self._act(action, match, value)
        except PlaywrightError as e:
            raise ActionFailed(str(e).splitlines()[0][:200]) from None

    def _act(self, action: ActionType, match: Match, value: str | None) -> str | None:
        h = match.handle
        if isinstance(h, tuple) and h[0] == "point":
            _, frame, x, y = h
            if action == ActionType.EXTRACT:
                return match.text
            if action == ActionType.SELECT:
                raise ActionFailed("SELECT cannot be performed through a coordinate fallback")
            off = self._frame(frame).frame_element().bounding_box() if frame else {"x": 0, "y": 0}
            self.page.mouse.click(off["x"] + x, off["y"] + y)
            if action == ActionType.FILL:
                self.page.keyboard.type(value or "")
            return None
        if action == ActionType.CLICK:
            h.click(timeout=self.action_timeout_ms)
        elif action == ActionType.FILL:
            h.fill(value or "", timeout=self.action_timeout_ms)
        elif action == ActionType.SELECT:
            h.select_option(value=value, timeout=self.action_timeout_ms)
        elif action == ActionType.EXTRACT:
            return h.inner_text(timeout=self.action_timeout_ms).strip()
        else:
            raise ActionFailed(f"unsupported action {action}")
        return None

    def read_value(self, match: Match) -> str:
        """Current value of a form control (or text of anything else). Read-only, not guarded."""
        h = match.handle
        if isinstance(h, tuple):
            return match.text
        try:
            return h.input_value(timeout=self.action_timeout_ms)
        except Exception:
            return h.inner_text(timeout=self.action_timeout_ms).strip()

    def click_role(self, frame: str | None, role: str, name: str) -> None:
        self._guard()
        try:
            self._frame(frame).get_by_role(role, name=name, exact=True).first.click(timeout=self.action_timeout_ms)
        except (PlaywrightError, LookupError) as e:
            raise ActionFailed(f"dismiss {role} '{name}': {str(e).splitlines()[0][:150]}") from None

    def reload(self, frame: str | None) -> None:
        self._guard()
        try:
            f = self._frame(frame)
            if frame is None:
                self.page.reload(wait_until="load")
            else:
                f.goto(f.url, wait_until="load")
        except (PlaywrightError, LookupError) as e:
            raise ActionFailed(f"reload: {str(e).splitlines()[0][:150]}") from None

    # ---- snapshot --------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        masks: list[Locator] = []
        for f in self.page.frames:
            masks.append(f.locator("input[type=password]"))
            for label in SENSITIVE_LABELS:
                masks.append(f.locator(f"xpath=//td[normalize-space(.)={_xpath_literal(label)}]/following-sibling::td[1]"))
            for pat in Redactor.mask_patterns():
                masks.append(f.get_by_text(re.compile(pat.pattern)))
        try:
            png = self.page.screenshot(mask=masks, animations="disabled")
        except Exception:
            png = b""
        text = "\n".join(f"[{n or 'top'}]\n{self.frame_text(n)}" for n in self._frame_names())
        return Snapshot(png=png, text=self.redactor.text(text))
