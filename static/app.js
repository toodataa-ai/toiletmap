'use strict';

const TOKYO_CENTER = [35.6812, 139.7671];

// ── 地図初期化 ────────────────────────────────────────────────────────────────
const map = L.map('map', { zoomControl: false }).setView(TOKYO_CENTER, 13);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 19,
}).addTo(map);

L.control.zoom({ position: 'bottomright' }).addTo(map);

const clusterGroup = L.markerClusterGroup({
  maxClusterRadius: 50,
  showCoverageOnHover: false,
  spiderfyOnMaxZoom: true,
  chunkedLoading: true,
});
map.addLayer(clusterGroup);

// ── 状態 ─────────────────────────────────────────────────────────────────────
let markers    = new Map();
let loadedIds  = new Set();
let currentId  = null;
let addMode    = false;
let addLatLng  = null;
let selectedType = 'playground';

// ── ユーティリティ ────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function markerColor(parkType, photoCount) {
  if (photoCount > 0)       return '#FFC107';  // 写真あり → 黄
  if (parkType === 'park')  return '#388E3C';  // 公園 → 濃緑
  return '#4CAF50';                            // 遊び場 → 緑
}

// ── マーカー作成 ──────────────────────────────────────────────────────────────
function makeIcon(color) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">
    <circle cx="10" cy="10" r="9" fill="${color}" stroke="white" stroke-width="2"/>
  </svg>`;
  return L.divIcon({ html: svg, className: '', iconSize: [20, 20], iconAnchor: [10, 10] });
}

function addMarker(p) {
  if (markers.has(p.id)) return;
  const marker = L.marker([p.lat, p.lon], {
    icon: makeIcon(markerColor(p.park_type, p.photo_count)),
  });
  marker.on('click', () => openPanel(p.id));
  clusterGroup.addLayer(marker);
  markers.set(p.id, { marker, data: p });
}

function updateMarkerColor(id, parkType, photoCount) {
  const entry = markers.get(id);
  if (!entry) return;
  entry.marker.setIcon(makeIcon(markerColor(parkType, photoCount)));
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
    const data = await fetch(`/api/parks?${p}`).then(r => r.json());
    data.forEach(park => {
      if (!loadedIds.has(park.id)) {
        loadedIds.add(park.id);
        addMarker(park);
      }
    });
    document.getElementById('park-count').textContent = `${loadedIds.size.toLocaleString()} 件`;
  } catch (e) {
    console.error('データ読み込み失敗:', e);
    document.getElementById('park-count').textContent = '取得失敗';
  } finally {
    document.getElementById('loading').classList.add('hidden');
  }
}

function scheduleLoad() {
  clearTimeout(loadTimer);
  loadTimer = setTimeout(loadViewport, 400);
}

map.on('moveend', scheduleLoad);

// ── Wikipedia 写真取得 ────────────────────────────────────────────────────────
async function fetchWikiPhoto(parkName) {
  if (!parkName || ['遊び場', '公園', ''].includes(parkName)) return null;
  try {
    const r = await fetch(
      `https://ja.wikipedia.org/api/rest_v1/page/summary/${encodeURIComponent(parkName)}`,
      { headers: { Accept: 'application/json' } }
    );
    if (!r.ok) return null;
    const d = await r.json();
    return d.thumbnail?.source || d.originalimage?.source || null;
  } catch (e) {
    return null;
  }
}

// ── Google マップ URL ─────────────────────────────────────────────────────────
function gmapUrl(lat, lon, name) {
  return `https://www.google.com/maps/search/${encodeURIComponent(name)}/@${lat},${lon},17z`;
}
function gmapPhotoUrl(lat, lon, name) {
  return `https://www.google.com/maps/search/${encodeURIComponent(name + ' 公園')}/@${lat},${lon},17z/data=!5m1!1e4`;
}
function koentanboUrl(osmId) {
  if (!osmId || !osmId.startsWith('koentanbo_')) return null;
  return `https://www.koentanbo.com/${osmId.slice('koentanbo_'.length)}/`;
}

// ── 詳細パネル ────────────────────────────────────────────────────────────────
async function openPanel(id) {
  currentId = id;
  const cached = markers.get(id)?.data;
  if (cached) renderPanel(cached, []);
  showPanel();
  try {
    const detail = await fetch(`/api/parks/${id}`).then(r => r.json());
    renderPanel(detail, detail.photos || []);
    if (markers.has(id)) markers.get(id).data = detail;

    // 写真がなければ Wikipedia から自動取得を試みる
    if ((detail.photos || []).length === 0) {
      document.getElementById('photo-fetching').classList.remove('hidden');
      const wikiUrl = await fetchWikiPhoto(detail.name);
      document.getElementById('photo-fetching').classList.add('hidden');
      if (wikiUrl) {
        // DB に保存
        await fetch(`/api/parks/${id}/photos`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ photo_url: wikiUrl, caption: 'Wikipedia より自動取得' }),
        });
        // パネルを更新
        const updated = await fetch(`/api/parks/${id}`).then(r => r.json());
        renderPanel(updated, updated.photos || []);
        updateMarkerColor(id, updated.park_type, updated.photo_count);
        if (markers.has(id)) markers.get(id).data = updated;
      } else {
        document.getElementById('no-photos').classList.remove('hidden');
      }
    }
  } catch (e) {
    console.error('詳細取得失敗:', e);
  }
}

function renderPanel(p, photos) {
  document.getElementById('panel-name').textContent = p.name || '公園';
  const typeLabel = p.park_type === 'playground' ? '🛝 遊び場' : '🌳 公園';
  const meta = [typeLabel, p.operator ? `管理: ${p.operator}` : ''].filter(Boolean).join('　');
  document.getElementById('panel-meta').textContent = meta;

  // 外部リンク
  const name = p.name || '公園';
  document.getElementById('gmap-link').href = gmapUrl(p.lat, p.lon, name);
  document.getElementById('gmap-photo-link').href = gmapPhotoUrl(p.lat, p.lon, name);
  const kbHref = koentanboUrl(p.osm_id);
  const kbEl = document.getElementById('kb-link');
  if (kbHref) {
    kbEl.href = kbHref;
    kbEl.classList.remove('hidden');
  } else {
    kbEl.classList.add('hidden');
  }

  const gallery  = document.getElementById('panel-gallery');
  const noPhotos = document.getElementById('no-photos');
  gallery.innerHTML = '';
  noPhotos.classList.add('hidden');
  document.getElementById('photo-fetching').classList.add('hidden');

  if (photos.length > 0) {
    photos.forEach(ph => {
      const item = document.createElement('div');
      item.className = 'gallery-item';
      item.innerHTML = `
        <img src="${escHtml(ph.photo_url)}" alt="${escHtml(ph.caption || '')}"
             onerror="this.parentElement.style.display='none'" />
        ${ph.caption ? `<p class="gallery-caption">${escHtml(ph.caption)}</p>` : ''}
      `;
      item.querySelector('img').addEventListener('click', () =>
        openLightbox(ph.photo_url, ph.caption || '')
      );
      gallery.appendChild(item);
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

// ── ライトボックス ────────────────────────────────────────────────────────────
function openLightbox(url, caption) {
  document.getElementById('lightbox-img').src = url;
  document.getElementById('lightbox-caption').textContent = caption;
  document.getElementById('lightbox').classList.remove('hidden');
}

function closeLightbox() {
  document.getElementById('lightbox').classList.add('hidden');
  document.getElementById('lightbox-img').src = '';
}

document.getElementById('lightbox-close').addEventListener('click', closeLightbox);
document.getElementById('lightbox-backdrop').addEventListener('click', closeLightbox);

// ── 写真投稿モーダル ──────────────────────────────────────────────────────────
function openPhotoModal() {
  if (!currentId) return;
  document.getElementById('photo-url-input').value = '';
  document.getElementById('photo-caption-input').value = '';
  document.getElementById('photo-preview-wrap').classList.add('hidden');
  document.getElementById('photo-preview').src = '';
  document.getElementById('form-error').classList.add('hidden');
  document.getElementById('submit-btn').disabled = false;
  document.getElementById('submit-btn').textContent = '投稿する';
  document.getElementById('photo-form').classList.remove('hidden');
  document.getElementById('submit-success').classList.add('hidden');
  document.getElementById('modal-name').textContent =
    document.getElementById('panel-name').textContent;
  document.getElementById('modal').classList.remove('hidden');
  document.getElementById('modal-backdrop').classList.remove('hidden');
}

function closeModal() {
  document.getElementById('modal').classList.add('hidden');
  document.getElementById('modal-backdrop').classList.add('hidden');
}

// URL入力でプレビュー
document.getElementById('photo-url-input').addEventListener('input', function () {
  const url = this.value.trim();
  const wrap = document.getElementById('photo-preview-wrap');
  const img  = document.getElementById('photo-preview');
  if (url.startsWith('http')) {
    img.src = url;
    wrap.classList.remove('hidden');
    img.onerror = () => wrap.classList.add('hidden');
  } else {
    wrap.classList.add('hidden');
  }
});

document.getElementById('photo-form').addEventListener('submit', async e => {
  e.preventDefault();
  const errEl = document.getElementById('form-error');
  errEl.classList.add('hidden');
  const url     = document.getElementById('photo-url-input').value.trim();
  const caption = document.getElementById('photo-caption-input').value.trim() || null;

  if (!url || !url.startsWith('http')) {
    errEl.textContent = '有効な写真URLを入力してください';
    errEl.classList.remove('hidden');
    return;
  }

  const btn = document.getElementById('submit-btn');
  btn.disabled = true;
  btn.textContent = '送信中…';
  try {
    const res = await fetch(`/api/parks/${currentId}/photos`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ photo_url: url, caption }),
    });
    if (!res.ok) throw new Error(await res.text());
    document.getElementById('photo-form').classList.add('hidden');
    document.getElementById('submit-success').classList.remove('hidden');
    // パネル更新
    const detail = await fetch(`/api/parks/${currentId}`).then(r => r.json());
    renderPanel(detail, detail.photos || []);
    updateMarkerColor(currentId, detail.park_type, detail.photo_count);
    if (markers.has(currentId)) markers.get(currentId).data = detail;
  } catch (err) {
    errEl.textContent = `送信失敗: ${err.message}`;
    errEl.classList.remove('hidden');
    btn.disabled = false;
    btn.textContent = '投稿する';
  }
});

// ── 現在地 ────────────────────────────────────────────────────────────────────
let locationMarker = null;

function flyToCurrentLocation() {
  if (!navigator.geolocation) {
    alert('このブラウザは位置情報に対応していません');
    return;
  }
  const btn = document.getElementById('locate-fab');
  btn.classList.add('locating');
  navigator.geolocation.getCurrentPosition(
    pos => {
      btn.classList.remove('locating');
      const { latitude: lat, longitude: lng } = pos.coords;
      map.setView([lat, lng], 16);
      if (locationMarker) locationMarker.remove();
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">
        <circle cx="10" cy="10" r="7" fill="#1976D2" stroke="white" stroke-width="2.5"/>
        <circle cx="10" cy="10" r="12" fill="#1976D2" fill-opacity="0.18"/>
      </svg>`;
      locationMarker = L.marker([lat, lng], {
        icon: L.divIcon({ html: svg, className: '', iconSize: [20, 20], iconAnchor: [10, 10] }),
        zIndexOffset: 1000,
      }).addTo(map);
    },
    err => {
      btn.classList.remove('locating');
      const msg = {
        1: '位置情報の使用が拒否されました。',
        2: '位置情報を取得できませんでした。',
        3: '位置情報の取得がタイムアウトしました。',
      }[err.code] || '位置情報の取得に失敗しました。';
      alert(msg);
    },
    { enableHighAccuracy: true, timeout: 10000 }
  );
}

// ── 公園追加機能 ──────────────────────────────────────────────────────────────
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
  selectedType = 'playground';
  document.querySelectorAll('.type-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.v === 'playground'));
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
    const res = await fetch('/api/parks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lat: addLatLng.lat, lon: addLatLng.lng, name, park_type: selectedType }),
    });
    if (!res.ok) throw new Error(await res.text());
    const { id } = await res.json();
    const newP = {
      id, lat: addLatLng.lat, lon: addLatLng.lng,
      name: name || (selectedType === 'playground' ? '遊び場' : '公園'),
      park_type: selectedType, photo_count: 0,
    };
    loadedIds.add(id);
    addMarker(newP);
    document.getElementById('park-count').textContent = `${loadedIds.size.toLocaleString()} 件`;
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
document.getElementById('open-photo-btn').addEventListener('click', openPhotoModal);
document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('modal-backdrop').addEventListener('click', closeModal);
document.getElementById('success-close').addEventListener('click', closeModal);
document.getElementById('locate-fab').addEventListener('click', flyToCurrentLocation);
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

// ── 進捗UI ───────────────────────────────────────────────────────────────────
const progressBar  = document.getElementById('progress-bar');
const syncCard     = document.getElementById('sync-card');
const syncCardFill = document.getElementById('sync-card-bar-fill');

function showSyncCard(label, pct, detail, indeterminate = false) {
  syncCard.classList.remove('hidden');
  document.getElementById('sync-card-label').textContent = label;
  document.getElementById('sync-card-detail').textContent = detail;
  const w = Math.max(2, Math.min(100, pct));
  syncCardFill.style.width = `${w}%`;
  if (indeterminate) {
    progressBar.style.width = '0';
    progressBar.classList.add('indeterminate');
  } else {
    progressBar.classList.remove('indeterminate');
    progressBar.style.width = `${w}%`;
  }
}

function hideSyncCard() {
  syncCard.classList.add('hidden');
  progressBar.classList.remove('indeterminate');
  progressBar.style.width = '100%';
  setTimeout(() => { progressBar.style.width = '0'; }, 600);
}

function reloadMarkers() {
  clusterGroup.clearLayers();
  markers.clear();
  loadedIds.clear();
  loadViewport();
}

// ── OSM 同期 ─────────────────────────────────────────────────────────────────
let syncPollTimer = null;

async function startSync() {
  const btn = document.getElementById('sync-btn');
  btn.disabled = true;
  btn.textContent = '⏳';
  showSyncCard('🔄 OSMから公園データを取得中…', 0, '開始しています…', true);

  try { await fetch('/api/sync', { method: 'POST' }); } catch (e) { /* ignore */ }

  let prev = 0;
  let tries = 0;
  clearInterval(syncPollTimer);
  syncPollTimer = setInterval(async () => {
    tries++;
    try {
      const d = await fetch('/api/stats').then(r => r.json());
      document.getElementById('park-count').textContent = `${d.parks.toLocaleString()} 件`;
      showSyncCard('🔄 OSMから公園データを取得中…', 0,
        `取得済み: ${d.parks.toLocaleString()} 件`, true);
      if (d.parks > prev || tries > 60) {
        clearInterval(syncPollTimer);
        btn.disabled = false;
        btn.textContent = '🔄';
        hideSyncCard();
        reloadMarkers();
      }
      prev = d.parks;
    } catch (e) { /* ignore */ }
  }, 5000);
}

document.getElementById('sync-btn').addEventListener('click', () => {
  if (confirm('OSM（OpenStreetMap）から東京の公園データを取得します。\n数分かかります。よろしいですか？')) {
    startSync();
  }
});

// ── 公園探訪郊外 同期 ─────────────────────────────────────────────────────────
let kbPollTimer = null;
let kbStartTime = 0;

async function startKoentanboSync() {
  const btn = document.getElementById('kb-sync-btn');
  btn.disabled = true;
  btn.textContent = '⏳';
  kbStartTime = Date.now();
  showSyncCard('🌐 公園探訪郊外からデータ取得中…', 0, '開始しています…', true);

  try { await fetch('/api/sync/koentanbo', { method: 'POST' }); } catch (e) { /* ignore */ }

  clearInterval(kbPollTimer);
  kbPollTimer = setInterval(async () => {
    try {
      const s = await fetch('/api/sync/koentanbo/status').then(r => r.json());
      const pct = s.total > 0 ? Math.round(s.done / s.total * 100) : 0;

      let etaText = '';
      if (s.done > 10 && s.total > 0) {
        const elapsed = (Date.now() - kbStartTime) / 1000;
        const rate    = s.done / elapsed;
        const eta     = Math.round((s.total - s.done) / rate);
        etaText = eta > 60
          ? `　残り約${Math.round(eta / 60)}分`
          : `　残り約${eta}秒`;
      }

      const detail = s.total === 0 && s.skipped > 0
        ? `全件登録済み・更新なし (スキップ: ${s.skipped.toLocaleString()}件)`
        : `${s.done.toLocaleString()} / ${s.total.toLocaleString()} 件処理`
          + `　登録/更新: ${s.inserted}件`
          + (s.skipped > 0 ? `　スキップ: ${s.skipped.toLocaleString()}件` : '')
          + etaText;

      document.getElementById('park-count').textContent =
        s.total > 0 ? `${s.done.toLocaleString()}/${s.total.toLocaleString()} 件` : '完了';

      showSyncCard('🌐 公園探訪郊外からデータ取得中…', pct, detail, s.total === 0);

      if (!s.running) {
        clearInterval(kbPollTimer);
        btn.disabled = false;
        btn.textContent = '🌐';
        hideSyncCard();
        reloadMarkers();
      }
    } catch (e) { /* ignore */ }
  }, 3000);
}

document.getElementById('kb-sync-btn').addEventListener('click', () => {
  if (confirm('公園探訪郊外（koentanbo.com）から公園データ・写真を取得します。\n初回は2〜3分、2回目以降は差分のみなので数秒〜数十秒で完了します。')) {
    startKoentanboSync();
  }
});

// 起動時にDBが空ならば自動でポーリング開始（初回起動時の同期を監視）
fetch('/api/stats').then(r => r.json()).then(d => {
  if (d.parks === 0) {
    showSyncCard('🔄 公園データを初回取得中…', 0, '開始しています…', true);
    let tries = 0;
    const t = setInterval(async () => {
      tries++;
      try {
        const d2 = await fetch('/api/stats').then(r => r.json());
        document.getElementById('park-count').textContent = `取得中… ${d2.parks.toLocaleString()} 件`;
        showSyncCard('🔄 公園データを初回取得中…', 0,
          `取得済み: ${d2.parks.toLocaleString()} 件`, true);
        if (d2.parks > 0 || tries > 60) {
          clearInterval(t);
          hideSyncCard();
          reloadMarkers();
        }
      } catch (e) { /* ignore */ }
    }, 5000);
  }
}).catch(() => {});

// ── 起動 ─────────────────────────────────────────────────────────────────────
loadViewport();
