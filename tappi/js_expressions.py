"""JavaScript expressions injected into the browser page via CDP.

These handle element indexing, shadow DOM piercing, and text extraction.
Kept in a separate module so the core stays clean.
"""

import json


def elements_js(selector: str | None = None) -> str:
    """Generate JS that indexes all interactive elements on the page.

    Returns a JSON array of {label, desc} objects. Each element gets a
    `data-bpy-idx` attribute for later click/type targeting.

    Pierces shadow DOM boundaries automatically.
    """
    sel_json = json.dumps(selector)
    return f"""
    (() => {{
      // ── Deep shadow DOM helpers ──
      function deepClearStamps(root) {{
        root.querySelectorAll('[data-bpy-idx]').forEach(el => el.removeAttribute('data-bpy-idx'));
        root.querySelectorAll('*').forEach(el => {{
          if (el.shadowRoot) deepClearStamps(el.shadowRoot);
        }});
      }}

      function deepQueryAll(root, selectors) {{
        const results = [];
        try {{ results.push(...root.querySelectorAll(selectors)); }} catch(e) {{}}
        const allEls = root.querySelectorAll('*');
        for (const el of allEls) {{
          if (el.shadowRoot) {{
            results.push(...deepQueryAll(el.shadowRoot, selectors));
          }}
        }}
        return results;
      }}

      function deepQueryStamp(root, idx) {{
        const found = root.querySelector('[data-bpy-idx="' + idx + '"]');
        if (found) return found;
        const allEls = root.querySelectorAll('*');
        for (const el of allEls) {{
          if (el.shadowRoot) {{
            const deep = deepQueryStamp(el.shadowRoot, idx);
            if (deep) return deep;
          }}
        }}
        return null;
      }}

      // Expose deepQueryStamp globally for click/type/etc
      window.__bpyDeepQuery = (idx) => deepQueryStamp(document, idx);

      // Clear old stamps first (including inside shadow DOMs)
      deepClearStamps(document);

      const sel = {sel_json};
      const root = sel ? document.querySelector(sel) : document;
      if (!root) return JSON.stringify({{ error: "Selector not found: " + sel }});

      const interactive = deepQueryAll(root,
        'a[href], button, input, select, textarea, [role="button"], [role="link"], ' +
        '[role="tab"], [role="menuitem"], [role="checkbox"], [role="radio"], ' +
        '[role="textbox"], [onclick], [tabindex]:not([tabindex="-1"]), details > summary, ' +
        '[contenteditable="true"]'
      );

      // Detect topmost modal/dialog to scope de-duplication
      const allDialogs = [...document.querySelectorAll('[role=dialog], [role=presentation], [aria-modal=true]')]
        .filter(d => d.offsetParent !== null || getComputedStyle(d).position === 'fixed');
      const realDialogs = allDialogs.filter(d => d.getAttribute('role') === 'dialog' || d.getAttribute('aria-modal') === 'true');
      const topDialog = (realDialogs.length > 0 ? realDialogs[realDialogs.length - 1] : allDialogs[allDialogs.length - 1]) || null;

      const seen = new Set();
      const results = [];

      // Sort: elements inside the top dialog come first (higher priority)
      const sorted = [...interactive].sort((a, b) => {{
        const aInDialog = topDialog && topDialog.contains(a) ? 0 : 1;
        const bInDialog = topDialog && topDialog.contains(b) ? 0 : 1;
        return aInDialog - bInDialog;
      }});

      for (const el of sorted) {{
        if (el.offsetParent === null && el.tagName !== 'BODY' && getComputedStyle(el).position !== 'fixed') continue;

        const isDisabled = el.disabled || el.getAttribute('aria-disabled') === 'true';

        const tag = el.tagName.toLowerCase();
        const type = el.type || '';
        const role = el.getAttribute('role') || '';
        const text = (el.textContent || '').trim().slice(0, 80).replace(/\\s+/g, ' ');
        const ariaLabel = el.getAttribute('aria-label') || '';
        const placeholder = el.placeholder || '';
        const href = el.href || '';
        const name = el.name || '';
        const value = (tag === 'input' || tag === 'select' || tag === 'textarea')
          ? (el.value || '').slice(0, 40) : '';

        let label = '';
        if (tag === 'a') label = 'link';
        else if (tag === 'button' || role === 'button') label = 'button';
        else if (tag === 'input') label = type ? 'input:' + type : 'input';
        else if (tag === 'select') label = 'select';
        else if (tag === 'textarea') label = 'textarea';
        else if (role === 'textbox') label = 'textbox';
        else if (role) label = role;
        else label = tag;
        if (isDisabled) label += ':disabled';

        let desc = ariaLabel || text || placeholder || name || '';
        if (value && !desc.includes(value)) desc += desc ? ' [' + value + ']' : value;
        if (tag === 'a' && href && !href.startsWith('javascript:')) {{
          const short = href;
          desc += desc ? ' → ' + short : short;
        }}

        // De-dup key includes dialog scope
        const scope = (topDialog && topDialog.contains(el)) ? 'modal' : 'page';
        const key = scope + '|' + label + '|' + desc;
        if (seen.has(key)) continue;
        seen.add(key);

        el.setAttribute('data-bpy-idx', results.length);
        results.push({{ label, desc: desc.slice(0, 120) }});
      }}

      return JSON.stringify(results);
    }})()
    """


def check_indexed_js() -> str:
    """Check if elements are currently indexed on the page."""
    return (
        "!!(document.querySelector('[data-bpy-idx]') || "
        "(window.__bpyDeepQuery && window.__bpyDeepQuery(0)))"
    )


def click_info_js(index: int) -> str:
    """Get element info and position for clicking."""
    return f"""
    (() => {{
      const el = (window.__bpyDeepQuery && window.__bpyDeepQuery({index})) || document.querySelector('[data-bpy-idx="{index}"]');
      if (!el) return JSON.stringify({{ error: 'Element [{index}] not found. Run: elements' }});
      el.scrollIntoView({{ block: 'center' }});
      const rect = el.getBoundingClientRect();
      const label = (el.getAttribute('role') || el.tagName.toLowerCase());
      const desc = (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 80);
      return JSON.stringify({{
        x: rect.x + rect.width / 2,
        y: rect.y + rect.height / 2,
        label, desc
      }});
    }})()
    """


def type_info_js(index: int) -> str:
    """Get element info and verify it's a text input."""
    return f"""
    (() => {{
      const el = (window.__bpyDeepQuery && window.__bpyDeepQuery({index})) || document.querySelector('[data-bpy-idx="{index}"]');
      if (!el) return JSON.stringify({{ error: 'Element [{index}] not found. Run: elements' }});
      const tag = el.tagName.toLowerCase();
      const ce = el.isContentEditable;
      const role = el.getAttribute('role') || '';
      const type = el.type || '';
      const typeable = tag === 'input' || tag === 'textarea' || ce || role === 'textbox';
      if (!typeable) return JSON.stringify({{ error: 'Element [{index}] is a ' + tag + ' (' + (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 40) + '), not a text input. Use click instead?' }});
      el.scrollIntoView({{ block: 'center' }});
      const rect = el.getBoundingClientRect();
      return JSON.stringify({{ ok: true, tag, ce, x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 }});
    }})()
    """


def clear_contenteditable_js(index: int) -> str:
    """Select all content in a contenteditable element for deletion."""
    return f"""
    (() => {{
      const el = (window.__bpyDeepQuery && window.__bpyDeepQuery({index})) || document.querySelector('[data-bpy-idx="{index}"]');
      if (el) {{
        const range = document.createRange();
        range.selectNodeContents(el);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      }}
    }})()
    """


def clear_input_js(index: int) -> str:
    """Clear value of an input/textarea element."""
    return f"""
    (() => {{
      const el = (window.__bpyDeepQuery && window.__bpyDeepQuery({index})) || document.querySelector('[data-bpy-idx="{index}"]');
      if (el) el.value = '';
    }})()
    """


def set_input_value_js(index: int, text: str) -> str:
    """Set input value with proper React/Vue event dispatching."""
    text_json = json.dumps(text)
    return f"""
    (() => {{
      const el = (window.__bpyDeepQuery && window.__bpyDeepQuery({index})) || document.querySelector('[data-bpy-idx="{index}"]');
      if (el && !el.isContentEditable) {{
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
          || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
        if (setter) setter.call(el, {text_json});
        else el.value = {text_json};
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
      }}
    }})()
    """


def extract_text_js(selector: str | None = None) -> str:
    """Extract visible text from page, piercing shadow DOM."""
    sel_json = json.dumps(selector)
    return f"""
    (() => {{
      const sel = {sel_json};
      const root = sel ? document.querySelector(sel) : document.body;
      if (!root) return 'Selector not found: ' + sel;

      const MAX = 8000;
      const chunks = [];
      let totalLen = 0;

      function extractText(node) {{
        if (totalLen >= MAX) return;
        if (node.nodeType === 3) {{
          const t = node.textContent.trim();
          if (t.length > 0) {{
            const parent = node.parentElement;
            if (parent) {{
              const tag = parent.tagName;
              if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG'].includes(tag)) return;
              if (parent.offsetParent === null && getComputedStyle(parent).position !== 'fixed') return;
            }}
            chunks.push(t);
            totalLen += t.length;
          }}
          return;
        }}
        if (node.nodeType === 1) {{
          if (node.shadowRoot) {{
            for (const child of node.shadowRoot.childNodes) extractText(child);
          }}
          for (const child of node.childNodes) extractText(child);
        }}
      }}

      extractText(root);
      let text = chunks.join(' ').replace(/\\s+/g, ' ').trim();
      if (text.length > MAX) text = text.slice(0, MAX) + '... (truncated)';
      return text || '(empty page)';
    }})()
    """


def get_html_js(selector: str) -> str:
    """Get outerHTML of an element by CSS selector."""
    sel_json = json.dumps(selector)
    return f"""
    (() => {{
      const el = document.querySelector({sel_json});
      if (!el) return 'Selector not found: ' + {sel_json};
      const html = el.outerHTML;
      return html.length > 10000 ? html.slice(0, 10000) + '... (truncated)' : html;
    }})()
    """
