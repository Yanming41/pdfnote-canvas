let docMeta = null;
let activeSelection = null;
let activeBlockId = null;
const pageTextCache = new Map();

const viewer = document.getElementById('viewer');
const selectionText = document.getElementById('selectionText');
const question = document.getElementById('question');
const askBtn = document.getElementById('askBtn');
const statusEl = document.getElementById('status');
const annotationsEl = document.getElementById('annotations');
const exportBtn = document.getElementById('exportBtn');

function setStatus(text) { statusEl.textContent = text || ''; }

async function loadDoc() {
  docMeta = await (await fetch('/api/doc')).json();
  docMeta.blocks = docMeta.blocks || docMeta.annotations || [];
  document.getElementById('title').textContent = docMeta.title;
  document.getElementById('path').textContent = docMeta.path;
  viewer.innerHTML = '';
  for (const page of docMeta.pages) renderPage(page);
  renderBlocks(docMeta.blocks);
}

function renderPage(page) {
  const scale = docMeta.scale;
  const pageEl = document.createElement('div');
  pageEl.className = 'page';
  pageEl.dataset.page = page.page;
  pageEl.style.width = `${page.width * scale}px`;
  pageEl.style.height = `${page.height * scale}px`;

  const img = document.createElement('img');
  img.src = `/api/page/${page.page}.png`;
  img.alt = `Page ${page.page}`;
  pageEl.appendChild(img);

  const highlights = document.createElement('div');
  highlights.className = 'highlights';
  pageEl.appendChild(highlights);

  const textLayer = document.createElement('div');
  textLayer.className = 'textLayer';
  pageEl.appendChild(textLayer);

  const no = document.createElement('div');
  no.className = 'pageNo';
  no.textContent = page.page;
  pageEl.appendChild(no);

  viewer.appendChild(pageEl);
  loadTextLayer(page.page, textLayer);
}

async function loadTextLayer(pageNo, textLayer) {
  const data = await (await fetch(`/api/page/${pageNo}/text`)).json();
  pageTextCache.set(pageNo, data.text || '');
  const scale = docMeta.scale;
  for (const span of data.spans) {
    const el = document.createElement('span');
    el.textContent = span.text;
    el.dataset.page = pageNo;
    el.style.left = `${span.x * scale}px`;
    el.style.top = `${span.y * scale}px`;
    el.style.width = `${span.w * scale}px`;
    el.style.height = `${span.h * scale}px`;
    el.style.fontSize = `${span.size * scale}px`;
    textLayer.appendChild(el);
  }
}

function pageElementFromNode(node) {
  if (!node) return null;
  const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
  return el ? el.closest('.page') : null;
}

function captureSelection() {
  const sel = window.getSelection();
  const text = sel ? sel.toString().trim() : '';
  if (!sel || !text || sel.rangeCount === 0) return;
  const range = sel.getRangeAt(0);
  const pageEl = pageElementFromNode(range.commonAncestorContainer);
  if (!pageEl) return;
  const pageNo = Number(pageEl.dataset.page);
  const pageBox = pageEl.getBoundingClientRect();
  const rects = [...range.getClientRects()]
    .filter(r => r.width > 1 && r.height > 1)
    .map(r => ({
      x: (r.left - pageBox.left) / docMeta.scale,
      y: (r.top - pageBox.top) / docMeta.scale,
      w: r.width / docMeta.scale,
      h: r.height / docMeta.scale,
    }));
  if (!rects.length) return;
  activeBlockId = null;
  activeSelection = {
    page: pageNo,
    selected_text: text,
    rects,
    surrounding_text: pageTextCache.get(pageNo) || '',
  };
  selectionText.textContent = text;
  setStatus(`New text block selected: ${text.length} chars on page ${pageNo}.`);
}

document.addEventListener('mouseup', () => setTimeout(captureSelection, 0));
document.addEventListener('keyup', () => setTimeout(captureSelection, 0));

askBtn.addEventListener('click', async () => {
  const q = question.value.trim() || '解释这段内容';
  let payload = null;
  if (activeBlockId) {
    payload = { block_id: activeBlockId, question: q };
  } else if (activeSelection) {
    payload = { ...activeSelection, question: q };
  } else {
    setStatus('Select text or click an existing text block first.');
    return;
  }
  askBtn.disabled = true;
  setStatus(activeBlockId ? 'Appending to conversation...' : 'Creating text block conversation...');
  try {
    const block = await (await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })).json();
    upsertBlock(block);
    activeBlockId = block.id;
    activeSelection = null;
    selectionText.textContent = block.selected_text;
    renderBlocks(docMeta.blocks);
    question.value = '';
    setStatus(`Saved turn under text block #${block.id}.`);
  } finally {
    askBtn.disabled = false;
  }
});

exportBtn.addEventListener('click', async () => {
  const res = await (await fetch('/api/export')).json();
  setStatus(`Exported: ${res.path}`);
});

function upsertBlock(block) {
  const idx = docMeta.blocks.findIndex(item => item.id === block.id);
  if (idx >= 0) docMeta.blocks[idx] = block;
  else docMeta.blocks.push(block);
}

function renderBlocks(items) {
  annotationsEl.innerHTML = '';
  clearHighlights();
  for (const block of items.slice().reverse()) {
    const el = document.createElement('article');
    el.className = 'annotation block';
    if (block.id === activeBlockId) el.classList.add('active');
    el.innerHTML = `
      <div class="meta">Text block #${block.id} · page ${block.page} · ${(block.messages || []).length} messages</div>
      <div class="quote"></div>
      <div class="thread"></div>
      <button class="continueBtn" type="button">Continue this thread</button>
    `;
    el.querySelector('.quote').textContent = block.selected_text;
    const thread = el.querySelector('.thread');
    for (const msg of block.messages || []) {
      const msgEl = document.createElement('div');
      msgEl.className = `msg ${msg.role}`;
      msgEl.textContent = `${labelForRole(msg.role)}: ${msg.content}`;
      thread.appendChild(msgEl);
    }
    const activate = () => activateBlock(block);
    el.querySelector('.quote').addEventListener('click', activate);
    el.querySelector('.continueBtn').addEventListener('click', activate);
    annotationsEl.appendChild(el);
  }
  setTimeout(() => items.forEach(drawBlockHighlight), 0);
}

function labelForRole(role) {
  if (role === 'user') return 'Q';
  if (role === 'assistant') return 'A';
  if (role === 'note') return 'Note';
  return role;
}

function activateBlock(block) {
  activeBlockId = block.id;
  activeSelection = null;
  selectionText.textContent = block.selected_text;
  const page = document.querySelector(`.page[data-page="${block.page}"]`);
  if (page) page.scrollIntoView({ behavior: 'smooth', block: 'center' });
  setStatus(`Text block #${block.id} selected. Your next question will append to this thread.`);
  renderBlocks(docMeta.blocks);
}

function clearHighlights() {
  document.querySelectorAll('.highlights').forEach(layer => { layer.innerHTML = ''; });
}

function drawBlockHighlight(block) {
  const page = document.querySelector(`.page[data-page="${block.page}"]`);
  if (!page) return;
  const layer = page.querySelector('.highlights');
  for (const rect of block.rects || []) {
    const hl = document.createElement('div');
    hl.className = 'hl';
    if (block.id === activeBlockId) hl.classList.add('active');
    hl.style.left = `${rect.x * docMeta.scale}px`;
    hl.style.top = `${rect.y * docMeta.scale}px`;
    hl.style.width = `${rect.w * docMeta.scale}px`;
    hl.style.height = `${rect.h * docMeta.scale}px`;
    layer.appendChild(hl);
  }
}

loadDoc().catch(err => {
  console.error(err);
  setStatus(String(err));
});
