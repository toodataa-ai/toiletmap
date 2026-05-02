'use strict';

// ── 定数 ──────────────────────────────────────────────────────────────────────
const TOKYO_CENTER = [35.6812, 139.7671];
const STAR_LABELS  = ['', '汚い', 'やや汚い', '普通', 'きれい', 'とてもきれい'];
const CROWD_LABELS = ['', '😊 空いてる', '😐 ふつう', '😰 混んでる'];

// ── 地図初期化 ────────────────────────────────────────────────────────────────
const map = L.map('map', { zoomControl: false }).setView(TOKYO_CENTER, 13);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 19,
}).addTo(map);

L.control.zoom({ position: 'bottomright' }).addTo(map);

// クラスタグループ（軽量化の核）
const clusterGroup = L.markerClusterGroup({
  maxClusterRadius: 50,
  showCoverageOnHover: false,
  spiderfyOnMaxZoom: true,
  chunkedLoading: true,
});
map.addLayer(clusterGroup);

// ── 状態 ─────────────────────────────────────────────────────────────────────
let markers       = new Map();  // id → { marker, data }
let loadedIds     = new Set();  // 取得済み ID（重複防止）
let currentId     = null;
let selectedStar  = 0;
let selectedCrowd = 0;
let addMode       = false;
let addLatLng     = null;
let selectedType  = '公衆';

// ── ユーティリティ ────────────────────────────────────────────────────────────
function starsHtml(avg, count) {
  if (!count) return '<span style="color:#9E9E9E">未評価</span>';
  const full = Math.round(avg);
  let html = '';
  for (let i = 1; i <= 5; i++) {
    html += `<span style="color:${i <= full ? '#FFC107' : '#DDD'}">★</span>`;
  }
  return html;
}

function markerColor(avg_clean, count) {
  if (!count)         return '#9E9E9E';
  if (avg_clean >= 4) return '#4CAF50';
  if (avg_clean >= 3) return '#FFC107';
  if (avg_clean >= 2) return '#FF9800';
  return '#F44336';
}

function formatDate(str) {
  if (!str) return '';
  const d = new Date(str.replace(' ', 'T'));
  if (isNaN(d)) return str;
  const diff = Math.floor((new Date() - d) / 60000);
  if (diff < 2)    return 'たった今';
  if (diff < 60)   return `${diff}分前`;
  if (diff < 1440) return `${Math.floor(diff / 60)}時間前`;
  return `${Math.floor(diff / 1440)}日前`;
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// ── マーカー作成 ──────────────────────────────────────────────────────────────
function makeIcon(color) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18">
    <circle cx="9" cy="9" r="8" fill="${color}" stroke="white" stroke-width="2"/>
  </svg>`;
  return L.divIcon({ html: svg, className: '', iconSize: [18, 18], iconAnchor: [9, 9] });
}

function addMarker(t) {
  if (markers.has(t.id)) return;
  const marker = L.marker([t.lat, t.lon], { icon: makeIcon(markerColor(t.avg_clean, t.rating_count)) });
  marker.on('click', () => openPanel(t.id));
  clusterGroup.addLayer(marker);
  markers.set(t.id, { marker, data: t });
}

function updateMarkerColor(id, avg_clean, count) {
  const entry = markers.get(id);
  if (!entry) return;
  entry.marker.setIcon(makeIcon(markerColor(avg_clean, count)));
}

// ── ビューポート読み込み ──────────────────────────────────────────────────────
let loadTimer = null;

async function loadViewport() {
  const b = map.getBounds();
  const p = new URLSearchParams({
    min_lat: b.getSouth().toFixed(5),
    max_lat: b.getNorth().toFixed(5),
    min_lon: b.getWest().toFixed(5),
    max_lon: b.getEast().toFixed(5),
  });
  try {
    const data = await fetch(`/api/toilets?${p}`).then(r => r.json());
    data.forEach(t => {
      if (!loadedIds.has(t.id)) {
        loadedIds.add(t.id);
        addMarker(t);
      }
    });
    document.getElementById('toilet-count').textContent = `${loadedIds.size.toLocaleString()} 件`;
  } catch (e) {
    console.error('データ読み込み失敗:', e);
    document.getElementById('toilet-count').textContent = '取得失敗';
  } finally {
    document.getElementById('loading').classList.add('hidden');
  }
}

function scheduleLoad() {
  clearTimeout(loadTimer);
  loadTimer = setTimeout(loadViewport, 400);
}

map.on('moveend', scheduleLoad);

// ── 詳細パネル ────────────────────────────────────────────────────────────────
async function openPanel(id) {
  currentId = id;
  const cached = markers.get(id)?.data;
  if (cached) renderPanel(cached, []);
  showPanel();
  try {
    const detail = await fetch(`/api/toilets/${id}`).then(r => r.json());
    renderPanel(detail, detail.recent_ratings || []);
    if (markers.has(id)) markers.get(id).data = detail;
  } catch (e) {
    console.error('詳細取得失敗:', e);
  }
}

function renderPanel(t, ratings) {
  document.getElementById('panel-name').textContent = t.name || '公衆トイレ';
  const opEl = document.getElementById('panel-operator');
  opEl.textContent = t.operator ? `管理: ${t.operator}` : (t.facility_type ? `種別: ${t.facility_type}` : '');
  document.getElementById('panel-stars').innerHTML = starsHtml(t.avg_clean, t.rating_count);
  document.getElementById('panel-clean-val').textContent =
    t.rating_count ? `${t.avg_clean} / 5` : '—';
  document.getElementById('panel-crowd-icon').textContent =
    t.rating_count ? CROWD_LABELS[Math.round(t.avg_crowd)].split(' ')[0] : '—';
  document.getElementById('panel-crowd-val').textContent =
    t.rating_count ? CROWD_LABELS[Math.round(t.avg_crowd)].split(' ').slice(1).join(' ') : '';
  document.getElementById('panel-count').textContent = t.rating_count ? `${t.rating_count}` : '0';
  document.getElementById('panel-wc-card').style.display = t.wheelchair ? '' : 'none';

  const list   = document.getElementById('comment-list');
  const noComm = document.getElementById('no-comments');
  list.innerHTML = '';
  const withComment = ratings.filter(r => r.comment);
  if (withComment.length === 0) {
    noComm.classList.remove('hidden');
  } else {
    noComm.classList.add('hidden');
    withComment.forEach(r => {
      const li = document.createElement('li');
      li.innerHTML = `
        <div>${escHtml(r.comment)}</div>
        <div class="cm-meta">
          清潔さ ${'★'.repeat(r.cleanliness)}${'☆'.repeat(5 - r.cleanliness)}
          &nbsp;${CROWD_LABELS[r.crowdedness]}
          &nbsp;${formatDate(r.created_at)}
        </div>`;
      list.appendChild(li);
    });
  }
}

function showPanel() {
  const panel = document.getElementById('panel');
  panel.classList.remove('hidden');
  requestAnimationFrame(() => panel.classList.add('visible'));
}

function closePanel() {
  const panel = document.getElementById('panel');
  panel.classList.remove('visible');
  setTimeout(() => panel.classList.add('hidden'), 300);
  currentId = null;
}

// ── 評価モーダル ──────────────────────────────────────────────────────────────
function openModal() {
  if (!currentId) return;
  resetForm();
  document.getElementById('modal-name').textContent =
    document.getElementById('panel-name').textContent;
  document.getElementById('modal').classList.remove('hidden');
  document.getElementById('modal-backdrop').classList.remove('hidden');
  document.getElementById('submit-success').classList.add('hidden');
  document.getElementById('rating-form').classList.remove('hidden');
}

function closeModal() {
  document.getElementById('modal').classList.add('hidden');
  document.getElementById('modal-backdrop').classList.add('hidden');
}

function resetForm() {
  selectedStar  = 0;
  selectedCrowd = 0;
  document.querySelectorAll('.star').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.crowd-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('star-label').textContent = 'タップして評価';
  document.getElementById('comment-input').value = '';
  document.getElementById('char-count').textContent = '0 / 200';
  document.getElementById('form-error').classList.add('hidden');
  document.getElementById('submit-btn').disabled = false;
  document.getElementById('submit-btn').textContent = '投稿する';
}

document.querySelectorAll('.star').forEach(star => {
  star.addEventListener('click', () => {
    selectedStar = parseInt(star.dataset.v);
    document.querySelectorAll('.star').forEach((s, i) =>
      s.classList.toggle('active', i < selectedStar));
    document.getElementById('star-label').textContent = STAR_LABELS[selectedStar];
  });
  star.addEventListener('mouseover', () => {
    const v = parseInt(star.dataset.v);
    document.querySelectorAll('.star').forEach((s, i) => { s.style.color = i < v ? '#FFC107' : ''; });
  });
  star.addEventListener('mouseout', () => {
    document.querySelectorAll('.star').forEach((s, i) => {
      s.style.color = i < selectedStar ? '#FFC107' : '';
    });
  });
});

document.querySelectorAll('.crowd-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    selectedCrowd = parseInt(btn.dataset.v);
    document.querySelectorAll('.crowd-btn').forEach(b => b.classList.toggle('active', b === btn));
  });
});

document.getElementById('comment-input').addEventListener('input', function () {
  document.getElementById('char-count').textContent = `${this.value.length} / 200`;
});

document.getElementById('rating-form').addEventListener('submit', async e => {
  e.preventDefault();
  const errEl = document.getElementById('form-error');
  errEl.classList.add('hidden');
  if (!selectedStar) {
    errEl.textContent = '清潔さを選択してください';
    errEl.classList.remove('hidden');
    return;
  }
  if (!selectedCrowd) {
    errEl.textContent = '混雑具合を選択してください';
    errEl.classList.remove('hidden');
    return;
  }
  const btn = document.getElementById('submit-btn');
  btn.disabled = true;
  btn.textContent = '送信中…';
  try {
    const res = await fetch(`/api/toilets/${currentId}/ratings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        cleanliness: selectedStar,
        crowdedness: selectedCrowd,
        comment: document.getElementById('comment-input').value || null,
      }),
    });
    if (!res.ok) throw new Error(await res.text());
    document.getElementById('rating-form').classList.add('hidden');
    document.getElementById('submit-success').classList.remove('hidden');
    const detail = await fetch(`/api/toilets/${currentId}`).then(r => r.json());
    renderPanel(detail, detail.recent_ratings || []);
    updateMarkerColor(currentId, detail.avg_clean, detail.rating_count);
    if (markers.has(currentId)) markers.get(currentId).data = detail;
  } catch (err) {
    errEl.textContent = `送信失敗: ${err.message}`;
    errEl.classList.remove('hidden');
    btn.disabled = false;
    btn.textContent = '投稿する';
  }
});

// ── トイレ追加機能 ────────────────────────────────────────────────────────────
function toggleAddMode() {
  addMode = !addMode;
  const fab  = document.getElementById('add-fab');
  const hint = document.getElementById('add-hint');
  if (addMode) {
    fab.classList.add('active');
    hint.classList.remove('hidden');
    map.getContainer().classList.add('add-cursor');
    closePanel();
  } else {
    fab.classList.remove('active');
    hint.classList.add('hidden');
    map.getContainer().classList.remove('add-cursor');
  }
}

function openAddModal() {
  document.getElementById('add-name').value = '';
  document.getElementById('add-error').classList.add('hidden');
  document.getElementById('add-submit-btn').disabled = false;
  document.getElementById('add-submit-btn').textContent = '追加する';
  selectedType = '公衆';
  document.querySelectorAll('.type-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.v === '公衆'));
  document.getElementById('add-modal').classList.remove('hidden');
  document.getElementById('add-modal-backdrop').classList.remove('hidden');
}

function closeAddModal() {
  document.getElementById('add-modal').classList.add('hidden');
  document.getElementById('add-modal-backdrop').classList.add('hidden');
}

document.querySelectorAll('.type-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    selectedType = btn.dataset.v;
    document.querySelectorAll('.type-btn').forEach(b => b.classList.toggle('active', b === btn));
  });
});

document.getElementById('add-form').addEventListener('submit', async e => {
  e.preventDefault();
  if (!addLatLng) return;
  const btn  = document.getElementById('add-submit-btn');
  const name = document.getElementById('add-name').value.trim() || null;
  btn.disabled = true;
  btn.textContent = '送信中…';
  try {
    const res = await fetch('/api/toilets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lat: addLatLng.lat, lon: addLatLng.lng, name, facility_type: selectedType }),
    });
    if (!res.ok) throw new Error(await res.text());
    const { id } = await res.json();
    const newT = {
      id, lat: addLatLng.lat, lon: addLatLng.lng,
      name: name || `${selectedType}トイレ`,
      facility_type: selectedType,
      rating_count: 0, avg_clean: null, avg_crowd: null, wheelchair: 0,
    };
    loadedIds.add(id);
    addMarker(newT);
    document.getElementById('toilet-count').textContent = `${loadedIds.size.toLocaleString()} 件`;
    closeAddModal();
    toggleAddMode();
    openPanel(id);
  } catch (err) {
    document.getElementById('add-error').textContent = `送信失敗: ${err.message}`;
    document.getElementById('add-error').classList.remove('hidden');
    btn.disabled = false;
    btn.textContent = '追加する';
  }
});

// ── イベントリスナー ──────────────────────────────────────────────────────────
document.getElementById('panel-close').addEventListener('click', closePanel);
document.getElementById('open-form-btn').addEventListener('click', openModal);
document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('modal-backdrop').addEventListener('click', closeModal);
document.getElementById('success-close').addEventListener('click', closeModal);
document.getElementById('add-fab').addEventListener('click', toggleAddMode);
document.getElementById('add-modal-close').addEventListener('click', () => {
  closeAddModal();
  if (addMode) toggleAddMode();
});
document.getElementById('add-modal-backdrop').addEventListener('click', () => {
  closeAddModal();
  if (addMode) toggleAddMode();
});

map.on('click', e => {
  if (addMode) {
    addLatLng = e.latlng;
    openAddModal();
    return;
  }
  closePanel();
});

// ── 起動 ─────────────────────────────────────────────────────────────────────
loadViewport();
