// Markdown renderer setup
const md = (typeof marked !== 'undefined') ? marked : null;
if (md && md.setOptions) {
  md.setOptions({ breaks: true, gfm: true });
}
function renderMd(text) {
  if (!text) return '';
  if (md && md.parse) return md.parse(text);
  // Fallback: escape HTML and preserve newlines
  var s = text.split('&').join('&amp;');
  s = s.split(String.fromCharCode(60)).join('&lt;');
  s = s.split('>').join('&gt;');
  return s.split('\n').join('<br>');
}

const chatEl = document.getElementById('chat-messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send');
const statusEl = document.getElementById('status');
let ws, providers = {}, currentCfg = {};

// ── Folder Picker ──
let _folderTarget = null;  // id of the input to fill
let _folderCurrent = '~';

async function openFolderPicker(inputId) {
  _folderTarget = inputId;
  const existing = document.getElementById(inputId).value.trim();
  _folderCurrent = existing || '~';
  await loadFolder(_folderCurrent);
  document.getElementById('folder-modal').classList.add('open');
}

function closeFolderPicker() {
  document.getElementById('folder-modal').classList.remove('open');
  _folderTarget = null;
}

async function loadFolder(path) {
  const list = document.getElementById('folder-list');
  list.innerHTML = '<div class="empty">Loading...</div>';
  try {
    const res = await fetch('/api/browse-dirs?path=' + encodeURIComponent(path));
    const data = await res.json();
    if (data.error) { list.innerHTML = `<div class="empty">${data.error}</div>`; return; }
    _folderCurrent = data.current;
    document.getElementById('folder-current-path').textContent = data.current;
    if (!data.dirs.length) {
      list.innerHTML = '<div class="empty">No subdirectories</div>';
      return;
    }
    list.innerHTML = data.dirs.map(d =>
      `<div class="folder-item" onclick="loadFolder('${(data.current + '/' + d).replace(/'/g, "\'")}')">` +
      `<span class="icon">📁</span>${d}</div>`
    ).join('');
  } catch (e) {
    list.innerHTML = `<div class="empty">Error: ${e.message}</div>`;
  }
}

function folderUp() {
  const parts = _folderCurrent.split('/');
  if (parts.length > 1) {
    parts.pop();
    loadFolder(parts.join('/') || '/');
  }
}

function selectFolder() {
  if (_folderTarget) {
    document.getElementById(_folderTarget).value = _folderCurrent;
  }
  closeFolderPicker();
}

// Close folder modal on overlay click
document.addEventListener('click', (e) => {
  if (e.target.id === 'folder-modal') closeFolderPicker();
});

// ── Model Picker Component ──
// A searchable combo-box that replaces the old <select>
class ModelPicker {
  constructor(containerId, opts = {}) {
    this.container = document.getElementById(containerId);
    this.allModels = [];
    this.filtered = [];
    this.selectedValue = '';
    this.highlightIdx = -1;
    this.onSelect = opts.onSelect || (() => {});
    this._render();
  }

  _render() {
    this.container.innerHTML = `
      <div class="field">
        <label>MODEL</label>
        <div class="model-search-wrap">
          <input type="text" class="model-search-input" placeholder="Search models..." autocomplete="off">
          <div class="model-dropdown"></div>
          <div class="model-count"></div>
        </div>
      </div>`;
    this.inputEl = this.container.querySelector('.model-search-input');
    this.dropdown = this.container.querySelector('.model-dropdown');
    this.countEl = this.container.querySelector('.model-count');

    this.inputEl.addEventListener('focus', () => this._showDropdown());
    this.inputEl.addEventListener('input', () => this._onInput());
    this.inputEl.addEventListener('keydown', (e) => this._onKeydown(e));
    document.addEventListener('click', (e) => {
      if (!this.container.contains(e.target)) this._hideDropdown();
    });
  }

  setModels(models, defaultValue) {
    this.allModels = models;
    this.filtered = models;
    this._updateCount();
    if (defaultValue) {
      this.selectedValue = defaultValue;
      const m = models.find(m => m.id === defaultValue);
      if (m) {
        this.inputEl.value = m.id;
      } else {
        this.inputEl.value = defaultValue;
      }
    }
    this._renderOptions();
  }

  getValue() {
    return this.selectedValue || this.inputEl.value.trim();
  }

  setLoading() {
    this.allModels = [];
    this.filtered = [];
    this.inputEl.placeholder = 'Loading models...';
    this.countEl.textContent = '';
    this.dropdown.innerHTML = '<div style="padding:8px 12px;color:var(--text-dim);font-size:12px">Loading...</div>';
  }

  _onInput() {
    const q = this.inputEl.value.toLowerCase().trim();
    if (!q) {
      this.filtered = this.allModels;
    } else {
      this.filtered = this.allModels.filter(m =>
        m.id.toLowerCase().includes(q) || (m.name || '').toLowerCase().includes(q)
      );
    }
    this.highlightIdx = -1;
    this._renderOptions();
    this._showDropdown();
    this._updateCount();
    // If user types a model ID directly, accept it
    this.selectedValue = this.inputEl.value.trim();
  }

  _onKeydown(e) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      this.highlightIdx = Math.min(this.highlightIdx + 1, this.filtered.length - 1);
      this._renderOptions();
      this._scrollToHighlighted();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      this.highlightIdx = Math.max(this.highlightIdx - 1, 0);
      this._renderOptions();
      this._scrollToHighlighted();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (this.highlightIdx >= 0 && this.highlightIdx < this.filtered.length) {
        this._select(this.filtered[this.highlightIdx]);
      }
      this._hideDropdown();
    } else if (e.key === 'Escape') {
      this._hideDropdown();
    }
  }

  _select(model) {
    this.selectedValue = model.id;
    this.inputEl.value = model.id;
    this._hideDropdown();
    this.onSelect(model);
  }

  _showDropdown() {
    if (this.filtered.length) this.dropdown.classList.add('open');
  }
  _hideDropdown() {
    this.dropdown.classList.remove('open');
  }

  _scrollToHighlighted() {
    const el = this.dropdown.querySelector('.highlighted');
    if (el) el.scrollIntoView({ block: 'nearest' });
  }

  _updateCount() {
    const total = this.allModels.length;
    const shown = this.filtered.length;
    if (total > 20) {
      this.countEl.textContent = shown === total
        ? `${total} models available — type to search`
        : `${shown} of ${total} models`;
    } else {
      this.countEl.textContent = '';
    }
    this.inputEl.placeholder = total > 20 ? 'Type to search models...' : 'Search or select a model...';
  }

  _renderOptions() {
    // Cap rendered items at 100 for performance
    const toRender = this.filtered.slice(0, 100);
    this.dropdown.innerHTML = toRender.map((m, i) => {
      const hl = i === this.highlightIdx ? ' highlighted' : '';
      const nameStr = m.name && m.name !== m.id ? m.name : '';
      const ctxStr = m.context ? `${Math.round(m.context/1000)}k ctx` : '';
      const meta = [nameStr, ctxStr].filter(Boolean).join(' · ');
      return `<div class="model-opt${hl}" data-idx="${i}">
        <span class="model-id">${m.id}</span>
        ${meta ? `<span class="model-meta">${meta}</span>` : ''}
      </div>`;
    }).join('');

    if (this.filtered.length > 100) {
      this.dropdown.innerHTML += `<div style="padding:6px 12px;color:var(--text-dim);font-size:11px;text-align:center">
        ${this.filtered.length - 100} more — refine your search</div>`;
    }

    if (!this.filtered.length) {
      this.dropdown.innerHTML = '<div style="padding:8px 12px;color:var(--text-dim);font-size:12px">No models match</div>';
    }

    // Click handlers
    this.dropdown.querySelectorAll('.model-opt').forEach(el => {
      el.addEventListener('click', () => {
        const idx = parseInt(el.dataset.idx);
        this._select(this.filtered[idx]);
      });
    });
  }
}

// ── Provider Fields Rendering ──
function renderProviderFields(provider, containerId, credentials, providerFields) {
  const el = document.getElementById(containerId);
  const info = providers[provider] || {};
  const creds = (credentials || {})[provider] || {};

  if (info.fields) {
    // Multi-field provider — show env var hints and detection status
    el.innerHTML = info.fields.map(f => {
      const fieldCred = (creds.fields || {})[f.key] || {};
      const envVar = f.env || '';
      const altEnvs = f.alt_env || [];
      const allEnvs = [envVar, ...altEnvs].filter(Boolean);
      const envHint = allEnvs.length
        ? `<span style="font-size:10px;color:var(--text-dim);margin-left:4px">env: ${allEnvs.map(e => '$' + e).join(' / ')}</span>`
        : '';

      let statusHtml = '';
      if (fieldCred.configured) {
        const sourceLabel = fieldCred.source.startsWith('env')
          ? '🌐 ' + fieldCred.source  // "env (AWS_DEFAULT_REGION)"
          : '💾 ' + fieldCred.source;  // "config"
        statusHtml = `<div class="key-status configured">✓ ${fieldCred.masked} — ${sourceLabel}</div>`;
      } else {
        const envName = fieldCred.env_var || envVar;
        statusHtml = envName
          ? `<div class="key-status missing">Not set — provide below or set <code style="font-size:10px;background:var(--bg);padding:1px 4px;border-radius:3px">$${envName}</code></div>`
          : `<div class="key-status missing">Not configured</div>`;
      }

      const inputType = f.secret ? 'password' : 'text';
      const placeholder = fieldCred.configured
        ? 'Leave empty to keep current'
        : (f.placeholder || '');
      // Pre-fill non-secret fields from providerFields
      const prefill = !f.secret && providerFields && providerFields[provider]
        ? (providerFields[provider][f.key] || '') : '';
      return `<div class="field">
        <label>${f.label} ${envHint}</label>
        <input type="${inputType}" data-field-key="${f.key}" placeholder="${placeholder}" value="${prefill}" autocomplete="off">
        ${statusHtml}
      </div>`;
    }).join('');

    // Add resolution order note + credential check button
    el.innerHTML += `<div style="margin-top:10px;padding:10px;background:var(--bg);border-radius:6px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <div style="flex:1;min-width:200px">
        <p style="font-size:11px;color:var(--text-dim);margin:0">
          ⚡ <strong>Resolution:</strong> tappi config → env var → CLI credentials file (boto3/gcloud/az).
          Refresh after running <code style="font-size:10px;background:var(--surface);padding:1px 4px;border-radius:3px">ada</code>,
          <code style="font-size:10px;background:var(--surface);padding:1px 4px;border-radius:3px">aws sso login</code>,
          <code style="font-size:10px;background:var(--surface);padding:1px 4px;border-radius:3px">gcloud auth</code>, etc.
        </p>
      </div>
      <button class="btn secondary" onclick="checkCredentials('${provider}')" style="padding:6px 14px;font-size:12px;white-space:nowrap" id="cred-check-btn-${provider}">
        🔄 Check Credentials
      </button>
    </div>
    <div id="cred-check-result-${provider}" style="display:none;margin-top:8px;padding:10px;border-radius:6px;font-size:12px"></div>`;
    return;
  }

  // Single API key provider
  const keySection = document.getElementById(containerId.replace('provider-fields', 'key-section'));
  if (keySection) {
    const isOauth = info.is_oauth;
    const label = isOauth ? 'OAuth Token' : 'API Key';
    const envVar = info.env_key || '';
    const envHint = envVar ? `<span style="font-size:10px;color:var(--text-dim);margin-left:4px">env: $${envVar}</span>` : '';
    const placeholder = creds.configured
      ? 'Leave empty to keep current'
      : (isOauth ? 'sk-ant-oat01-...' : 'sk-...');

    let statusHtml = '';
    if (creds.configured) {
      const sourceIcon = creds.source === 'env' ? '🌐' : '💾';
      statusHtml = `<div class="key-status configured">✓ ${creds.masked} — ${sourceIcon} ${creds.source}</div>`;
    } else {
      statusHtml = envVar
        ? `<div class="key-status missing">Not set — provide below or set <code style="font-size:10px;background:var(--bg);padding:1px 4px;border-radius:3px">$${envVar}</code></div>`
        : `<div class="key-status missing">Not configured</div>`;
    }

    const hint = isOauth
      ? '<p style="font-size:11px;color:var(--text-dim);margin-top:4px">From your Claude Max/Pro subscription. Same token Claude Code uses.</p>'
      : '';

    keySection.innerHTML = `<div class="field">
      <label>${label} ${envHint}</label>
      <input type="password" id="${containerId.replace('provider-fields','key')}" placeholder="${placeholder}" autocomplete="off">
      ${statusHtml}
      ${hint}
    </div>`;
  }
  el.innerHTML = '';
}

// ── Credential Check ──
async function checkCredentials(provider) {
  const btn = document.getElementById('cred-check-btn-' + provider);
  const resultEl = document.getElementById('cred-check-result-' + provider);
  if (!btn || !resultEl) return;

  btn.disabled = true;
  btn.textContent = '🔄 Checking...';
  resultEl.style.display = 'block';
  resultEl.style.background = 'rgba(88,166,255,0.08)';
  resultEl.style.color = 'var(--accent)';
  resultEl.textContent = 'Resolving credentials...';

  try {
    const res = await fetch('/api/credentials/check', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider }),
    });
    const data = await res.json();

    if (data.resolved) {
      resultEl.style.background = 'rgba(63,185,80,0.1)';
      resultEl.style.color = 'var(--success)';
      let text = '✅ Credentials resolved — source: ' + data.source;
      if (data.details) {
        const parts = [];
        if (data.details.region) parts.push('region: ' + data.details.region);
        if (data.details.access_key_prefix) parts.push('key: ' + data.details.access_key_prefix);
        if (data.details.method) parts.push('method: ' + data.details.method);
        if (data.details.project) parts.push('project: ' + data.details.project);
        if (parts.length) text += ' (' + parts.join(', ') + ')';
      }
      resultEl.textContent = text;
    } else {
      resultEl.style.background = 'rgba(248,81,73,0.1)';
      resultEl.style.color = 'var(--danger)';
      resultEl.textContent = '❌ ' + (data.error || 'No credentials found');
    }

    // Also refresh the credential status display
    const cres = await fetch('/api/config');
    currentCfg = await cres.json();
  } catch(e) {
    resultEl.style.background = 'rgba(248,81,73,0.1)';
    resultEl.style.color = 'var(--danger)';
    resultEl.textContent = '❌ Check failed: ' + e.message;
  }

  btn.disabled = false;
  btn.textContent = '🔄 Check Credentials';
}

// ── Init ──
async function init() {
  const pres = await fetch('/api/providers');
  providers = await pres.json();

  const cres = await fetch('/api/config');
  currentCfg = await cres.json();

  if (!currentCfg.configured) {
    await initSetupPage(currentCfg);
    showPage('setup');
  } else {
    connect();
    loadVersionInfo(currentCfg);
    loadSessions();
    loadProfileSwitcher();
    showPage('chat');
    // Sync decompose toggle with config
    const dt = document.getElementById('decompose-toggle');
    if (dt) dt.checked = currentCfg.decompose_enabled !== false;
  }
}

function loadVersionInfo(cfg) {
  document.getElementById('version-info').textContent =
    `${providers[cfg.provider]?.name || cfg.provider} · ${(cfg.model || '').split('/').pop() || ''}`;
}

// ── Sessions ──
async function loadSessions() {
  try {
    const res = await fetch('/api/sessions');
    const data = await res.json();
    const el = document.getElementById('sessions-list');
    const sessions = data.sessions || [];
    if (!sessions.length) {
      el.innerHTML = '<div style="padding:4px 16px;font-size:11px;color:var(--text-dim)">No saved chats</div>';
      return;
    }
    el.innerHTML = sessions.slice(0, 10).map(s => {
      const title = s.title || 'Untitled';
      const active = _get_agent_session_id() === s.id ? ' active' : '';
      return `<div class="session-item${active}" onclick="loadSession('${s.id}')" title="${title}">${title}</div>`;
    }).join('');
  } catch(e) {}
}

function _get_agent_session_id() {
  return window._currentSessionId || '';
}

async function loadSession(id) {
  const res = await fetch('/api/sessions/load', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ session_id: id })
  });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }

  window._currentSessionId = id;

  // Reload chat history
  const hres = await fetch('/api/history');
  const hdata = await hres.json();
  const chatEl = document.getElementById('chat-messages');
  chatEl.innerHTML = '';
  (hdata.messages || []).forEach(m => {
    if (m.role === 'user' && m.content) addMsg(m.content, 'user');
    else if (m.role === 'assistant' && m.content) addMsg(m.content, 'agent');
  });

  updateTokenBar(data.token_usage);
  loadSessions();
  showPage('chat');
}

// ── Token Usage ──
function updateTokenBar(usage) {
  if (!usage) return;
  const wrap = document.getElementById('token-bar-wrap');
  const fill = document.getElementById('token-fill');
  const label = document.getElementById('token-label');
  const pct = document.getElementById('token-pct');

  wrap.style.display = 'block';
  const pctVal = Math.min(usage.usage_percent || 0, 100);
  fill.style.width = pctVal + '%';
  fill.className = 'fill ' + (usage.critical ? 'crit' : usage.warning ? 'warn' : 'ok');
  label.textContent = `${(usage.context_used || usage.total_tokens || 0).toLocaleString()} tokens`;
  pct.textContent = pctVal + '%';

  // Show warning banner
  const warn = document.getElementById('context-warning');
  const warnText = document.getElementById('context-warning-text');
  if (usage.critical) {
    warn.style.display = 'block';
    warn.className = 'context-warning critical';
    warnText.textContent = `Context ${pctVal}% full (${(usage.context_used||usage.total_tokens||0).toLocaleString()} / ${(usage.context_limit||0).toLocaleString()}). Start a new chat for best results.`;
  } else if (usage.warning) {
    warn.style.display = 'block';
    warn.className = 'context-warning';
    warnText.textContent = `Context ${pctVal}% full. Consider starting a new chat soon.`;
  } else {
    warn.style.display = 'none';
  }
}

// ── Setup Wizard ──
let wizardStep = 1;
function wizardNext(step) {
  // Validation per step
  if (step === 1) {
    const p = document.getElementById('setup-provider').value;
    if (!p) { showSetupError('Please select a provider.'); return; }
  }
  clearSetupError();
  wizardStep = step + 1;
  renderWizard();
}
function wizardBack(step) {
  wizardStep = step - 1;
  renderWizard();
}
function renderWizard() {
  for (let i = 1; i <= 6; i++) {
    const sec = document.getElementById('wizard-' + i);
    sec.classList.toggle('active', i === wizardStep);
    const stepEl = document.querySelector(`.wizard-step[data-step="${i}"]`);
    stepEl.className = 'wizard-step' + (i < wizardStep ? ' done' : i === wizardStep ? ' current' : '');
  }
}
function showSetupError(msg) {
  const el = document.getElementById('setup-error');
  el.textContent = msg; el.style.display = 'block';
}
function clearSetupError() {
  document.getElementById('setup-error').style.display = 'none';
}

// Tool-use filter for setup
function onSetupToolFilterChange() {
  const p = document.getElementById('setup-provider').value;
  if (p) reloadSetupModels(p);
}
async function reloadSetupModels(provider) {
  const toolOnly = document.getElementById('setup-tool-filter')?.checked || false;
  const info = providers[provider] || {};
  setupModelPicker.setLoading();
  try {
    let url = '/api/models/' + provider;
    const params = [];
    if (toolOnly) params.push('tool_use_only=true');
    if (params.length) url += '?' + params.join('&');
    const res = await fetch(url);
    const data = await res.json();
    setupModelPicker.setModels(data.models || [], info.default_model);
  } catch(e) {
    setupModelPicker.setModels([], null);
  }
}

// ── Research ──
// ── Navigation ──
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('#sidebar nav a').forEach(a => a.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  const navEl = document.querySelector(`[data-page="${name}"]`);
  if (navEl) navEl.classList.add('active');
  if (name === 'profiles') loadProfiles();
  if (name === 'jobs') loadJobs();
  if (name === 'settings') loadSettingsPage();
  if (name === 'setup') initSetupPage();
  if (name === 'chat') { loadSessions(); loadProfileSwitcher(); }
  // cron-run page is opened via openCronRun(), not showPage
}

// ── Setup Page ──
let setupModelPicker;
async function initSetupPage(cfg) {
  cfg = cfg || currentCfg;
  const sel = document.getElementById('setup-provider');
  sel.innerHTML = '<option value="">— Select —</option>' +
    Object.entries(providers).map(([k,v]) => {
      const tag = v.is_oauth ? ' ⭐ no API cost' : '';
      return `<option value="${k}">${v.name}${tag}</option>`;
    }).join('');

  // Init model picker
  if (!setupModelPicker) {
    setupModelPicker = new ModelPicker('setup-model-picker');
  }

  const wsInput = document.getElementById('setup-workspace');
  if (!wsInput.value) wsInput.value = cfg?.workspace || '~/tappi-workspace';

  await loadSetupProfiles();

  if (cfg?.provider) {
    sel.value = cfg.provider;
    await onSetupProviderChange();
    if (cfg.model) {
      const custom = document.getElementById('setup-model-custom');
      setupModelPicker.setModels(setupModelPicker.allModels, cfg.model);
      if (!setupModelPicker.allModels.find(m => m.id === cfg.model)) {
        custom.value = cfg.model;
      }
    }
  }
}

async function loadSetupProfiles() {
  const res = await fetch('/api/profiles');
  const data = await res.json();
  const sel = document.getElementById('setup-browser-profile');
  const profiles = data.profiles || [];
  if (!profiles.length) {
    sel.innerHTML = '<option value="default">default (will be created)</option>';
  } else {
    sel.innerHTML = profiles.map(p =>
      `<option value="${p.name}">${p.name} (port ${p.port})</option>`
    ).join('');
  }
}

async function onSetupProviderChange() {
  const p = document.getElementById('setup-provider').value;
  const info = providers[p] || {};
  const note = document.getElementById('setup-provider-note');

  // Show note
  if (info.note) { note.textContent = info.note; note.style.display = 'block'; }
  else { note.style.display = 'none'; }

  // Render credential fields
  renderProviderFields(p, 'setup-provider-fields', currentCfg.credentials, currentCfg.provider_fields);

  // For single-key providers, render key section
  if (!info.fields) {
    const keySection = document.getElementById('setup-key-section');
    const isOauth = info.is_oauth;
    const creds = (currentCfg.credentials || {})[p] || {};
    const label = isOauth ? 'OAuth Token' : 'API Key';
    const envVar = info.env_key || '';
    const envHint = envVar ? `<span style="font-size:10px;color:var(--text-dim);margin-left:4px">env: $${envVar}</span>` : '';
    const placeholder = creds.configured ? 'Leave empty to keep current' : (isOauth ? 'sk-ant-oat01-...' : 'sk-...');
    let statusHtml = '';
    if (creds.configured) {
      const sourceIcon = creds.source === 'env' ? '🌐' : '💾';
      statusHtml = `<div class="key-status configured">✓ ${creds.masked} — ${sourceIcon} ${creds.source}</div>`;
    } else if (envVar) {
      statusHtml = `<div class="key-status missing">Not set — provide below or set <code style="font-size:10px;background:var(--bg);padding:1px 4px;border-radius:3px">$${envVar}</code></div>`;
    }
    const hint = isOauth
      ? '<p style="font-size:11px;color:var(--text-dim);margin-top:4px">From your Claude Max/Pro subscription.</p>'
      : '';
    keySection.innerHTML = `<div class="field">
      <label>${label} ${envHint}</label>
      <input type="password" id="setup-key" placeholder="${placeholder}" autocomplete="off">
      ${statusHtml}${hint}
    </div>`;
  } else {
    document.getElementById('setup-key-section').innerHTML = '';
  }

  // Fetch models (with tool-use filter if checked)
  await reloadSetupModels(p);
}

async function loadModelsForPicker(provider, picker, defaultModel, apiKey) {
  picker.setLoading();
  try {
    let url = '/api/models/' + provider;
    if (apiKey) url += '?api_key=' + encodeURIComponent(apiKey);
    const res = await fetch(url);
    const data = await res.json();
    picker.setModels(data.models || [], defaultModel);
  } catch(e) {
    picker.setModels([], null);
  }
}

// Debounced key input → refresh models for setup
document.addEventListener('input', function(e) {
  if (e.target.id !== 'setup-key') return;
  clearTimeout(window._setupKeyTimer);
  window._setupKeyTimer = setTimeout(async () => {
    const p = document.getElementById('setup-provider').value;
    const key = e.target.value.trim();
    if (key.length > 10 && p) {
      await loadModelsForPicker(p, setupModelPicker, providers[p]?.default_model, key);
    }
  }, 800);
});

async function setupLaunchBrowser() {
  const profile = document.getElementById('setup-browser-profile').value || 'default';
  const btn = document.getElementById('setup-launch-browser');
  const status = document.getElementById('setup-browser-status');
  btn.disabled = true;
  btn.textContent = '🌍 Opening...';
  try {
    const res = await fetch('/api/profiles/launch', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name: profile }),
    });
    const data = await res.json();
    if (data.error) { status.textContent = 'Error: ' + data.error; return; }
    btn.textContent = '🌍 Browser Open';
    status.textContent = data.status === 'already_running' ? 'Already running — log in to your accounts, then click Next' : 'Browser launched — log in to your accounts, then click Next';
  } catch(e) { status.textContent = 'Failed: ' + e; }
  finally { setTimeout(() => { btn.disabled = false; }, 2000); }
}

async function setupCreateProfile() {
  const nameInput = document.getElementById('setup-new-profile');
  const name = nameInput.value.trim();
  if (!name) return;
  const res = await fetch('/api/profiles', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ name })
  });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }
  nameInput.value = '';
  await loadSetupProfiles();
  // Use the actual profile name from the API (may be sanitized/lowercased)
  const createdName = data.profile?.name || name.toLowerCase();
  document.getElementById('setup-browser-profile').value = createdName;
}

async function submitSetup() {
  const btn = document.getElementById('setup-submit');
  const errEl = document.getElementById('setup-error');
  errEl.style.display = 'none';

  const provider = document.getElementById('setup-provider').value;
  if (!provider) { errEl.textContent = 'Please select a provider.'; errEl.style.display = 'block'; return; }

  const info = providers[provider] || {};
  const modelCustom = document.getElementById('setup-model-custom').value.trim();
  const model = modelCustom || setupModelPicker.getValue();
  const workspace = document.getElementById('setup-workspace').value.trim() || '~/tappi-workspace';
  const browser_profile = document.getElementById('setup-browser-profile').value || 'default';
  const shell_enabled = document.getElementById('setup-shell').checked;

  const body = { provider, model, workspace, browser_profile, shell_enabled };

  // Collect credentials
  if (info.fields) {
    const fieldInputs = document.querySelectorAll('#setup-provider-fields [data-field-key]');
    fieldInputs.forEach(inp => {
      const val = inp.value.trim();
      if (val) body[inp.dataset.fieldKey] = val;
    });
  } else {
    const keyEl = document.getElementById('setup-key');
    if (keyEl && keyEl.value.trim()) body.api_key = keyEl.value.trim();
  }

  // Check if any credentials exist (from config or new input)
  const creds = (currentCfg.credentials || {})[provider] || {};
  const hasExistingCreds = creds.configured;
  const hasNewKey = body.api_key || Object.keys(body).some(k => info.fields?.some(f => f.key === k));
  if (!hasExistingCreds && !hasNewKey && !['bedrock','vertex'].includes(provider)) {
    errEl.textContent = 'API key is required.';
    errEl.style.display = 'block';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Saving...';

  try {
    const res = await fetch('/api/setup', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (data.error) {
      errEl.textContent = data.error; errEl.style.display = 'block';
      btn.disabled = false; btn.textContent = 'Save & Start'; return;
    }
    connect();
    const cres2 = await fetch('/api/config');
    currentCfg = await cres2.json();
    loadVersionInfo(currentCfg);
    loadSessions();
    showPage('chat');
    addMsg('Setup complete! How can I help?', 'agent');
  } catch(e) {
    errEl.textContent = 'Setup failed: ' + e; errEl.style.display = 'block';
  }
  btn.disabled = false;
  btn.textContent = 'Save & Start';
}

// ── WebSocket ──
function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => {
    statusEl.textContent = 'Connected';
    // Fetch current token state on connect
    fetch('/api/tokens').then(r => r.json()).then(u => {
      if (u.total_tokens > 0) updateTokenBar(u);
    }).catch(() => {});
  };
  ws.onclose = () => { statusEl.textContent = 'Disconnected'; setTimeout(connect, 2000); };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);

    // ── Cron run events ──
    // Events from cron jobs have source="cron" and run_id
    if (msg.source === 'cron' && msg.run_id) {
      // Route to cron run viewer if it's open and viewing this run
      if (window._viewingCronRun === msg.run_id) {
        replayCronEvent(msg);
      }
      return;
    }
    if (msg.type === 'cron_run_start') {
      // A cron job started — refresh jobs page if visible
      if (document.getElementById('page-jobs').classList.contains('active')) loadJobRuns();
      return;
    }
    if (msg.type === 'cron_run_done' || msg.type === 'cron_run_error') {
      // A cron job finished
      if (document.getElementById('page-jobs').classList.contains('active')) loadJobRuns();
      if (window._viewingCronRun === msg.run_id) {
        const statusEl = document.getElementById('cron-run-status');
        if (msg.type === 'cron_run_done') {
          statusEl.textContent = '✅ Done';
          if (msg.result) addCronMsg(msg.result, 'agent');
        } else {
          statusEl.textContent = '❌ Error';
          addCronMsg('Error: ' + (msg.error || 'Unknown'), 'tool');
        }
      }
      return;
    }

    if (msg.type === 'thinking') {
      removeThinking();
      addMsg('Thinking...', 'agent thinking');
    } else if (msg.type === 'tool_call') {
      removeThinking();
      // During subtask execution, suppress tool calls from main chat
      // (the step's stream shows the output directly)
      if (!document.querySelector('.subtask-item.active')) {
        const action = msg.params?.action || '';
        const detail = action ? ` \u2192 ${action}` : '';
        let text = `\ud83d\udd27 ${msg.tool}${detail}`;
        if (msg.result) text += '\n' + msg.result.slice(0, 500);
        addMsg(text, 'tool');
      }
    } else if (msg.type === 'response') {
      removeThinking();
      addMsg(msg.content, 'agent');
      sendBtn.disabled = false;
      inputEl.focus();
      if (msg.token_usage) updateTokenBar(msg.token_usage);
      if (msg.session_id) window._currentSessionId = msg.session_id;
      loadSessions();
    } else if (msg.type === 'token_update') {
      // During decomposed tasks, also update the active step's token count
      if (msg.subtask_total_tokens != null) {
        const activeStep = document.querySelector('.subtask-item.active');
        if (activeStep) {
          let badge = activeStep.querySelector('.step-tokens');
          if (!badge) {
            badge = document.createElement('span');
            badge.className = 'step-tokens';
            badge.style.cssText = 'font-size:10px;color:var(--text-dim);margin-left:auto';
            activeStep.querySelector('.subtask-header').appendChild(badge);
          }
          const ctx = msg.context_used || msg.total_tokens || 0;
          const limit = msg.context_limit || 1;
          badge.textContent = Math.round(ctx/1000) + 'k / ' + Math.round(limit/1000) + 'k';
        }
      }
      updateTokenBar(msg);
    } else if (msg.type === 'context_warning') {
      updateTokenBar(msg.usage);
    } else if (msg.type === 'reset_ok') {
      chatEl.innerHTML = '';
      addMsg('Chat cleared. How can I help?', 'agent');
      window._currentSessionId = null;
      document.getElementById('token-bar-wrap').style.display = 'none';
      document.getElementById('context-warning').style.display = 'none';
      loadSessions();
    } else if (msg.type === 'plan') {
      // Subtask decomposition plan — each step gets its own stream area
      removeThinking();
      let html = '<div class="subtask-plan"><strong>📋 Task decomposed into ' + msg.subtasks.length + ' steps:</strong><div class="subtask-list">';
      msg.subtasks.forEach((s, i) => {
        html += '<div class="subtask-item" id="subtask-' + i + '">' +
          '<div class="subtask-header" onclick="toggleSubtaskStream(' + i + ')">' +
          '<span class="chevron" id="subtask-chevron-' + i + '">▶</span>' +
          '<span class="subtask-status">⏳</span> <strong>Step ' + (i+1) + '</strong> (' + s.tool + '): ' + s.task.slice(0, 100) +
          '</div>' +
          '<div class="subtask-stream" id="subtask-stream-' + i + '"></div>' +
          '</div>';
      });
      html += '</div></div>';
      addMsg(html, 'agent', true);
      // Track raw text per subtask for markdown rendering
      window._subtaskText = {};
    } else if (msg.type === 'subtask_start') {
      const idx = msg.subtask.index;
      const el = document.getElementById('subtask-' + idx);
      if (el) {
        el.querySelector('.subtask-status').textContent = '▶️';
        el.classList.add('active');
        // Auto-expand this step's stream and collapse others
        document.querySelectorAll('.subtask-stream.visible').forEach(s => {
          if (s.id !== 'subtask-stream-' + idx) {
            s.classList.remove('visible', 'streaming');
            const chevId = s.id.replace('stream-', 'chevron-');
            const chev = document.getElementById(chevId);
            if (chev) chev.classList.remove('open');
          }
        });
        const stream = document.getElementById('subtask-stream-' + idx);
        if (stream) {
          stream.classList.add('visible', 'streaming');
          stream.innerHTML = '<span style="color:var(--text-dim);font-style:italic">Working...</span>';
          const chev = document.getElementById('subtask-chevron-' + idx);
          if (chev) chev.classList.add('open');
        }
        // Clear raw text buffer
        window._subtaskText = window._subtaskText || {};
        window._subtaskText[idx] = '';
        // Scroll this step into view
        el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    } else if (msg.type === 'subtask_done') {
      const idx = msg.subtask.index;
      const el = document.getElementById('subtask-' + idx);
      if (el) {
        el.querySelector('.subtask-status').textContent = '✅';
        el.classList.remove('active');
        el.classList.add('done');
        // Add duration to header
        const header = el.querySelector('.subtask-header strong');
        if (header && !header.querySelector('small')) {
          header.insertAdjacentHTML('afterend', ' <small>(' + msg.subtask.duration + 's)</small>');
        }
        // Final markdown render of completed step
        const stream = document.getElementById('subtask-stream-' + idx);
        if (stream && window._subtaskText && window._subtaskText[idx]) {
          stream.innerHTML = renderMd(window._subtaskText[idx]);
          stream.classList.remove('streaming');
        }
      }
    } else if (msg.type === 'stream_chunk') {
      const idx = msg.index != null ? msg.index : (window._activeSubtaskIdx || 0);
      const stream = document.getElementById('subtask-stream-' + idx);
      if (stream) {
        // Accumulate raw text
        window._subtaskText = window._subtaskText || {};
        window._subtaskText[idx] = (window._subtaskText[idx] || '') + msg.chunk;
        // Render as markdown (live)
        stream.innerHTML = renderMd(window._subtaskText[idx]);
        stream.classList.add('visible', 'streaming');
        // Auto-scroll within the stream div
        stream.scrollTop = stream.scrollHeight;
        // Keep the step in view
        const item = document.getElementById('subtask-' + idx);
        if (item) {
          const rect = item.getBoundingClientRect();
          if (rect.bottom > window.innerHeight || rect.top < 0) {
            stream.scrollIntoView({ behavior: 'smooth', block: 'end' });
          }
        }
        // Ensure chevron is open
        const chev = document.getElementById('subtask-chevron-' + idx);
        if (chev) chev.classList.add('open');
      }
    }
  };
}

function addMsg(text, cls, raw) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  if (raw) {
    div.innerHTML = text;
  } else if (cls === 'tool') {
    const parts = text.split('\n');
    const nameSpan = document.createElement('span');
    nameSpan.className = 'tool-name';
    nameSpan.textContent = parts[0];
    div.appendChild(nameSpan);
    if (parts.length > 1) div.appendChild(document.createTextNode('\n' + parts.slice(1).join('\n')));
  } else if (cls === 'agent') {
    // Render agent messages as markdown
    div.innerHTML = '<div class="md-content">' + renderMd(text) + '</div>';
  } else { div.textContent = text; }
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function toggleSubtaskStream(idx) {
  const stream = document.getElementById('subtask-stream-' + idx);
  const chev = document.getElementById('subtask-chevron-' + idx);
  if (!stream) return;
  const isVisible = stream.classList.contains('visible');
  if (isVisible) {
    stream.classList.remove('visible');
    if (chev) chev.classList.remove('open');
  } else {
    stream.classList.add('visible');
    if (chev) chev.classList.add('open');
  }
}

function toggleCronSubtask(idx) {
  const stream = document.getElementById('cron-subtask-stream-' + idx);
  const chev = document.getElementById('cron-subtask-chevron-' + idx);
  if (!stream) return;
  const isVisible = stream.classList.contains('visible');
  if (isVisible) {
    stream.classList.remove('visible');
    if (chev) chev.classList.remove('open');
  } else {
    stream.classList.add('visible');
    if (chev) chev.classList.add('open');
  }
}

function removeThinking() {
  const t = chatEl.querySelector('.thinking');
  if (t) t.remove();
}

function send() {
  const text = inputEl.value.trim();
  if (!text || sendBtn.disabled) return;
  addMsg(text, 'user');
  ws.send(JSON.stringify({ type: 'chat', message: text }));
  inputEl.value = '';
  inputEl.style.height = 'auto';
  sendBtn.disabled = true;
}

function _probeText(data) {
  let text = '🔍 Agent status: ' + (data.state || 'idle');
  if (data.tool) text += ' — ' + data.tool + '(' + JSON.stringify(data.params || {}).slice(0, 100) + ')';
  if (data.iteration) text += ' [iteration ' + data.iteration + ']';
  if (data.elapsed_seconds) text += ' (' + data.elapsed_seconds + 's ago)';
  if (data.token_usage) text += '\nContext: ' + (data.token_usage.context_used || data.token_usage.total_tokens || 0).toLocaleString() +
    ' / ' + (data.token_usage.context_limit || 0).toLocaleString() +
    ' (' + (data.token_usage.usage_percent || 0) + '%)';
  return text;
}

function _showOnActivePage(text, cls) {
  addMsg(text, cls || 'tool');
}

async function probeAgent() {
  try {
    const res = await fetch('/api/probe');
    const data = await res.json();
    _showOnActivePage(_probeText(data), 'tool');
  } catch(e) { _showOnActivePage('Probe failed: ' + e, 'tool'); }
}

async function flushAgent() {
  if (!confirm('Stop the agent and dump context? The current task will be aborted.')) return;
  try {
    const res = await fetch('/api/flush', { method: 'POST' });
    const data = await res.json();
    const text = '⏹ ' + (data.message || 'Flush requested');
    _showOnActivePage(text, 'tool');
    sendBtn.disabled = false;
  } catch(e) { _showOnActivePage('Flush failed: ' + e, 'tool'); }
}

async function resetChat() {
  // Save current session first
  if (window._currentSessionId) {
    await fetch('/api/sessions/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
  }
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'reset' }));
}

inputEl.addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});

// ── Profiles ──
async function loadProfiles() {
  const res = await fetch('/api/profiles/status');
  const data = await res.json();
  const el = document.getElementById('profiles-list');
  if (!data.profiles?.length) { el.innerHTML = '<div class="empty">No profiles yet.</div>'; return; }
  el.innerHTML = data.profiles.map(p => `
    <div class="list-item">
      <span class="name">${p.name}</span>
      <span class="meta">port ${p.port}</span>
      ${p.is_default ? '<span class="badge active">default</span>' : ''}
      ${p.running
        ? '<span class="badge active">running</span>'
        : `<button class="btn" style="padding:4px 12px;font-size:12px" onclick="launchProfile('${p.name}')">Launch</button>`
      }
    </div>
  `).join('');
}

async function launchProfile(name) {
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = 'Starting...';
  try {
    const res = await fetch('/api/profiles/launch', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name })
    });
    const data = await res.json();
    if (data.error) { alert(data.error); return; }
    loadProfiles();
  } catch(e) { alert('Failed to launch: ' + e); }
}

async function createProfile() {
  const name = document.getElementById('new-profile-name').value.trim();
  if (!name) return;
  const res = await fetch('/api/profiles', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ name })
  });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }
  document.getElementById('new-profile-name').value = '';
  loadProfiles();
}

// ── Jobs ──
// Track the currently viewed cron run (for live streaming)
window._viewingCronRun = null;
window._cronRunText = {};  // per-subtask raw text for viewed cron run

async function loadJobs() {
  // Load job definitions
  const res = await fetch('/api/jobs');
  const data = await res.json();
  const el = document.getElementById('jobs-list');
  const jobs = Object.values(data.jobs || {});
  if (!jobs.length) { el.innerHTML = '<div class="empty">No scheduled jobs</div>'; return; }
  el.innerHTML = jobs.map(j => {
    const sched = j.cron || (j.interval_minutes ? `every ${j.interval_minutes}m` : j.run_at || '?');
    const badge = j.paused ? '<span class="badge paused">paused</span>' : '<span class="badge active">active</span>';
    return `<div class="list-item">
      <span class="name">${j.name}</span>
      <span class="meta">${sched}</span>
      ${badge}
      <button class="btn secondary" style="padding:4px 10px;font-size:11px" onclick="event.stopPropagation();runJobNow('${j.id}')">▶ Run</button>
    </div>`;
  }).join('');

  // Load recent runs
  await loadJobRuns();
}

async function loadJobRuns() {
  const res = await fetch('/api/jobs/runs?limit=20');
  const data = await res.json();
  const runs = data.runs || [];

  // Active runs card
  const activeRuns = runs.filter(r => r.status === 'running');
  const activeCard = document.getElementById('active-runs-card');
  const activeList = document.getElementById('active-runs-list');
  if (activeRuns.length) {
    activeCard.style.display = 'block';
    activeList.innerHTML = activeRuns.map(r => {
      const elapsed = Math.round((Date.now()/1000 - r.started));
      return `<div class="run-item" onclick="openCronRun('${r.run_id}')">
        <span class="run-status pulse">🔴</span>
        <span class="run-name">${r.job_name}</span>
        <span class="run-meta">${elapsed}s ago</span>
      </div>`;
    }).join('');
  } else {
    activeCard.style.display = 'none';
  }

  // Recent completed runs
  const doneRuns = runs.filter(r => r.status !== 'running');
  const runsList = document.getElementById('runs-list');
  if (!doneRuns.length) {
    runsList.innerHTML = '<div class="empty">No recent runs</div>';
  } else {
    runsList.innerHTML = doneRuns.slice(0, 15).map(r => {
      const icon = r.status === 'done' ? '✅' : '❌';
      const when = new Date(r.started * 1000).toLocaleString();
      const dur = r.ended ? Math.round(r.ended - r.started) + 's' : '—';
      return `<div class="run-item" onclick="openCronRun('${r.run_id}')">
        <span class="run-status">${icon}</span>
        <span class="run-name">${r.job_name}</span>
        <span class="run-meta">${when} · ${dur}</span>
      </div>`;
    }).join('');
  }
}

async function runJobNow(jobId) {
  // Trigger via the agent's cron tool run_now path
  const res = await fetch('/api/jobs');
  const data = await res.json();
  const jobs = data.jobs || {};
  const job = jobs[jobId];
  if (!job) { alert('Job not found'); return; }

  // Direct trigger — POST a run_now to a simple endpoint
  // Actually, we can call _run_scheduled_task via a new endpoint
  const rres = await fetch('/api/jobs/trigger', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ job_id: jobId }),
  });
  const rdata = await rres.json();
  if (rdata.run_id) {
    // Auto-open the run viewer
    openCronRun(rdata.run_id);
  }
}

async function openCronRun(runId) {
  window._viewingCronRun = runId;
  window._cronRunText = {};

  // Switch to the cron run page
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('#sidebar nav a').forEach(a => a.classList.remove('active'));
  document.getElementById('page-cron-run').classList.add('active');
  document.querySelector('[data-page="jobs"]').classList.add('active');

  const msgArea = document.getElementById('cron-run-messages');
  msgArea.innerHTML = '<div class="msg agent thinking">Loading run details...</div>';

  // Fetch run data
  const res = await fetch('/api/jobs/runs/' + runId);
  const run = await res.json();
  if (run.error) { msgArea.innerHTML = `<div class="msg tool">${run.error}</div>`; return; }

  document.getElementById('cron-run-title').textContent = `⏰ ${run.job_name}`;
  document.getElementById('cron-run-status').textContent =
    run.status === 'running' ? '🔴 Running' : run.status === 'done' ? '✅ Done' : '❌ Error';

  msgArea.innerHTML = '';

  // Show task
  addCronMsg(`**Task:** ${run.task}`, 'user');

  // Replay events
  for (const ev of (run.events || [])) {
    replayCronEvent(ev);
  }

  // If done, show final result
  if (run.status !== 'running' && run.result) {
    addCronMsg(run.result, 'agent');
  }
}

function addCronMsg(text, cls, raw) {
  const msgArea = document.getElementById('cron-run-messages');
  if (!msgArea) return;
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  if (raw) {
    div.innerHTML = text;
  } else if (cls === 'tool') {
    div.textContent = text;
  } else if (cls === 'agent') {
    div.innerHTML = '<div class="md-content">' + renderMd(text) + '</div>';
  } else {
    div.innerHTML = '<div class="md-content">' + renderMd(text) + '</div>';
  }
  msgArea.appendChild(div);
  msgArea.scrollTop = msgArea.scrollHeight;
}

function replayCronEvent(ev) {
  if (ev.type === 'plan') {
    let html = '<div class="subtask-plan"><strong>📋 ' + ev.subtasks.length + ' steps:</strong><div class="subtask-list">';
    ev.subtasks.forEach((s, i) => {
      html += '<div class="subtask-item" id="cron-subtask-' + i + '">' +
        '<div class="subtask-header" onclick="toggleCronSubtask(' + i + ')">' +
        '<span class="chevron" id="cron-subtask-chevron-' + i + '">▶</span>' +
        '<span class="subtask-status">⏳</span> <strong>Step ' + (i+1) + '</strong> (' + s.tool + '): ' + s.task.slice(0, 100) +
        '</div>' +
        '<div class="subtask-stream" id="cron-subtask-stream-' + i + '"></div>' +
        '</div>';
    });
    html += '</div></div>';
    addCronMsg(html, 'agent', true);
  } else if (ev.type === 'subtask_start') {
    const el = document.getElementById('cron-subtask-' + ev.subtask.index);
    if (el) { el.querySelector('.subtask-status').textContent = '▶️'; el.classList.add('active'); }
  } else if (ev.type === 'subtask_done') {
    const el = document.getElementById('cron-subtask-' + ev.subtask.index);
    if (el) {
      el.querySelector('.subtask-status').textContent = '✅';
      el.classList.remove('active'); el.classList.add('done');
    }
  } else if (ev.type === 'tool_call') {
    // Suppress during subtask replay (same as main chat)
    if (!document.querySelector('#cron-run-messages .subtask-item.active')) {
      addCronMsg('🔧 ' + ev.tool + (ev.params?.action ? ' → ' + ev.params.action : ''), 'tool');
    }
  } else if (ev.type === 'stream_chunk') {
    const idx = ev.index != null ? ev.index : 0;
    const stream = document.getElementById('cron-subtask-stream-' + idx);
    if (stream) {
      window._cronRunText[idx] = (window._cronRunText[idx] || '') + ev.chunk;
      stream.innerHTML = renderMd(window._cronRunText[idx]);
      stream.classList.add('visible');
    }
  }
}

async function probeCronRun() {
  const runId = window._viewingCronRun;
  if (!runId) return;
  try {
    const res = await fetch('/api/jobs/runs/' + runId + '/probe');
    const data = await res.json();
    addCronMsg(_probeText(data), 'tool');
  } catch(e) { addCronMsg('Probe failed: ' + e, 'tool'); }
}

// ── Profile Switcher & Controls ──

async function loadProfileSwitcher() {
  try {
    const res = await fetch('/api/profiles/status');
    const data = await res.json();
    const sel = document.getElementById('profile-switcher');
    if (!sel) return;
    const profiles = data.profiles || [];
    const active = currentCfg.browser_profile || 'default';
    sel.innerHTML = profiles.map(p => {
      const running = p.running ? ' ●' : '';
      return '<option value="' + p.name + '"' + (p.name === active ? ' selected' : '') + '>' + p.name + running + '</option>';
    }).join('');
  } catch(e) {}
}

async function switchProfile() {
  const sel = document.getElementById('profile-switcher');
  const profile = sel.value;
  await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ browser_profile: profile }),
  });
  currentCfg.browser_profile = profile;
}

async function toggleDecompose() {
  const enabled = document.getElementById('decompose-toggle').checked;
  await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ decompose_enabled: enabled }),
  });
  currentCfg.decompose_enabled = enabled;
}

async function launchActiveBrowser() {
  const btn = document.getElementById('launch-browser-btn');
  btn.disabled = true;
  btn.textContent = '🌍 Connecting...';

  // If CDP_URL is set, just verify connection — don't launch a new browser
  const cdpUrl = (currentCfg.cdp_url || '').trim();
  if (cdpUrl) {
    try {
      const res = await fetch('/api/cdp/check', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ cdp_url: cdpUrl }),
      });
      const data = await res.json();
      if (data.connected) {
        btn.textContent = '🌍 Connected (' + cdpUrl.replace('http://', '') + ')';
      } else {
        btn.textContent = '🌍 Not reachable';
        alert('Cannot reach ' + cdpUrl + '. Make sure the external browser is running.');
      }
    } catch(e) { alert('Failed: ' + e); }
    finally {
      setTimeout(() => { btn.disabled = false; btn.textContent = '🌍 Open Browser'; }, 3000);
    }
    return;
  }

  // Normal profile launch
  const profile = currentCfg.browser_profile || 'default';
  try {
    const res = await fetch('/api/profiles/launch', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name: profile }),
    });
    const data = await res.json();
    if (data.error) { alert(data.error); return; }
    btn.textContent = '🌍 ' + profile + ' (' + (data.status === 'already_running' ? 'running' : 'launched') + ')';
    loadProfileSwitcher();  // refresh running status
  } catch(e) { alert('Failed: ' + e); }
  finally {
    setTimeout(() => { btn.disabled = false; btn.textContent = '🌍 Open Browser'; }, 3000);
  }
}

// ── Settings Page ──
let cfgModelPicker;
async function loadSettingsPage() {
  const cres = await fetch('/api/config');
  currentCfg = await cres.json();
  const cfg = currentCfg;

  // Provider dropdown
  const provSel = document.getElementById('cfg-provider');
  provSel.innerHTML = Object.entries(providers).map(([k,v]) =>
    `<option value="${k}" ${k === cfg.provider ? 'selected' : ''}>${v.name}</option>`
  ).join('');

  // Init model picker
  if (!cfgModelPicker) {
    cfgModelPicker = new ModelPicker('cfg-model-picker');
  }

  await onCfgProviderChange();

  // Set model value
  if (cfg.model) {
    const found = cfgModelPicker.allModels.find(m => m.id === cfg.model);
    if (found) {
      cfgModelPicker.setModels(cfgModelPicker.allModels, cfg.model);
      document.getElementById('cfg-model').value = '';
    } else {
      document.getElementById('cfg-model').value = cfg.model || '';
    }
  }

  // Workspace
  document.getElementById('cfg-workspace').value = cfg.workspace || '';
  // Shell
  document.getElementById('cfg-shell').checked = cfg.shell_enabled !== false;
  document.getElementById('cfg-decompose').checked = cfg.decompose_enabled !== false;
  document.getElementById('cfg-cdp-url').value = cfg.cdp_url || '';
  document.getElementById('cfg-timeout').value = cfg.timeout || 300;
  document.getElementById('cfg-main-max-tokens').value = cfg.main_max_tokens || 8192;
  document.getElementById('cfg-subagent-max-tokens').value = cfg.subagent_max_tokens || 4096;

  // Profiles dropdown
  const pres = await fetch('/api/profiles');
  const pdata = await pres.json();
  const psel = document.getElementById('cfg-profile');
  psel.innerHTML = (pdata.profiles || []).map(p =>
    `<option value="${p.name}" ${p.name === cfg.browser_profile ? 'selected' : ''}>${p.name} (port ${p.port})</option>`
  ).join('');
}

async function onCfgProviderChange() {
  const p = document.getElementById('cfg-provider').value;
  const info = providers[p] || {};

  // Show credential status
  const statusEl = document.getElementById('cfg-credentials-status');
  const creds = (currentCfg.credentials || {})[p] || {};
  if (info.fields) {
    // Multi-field: show per-field status
    document.getElementById('cfg-key-section').innerHTML = '';
    renderProviderFields(p, 'cfg-provider-fields', currentCfg.credentials, currentCfg.provider_fields);
    statusEl.innerHTML = '';
    if (info.note) statusEl.innerHTML = `<p style="font-size:12px;color:var(--text-dim);margin:8px 0">${info.note}</p>`;
  } else {
    // Single key
    document.getElementById('cfg-provider-fields').innerHTML = '';
    const isOauth = info.is_oauth;
    const label = isOauth ? 'OAuth Token' : 'API Key';
    const placeholder = creds.configured ? 'Leave empty to keep current' : (isOauth ? 'sk-ant-oat01-...' : 'sk-...');
    const credStatus = creds.configured
      ? `<div class="key-status configured">✓ Configured (${creds.masked}) — from ${creds.source}</div>`
      : `<div class="key-status missing">Not configured</div>`;
    document.getElementById('cfg-key-section').innerHTML = `<div class="field">
      <label>${label}</label>
      <input type="password" id="cfg-key" placeholder="${placeholder}" autocomplete="off">
      ${credStatus}
    </div>`;
    statusEl.innerHTML = info.note ? `<p style="font-size:12px;color:var(--text-dim);margin:8px 0">${info.note}</p>` : '';
  }

  // Fetch models
  if (!cfgModelPicker) cfgModelPicker = new ModelPicker('cfg-model-picker');
  await loadModelsForPicker(p, cfgModelPicker, info.default_model);
}

async function saveSettings() {
  const provider = document.getElementById('cfg-provider').value;
  const info = providers[provider] || {};
  const modelCustom = document.getElementById('cfg-model').value.trim();
  const model = modelCustom || cfgModelPicker.getValue();
  const workspace = document.getElementById('cfg-workspace').value.trim();
  const shell_enabled = document.getElementById('cfg-shell').checked;
  const decompose_enabled = document.getElementById('cfg-decompose').checked;
  const browser_profile = document.getElementById('cfg-profile').value;
  const cdp_url = document.getElementById('cfg-cdp-url').value.trim();
  const timeout = parseInt(document.getElementById('cfg-timeout').value) || 300;
  const main_max_tokens = parseInt(document.getElementById('cfg-main-max-tokens').value) || 8192;
  const subagent_max_tokens = parseInt(document.getElementById('cfg-subagent-max-tokens').value) || 4096;

  const body = { provider, model, workspace, browser_profile, cdp_url, shell_enabled, decompose_enabled, timeout, main_max_tokens, subagent_max_tokens };

  // Collect credentials
  if (info.fields) {
    const fieldInputs = document.querySelectorAll('#cfg-provider-fields [data-field-key]');
    fieldInputs.forEach(inp => {
      const val = inp.value.trim();
      if (val) body[inp.dataset.fieldKey] = val;
    });
  } else {
    const keyEl = document.getElementById('cfg-key');
    if (keyEl && keyEl.value.trim()) body.api_key = keyEl.value.trim();
  }

  await fetch('/api/setup', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });

  const savedEl = document.getElementById('cfg-saved');
  savedEl.style.display = 'block';
  setTimeout(() => savedEl.style.display = 'none', 3000);

  const cres = await fetch('/api/config');
  currentCfg = await cres.json();
  loadVersionInfo(currentCfg);
}

// ── File Attachment ──
let _pendingFiles = [];  // [{name, type, data (base64), preview?}]

function handleFileSelect(e) {
  const files = Array.from(e.target.files || []);
  files.forEach(f => readFileForAttach(f));
  e.target.value = '';  // reset so same file can be re-selected
}

function readFileForAttach(file) {
  if (file.size > 15 * 1024 * 1024) {
    alert('File too large (max 15MB): ' + file.name);
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    const dataUrl = reader.result;  // data:type;base64,...
    _pendingFiles.push({
      name: file.name,
      type: file.type || 'application/octet-stream',
      data: dataUrl,
    });
    renderFilePreviews();
  };
  reader.readAsDataURL(file);
}

function renderFilePreviews() {
  const container = document.getElementById('file-previews');
  container.innerHTML = _pendingFiles.map((f, i) => {
    const isImage = f.type.startsWith('image/');
    const thumb = isImage
      ? '<img src="' + f.data + '">'
      : '<span style="font-size:16px">📄</span>';
    return '<div class="file-preview">' + thumb +
      '<span>' + f.name + '</span>' +
      '<span class="remove" onclick="removeFile(' + i + ')">✕</span></div>';
  }).join('');
}

function removeFile(idx) {
  _pendingFiles.splice(idx, 1);
  renderFilePreviews();
}

// Drag & drop on chat area
(function() {
  const chatEl = document.getElementById('chat-messages');
  const overlay = document.getElementById('drag-overlay');
  let dragCounter = 0;

  chatEl.addEventListener('dragenter', (e) => {
    e.preventDefault();
    dragCounter++;
    overlay.classList.add('active');
  });
  chatEl.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dragCounter--;
    if (dragCounter <= 0) { overlay.classList.remove('active'); dragCounter = 0; }
  });
  chatEl.addEventListener('dragover', (e) => e.preventDefault());
  chatEl.addEventListener('drop', (e) => {
    e.preventDefault();
    dragCounter = 0;
    overlay.classList.remove('active');
    const files = Array.from(e.dataTransfer.files || []);
    files.forEach(f => readFileForAttach(f));
  });
})();

// Override send to include files
const _origSend = send;
send = function() {
  const text = inputEl.value.trim();
  if (!text && !_pendingFiles.length) return;
  if (sendBtn.disabled) return;

  // Build display message
  let displayText = text;
  if (_pendingFiles.length) {
    const names = _pendingFiles.map(f => f.name).join(', ');
    displayText = (text || '') + (text ? '\n' : '') + '📎 ' + names;
  }
  addMsg(displayText, 'user');

  // Build WS payload
  const payload = { type: 'chat', message: text || '' };
  if (_pendingFiles.length) {
    payload.files = _pendingFiles.map(f => ({
      name: f.name,
      type: f.type,
      data: f.data,
    }));
  }
  ws.send(JSON.stringify(payload));

  _pendingFiles = [];
  renderFilePreviews();
  inputEl.value = '';
  inputEl.style.height = 'auto';
  sendBtn.disabled = true;
};

init();