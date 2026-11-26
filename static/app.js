/* DouK Studio frontend */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const PAGE_META = {
  dashboard: ['OVERVIEW', '仪表盘'],
  parse: ['RESOLVE', '链接解析'],
  caption: ['TEXT', '文案采集'],
  account: ['COLLECT', '账号作品'],
  mix: ['COLLECT', '合集作品'],
  search: ['COLLECT', '内容搜索'],
  live: ['COLLECT', '直播数据'],
  comment: ['COLLECT', '评论采集'],
  reply: ['COLLECT', '评论回复'],
  downloads: ['MANAGE', '下载队列'],
  files: ['MANAGE', '文件管理'],
  history: ['MANAGE', '下载历史'],
  original: ['TOOLS', '原工具功能'],
  settings: ['SYSTEM', '设置'],
  logs: ['SYSTEM', '运行日志'],
};

const state = {
  page: 'dashboard',
  status: {},
  settings: {},
  tasks: [],
  results: new Map(),
  taskFilter: 'all',
  searchType: 'general',
  files: [],
  captions: [],
  captionSelected: new Set(),
};

/* ------------------------------------------------------------------ utils */

async function api(path, options = {}) {
  const init = { method: options.method || 'GET', headers: { 'Content-Type': 'application/json' } };
  if (options.body !== undefined) init.body = JSON.stringify(options.body);
  const res = await fetch(path, init);
  let data;
  try { data = await res.json(); } catch { data = { ok: false, error: `HTTP ${res.status}` }; }
  if ((!res.ok || data.ok === false) && !options.allowNotOk) {
    throw new Error(data.error || data.message || `请求失败 ${res.status}`);
  }
  return data;
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toast(message, type = 'info') {
  const host = $('#toastHost');
  const el = document.createElement('div');
  el.className = `toast ${type === 'ok' ? 'ok' : type === 'err' ? 'err' : ''}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateX(12px)'; }, 3200);
  setTimeout(() => el.remove(), 3600);
}

function fmtDuration(value) {
  const sec = Number(value || 0);
  if (!sec) return '--:--';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}

function fmtBytes(n) {
  n = Number(n || 0);
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function fmtSpeed(n) { return n ? `${fmtBytes(n)}/s` : '--'; }

function parseCookie(text) {
  const result = {};
  String(text || '').split(';').forEach((part) => {
    const idx = part.indexOf('=');
    if (idx <= 0) return;
    const key = part.slice(0, idx).trim();
    const value = part.slice(idx + 1).trim();
    if (key) result[key] = value;
  });
  return result;
}

function setLoading(btn, loading, text) {
  if (!btn) return;
  if (loading && !btn.disabled) btn.dataset.label = btn.textContent;
  btn.disabled = loading;
  btn.textContent = loading ? (text || '处理中...') : (btn.dataset.label || btn.textContent);
}

const DOUK_SOURCES = ['douk', 'douyin', 'tiktok'];

function isDoukItem(item, source) {
  if (DOUK_SOURCES.includes(String(source || '').toLowerCase())) return true;
  return DOUK_SOURCES.includes(String(item?.source || '').toLowerCase());
}

function safeHttpUrl(url) {
  const value = String(url || '').trim();
  return /^https?:\/\//i.test(value) ? value : '';
}

function resultFor(scope) {
  if (!state.results.has(scope)) {
    state.results.set(scope, { items: [], selected: new Set(), source: '' });
  }
  return state.results.get(scope);
}

function resetResult(container) {
  if (!container) return;
  const result = resultFor(container.id);
  result.items = [];
  result.selected.clear();
  result.source = '';
}

/* --------------------------------------------------------------- navigation */

function goto(page) {
  if (!PAGE_META[page]) return;
  state.page = page;
  $('.content').scrollTop = 0;
  $$('.page').forEach((el) => el.classList.remove('active'));
  $(`#page-${page}`)?.classList.add('active');
  $$('#nav .nav-item').forEach((el) => el.classList.toggle('active', el.dataset.page === page));
  $('#pageTitle').textContent = PAGE_META[page][1];
  $('#pageKicker').textContent = PAGE_META[page][0];
  if (page === 'downloads') refreshTasks();
  if (page === 'files') loadFiles();
  if (page === 'history') loadHistory();
  if (page === 'logs') loadLogs();
}

/* ------------------------------------------------------------------ status */

async function refreshStatus(full = false) {
  try {
    const data = await api('/api/status');
    state.status = data;
    const on = !!data.engine_running;
    const pill = $('#statusPill');
    pill.className = `status-pill ${on ? 'on' : 'off'}`;
    pill.innerHTML = `<span class="dot ${on ? 'on' : 'off'}"></span> ${on ? 'DouK 引擎已连接' : 'DouK 引擎未运行'}`;
    const sideDot = $('#sideDot');
    sideDot.className = `dot ${on ? 'on' : 'off'}`;
    $('#sideEngineText').textContent = on ? '引擎在线' : '引擎离线';
    $('#sideApiText').textContent = data.api_url || '';
    $('#statRunning').textContent = data.counts?.running || 0;
    $('#statDone').textContent = data.counts?.completed || 0;
    $('#statDir').textContent = data.download_dir || '-';
    $('#statEngine').textContent = on ? '在线' : '离线';
    const tools = [];
    tools.push(data.yt_dlp ? 'yt-dlp ✓' : 'yt-dlp ✗');
    tools.push(data.ffmpeg ? 'FFmpeg ✓' : 'FFmpeg ✗');
    $('#statTools').textContent = tools.join(' · ');
    const activeCount = (data.counts?.running || 0) + (data.counts?.queued || 0) + (data.counts?.paused || 0);
    const badge = $('#navTaskCount');
    if (badge) {
      badge.textContent = activeCount ? String(activeCount) : '';
      badge.style.display = activeCount ? '' : 'none';
    }
    if (data.version) $('.side-version').textContent = `DouK Studio v${data.version} · 本地运行`;
    if (full) {
      try {
        const files = await api('/api/files');
        $('#statFiles').textContent = `${files.files.length} 个文件`;
      } catch { /* ignore */ }
    }
  } catch (err) {
    $('#statusPill').innerHTML = `<span class="dot off"></span> 后端未连接`;
    const sideDot = $('#sideDot');
    if (sideDot) sideDot.className = 'dot off';
    if ($('#sideEngineText')) $('#sideEngineText').textContent = '引擎离线';
    if ($('#statEngine')) $('#statEngine').textContent = '离线';
  }
}

/* ------------------------------------------------------------------ media */

function qualitySelectHtml(item, index, scope) {
  const formats = Array.isArray(item.formats) ? item.formats : [];
  if (formats.length < 2) return '';
  const current = String(item.selectedFormatId ?? (formats[0]?.id ?? '0'));
  const options = formats.map((fmt, i) => {
    const value = String(fmt.id ?? i);
    const label = fmt.label || (fmt.height ? `${fmt.height}P` : `清晰度 ${i + 1}`);
    return `<option value="${esc(value)}" ${value === current ? 'selected' : ''}>${esc(label)}</option>`;
  }).join('');
  return `<label class="quality-row"><span>清晰度</span><select class="input quality-select" data-index="${index}" data-scope="${scope}" aria-label="选择下载清晰度">${options}</select></label>`;
}

function itemForDownload(scope, index, kind) {
  const base = resultFor(scope).items[index];
  if (!base) return null;
  let chosen = null;
  let url = '';
  if (kind === 'video') {
    const formats = Array.isArray(base.formats) ? base.formats : [];
    if (formats.length > 1) {
      const container = document.getElementById(scope);
      const selector = container ? $(`.quality-select[data-index="${index}"][data-scope="${scope}"]`, container) : null;
      const value = String(base.selectedFormatId ?? selector?.value ?? (formats[0]?.id ?? '0'));
      chosen = formats.find((fmt, i) => String(fmt.id ?? i) === value) || formats[0];
    }
    url = chosen?.url || base.video_url || '';
  } else if (kind === 'audio') {
    url = base.audio_url || '';
  } else if (kind === 'thumbnail') {
    url = base.cover || '';
  } else if (kind === 'gallery') {
    url = (Array.isArray(base.images) ? base.images : [])[0] || base.cover || '';
  }
  const item = { ...base };
  if (kind === 'video' && chosen?.url) item.video_url = chosen.url;
  if (kind === 'audio' && url) item.audio_url = url;
  if (kind === 'thumbnail' && url) item.cover = url;
  if (kind === 'gallery' && url) {
    item.images = Array.isArray(base.images) && base.images.length ? base.images : [url];
  }
  return {
    item,
    format: kind === 'video' && chosen ? String(chosen.id ?? chosen.label ?? '') : '',
    title: `${base.title || '作品'}${kind === 'video' && chosen?.height ? ` [${chosen.height}P]` : ''}`,
    sourceUrl: url || safeHttpUrl(base.raw?.webpage_url),
  };
}

function renderItems(container, items, source, message = '') {
  if (!container) return;
  const scope = container.id;
  const result = resultFor(scope);
  result.items = items || [];
  // Default to all selected: bulk collection is the common case.
  result.selected = new Set(result.items.map((_, i) => i));
  result.source = source || '';
  if (!result.items.length) {
    container.innerHTML = `<div class="empty"><div class="empty-ico">◇</div><h4>${message ? '没有找到可下载内容' : '暂无结果'}</h4><p>${esc(message || '解析或采集后，结果会显示在这里')}</p></div>`;
    return;
  }

  const cards = result.items.map((item, index) => {
    const cover = item.cover
      ? `<img src="${esc(item.cover)}" alt="" onerror="this.style.display='none'">`
      : `<div class="ph">▶</div>`;
    const dur = item.duration ? `<span class="media-dur">${fmtDuration(item.duration)}</span>` : '';
    const isDouk = isDoukItem(item, source);
    const buttons = isDouk
      ? [
          ['video', '视频', true],
          ['audio', '音频', !!item.audio_url],
          ['thumbnail', '封面', !!item.cover],
          ['gallery', '图集', (item.images || []).length > 0],
        ].filter(([, , enabled]) => enabled)
      : [['video', '最佳画质', true], ['audio', '仅音频', true], ['thumbnail', '封面', true]];
    const actions = buttons.map(([kind, label]) =>
      `<button class="btn btn-sm" data-dl="1" data-scope="${scope}" data-index="${index}" data-kind="${kind}" data-engine="${isDouk ? 'douk' : 'ytdlp'}">${label}</button>`
    ).join('');

    return `<article class="media-card">
      <div class="media-thumb">${cover}${dur}
        <label class="media-check"><input type="checkbox" class="item-check" data-scope="${scope}" data-index="${index}" ${result.selected.has(index) ? 'checked' : ''} aria-label="选择"></label>
      </div>
      <div class="media-body">
        <div class="media-title" title="${esc(item.title)}">${esc(item.title)}</div>
        <div class="media-meta"><span>${esc(item.author || '未知作者')}</span><span>${esc(item.id || '')}</span></div>
        ${qualitySelectHtml(item, index, scope)}
        <div class="media-actions">${actions}</div>
      </div>
    </article>`;
  }).join('');

  const heights = [...new Set(
    result.items.flatMap((it) => (Array.isArray(it.formats) ? it.formats : []).map((f) => Number(f.height) || 0))
  )].filter((h) => h > 0).sort((a, b) => b - a);
  const qualityBar = heights.length >= 2
    ? `<label class="quality-row"><span>统一清晰度</span><select class="input quality-all"><option value="">默认（逐卡片选择）</option>${heights.map((h) => `<option value="${h}">${h}P</option>`).join('')}</select></label>`
    : '';

  container.innerHTML = `
    <div class="media-grid">${cards}</div>
    <div class="toolbar" style="margin-top:14px">
      <label class="check sel-all-row"><input type="checkbox" class="sel-all" checked> 全选 / 全不选</label>
      <span class="toolbar-tip sel-tip">已选择 ${result.selected.size} 项 · 批量下载使用 DouK 直链</span>
      ${qualityBar}
      <button class="btn btn-sm" data-batch="video" data-scope="${scope}">批量下载视频</button>
      <button class="btn btn-sm" data-batch="audio" data-scope="${scope}">批量下载音频</button>
      <button class="btn btn-sm" data-batch="caption" data-scope="${scope}">导出选中文案</button>
    </div>`;

  const updateTip = () => {
    const tip = $('.sel-tip', container);
    if (tip) tip.textContent = `已选择 ${result.selected.size} 项 · 批量下载使用 DouK 直链`;
  };
  const selAll = $('.sel-all', container);
  const syncMaster = () => {
    if (!selAll) return;
    selAll.checked = result.selected.size === result.items.length;
    selAll.indeterminate = result.selected.size > 0 && result.selected.size < result.items.length;
    // Keep the batch-parse page header checkbox in sync too.
    const pageAll = document.getElementById('selectAllItems');
    if (pageAll && pageAll.dataset.scope === scope) {
      pageAll.checked = selAll.checked;
      pageAll.indeterminate = selAll.indeterminate;
    }
  };
  syncMaster();
  $$('.item-check', container).forEach((box) => box.addEventListener('change', () => {
    const idx = Number(box.dataset.index);
    if (box.checked) result.selected.add(idx); else result.selected.delete(idx);
    updateTip();
    syncMaster();
  }));
  if (selAll) selAll.addEventListener('change', () => {
    const on = selAll.checked;
    result.selected = on ? new Set(result.items.map((_, i) => i)) : new Set();
    $$('.item-check', container).forEach((box) => { box.checked = on; });
    updateTip();
  });
  $$('.quality-select', container).forEach((select) => select.addEventListener('change', () => {
    const idx = Number(select.dataset.index);
    const item = result.items[idx];
    if (item) item.selectedFormatId = select.value;
  }));
  const qualityAll = $('.quality-all', container);
  if (qualityAll) qualityAll.addEventListener('change', () => {
    const target = Number(qualityAll.value);
    if (!target) return;
    let applied = 0;
    result.items.forEach((item, idx) => {
      const fmts = Array.isArray(item.formats) ? item.formats : [];
      if (!fmts.length) return;
      const matches = fmts.filter((f) => Number(f.height) === target)
        .sort((a, b) => Number(b.bitrate || 0) - Number(a.bitrate || 0));
      if (!matches.length) return;
      item.selectedFormatId = String(matches[0].id ?? fmts.indexOf(matches[0]));
      const sel = $(`.quality-select[data-index="${idx}"]`, container);
      if (sel) sel.value = item.selectedFormatId;
      applied += 1;
    });
    toast(applied ? `已将 ${applied} 个作品统一为 ${target}P（同档位优先最高码率）` : '这些作品没有该清晰度档位', applied ? 'ok' : 'err');
  });
}

function selectedEntries(scope, kind, engineHint) {
  const result = resultFor(scope);
  return Array.from(result.selected).map((index) => {
    const prepared = itemForDownload(scope, index, kind);
    if (!prepared) return null;
    const item = prepared.item;
    return {
      engine: isDoukItem(item, result.source) ? 'douk' : (engineHint || 'ytdlp'),
      kind,
      source_url: prepared.sourceUrl,
      title: prepared.title,
      format: prepared.format,
      item,
    };
  }).filter(Boolean);
}

/* ---------------------------------------------------------------- captions */

function itemToCaption(item) {
  const raw = (item.raw && typeof item.raw === 'object') ? item.raw : {};
  const authorRaw = (raw.author && typeof raw.author === 'object') ? raw.author : {};
  const text = String(raw.desc || raw.description || raw.title || item.title || '').trim();
  let tags = [];
  for (const extra of raw.text_extra || []) {
    if (extra && extra.hashtag_name) tags.push(String(extra.hashtag_name));
  }
  if (!tags.length) tags = Array.from(text.matchAll(/#([^\s#]+)/g), (m) => m[1]);
  tags = tags.map((t) => String(t).replace(/^#/, '')).filter((t, i, arr) => t && arr.indexOf(t) === i);

  const author = String(item.author || authorRaw.nickname || raw.author_name || '');
  const authorId = String(raw.author_sec_uid || authorRaw.sec_uid || raw.sec_user_id || '');
  let created = '';
  const ts = Number(raw.create_time || raw.create_at || raw.timestamp || 0);
  if (ts) {
    const d = new Date(ts > 1e11 ? ts : ts * 1000);
    if (!isNaN(d.getTime())) {
      const pad = (n) => String(n).padStart(2, '0');
      created = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
  }
  const src = String(item.source || '');
  const platform = ['douk', 'douyin'].includes(src) ? 'douyin' : (src === 'tiktok' ? 'tiktok' : 'ytdlp');
  let url = String(raw.source_url || raw.webpage_url || '');
  if (!url && item.id) {
    if (platform === 'douyin') url = `https://www.douyin.com/video/${item.id}`;
    else if (platform === 'tiktok') url = `https://www.tiktok.com/video/${item.id}`;
  }
  let mediaUrl = String(item.video_url || '');
  if (!mediaUrl) {
    const videoRaw = (raw.video && typeof raw.video === 'object') ? raw.video : {};
    const pa = videoRaw.play_addr?.url_list || videoRaw.download_addr?.url_list || [];
    mediaUrl = pa.find((u) => /^https?:/.test(u)) || '';
  }
  return {
    id: String(item.id || ''), text, hashtags: tags, author, author_id: authorId,
    create_time: created, source_url: url, platform, duration: item.duration || 0,
    media_url: mediaUrl, narration: String(item.narration || ''), narration_state: String(item.narration_state || ''),
  };
}

function setCaptions(records) {
  state.captions = (records || []).filter((r) => r && (r.text || r.narration));
  state.captionSelected = new Set(state.captions.map((_, i) => i));
  const label = $('#capCount');
  if (label) label.textContent = `${state.captions.length} 条`;
  renderCaptions();
}

function renderCaptions() {
  const host = $('#captionList');
  if (!host) return;
  const term = ($('#capFilter')?.value || '').trim().toLowerCase();
  const rows = state.captions
    .map((rec, index) => ({ rec, index }))
    .filter(({ rec }) => !term || (rec.text + ' ' + rec.hashtags.join(' ') + ' ' + rec.author).toLowerCase().includes(term));
  host.innerHTML = rows.length
    ? rows.map(({ rec, index }) => `<article class="caption-item ${state.captionSelected.has(index) ? 'selected' : ''}">
        <label class="cap-check"><input type="checkbox" class="cap-checkbox" data-capindex="${index}" ${state.captionSelected.has(index) ? 'checked' : ''} aria-label="选择文案"></label>
        <div class="cap-body">
          ${rec.text ? `<div class="cap-text" data-capexpand="${index}" title="点击展开/收起">${esc(rec.text)}</div>` : ''}
          ${narrationBlockHtml(rec)}
          ${rec.hashtags.length ? `<div class="cap-tags">${rec.hashtags.map((t) => `<span class="cap-tag">#${esc(t)}</span>`).join('')}</div>` : ''}
          <div class="cap-meta"><span>${esc(rec.author || '未知作者')}</span><span>${esc(rec.create_time || '未知时间')}</span><span>${esc(rec.platform || '')}</span><span>${rec.hashtags.length} 个话题</span></div>
        </div>
        <div class="cap-actions">
          <button class="btn btn-sm" data-capcopy="${index}">复制</button>
          ${safeHttpUrl(rec.source_url) ? `<a class="btn btn-sm" href="${esc(safeHttpUrl(rec.source_url))}" target="_blank" rel="noopener noreferrer">原作品</a>` : ''}
        </div>
      </article>`).join('')
    : `<div class="empty"><div class="empty-ico">✎</div><h4>${term ? '没有匹配的文案' : '还没有文案'}</h4><p>${term ? '换个关键词试试' : '用上方任意一种方式提取文案'}</p></div>`;
  updateCapTip();
}

function narrationBlockHtml(rec) {
  const state = rec.narration_state || '';
  if (state === 'ok' && rec.narration) {
    return `<details class="cap-narration open"><summary>🎙 解说文案（AI 语音识别）</summary><div class="cap-narration-text">${esc(rec.narration)}</div></details>`;
  }
  if (state === 'no-speech') {
    return `<div class="cap-narration-empty">🎙 该视频未识别到口播语音（可能纯音乐/无解说）</div>`;
  }
  if (state === 'failed') {
    return `<div class="cap-narration-empty">🎙 解说提取失败：${esc(rec.narration_error || '未知原因')}</div>`;
  }
  return '';
}

function updateCapTip() {
  const tip = $('#capSelTip');
  if (tip) tip.textContent = `已选择 ${state.captionSelected.size} / ${state.captions.length} 条`;
}

function selectedCaptions() {
  return Array.from(state.captionSelected).map((i) => state.captions[i]).filter(Boolean);
}

async function exportCaptionsRequest(fmt, rows, name) {
  const list = rows || selectedCaptions();
  if (!list.length) { toast('请先勾选要导出的文案', 'err'); return; }
  try {
    const res = await api('/api/caption/export', {
      method: 'POST',
      body: { records: list, format: fmt, name: name || list[0].author || '' },
    });
    toast(`已导出 ${res.count} 条：${res.file}`, 'ok');
  } catch (err) { toast(err.message, 'err'); }
}

async function extractNarrations(btn) {
  const rows = selectedCaptions();
  if (!rows.length) { toast('请先勾选要提取解说的文案', 'err'); return; }
  if (rows.length > 30) { toast('一次最多提取 30 条', 'err'); return; }
  setLoading(btn, true);
  const host = $('#captionList');
  captionLoading(host, `正在下载音轨并识别解说文案（${rows.length} 条）...`);
  try {
    const res = await api('/api/caption/deep', {
      method: 'POST',
      body: { records: rows },
    });
    // merge narration back into state.captions by id
    const byId = new Map((res.records || []).map((r) => [String(r.id), r]));
    state.captions = state.captions.map((rec) => {
      const updated = byId.get(String(rec.id));
      return updated ? { ...rec, ...updated } : rec;
    });
    renderCaptions();
    const parts = [`成功 ${res.ok_count ?? 0} 条`];
    if (res.no_speech) parts.push(`无口播 ${res.no_speech} 条`);
    if (res.failed) parts.push(`失败 ${res.failed} 条`);
    toast(`解说提取完成：${parts.join('，')}`, res.ok ? 'ok' : 'err');
    host.insertAdjacentHTML('afterbegin', `<div class="success-box">🎙 解说文案提取完成：${esc(parts.join('，'))}。点开「解说文案」可查看全文，导出时也会一并写入。</div>`);
  } catch (err) {
    renderCaptions();
    host.insertAdjacentHTML('afterbegin', `<div class="error-box">${esc(err.message)}</div>`);
    toast(err.message, 'err');
  } finally { setLoading(btn, false); }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制到剪贴板', 'ok');
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
    toast('已复制到剪贴板', 'ok');
  }
}

function captionLoading(host, text) {
  host.innerHTML = `<div class="empty"><div class="empty-ico">◌</div><h4>${esc(text)}</h4><p>解析 / 抓取可能需要数十秒，请不要关闭页面</p></div>`;
}

async function captionFromSingle(btn) {
  const text = $('#capSingleUrl').value.trim();
  if (!text) { toast('请输入链接', 'err'); return; }
  setLoading(btn, true);
  const host = $('#captionList');
  captionLoading(host, '正在提取文案...');
  try {
    const res = await api('/api/resolve', {
      method: 'POST',
      body: { url: text, engine: $('#capSingleEngine')?.value || 'auto' },
    });
    if (res.account) {
      host.innerHTML = `<div class="success-box">已识别为博主主页：<b>${esc(res.account.sec_user_id || '')}</b>，请切换到「博主主页」模式采集文案</div>`;
      toast('这是博主主页链接，请切换到「博主主页」模式', 'err');
      return;
    }
    if (!res.ok || !res.items?.length) throw new Error(res.error || '解析失败');
    setCaptions(res.items.map(itemToCaption));
    toast(`已提取 ${state.captions.length} 条文案`, 'ok');
  } catch (err) {
    host.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  } finally { setLoading(btn, false); }
}

async function captionsFromBatch(btn) {
  const text = $('#capBatchUrls').value.trim();
  const urls = text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  if (!urls.length) { toast('请粘贴链接，每行一条', 'err'); return; }
  if (urls.length > 10) { toast('一次最多提取 10 条链接', 'err'); return; }
  setLoading(btn, true);
  const host = $('#captionList');
  captionLoading(host, `正在提取 ${urls.length} 条文案...`);
  try {
    const res = await api('/api/resolve/batch', {
      method: 'POST',
      body: { urls, engine: $('#capBatchEngine')?.value || 'auto' },
    });
    setCaptions(res.items.map(itemToCaption));
    toast(`已提取 ${state.captions.length} 条文案${res.failed ? `，${res.failed} 条失败` : ''}`, 'ok');
    if (res.failed) {
      const failed = res.results.filter((r) => !r.ok).map((r) => `${r.url}：${r.error || '失败'}`);
      host.insertAdjacentHTML('beforeend', `<div class="error-box">${failed.map(esc).join('<br>')}</div>`);
    }
  } catch (err) {
    host.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  } finally { setLoading(btn, false); }
}

function secUidFromInput(raw) {
  const match = String(raw || '').match(/sec_uid=([^&#?]+)/);
  const value = match ? match[1] : String(raw || '');
  try { return decodeURIComponent(value.trim()); } catch { return value.trim(); }
}

/* Accept sec_uid, a /user/ homepage link, a share short link, or any work link of the author. */
async function resolveSecUid(raw) {
  const text = String(raw || '').trim();
  if (!/^https?:\/\//i.test(text)) return text;
  if (/\/user\//i.test(text)) {
    const m = text.match(/sec_uid=([^&#?]+)/) || text.match(/\/user\/([^\/?&#]+)/);
    if (m) {
      try { return decodeURIComponent(m[1]); } catch { return m[1]; }
    }
    return '';
  }
  try {
    // Short share links (v.douyin.com/xxx) carry no sec_uid; the backend
    // resolves the redirect and returns the author in `account`.
    const res = await api('/api/resolve', { method: 'POST', body: { url: text, engine: 'auto' } });
    const fromAccount = res.account?.sec_user_id || '';
    if (fromAccount) return fromAccount;
    return res.items?.[0]?.raw?.author_sec_uid || '';
  } catch { return ''; }
}

async function fetchAccountCaptions(btn, exportAll) {
  const raw = $('#capAccountId').value.trim();
  if (!raw) { toast('请输入 sec_uid 或链接', 'err'); return; }
  setLoading(btn, true);
  let secUid = '';
  try {
    secUid = await resolveSecUid(raw);
  } catch (err) {
    setLoading(btn, false);
    toast(err.message, 'err');
    return;
  }
  setLoading(btn, false);
  if (!secUid || secUid.length < 20) {
    toast('无法识别博主：请检查链接是否有效，或直接粘贴完整的 sec_uid（区分大小写）', 'err');
    return;
  }
  if (secUid !== raw) $('#capAccountId').value = secUid;
  setLoading(btn, true);
  const host = $('#captionList');
  captionLoading(host, exportAll ? '正在抓取主页文案并下载...' : '正在抓取主页文案...');
  try {
    const res = await api('/api/caption/account', {
      method: 'POST',
      body: {
        sec_uid: secUid,
        platform: 'douyin',
        count: Number($('#capAccountCount').value || 100),
        export: !!exportAll,
      },
    });
    setCaptions(res.records || []);
    if (exportAll && res.export?.ok) toast(`已抓取并导出 ${res.export.count} 条：${res.export.file}`, 'ok');
    else toast(`抓取完成，共 ${res.count} 条${res.fallback ? '（浏览器兜底）' : ''}`, 'ok');
  } catch (err) {
    host.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  } finally { setLoading(btn, false); }
}

async function addDownloads(entries) {
  if (!entries.length) { toast('请先选择内容', 'err'); return; }
  try {
    const res = await api('/api/download', { method: 'POST', body: { items: entries } });
    toast(`已加入 ${res.tasks.length} 个下载任务`, 'ok');
    await refreshStatus(true);
    await refreshTasks();
  } catch (err) {
    toast(err.message, 'err');
  }
}

function rawBlock(raw) {
  if (!raw) return '';
  return `<details class="raw-json"><summary>查看接口原始数据</summary><pre>${esc(JSON.stringify(raw, null, 2).slice(0, 20000))}</pre></details>`;
}

/* ---------------------------------------------------------------- resolving */

async function doResolve(inputId, outputId, btn, engine = null) {
  const url = $(inputId).value.trim();
  if (!url) { toast('请输入链接', 'err'); return; }
  setLoading(btn, true);
  const container = $(outputId);
  resetResult(container);
  container.innerHTML = `<div class="empty"><div class="empty-ico">◌</div><h4>正在解析...</h4><p>正在连接最合适的下载引擎</p></div>`;
  try {
    const resolvedEngine = engine || $('#parseEngine')?.value || 'auto';
    const res = await api('/api/resolve', { method: 'POST', body: { url, engine: resolvedEngine } });
    if (res.account) {
      container.innerHTML = `<div class="success-box">已识别账号主页：<b>${esc(res.account.sec_user_id)}</b></div>
        <div class="toolbar"><button class="btn btn-primary btn-sm" id="goAccountBtn">前往账号作品采集</button></div>`;
      $('#goAccountBtn').onclick = () => {
        $('#accountId').value = res.account.sec_user_id;
        goto('account');
      };
      return;
    }
    if (!res.ok || !res.items?.length) {
      container.innerHTML = `<div class="error-box">${esc(res.error || '解析失败')}</div>` + rawBlock(res.raw);
      return;
    }
    renderItems(container, res.items, res.source);
    toast(`解析成功，找到 ${res.items.length} 个作品`, 'ok');
  } catch (err) {
    container.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  } finally {
    setLoading(btn, false);
  }
}

/* ----------------------------------------------------------- batch resolve */

async function batchResolve() {
  const text = $('#batchUrls')?.value.trim();
  if (!text) { toast('请粘贴链接，每行一条', 'err'); return; }
  const urls = text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  if (urls.length > 10) { toast('一次最多解析 10 条链接', 'err'); return; }
  const btn = $('#batchParseBtn');
  setLoading(btn, true);
  const container = $('#parseResult');
  resetResult(container);
  container.innerHTML = `<div class="empty"><div class="empty-ico">◌</div><h4>正在批量解析 ${urls.length} 条链接...</h4><p>每条链接可能需要数十秒，请不要关闭页面</p></div>`;
  try {
    const res = await api('/api/resolve/batch', {
      method: 'POST',
      body: { urls, engine: $('#parseEngine')?.value || 'auto' },
    });
    renderItems(container, res.items, '', res.failed ? `${res.failed} 条链接解析失败` : '');
    if (res.failed) {
      const failed = res.results.filter((r) => !r.ok).map((r) => `${r.url}：${r.error || '失败'}`);
      container.insertAdjacentHTML('beforeend', `<div class="error-box">${failed.map(esc).join('<br>')}</div>`);
    }
    toast(`批量解析完成，共 ${res.items.length} 个作品`, 'ok');
  } catch (err) {
    container.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  } finally {
    setLoading(btn, false);
  }
}

/* ------------------------------------------------------------------ fetch */

async function runFetch(btn, endpoint, payload, outputId, type = 'media') {
  setLoading(btn, true);
  const container = $(outputId);
  resetResult(container);
  container.innerHTML = `<div class="empty"><div class="empty-ico">◌</div><h4>采集中...</h4><p>请稍候，数据量较大时可能需要几十秒</p></div>`;
  try {
    if (!state.status.engine_running) {
      toast('正在启动 DouK 引擎...');
      await api('/api/engine/start', { method: 'POST', body: {} });
      await refreshStatus();
    }
    const res = await api('/api/fetch', { method: 'POST', body: { endpoint, payload, type }, allowNotOk: true });
    const items = res.items || [];
    if (!items.length && (res.error || res.ok === false)) {
      container.innerHTML = `<div class="error-box">${esc(res.error || '未获取到数据')}</div>`;
      toast('采集失败', 'err');
      return;
    }
    renderItems(container, items, endpoint.split('/')[0], '该接口未返回标准作品列表');
    if (!items.length) {
      container.insertAdjacentHTML('beforeend', rawBlock(res.raw));
    } else {
      container.insertAdjacentHTML('beforeend', rawBlock(res.raw));
    }
    toast(`采集完成，共 ${items.length} 条${res.fallback ? '（DouK 接口无数据，已用浏览器兜底）' : ''}`, 'ok');
  } catch (err) {
    container.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  } finally {
    setLoading(btn, false);
  }
}

/* ------------------------------------------------------------------ tasks */

async function refreshTasks() {
  try {
    const res = await api('/api/tasks');
    state.tasks = res.tasks || [];
    renderTasks();
  } catch { /* ignore */ }
}

function statusBadge(status) {
  const map = { running: '运行中', queued: '排队中', completed: '已完成', failed: '失败', canceled: '已取消', paused: '已暂停' };
  return `<span class="badge ${esc(status)}">${map[status] || esc(status)}</span>`;
}

function taskRow(task, mini = false) {
  const progress = Math.min(100, Number(task.progress || 0));
  const pct = `${progress.toFixed(progress % 1 ? 1 : 0)}%`;
  const meta = `${fmtBytes(task.downloaded)} / ${fmtBytes(task.total)} · ${fmtSpeed(task.speed)}${task.eta ? ` · ETA ${esc(task.eta)}` : ''}`;
  const subtitle = `${esc(task.engine === 'douk' ? 'DouK' : 'yt-dlp')} · ${esc(task.kind || 'video')} · ${esc(task.created_at || '')}`;
  if (mini) {
    return `<div class="mini-task">
      <span class="task-icon">${task.engine === 'douk' ? 'D' : 'Y'}</span>
      <div class="task-main"><div class="task-title">${esc(task.title || '未命名任务')}</div><div class="task-sub">${subtitle}</div></div>
      ${statusBadge(task.status)}<span style="font-size:11px;color:var(--text-3)">${pct}</span>
    </div>`;
  }
  const actions = [];
  if (task.status === 'running' || task.status === 'queued') actions.push(['pause', '暂停']);
  if (task.status === 'paused') actions.push(['resume', '继续']);
  if (['running', 'queued', 'paused'].includes(task.status)) actions.push(['cancel', '取消']);
  if (['failed', 'canceled'].includes(task.status)) actions.push(['retry', '重试']);
  if (task.file) actions.push(['reveal', '打开文件']);
  actions.push(['remove', '移除']);
  const buttons = actions.map(([action, label]) =>
    `<button class="btn btn-sm ${action === 'remove' ? 'btn-danger' : ''}" data-task-action="${action}" data-id="${esc(task.id)}">${label}</button>`
  ).join('');
  return `<div class="task-row">
    <div class="task-top">
      <span class="task-icon">${task.engine === 'douk' ? 'D' : 'Y'}</span>
      <div class="task-main">
        <div class="task-title" title="${esc(task.title)}">${esc(task.title || '未命名任务')}</div>
        <div class="task-sub">${subtitle} · ${esc(task.message || '')}</div>
      </div>
      ${statusBadge(task.status)}
      <div class="task-actions">${buttons}</div>
    </div>
    <div class="task-progress">
      <div class="progress-track"><div class="progress-fill" style="width:${progress}%"></div></div>
      <div class="progress-meta"><span>${esc(meta)}</span><span>${pct}</span></div>
    </div>
    ${task.error ? `<details class="raw-json"><summary>错误详情</summary><pre>${esc(task.error)}</pre></details>` : ''}
  </div>`;
}

function renderTasks() {
  const list = $('#taskList');
  if (!list) return;
  const filtered = state.taskFilter === 'all' ? state.tasks : state.tasks.filter((t) => t.status === state.taskFilter);
  list.innerHTML = filtered.length
    ? filtered.map((t) => taskRow(t)).join('')
    : `<div class="empty"><div class="empty-ico">≡</div><h4>这里还没有任务</h4><p>解析链接后点击任意下载按钮即可加入队列</p></div>`;
  const dash = $('#dashTasks');
  if (dash) {
    dash.innerHTML = state.tasks.length
      ? state.tasks.slice(0, 5).map((t) => taskRow(t, true)).join('')
      : `<div class="empty"><div class="empty-ico">≡</div><h4>暂无任务</h4><p>试试上方的快速解析</p></div>`;
  }
}

async function taskAction(action, id) {
  try {
    await api('/api/task/action', { method: 'POST', body: { action, id } });
    toast('操作已提交', 'ok');
    await refreshTasks();
    await refreshStatus();
  } catch (err) { toast(err.message, 'err'); }
}

/* ------------------------------------------------------------------- files */

async function loadFiles() {
  try {
    const res = await api('/api/files');
    state.files = res.files || [];
    $('#fileDirLabel').textContent = res.directory;
    renderFiles();
  } catch (err) { toast(err.message, 'err'); }
}

function renderFiles() {
  const term = ($('#fileSearch')?.value || '').trim().toLowerCase();
  const files = state.files.filter((f) => !term || f.name.toLowerCase().includes(term));
  $('#fileList').innerHTML = files.length
    ? files.map((f) => `<div class="file-row">
        <span class="file-ico">${f.is_dir ? '▣' : (/\.(mp3|m4a|flac|wav|opus)$/i.test(f.name) ? '♪' : '▶')}</span>
        <div class="file-info"><div class="file-name" title="${esc(f.name)}">${esc(f.name)}</div><div class="file-meta">${esc(f.size_text)} · ${esc(f.mtime)}</div></div>
        <button class="btn btn-sm" data-reveal="${esc(f.path)}">定位</button>
      </div>`).join('')
    : `<div class="empty"><div class="empty-ico">▣</div><h4>${term ? '没有匹配的文件' : '下载目录是空的'}</h4><p>${term ? '试试别的关键词' : '完成下载后文件会出现在这里'}</p></div>`;
}

/* ----------------------------------------------------------------- history */

async function loadHistory() {
  try {
    const res = await api('/api/history');
    const list = res.history || [];
    $('#historyList').innerHTML = list.length
      ? list.map((h) => `<div class="history-row">
          <span class="task-icon">${h.engine === 'douk' ? 'D' : 'Y'}</span>
          <div class="file-info">
            <div class="file-name" title="${esc(h.file)}">${esc(h.title || '未命名')}</div>
            <div class="file-meta">${esc(h.at || '')} · ${esc(h.kind || '')} · ${esc(h.file)}</div>
          </div>
          <span class="history-tag">${esc(h.engine || '')}</span>
          <button class="btn btn-sm" data-reveal="${esc(h.file)}">定位</button>
        </div>`).join('')
      : `<div class="empty"><div class="empty-ico">◷</div><h4>暂无下载历史</h4><p>下载完成后会自动记录在这里</p></div>`;
  } catch (err) { toast(err.message, 'err'); }
}

/* ----------------------------------------------------------- original tool */

async function runOriginalAction(action, btn) {
  setLoading(btn, true);
  try {
    const res = await api('/api/original/action', { method: 'POST', body: { action } });
    $('#originalOutput').textContent = (res.output || res.message || '').trim() || '操作已完成';
    toast(res.message, res.ok ? 'ok' : 'err');
    await refreshStatus();
    try {
      state.settings = await api('/api/settings');
      applyCookieNote(state.settings);
    } catch { /* ignore */ }
  } catch (err) {
    $('#originalOutput').textContent = err.message;
    toast(err.message, 'err');
  } finally {
    setLoading(btn, false);
  }
}

/* ---------------------------------------------------------------- settings */

function applyCookieNote(data) {
  const note = $('#cookieNote');
  if (!note) return;
  note.textContent = data.cookie_configured
    ? '抖音 Cookie 已配置（出于安全考虑，界面不显示明文）'
    : '抖音 Cookie 未配置，抖音相关采集可能失败';
}

function fillSettings(data) {
  const app = data.app || {};
  const orig = data.original || {};
  const put = (id, value) => { const el = $(id); if (el) el.value = value ?? ''; };
  put('#setDownloadDir', app.download_dir);
  put('#setConcurrent', app.max_concurrent);
  put('#setYtdlp', app.yt_dlp_path);
  put('#setFfmpeg', app.ffmpeg_path);
  put('#setQuality', app.default_quality);
  put('#setAudioFormat', app.audio_format);
  put('#setSubLangs', app.subtitle_langs);
  put('#setCookieFile', app.cookie_file);
  put('#setProxy', app.proxy);
  put('#setTemplate', app.filename_template);
  $('#setWriteSubs').checked = !!app.write_subtitles;
  $('#setEmbedSubs').checked = !!app.embed_subtitles;
  $('#setThumb').checked = !!app.write_thumbnail;
  $('#setAutoStart').checked = !!app.auto_start_engine;
  put('#origRoot', orig.root);
  put('#origFolderName', orig.folder_name);
  put('#origNameFormat', orig.name_format);
  put('#origDescLength', orig.desc_length);
  put('#origChunk', orig.chunk);
  put('#origTimeout', orig.timeout);
  put('#origRetry', orig.max_retry);
  put('#origNameLength', orig.name_length);
  put('#origTruncate', orig.truncate);
  put('#origDateFormat', orig.date_format);
  put('#origSplit', orig.split);
  put('#origStorageFormat', orig.storage_format);
  put('#origMaxSize', orig.max_size);
  put('#origMaxPages', orig.max_pages);
  put('#origLiveQualities', orig.live_qualities);
  put('#origProxyTiktok', orig.proxy_tiktok);
  put('#origTwcTiktok', orig.twc_tiktok);
  put('#origRunCommand', orig.run_command);
  $('#origFolderMode').checked = !!orig.folder_mode;
  $('#origDownload').checked = orig.download !== false;
  $('#origDynamic').checked = !!orig.dynamic_cover;
  $('#origStatic').checked = !!orig.static_cover;
  $('#origMusic').checked = !!orig.music;
  $('#origDouyinPlatform').checked = orig.douyin_platform !== false;
  $('#origTiktokPlatform').checked = orig.tiktok_platform !== false;
  applyCookieNote(data);
  document.documentElement.dataset.theme = app.theme || 'light';
}

async function saveSettings() {
  const btn = $('#saveSettingsBtn');
  setLoading(btn, true);
  const app = {
    download_dir: $('#setDownloadDir').value.trim(),
    max_concurrent: Math.min(10, Math.max(1, Number($('#setConcurrent').value || 3))),
    yt_dlp_path: $('#setYtdlp').value.trim(),
    ffmpeg_path: $('#setFfmpeg').value.trim(),
    default_quality: $('#setQuality').value.trim(),
    audio_format: $('#setAudioFormat').value,
    subtitle_langs: $('#setSubLangs').value.trim(),
    cookie_file: $('#setCookieFile').value.trim(),
    proxy: $('#setProxy').value.trim(),
    filename_template: $('#setTemplate').value.trim() || '{title} [{id}]',
    write_subtitles: $('#setWriteSubs').checked,
    embed_subtitles: $('#setEmbedSubs').checked,
    write_thumbnail: $('#setThumb').checked,
    auto_start_engine: $('#setAutoStart').checked,
    theme: document.documentElement.dataset.theme || 'light',
  };
  const original = {
    root: $('#origRoot').value.trim(),
    folder_name: $('#origFolderName').value.trim(),
    name_format: $('#origNameFormat').value.trim(),
    desc_length: Number($('#origDescLength').value || 64),
    chunk: Number($('#origChunk').value || 2097152),
    timeout: Number($('#origTimeout').value || 10),
    max_retry: Number($('#origRetry').value || 5),
    name_length: Number($('#origNameLength').value || 128),
    truncate: Number($('#origTruncate').value || 50),
    date_format: $('#origDateFormat').value.trim() || '%Y-%m-%d %H:%M:%S',
    split: $('#origSplit').value.trim() || '-',
    storage_format: $('#origStorageFormat').value.trim(),
    max_size: Number($('#origMaxSize').value || 0),
    max_pages: Number($('#origMaxPages').value || 0),
    live_qualities: $('#origLiveQualities').value.trim(),
    proxy_tiktok: $('#origProxyTiktok').value.trim(),
    twc_tiktok: $('#origTwcTiktok').value.trim(),
    run_command: $('#origRunCommand').value.trim(),
    folder_mode: $('#origFolderMode').checked,
    download: $('#origDownload').checked,
    dynamic_cover: $('#origDynamic').checked,
    static_cover: $('#origStatic').checked,
    music: $('#origMusic').checked,
    douyin_platform: $('#origDouyinPlatform').checked,
    tiktok_platform: $('#origTiktokPlatform').checked,
  };
  const douyinCookie = $('#origCookie').value.trim();
  const tiktokCookie = $('#origCookieTiktok').value.trim();
  if (douyinCookie) original.cookie = parseCookie(douyinCookie);
  if (tiktokCookie) original.cookie_tiktok = tiktokCookie;
  try {
    await api('/api/settings', { method: 'POST', body: { app, original } });
    $('#origCookie').value = '';
    $('#origCookieTiktok').value = '';
    $('#setConcurrent').value = app.max_concurrent;
    toast('设置已保存', 'ok');
    await refreshStatus(true);
    try {
      state.settings = await api('/api/settings');
      applyCookieNote(state.settings);
    } catch { /* ignore */ }
  } catch (err) {
    toast(err.message, 'err');
    await refreshStatus();
  } finally {
    setLoading(btn, false);
  }
}

async function loadLogs() {
  try {
    const res = await api('/api/logs');
    $('#logView').textContent = res.logs.length ? res.logs.join('\n') : '暂无日志';
  } catch (err) { toast(err.message, 'err'); }
}

/* --------------------------------------------------------------- event bind */

function bindEvents() {
  $$('#nav .nav-item').forEach((btn) => btn.addEventListener('click', () => goto(btn.dataset.page)));
  $$('[data-goto]').forEach((btn) => btn.addEventListener('click', () => goto(btn.dataset.goto)));

  $('#themeBtn').addEventListener('click', async () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    // 提交完整 app 设置，只改动 theme，避免覆盖其他设置
    const app = { ...(state.settings?.app || {}), theme: next };
    try {
      await api('/api/settings', { method: 'POST', body: { app } });
      if (state.settings) state.settings = { ...state.settings, app };
    } catch { /* ignore */ }
  });

  $('#engineStartBtn').addEventListener('click', async () => {
    try {
      const res = await api('/api/engine/start', { method: 'POST', body: {} });
      toast(res.message, res.ok ? 'ok' : 'err');
      await refreshStatus(true);
    } catch (err) { toast(err.message, 'err'); }
  });
  $('#engineStopBtn').addEventListener('click', async () => {
    try {
      const res = await api('/api/engine/stop', { method: 'POST', body: {} });
      toast(res.message, 'ok');
      await refreshStatus(true);
    } catch (err) { toast(err.message, 'err'); }
  });

  $('#dashParseBtn').addEventListener('click', () => doResolve('#dashUrl', '#dashResult', $('#dashParseBtn'), 'auto'));
  $('#dashUrl').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#dashParseBtn').click(); });
  $('#parseBtn').addEventListener('click', () => doResolve('#parseUrl', '#parseResult', $('#parseBtn')));
  $('#parseUrl').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#parseBtn').click(); });

  document.addEventListener('click', async (e) => {
    const dlBtn = e.target.closest('[data-dl]');
    if (dlBtn) {
      const scope = dlBtn.dataset.scope;
      const index = Number(dlBtn.dataset.index);
      const prepared = itemForDownload(scope, index, dlBtn.dataset.kind);
      if (!prepared) return;
      const item = prepared.item;
      await addDownloads([{
        engine: dlBtn.dataset.engine,
        kind: dlBtn.dataset.kind,
        source_url: prepared.sourceUrl,
        title: prepared.title,
        format: prepared.format,
        item,
      }]);
      return;
    }
    const batchBtn = e.target.closest('[data-batch]');
    if (batchBtn) {
      const scope = batchBtn.dataset.scope;
      if (batchBtn.dataset.batch === 'caption') {
        await exportCaptionsRequest('txt', selectedEntries(scope, 'video').map((entry) => itemToCaption(entry.item)));
      } else {
        await addDownloads(selectedEntries(scope, batchBtn.dataset.batch));
      }
      return;
    }
    const capCheck = e.target.closest('.cap-checkbox');
    if (capCheck) {
      const index = Number(capCheck.dataset.capindex);
      if (capCheck.checked) state.captionSelected.add(index);
      else state.captionSelected.delete(index);
      capCheck.closest('.caption-item')?.classList.toggle('selected', capCheck.checked);
      updateCapTip();
      return;
    }
    const capExpand = e.target.closest('[data-capexpand]');
    if (capExpand) { capExpand.classList.toggle('open'); return; }
    const capCopy = e.target.closest('[data-capcopy]');
    if (capCopy) {
      const rec = state.captions[Number(capCopy.dataset.capcopy)];
      if (rec) await copyText(rec.text);
      return;
    }
    const capExport = e.target.closest('[data-capexport]');
    if (capExport) { await exportCaptionsRequest(capExport.dataset.capexport); return; }
    const actionBtn = e.target.closest('[data-task-action]');
    if (actionBtn) {
      const act = actionBtn.dataset.taskAction;
      if (act === 'reveal') {
        const t = state.tasks.find((x) => x.id === actionBtn.dataset.id);
        if (!t || !t.file) { toast('未找到任务对应的文件路径', 'err'); return; }
        try { await api('/api/files/reveal', { method: 'POST', body: { path: t.file } }); }
        catch (err) { toast(err.message, 'err'); }
        return;
      }
      await taskAction(act, actionBtn.dataset.id);
      return;
    }
    const reveal = e.target.closest('[data-reveal]');
    if (reveal) {
      try { await api('/api/files/reveal', { method: 'POST', body: { path: reveal.dataset.reveal } }); }
      catch (err) { toast(err.message, 'err'); }
      return;
    }
    const filter = e.target.closest('[data-filter]');
    if (filter) {
      state.taskFilter = filter.dataset.filter;
      $$('#taskFilters .chip').forEach((c) => c.classList.toggle('active', c === filter));
      renderTasks();
    }
    const sTab = e.target.closest('#searchTabs .tab');
    if (sTab) {
      state.searchType = sTab.dataset.type;
      $$('#searchTabs .tab').forEach((t) => t.classList.toggle('active', t === sTab));
      const adv = $('#searchAdvanced');
      adv.style.display = ['general', 'video'].includes(state.searchType) ? '' : 'none';
    }
  });

  $('#selectAllItems')?.addEventListener('change', (e) => {
    const container = document.getElementById(e.target.dataset.scope || 'parseResult');
    if (!container) return;
    $$('.item-check', container).forEach((box) => {
      box.checked = e.target.checked;
      box.dispatchEvent(new Event('change'));
    });
  });

  $('#accountBtn').addEventListener('click', async (e) => {
    const platform = $('#accountPlatform').value;
    const raw = $('#accountId').value.trim();
    if (!raw) { toast('请输入 sec_uid 或链接', 'err'); return; }
    const btn = e.currentTarget;
    setLoading(btn, true);
    let sec = '';
    try {
      sec = await resolveSecUid(raw);
    } catch (err) {
      setLoading(btn, false);
      toast(err.message, 'err');
      return;
    }
    setLoading(btn, false);
    if (!sec || sec.length < 20) {
      toast('无法识别博主：请检查链接是否有效，或直接粘贴完整的 sec_uid（区分大小写）', 'err');
      return;
    }
    if (sec !== raw) $('#accountId').value = sec;
    const payload = {
      sec_user_id: sec,
      tab: $('#accountTab').value,
      count: Number($('#accountCount').value || 30),
      source: false,
    };
    runFetch(btn, `${platform}/account`, payload, '#accountResult');
  });

  $('#mixBtn').addEventListener('click', (e) => {
    const platform = $('#mixPlatform').value;
    const raw = $('#mixId').value.trim();
    if (!raw) { toast('请输入 ID', 'err'); return; }
    const videoMatch = raw.match(/(?:video|v)\/(\d+)/);
    const count = Number($('#mixCount').value || 50);
    const payload = platform === 'tiktok'
      ? { mix_id: raw, count }
      : videoMatch ? { detail_id: videoMatch[1], count } : { mix_id: raw, count };
    runFetch(e.currentTarget, `${platform}/mix`, payload, '#mixResult');
  });

  $('#searchBtn').addEventListener('click', (e) => {
    const keyword = $('#searchKeyword').value.trim();
    if (!keyword) { toast('请输入关键词', 'err'); return; }
    const payload = {
      keyword,
      pages: Number($('#searchPages').value || 1),
      count: Number($('#searchCount').value || 20),
    };
    if (['general', 'video'].includes(state.searchType)) {
      payload.sort_type = Number($('#searchSort').value);
      payload.publish_time = Number($('#searchTime').value);
      payload.duration = Number($('#searchDuration').value);
    }
    runFetch(e.currentTarget, `douyin/search/${state.searchType}`, payload, '#searchResult');
  });

  $('#liveBtn').addEventListener('click', (e) => {
    const platform = $('#livePlatform').value;
    const id = $('#liveId').value.trim();
    if (!id) { toast('请输入直播间 ID', 'err'); return; }
    const payload = platform === 'tiktok' ? { room_id: id } : { web_rid: id };
    runFetch(e.currentTarget, `${platform}/live`, payload, '#liveResult', 'raw');
  });

  $('#commentBtn').addEventListener('click', (e) => {
    const id = $('#commentId').value.trim();
    if (!id) { toast('请输入作品 ID', 'err'); return; }
    const payload = {
      detail_id: id,
      pages: Number($('#commentPages').value || 1),
      count: Number($('#commentCount').value || 30),
      count_reply: Number($('#commentReplyCount').value || 3),
      reply: $('#commentReply').checked,
    };
    runFetch(e.currentTarget, 'douyin/comment', payload, '#commentResult', 'raw');
  });

  $('#replyBtn').addEventListener('click', (e) => {
    const detailId = $('#replyDetailId').value.trim();
    const commentId = $('#replyCommentId').value.trim();
    if (!detailId || !commentId) { toast('请填写作品 ID 和评论 ID', 'err'); return; }
    const payload = {
      detail_id: detailId,
      comment_id: commentId,
      pages: Number($('#replyPages').value || 1),
      count: Number($('#replyCount').value || 10),
    };
    runFetch(e.currentTarget, 'douyin/reply', payload, '#replyResult', 'raw');
  });

  $$('#captionTabs .tab').forEach((tab) =>
    tab.addEventListener('click', () => {
      $$('#captionTabs .tab').forEach((t) => t.classList.toggle('active', t === tab));
      $$('.cap-panel').forEach((panel) => { panel.hidden = panel.dataset.panel !== tab.dataset.capmode; });
    })
  );
  $('#capSingleBtn').addEventListener('click', (e) => captionFromSingle(e.currentTarget));
  $('#capSingleUrl').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#capSingleBtn').click(); });
  $('#capBatchBtn').addEventListener('click', (e) => captionsFromBatch(e.currentTarget));
  $('#capAccountBtn').addEventListener('click', (e) => fetchAccountCaptions(e.currentTarget, false));
  $('#capAccountAllBtn').addEventListener('click', (e) => fetchAccountCaptions(e.currentTarget, true));
  $('#capFilter').addEventListener('input', renderCaptions);
  $('#capNarrateBtn').addEventListener('click', (e) => extractNarrations(e.currentTarget));
  $('#capSelectAll').addEventListener('click', () => {
    state.captionSelected = new Set(state.captions.map((_, i) => i));
    renderCaptions();
  });
  $('#capSelectNone').addEventListener('click', () => {
    state.captionSelected.clear();
    renderCaptions();
  });
  $('#capCopyBtn').addEventListener('click', async () => {
    const rows = selectedCaptions();
    if (!rows.length) { toast('请先勾选文案', 'err'); return; }
    const blocks = rows.map((r) => {
      const parts = [];
      if (r.text) parts.push(r.text);
      if (r.narration) parts.push(`解说文案：${r.narration}`);
      return parts.join('\n');
    });
    await copyText(blocks.filter(Boolean).join('\n\n'));
  });
  $('#capOpenDirBtn').addEventListener('click', async () => {
    try { await api('/api/caption/open-dir', { method: 'POST', body: {} }); }
    catch (err) { toast(err.message, 'err'); }
  });

  $('#pickDirBtn').addEventListener('click', async () => {
    const current = $('#setDownloadDir').value.trim();
    try {
      const res = await api('/api/files/pick', { method: 'POST', body: { current } });
      if (!res?.path) { toast('未获取到有效目录', 'err'); return; }
      $('#setDownloadDir').value = res.path;
      toast(`已选择：${res.path}`, 'ok');
    } catch (err) {
      if (!/未选择有效目录/.test(err.message)) toast(err.message, 'err');
    }
  });

  $('#batchParseBtn')?.addEventListener('click', batchResolve);
  $('#fileSearch')?.addEventListener('input', renderFiles);
  $('#clearHistoryBtn')?.addEventListener('click', async () => {
    if (!confirm('确定清空 DouK Studio 的下载历史吗？不会删除已下载的文件。')) return;
    try {
      const res = await api('/api/history/clear', { method: 'POST', body: {} });
      toast(res.message, 'ok');
      await loadHistory();
      await refreshStatus();
    } catch (err) { toast(err.message, 'err'); }
  });
  const clearTasks = async (scope, label) => {
    if (!confirm(label)) return;
    try {
      const res = await api('/api/tasks/clear', { method: 'POST', body: { scope } });
      toast(`已清理 ${res.removed || 0} 条任务记录`, 'ok');
      await refreshTasks();
      await refreshStatus();
    } catch (err) { toast(err.message, 'err'); }
  };
  $('#clearCompletedBtn')?.addEventListener('click', () => clearTasks('completed', '确定清空所有已完成的任务记录吗？'));
  $('#clearFailedBtn')?.addEventListener('click', () => clearTasks('failed', '确定清空所有失败/已取消的任务记录吗？'));
  $('#clearAllBtn')?.addEventListener('click', () => clearTasks('all', '确定清空全部任务记录吗？运行中/排队中的任务不受影响。'));
  $$('[data-original]').forEach((btn) =>
    btn.addEventListener('click', () => runOriginalAction(btn.dataset.original, btn))
  );

  $('#saveSettingsBtn').addEventListener('click', saveSettings);
  $('#refreshLogsBtn').addEventListener('click', loadLogs);
  $('#openFolderBtn').addEventListener('click', async () => {
    try { await api('/api/files/open', { method: 'POST', body: {} }); }
    catch (err) { toast(err.message, 'err'); }
  });
}

/* ------------------------------------------------- douyin login / cookie */

let loginPollTimer = null;

function openModal(id) { const m = document.getElementById(id); if (m) m.hidden = false; }
function closeModal(id) { const m = document.getElementById(id); if (m) m.hidden = true; }

const LOGIN_METHOD_TEXT = { qr: '扫码登录', 'browser-import': '本机浏览器提取', manual: '手动粘贴' };

function renderLoginStatus(info) {
  const badge = $('#loginBadge');
  const meta = $('#loginMeta');
  const logoutBtn = $('#logoutBtn');
  const valEl = $('#loginValidation');
  if (!badge) return;
  if (info.logged_in) {
    badge.textContent = '已登录';
    badge.className = 'login-badge ok';
    const methodText = LOGIN_METHOD_TEXT[info.method] || info.method || '-';
    meta.textContent = `登录方式：${methodText} · Cookie 字段 ${info.cookie_count} 条 · 保存于 ${info.saved_at || '-'}（仅保存在本机）`;
    if (logoutBtn) logoutBtn.hidden = false;
  } else {
    badge.textContent = '未登录';
    badge.className = 'login-badge err';
    meta.textContent = '未登录：内容搜索、评论回复等采集会被抖音限制，建议一键扫码登录提升成功率';
    if (logoutBtn) logoutBtn.hidden = true;
  }
  const val = info.last_validation;
  if (valEl) {
    if (val && val.checked_at) {
      valEl.hidden = false;
      const time = String(val.checked_at).slice(5, 19);
      valEl.textContent = `上次验证（${time}）：${val.valid ? '✓ 有效' : '✗ 无效'}${val.nickname ? ' · 账号 ' + val.nickname : ''} — ${val.message || ''}`;
      valEl.style.color = val.valid ? 'var(--success, #16a34a)' : 'var(--danger, #dc2626)';
    } else {
      valEl.hidden = true;
    }
  }
}

async function refreshLoginStatus() {
  try {
    const res = await api('/api/login/status');
    renderLoginStatus(res);
    return res;
  } catch { return null; }
}

function setQrStatus(text, cls) {
  const el = $('#qrStatus');
  if (!el) return;
  el.textContent = text;
  el.className = `login-status ${cls || ''}`;
}

function pollLoginDuringQr() {
  return new Promise((resolve) => {
    if (loginPollTimer) clearInterval(loginPollTimer);
    loginPollTimer = setInterval(async () => {
      const info = await refreshLoginStatus();
      if (!info) return;
      const active = info.phase === 'opening' || info.phase === 'waiting';
      if (active) {
        setQrStatus(info.message || '请在浏览器窗口中扫码登录', 'active');
        return;
      }
      clearInterval(loginPollTimer);
      loginPollTimer = null;
      resolve(info);
    }, 2000);
  });
}

function bindLoginEvents() {
  $('#validateCookieBtn')?.addEventListener('click', async () => {
    const btn = $('#validateCookieBtn');
    const valEl = $('#loginValidation');
    try {
      btn.disabled = true;
      if (valEl) { valEl.hidden = false; valEl.style.color = ''; valEl.textContent = '正在打开浏览器窗口验证 Cookie（几秒钟，请勿关闭弹出的窗口）...'; }
      const res = await api('/api/login/validate', { method: 'POST', body: {} });
      const v = res.validation || {};
      await refreshLoginStatus();
      toast(v.valid ? `Cookie 有效（${v.nickname || '已登录'}）` : `Cookie 无效：${v.message || '请重新登录'}`, v.valid ? 'ok' : 'err');
    } catch (e) {
      if (valEl) valEl.textContent = `验证失败：${e.message || e}`;
      toast('验证失败', 'err');
    } finally {
      btn.disabled = false;
    }
  });

  $('#qrLoginBtn')?.addEventListener('click', async () => {
    openModal('loginQrModal');
    const info = await refreshLoginStatus();
    if (info?.logged_in) setQrStatus('当前已登录抖音。如需更换账号，请先退出登录', '');
    else if (info?.phase === 'waiting' || info?.phase === 'opening') setQrStatus(info.message || '登录窗口进行中...', 'active');
    else setQrStatus('尚未开始，点击下方按钮打开登录窗口', '');
  });

  $('#qrStartBtn')?.addEventListener('click', async () => {
    const btn = $('#qrStartBtn');
    try {
      btn.disabled = true;
      setQrStatus('正在打开登录窗口...', 'active');
      await api('/api/login/qr', { method: 'POST', body: {} });
      const info = await pollLoginDuringQr();
      if (info?.logged_in) {
        setQrStatus('登录成功！Cookie 已保存并自动同步到采集引擎', 'success');
        toast('抖音登录成功', 'ok');
        setTimeout(() => closeModal('loginQrModal'), 1200);
      } else if (info?.phase === 'canceled') {
        setQrStatus(info.message || '登录窗口已关闭，登录未完成', 'error');
      } else if (info?.phase === 'timeout') {
        setQrStatus('登录超时（5 分钟），请重试', 'error');
      } else if (info?.phase === 'error') {
        setQrStatus(info.message || '登录失败', 'error');
      } else {
        setQrStatus(info?.message || '登录未完成', '');
      }
    } catch (err) {
      setQrStatus(err.message, 'error');
    } finally {
      btn.disabled = false;
    }
  });

  $('#importCookieBtn')?.addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = '正在读取...';
    try {
      const res = await api('/api/login/import', { method: 'POST', body: {} });
      toast(res.message, res.ok ? 'ok' : 'err');
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = originalText;
    }
    await refreshLoginStatus();
  });

  $('#manualCookieBtn')?.addEventListener('click', () => {
    $('#manualCookieInput').value = '';
    openModal('loginCookieModal');
  });

  $('#manualCookieSave')?.addEventListener('click', async () => {
    const text = $('#manualCookieInput').value.trim();
    if (!text) { toast('请先粘贴 Cookie 内容', 'err'); return; }
    try {
      const res = await api('/api/login/manual', { method: 'POST', body: { cookie: text } });
      toast(res.message, 'ok');
      $('#manualCookieInput').value = '';
      closeModal('loginCookieModal');
      await refreshLoginStatus();
    } catch (err) {
      toast(err.message, 'err');
    }
  });

  $('#logoutBtn')?.addEventListener('click', async () => {
    if (!confirm('确定退出抖音登录并清除保存的 Cookie 吗？')) return;
    try {
      const res = await api('/api/login/logout', { method: 'POST', body: {} });
      toast(res.message, 'ok');
    } catch (err) {
      toast(err.message, 'err');
    }
    await refreshLoginStatus();
  });

  $$('[data-close-modal]').forEach((btn) =>
    btn.addEventListener('click', () => closeModal(btn.dataset.closeModal))
  );
  $$('.modal-mask').forEach((mask) =>
    mask.addEventListener('click', (e) => { if (e.target === mask) mask.hidden = true; })
  );
}

/* -------------------------------------------------------------------- boot */

async function boot() {
  bindEvents();
  bindLoginEvents();
  goto('dashboard');
  try {
    const settings = await api('/api/settings');
    state.settings = settings;
    fillSettings(settings);
  } catch { /* ignore */ }
  await refreshStatus(true);
  await refreshLoginStatus();
  await refreshTasks();
  setInterval(async () => {
    if (document.hidden) return;
    await refreshStatus();
    if (state.page === 'downloads' || state.page === 'dashboard') await refreshTasks();
  }, 2000);
}

boot();
