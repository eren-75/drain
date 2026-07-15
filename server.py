from flask import Flask, request, jsonify, render_template_string
from datetime import datetime, timedelta
import sqlite3
import threading
import time
import os

app = Flask(__name__)
# Kalici disk (volume) yolu ortam degiskeninden okunuyor.
# Railway'de: Volume ekleyip mount path'i DATA_DIR olarak Variables'a yazarsin.
# Render'da: render.yaml zaten /var/data'ya baglar, onu kullanmak icin
#            DATA_DIR=/var/data set edebilirsin (ya da bos birak, "." kullanilir).
# Hicbir volume/env yoksa "." (calisma dizini) kullanilir — ama bu durumda
# yeniden deploy'da veritabani sifirlanabilir (ephemeral disk).
_DATA_DIR = os.environ.get("DATA_DIR", ".")
if not os.path.isdir(_DATA_DIR):
    os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, "drain_monitor.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS measurements (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id    TEXT    NOT NULL,
            session_id    INTEGER NOT NULL DEFAULT 1,
            weight        REAL    NOT NULL,
            hourly_output REAL    NOT NULL,
            status        TEXT    NOT NULL,
            fluid_intake  REAL    NOT NULL DEFAULT 0,
            room_number   INTEGER NOT NULL DEFAULT 0,
            timestamp     TEXT    NOT NULL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id   TEXT    NOT NULL,
            device_id    TEXT    DEFAULT '',
            started_at   TEXT    NOT NULL,
            ended_at     TEXT,
            total_output REAL    DEFAULT 0,
            note         TEXT,
            room_number  INTEGER DEFAULT 0,
            patient_name TEXT    DEFAULT ''
        )
    ''')
    try:
        c.execute('ALTER TABLE measurements ADD COLUMN room_number INTEGER DEFAULT 0')
    except Exception:
        pass
    try:
        c.execute('ALTER TABLE sessions ADD COLUMN room_number INTEGER DEFAULT 0')
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE sessions ADD COLUMN patient_name TEXT DEFAULT ''")
    except Exception:
        pass
    conn.commit()
    conn.close()
    print("[DB] Veritabani hazir.")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_active_session(patient_id):
    conn = get_db()
    row = conn.execute('''
        SELECT * FROM sessions
        WHERE TRIM(patient_id) = TRIM(?) AND ended_at IS NULL
        ORDER BY id DESC LIMIT 1
    ''', (patient_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def create_session(patient_id, device_id="", room_number=0, patient_name=""):
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute('''
        INSERT INTO sessions (patient_id, device_id, started_at, room_number, patient_name)
        VALUES (?, ?, ?, ?, ?)
    ''', (patient_id, device_id, now, room_number, patient_name))
    session_id = cur.lastrowid
    conn.commit()
    conn.close()
    print(f"[SESSION] {patient_id} ({device_id}) Oda:{room_number} yeni session: {session_id}")
    return session_id

def close_session(session_id):
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = conn.execute('''
        SELECT COALESCE(SUM(hourly_output), 0) as total
        FROM measurements
        WHERE session_id = ? AND status NOT IN ('reset', 'live') AND hourly_output > 0
    ''', (session_id,)).fetchone()['total']
    conn.execute('''
        UPDATE sessions SET ended_at = ?, total_output = ? WHERE id = ?
    ''', (now, total, session_id))
    conn.commit()
    conn.close()
    print(f"[SESSION] Session {session_id} kapatildi. Toplam: {total}g")

def auto_close_sessions():
    while True:
        try:
            conn = get_db()
            threshold = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            stale = conn.execute('''
                SELECT s.id, s.patient_id FROM sessions s
                WHERE s.ended_at IS NULL
                AND (
                    SELECT MAX(m.timestamp) FROM measurements m
                    WHERE m.session_id = s.id
                ) < ?
            ''', (threshold,)).fetchall()
            conn.close()
            for row in stale:
                print(f"[AUTO] {row['patient_id']} session {row['id']} kapatiliyor...")
                close_session(row['id'])
        except Exception as e:
            print(f"[AUTO] Hata: {e}")
        time.sleep(5)

def auto_cleanup():
    while True:
        try:
            conn = get_db()
            threshold = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            deleted = conn.execute('DELETE FROM measurements WHERE timestamp < ?', (threshold,)).rowcount
            conn.commit()
            conn.close()
            if deleted > 0:
                print(f"[CLEANUP] {deleted} eski olcum silindi.")
        except Exception as e:
            print(f"[CLEANUP] Hata: {e}")
        time.sleep(3600)

# Not: Bulut deploy mimarisinde server ESP32'ye dogrudan istek atamaz
# (ESP32 NAT arkasinda, disaridan ulasilamaz). Bunun yerine ESP32 zaten
# her "live" mesajinda /api/measurement'a istek atiyor ve response
# icinde guncel fluid_intake degerini okuyup kendi 40016 register'ini
# gunceller. Intake sayfasindan (veya Kinco'dan) eklenen su, bir sonraki
# ESP32 "live" istegi geldiginde (en gec ~2 saniye icinde) otomatik
# senkronize olur — ayri bir bildirim mekanizmasina gerek yok.

# ─────────────────────────────────────────
# ANA SAYFA
# ─────────────────────────────────────────
HOME_HTML = '''
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Drain Monitor</title>
  <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body { font-family:Arial,sans-serif; background:#0f172a; color:#e2e8f0; }
    header {
      background:#1e293b; padding:16px 24px;
      border-bottom:2px solid #334155;
      display:flex; align-items:center; justify-content:space-between;
    }
    header h1 { font-size:20px; color:#38bdf8; }
    .container { padding:24px; max-width:1400px; margin:auto; }
    .top-bar { display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; }
    .tabs { display:flex; gap:8px; }
    .tab {
      padding:8px 16px; border-radius:8px; font-size:13px;
      cursor:pointer; border:1px solid #334155;
      background:#1e293b; color:#94a3b8;
    }
    .tab.active { background:#38bdf8; color:#0f172a; border-color:#38bdf8; font-weight:bold; }
    .refresh-btn {
      background:#38bdf8; color:#0f172a; border:none;
      border-radius:8px; padding:8px 16px; font-size:14px;
      font-weight:bold; cursor:pointer;
    }
    .refresh-btn:hover { background:#7dd3fc; }
    .timer { font-size:12px; color:#64748b; }

    .filter-bar { display:flex; gap:8px; margin-bottom:16px; flex-wrap:wrap; align-items:center; }
    .filter-bar label { color:#94a3b8; font-size:13px; }
    .filter-bar select {
      background:#1e293b; color:#e2e8f0;
      border:1px solid #334155; border-radius:8px;
      padding:6px 12px; font-size:13px;
    }

    .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:16px; }
    .drain-card {
      background:#1e293b; border-radius:12px; padding:20px;
      border:1px solid #334155; cursor:pointer;
      transition:border-color 0.2s, transform 0.1s;
      text-decoration:none; color:inherit; display:block;
    }
    .drain-card:hover { border-color:#38bdf8; transform:translateY(-2px); }
    .drain-card.closed { opacity:0.75; }
    .drain-card .header-row { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }
    .drain-card .patient { font-size:17px; font-weight:bold; }
    .drain-card .device-tag {
      font-size:11px; background:#0f172a; color:#38bdf8;
      padding:2px 8px; border-radius:10px; border:1px solid #334155;
    }
    .drain-card .row { display:flex; justify-content:space-between; margin-bottom:6px; font-size:13px; }
    .drain-card .label { color:#94a3b8; }
    .drain-card .value { font-weight:bold; }
    .drain-card .divider { border-top:1px solid #334155; margin:10px 0; }
    .drain-card .stat-row { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:8px; }
    .drain-card .stat { background:#0f172a; border-radius:8px; padding:8px 10px; }
    .drain-card .stat .s-label { font-size:10px; color:#64748b; margin-bottom:2px; }
    .drain-card .stat .s-value { font-size:15px; font-weight:bold; }
    .drain-card .stat .s-value.blue   { color:#38bdf8; }
    .drain-card .stat .s-value.green  { color:#4ade80; }
    .drain-card .stat .s-value.purple { color:#a78bfa; }
    .badge { display:inline-block; padding:3px 10px; border-radius:20px; font-size:12px; font-weight:bold; }
    .badge.normal  { background:#14532d; color:#4ade80; }
    .badge.blocked { background:#713f12; color:#facc15; }
    .badge.full    { background:#7f1d1d; color:#f87171; }
    .badge.reset   { background:#1e3a5f; color:#38bdf8; }
    .badge.closed  { background:#1e293b; color:#64748b; border:1px solid #334155; }
    .no-data { text-align:center; color:#64748b; padding:60px; font-size:16px; }
  </style>
</head>
<body>
<header>
  <div>
    <h1>🏥 Drain Monitor</h1>
    <span style="font-size:13px;color:#94a3b8">Hasta drain takip sistemi</span>
  </div>
</header>
<div class="container">
  <div class="top-bar">
    <div class="tabs">
      <div class="tab active" onclick="switchTab('active',this)">🟢 Aktif</div>
      <div class="tab" onclick="switchTab('history',this)">📁 Geçmiş</div>
    </div>
    <div style="display:flex;align-items:center;gap:12px">
      <span class="timer" id="timer">Yenileniyor: 3s</span>
      <button class="refresh-btn" onclick="loadDrains()">🔄 Yenile</button>
    </div>
  </div>

  <div class="filter-bar">
    <label>Cihaz:</label>
    <select id="deviceFilter" onchange="loadDrains()">
      <option value="">Tüm Cihazlar</option>
    </select>
  </div>

  <div class="grid" id="grid"><div class="no-data">Yükleniyor...</div></div>
</div>
<script>
let countdown = 3;
let currentTab = 'active';

const badgeMap = {
  normal:  '<span class="badge normal">✅ Normal</span>',
  blocked: '<span class="badge blocked">⚠️ Tıkalı</span>',
  full:    '<span class="badge full">🚨 Dolu</span>',
  reset:   '<span class="badge reset">🔄 Reset</span>',
  closed:  '<span class="badge closed">⬛ Kapalı</span>',
  live:    '<span class="badge" style="background:#0f3460;color:#38bdf8;border:1px solid #38bdf8">📡 Canlı</span>',
};

async function loadDevices() {
  const res  = await fetch('/api/devices');
  const data = await res.json();
  const sel  = document.getElementById('deviceFilter');
  const cur  = sel.value;
  sel.innerHTML = '<option value="">Tüm Cihazlar</option>';
  data.forEach(d => {
    if (d) sel.innerHTML += `<option value="${d}" ${d===cur?'selected':''}>${d}</option>`;
  });
}

function switchTab(tab, el) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  loadDrains();
}

async function loadDrains() {
  const device = document.getElementById('deviceFilter').value;
  let url = currentTab === 'active'
    ? '/api/patients/summary?active=true'
    : '/api/patients/summary?active=false';
  if (device) url += '&device_id=' + encodeURIComponent(device);

  const res  = await fetch(url);
  const data = await res.json();
  const grid = document.getElementById('grid');

  if (!data.length) {
    grid.innerHTML = '<div class="no-data">' +
      (currentTab === 'active' ? 'Aktif drain yok' : 'Geçmiş kayıt yok') + '</div>';
    return;
  }

  grid.innerHTML = data.map(d => `
    <a class="drain-card ${d.ended_at ? 'closed' : ''}" href="/drain/${d.patient_id}/${d.id}">
      <div class="header-row">
        <div>
          <div class="patient">🩺 ${d.patient_id}</div>
          ${d.room_number ? `<div style="font-size:11px;color:#64748b;margin-top:2px">🏥 Oda: ${d.room_number}</div>` : ''}
        </div>
        ${d.device_id ? `<div class="device-tag">📟 ${d.device_id}</div>` : ''}
      </div>

      ${currentTab === 'active' ? `
      <div class="stat-row">
        <div class="stat">
          <div class="s-label">ANLIK MİKTAR</div>
          <div class="s-value blue">${(d.last_weight||0).toFixed(1)} ml</div>
        </div>
        <div class="stat">
          <div class="s-label">SON SAATLİK</div>
          <div class="s-value green">${(d.last_hourly||0).toFixed(1)} ml</div>
        </div>
      </div>
      <div class="stat-row">
        <div class="stat">
          <div class="s-label">TOPLAM ÇEKİLEN</div>
          <div class="s-value purple">${(d.total_output||0).toFixed(1)} ml</div>
        </div>
        <div class="stat">
          <div class="s-label">İÇİLEN SIVI</div>
          <div class="s-value" style="color:#fb923c">${(d.last_fluid||0).toFixed(1)} ml</div>
        </div>
      </div>
      <div class="stat-row">
        <div class="stat" style="grid-column:span 2">
          <div class="s-label">DURUM</div>
          <div class="s-value">${badgeMap[d.last_status] || '—'}</div>
        </div>
      </div>
      <div class="row">
        <span class="label">Başlangıç</span>
        <span class="value" style="font-size:11px;color:#64748b">${d.started_at}</span>
      </div>
      ` : `
      <div class="row">
        <span class="label">Başlangıç</span>
        <span class="value" style="font-size:11px;color:#64748b">${d.started_at}</span>
      </div>
      <div class="row">
        <span class="label">Bitiş</span>
        <span class="value" style="font-size:11px;color:#64748b">${d.ended_at||'—'}</span>
      </div>
      <div class="row">
        <span class="label">Durum</span>
        <span class="value">${badgeMap.closed}</span>
      </div>
      <div class="divider"></div>
      <div class="row">
        <span class="label">💜 Toplam Çekilen</span>
        <span class="value" style="color:#a78bfa;font-weight:bold">${(d.total_output||0).toFixed(1)} ml</span>
      </div>
      `}
    </a>
  `).join('');

  countdown = 3;
}

setInterval(() => {
  countdown--;
  document.getElementById('timer').textContent = 'Yenileniyor: ' + countdown + 's';
  if (countdown <= 0) { loadDevices(); loadDrains(); }
}, 1000);

loadDevices().then(() => loadDrains());
</script>
</body>
</html>
'''

DETAIL_HTML = '''
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ patient_id }} - İşlem {{ session_id }}</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
  <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body { font-family:Arial,sans-serif; background:#0f172a; color:#e2e8f0; }
    header {
      background:#1e293b; padding:16px 24px;
      border-bottom:2px solid #334155;
      display:flex; align-items:center; gap:16px;
    }
    .back { color:#38bdf8; text-decoration:none; font-size:14px; }
    header h1 { font-size:20px; color:#38bdf8; }
    .container { padding:24px; max-width:1200px; margin:auto; }
    .refresh-btn {
      background:#38bdf8; color:#0f172a; border:none;
      border-radius:8px; padding:8px 16px; font-size:14px;
      font-weight:bold; cursor:pointer;
    }
    .meta-bar {
      background:#1e293b; border-radius:8px; padding:12px 16px;
      display:flex; gap:24px; margin-bottom:20px; font-size:13px;
      border:1px solid #334155; flex-wrap:wrap;
    }
    .meta-bar .item { display:flex; flex-direction:column; gap:2px; }
    .meta-bar .m-label { color:#64748b; font-size:11px; }
    .meta-bar .m-value { color:#e2e8f0; font-weight:bold; }
    .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:16px; margin-bottom:24px; }
    .card { background:#1e293b; border-radius:12px; padding:20px; border:1px solid #334155; }
    .card .label { font-size:12px; color:#94a3b8; margin-bottom:8px; }
    .card .value { font-size:24px; font-weight:bold; }
    .card.info    .value { color:#38bdf8; }
    .card.normal  .value { color:#4ade80; }
    .card.warning .value { color:#facc15; }
    .card.danger  .value { color:#f87171; }
    .card.purple  .value { color:#a78bfa; }
    .badge { display:inline-block; padding:4px 10px; border-radius:20px; font-size:12px; font-weight:bold; }
    .badge.normal  { background:#14532d; color:#4ade80; }
    .badge.blocked { background:#713f12; color:#facc15; }
    .badge.full    { background:#7f1d1d; color:#f87171; }
    .badge.reset   { background:#1e3a5f; color:#38bdf8; }
    .chart-box { background:#1e293b; border-radius:12px; padding:20px; border:1px solid #334155; margin-bottom:24px; overflow-x:auto; overflow-y:hidden; }
    .chart-box h2 { font-size:15px; color:#94a3b8; margin-bottom:16px; }
    .table-box { background:#1e293b; border-radius:12px; padding:20px; border:1px solid #334155; max-height:400px; overflow-y:auto; }
    .table-box h2 { font-size:15px; color:#94a3b8; margin-bottom:16px; }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    thead { position:sticky; top:0; background:#1e293b; z-index:1; }
    th { text-align:left; color:#64748b; padding:8px 12px; border-bottom:1px solid #334155; }
    td { padding:10px 12px; border-bottom:1px solid #1e293b; }
    tr:hover td { background:#0f172a; }
    .timer { font-size:12px; color:#64748b; }
    .closed-banner {
      background:#1e293b; color:#94a3b8; padding:10px 16px;
      border-radius:8px; margin-bottom:16px; font-size:13px;
      border:1px solid #334155;
    }
  </style>
</head>
<body>
<header>
  <a class="back" href="/">← Geri</a>
  <h1>🩺 {{ patient_id }}</h1>
  <span style="margin-left:auto;display:flex;align-items:center;gap:10px">
    <span class="timer" id="timer">Yenileniyor: 3s</span>
    <button class="refresh-btn" onclick="loadData()">🔄 Yenile</button>
  </span>
</header>
<div class="container">
  <div id="closedBanner" style="display:none" class="closed-banner">
    ⬛ Bu işlem kapatılmıştır. Veriler salt okunur modda gösterilmektedir.
  </div>

  <div class="meta-bar" id="metaBar"></div>

  <div class="stats">
    <div class="card info">
      <div class="label">ANLIK MİKTAR</div>
      <div class="value" id="weight">—</div>
    </div>
    <div class="card normal">
      <div class="label">SON SAATLİK ÇIKIŞ</div>
      <div class="value" id="hourly">—</div>
    </div>
    <div class="card purple">
      <div class="label">TOPLAM ÇEKİLEN</div>
      <div class="value" id="total_output">—</div>
    </div>
    <div class="card" style="border-color:#fb923c">
      <div class="label" style="color:#fb923c">İÇİLEN SIVI</div>
      <div class="value" id="fluid_intake" style="color:#fb923c">—</div>
    </div>
    <div class="card" style="border-color:#f472b6">
      <div class="label" style="color:#f472b6">FARK (İÇİLEN - ÇEKİLEN)</div>
      <div class="value" id="diff_value" style="color:#f472b6">—</div>
    </div>
    <div class="card" style="border-color:#facc15">
      <div class="label" style="color:#facc15">SAATLİK ORTALAMA</div>
      <div class="value" id="avg_value" style="color:#facc15">—</div>
    </div>
    <div class="card info">
      <div class="label">TOPLAM ÖLÇÜM</div>
      <div class="value" id="total">—</div>
    </div>
    <div class="card" id="statusCard">
      <div class="label">DURUM</div>
      <div class="value" id="status">—</div>
    </div>
  </div>

  <div class="chart-box">
    <h2>📈 Saatlik Drain Çıkışı (ml)</h2>
    <div style="min-width: 800px;">
      <canvas id="chart" height="80"></canvas>
    </div>
  </div>

  <div class="table-box">
    <h2>📋 Ölçüm Geçmişi</h2>
    <table>
      <thead>
        <tr><th>Zaman</th><th>Miktar</th><th>Saatlik</th><th>İçilen Sıvı</th><th>Durum</th></tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</div>
<script>
const patientId = "{{ patient_id }}";
const sessionId = "{{ session_id }}";
let chart = null;
let countdown = 3;
const badgeMap = {
  normal:  '<span class="badge normal">✅ Normal</span>',
  blocked: '<span class="badge blocked">⚠️ Tıkalı</span>',
  full:    '<span class="badge full">🚨 Dolu</span>',
  reset:   '<span class="badge reset">🔄 Reset</span>',
  live:    '<span class="badge" style="background:#0f3460;color:#38bdf8;border:1px solid #38bdf8">📡 Canlı</span>',
};
const statusMap = {
  normal:  { text:'✅ Normal',  cls:'normal'  },
  blocked: { text:'⚠️ Tıkalı', cls:'warning' },
  full:    { text:'🚨 Dolu',   cls:'danger'  },
  reset:   { text:'🔄 Reset',  cls:'info'    },
  live:    { text:'📡 Canlı',  cls:'info'    },
};
async function loadData() {
  const sRes = await fetch('/api/sessions/' + patientId);
  const sessions = await sRes.json();
  const session = sessions.find(s => s.id == sessionId);

  if (session) {
    if (session.ended_at) {
      document.getElementById('closedBanner').style.display = 'block';
    }
    document.getElementById('metaBar').innerHTML = `
      <div class="item"><div class="m-label">HASTA</div><div class="m-value">${session.patient_id}</div></div>
      <div class="item"><div class="m-label">ODA</div><div class="m-value">${session.room_number || '—'}</div></div>
      <div class="item"><div class="m-label">CİHAZ</div><div class="m-value">${session.device_id || '—'}</div></div>
      <div class="item"><div class="m-label">BAŞLANGIÇ</div><div class="m-value">${session.started_at}</div></div>
      <div class="item"><div class="m-label">BİTİŞ</div><div class="m-value">${session.ended_at || 'Devam ediyor'}</div></div>
      <div class="item"><div class="m-label">İŞLEM NO</div><div class="m-value">#${session.id}</div></div>
    `;
  }

  const res  = await fetch(`/api/measurements?patient_id=${patientId}&session_id=${sessionId}`);
  const data = await res.json();
  if (!data.length) {
    document.getElementById('tbody').innerHTML =
      '<tr><td colspan="4" style="text-align:center;color:#64748b;padding:20px">Veri yok</td></tr>';
    return;
  }

  const latest = data[0];
  const totalOutput = data
    .filter(d => d.status !== 'reset' && d.hourly_output > 0)
    .reduce((sum, d) => sum + d.hourly_output, 0);

  const latestFluid = latest.fluid_intake || 0;

  document.getElementById('weight').textContent       = latest.weight.toFixed(1) + ' ml';
  document.getElementById('hourly').textContent       = latest.hourly_output.toFixed(1) + ' ml';
  document.getElementById('total_output').textContent = totalOutput.toFixed(1) + ' ml';
  document.getElementById('fluid_intake').textContent = latestFluid.toFixed(1) + ' ml';
  const diff = Math.max(0, latestFluid - totalOutput);
  document.getElementById('diff_value').textContent = diff.toFixed(1) + ' ml';
  const hourlyRows = data.filter(d => d.status !== 'reset' && d.status !== 'live' && d.status !== 'fluid_update' && d.hourly_output > 0);
  const avg = hourlyRows.length > 0 ? (totalOutput / hourlyRows.length) : 0;
  document.getElementById('avg_value').textContent = avg.toFixed(1) + ' ml';
  document.getElementById('total').textContent        = data.length;

  const s = statusMap[latest.status] || { text: latest.status, cls: 'info' };
  document.getElementById('status').textContent = s.text;
  document.getElementById('statusCard').className = 'card ' + s.cls;

  const filtered = data
    .filter(d => ['normal', 'blocked', 'full'].includes(d.status))
    .slice(0, 24)
    .reverse();
  const minWidth = Math.max(800, filtered.length * 60);
  document.getElementById('chart').width  = minWidth;
  document.getElementById('chart').height = 300;
  if (chart) chart.destroy();
  chart = new Chart(document.getElementById('chart'), {
    type: 'bar',
    data: {
      labels: filtered.map(d => d.timestamp.slice(11, 16)),
      datasets: [{
        label: 'Saatlik Çıkış (ml)',
        data: filtered.map(d => d.hourly_output),
        backgroundColor: filtered.map(d =>
          d.status === 'blocked' ? '#facc15' :
          d.status === 'full'    ? '#f87171' : '#4ade80'),
        borderRadius: 6,
      }]
    },
    options: {
      responsive: false,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color:'#94a3b8' }, grid: { color:'#1e293b' } },
        y: { ticks: { color:'#94a3b8' }, grid: { color:'#334155' } }
      }
    }
  });

  document.getElementById('tbody').innerHTML = data
    .filter(d => !['live', 'fluid_update'].includes(d.status))
    .slice(0, 100)
    .map(d => `
    <tr>
      <td>${d.timestamp}</td>
      <td>${d.weight.toFixed(1)} ml</td>
      <td>${d.hourly_output.toFixed(1)} ml</td>
      <td style="color:#fb923c">${(d.fluid_intake||0).toFixed(1)} ml</td>
      <td>${badgeMap[d.status] || d.status}</td>
    </tr>
  `).join('');

  countdown = 3;
}
setInterval(() => {
  countdown--;
  document.getElementById('timer').textContent = 'Yenileniyor: ' + countdown + 's';
  if (countdown <= 0) loadData();
}, 1000);
loadData();
</script>
</body>
</html>
'''

INTAKE_HTML = '''
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
  <title>{{ patient_id }} — Sıvı Takibi</title>
  <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body {
      font-family: Arial, sans-serif;
      background: #0f172a;
      color: #e2e8f0;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
    }
    header {
      width: 100%;
      background: #1e293b;
      border-bottom: 2px solid #334155;
      text-align: center;
      padding: 20px 16px 16px;
    }
    header h1 { font-size: 24px; font-weight: bold; color: #38bdf8; }
    header p  { font-size: 14px; margin-top: 4px; color: #94a3b8; }

    .main-card {
      background: #1e293b;
      border-radius: 16px;
      border: 1px solid #334155;
      padding: 24px 20px;
      margin: 20px 16px;
      width: calc(100% - 32px);
      max-width: 500px;
    }

    .section-title {
      font-size: 13px;
      font-weight: bold;
      color: #64748b;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 16px;
    }

    .bottle-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-bottom: 24px;
    }
    .bottle-card {
      background: #1e293b;
      border: 2px solid #334155;
      border-radius: 14px;
      padding: 16px 10px 14px;
      display: flex;
      flex-direction: column;
      align-items: center;
      cursor: pointer;
      transition: border-color 0.15s, background 0.15s, transform 0.15s;
      user-select: none;
    }
    .bottle-card:active { transform: scale(0.97); }
    .bottle-card.selected {
      border-color: #38bdf8;
      background: #0f3460;
      transform: scale(1.05);
    }
    .bottle-label {
      font-size: 22px;
      font-weight: bold;
      color: #38bdf8;
      margin-top: 10px;
    }
    .bottle-sub {
      font-size: 12px;
      color: #64748b;
      margin-top: 2px;
    }

    .divider {
      border: none;
      border-top: 1px solid #334155;
      margin: 4px 0 20px;
    }

    .manual-group {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .ml-input {
      width: 100%;
      padding: 16px;
      font-size: 26px;
      font-weight: bold;
      border: 2px solid #334155;
      border-radius: 12px;
      text-align: center;
      color: #e2e8f0;
      background: #0f172a;
      outline: none;
      transition: border-color 0.15s;
    }
    .ml-input:focus { border-color: #38bdf8; }
    .ml-input::placeholder { color: #475569; }

    .add-btn {
      width: 100%;
      padding: 18px;
      font-size: 18px;
      font-weight: bold;
      background: #334155;
      color: #64748b;
      border: none;
      border-radius: 12px;
      cursor: not-allowed;
      transition: background 0.15s, color 0.15s;
    }
    .add-btn.active {
      background: #38bdf8;
      color: #0f172a;
      cursor: pointer;
    }
    .add-btn.active:hover  { background: #7dd3fc; }
    .add-btn.active:active { background: #0ea5e9; }

    #msg {
      display: none;
      margin: 0 16px 20px;
      width: calc(100% - 32px);
      max-width: 500px;
      padding: 16px 18px;
      border-radius: 12px;
      font-size: 16px;
      font-weight: bold;
      text-align: center;
      line-height: 1.5;
    }
    #msg.ok    { background: #14532d; color: #4ade80; border: 1px solid #4ade80; }
    #msg.error { background: #7f1d1d; color: #f87171; border: 1px solid #f87171; }

    .modal-overlay {
      display: none;
      position: fixed; inset: 0;
      background: rgba(0,0,0,0.75);
      z-index: 100;
      align-items: center;
      justify-content: center;
    }
    .modal-overlay.show { display: flex; }
    .modal-box {
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 16px;
      padding: 32px 24px;
      max-width: 320px;
      width: calc(100% - 48px);
      text-align: center;
    }
    .modal-title {
      font-size: 18px;
      font-weight: bold;
      color: #e2e8f0;
      margin-bottom: 8px;
    }
    .modal-amount {
      font-size: 36px;
      font-weight: bold;
      color: #38bdf8;
      margin: 12px 0 20px;
    }
    .modal-sub {
      font-size: 14px;
      color: #94a3b8;
      margin-bottom: 24px;
    }
    .modal-btns { display: flex; gap: 12px; }
    .modal-btn {
      flex: 1;
      padding: 14px;
      font-size: 15px;
      font-weight: bold;
      border: none;
      border-radius: 10px;
      cursor: pointer;
      transition: opacity 0.15s;
    }
    .modal-btn:active { opacity: 0.8; }
    .modal-btn.confirm { background: #38bdf8; color: #0f172a; }
    .modal-btn.cancel  { background: #334155; color: #94a3b8; }
  </style>
</head>
<body>
<header>
  <h1>{{ patient_id }}</h1>
  <p>Sıvı Takibi</p>
</header>

<div class="main-card">
  <div class="section-title">Şişe Seç</div>
  <div class="bottle-grid">

    <div class="bottle-card" onclick="selectBottle(this, 500)">
      <svg viewBox="0 0 60 120" width="60" height="120">
        <rect x="22" y="8" width="16" height="10" rx="3" fill="#38bdf8" opacity="0.7"/>
        <rect x="18" y="18" width="24" height="4" rx="2" fill="#1e40af"/>
        <rect x="14" y="22" width="32" height="80" rx="10" fill="#1e3a5f" stroke="#38bdf8" stroke-width="2"/>
        <rect x="14" y="72" width="32" height="30" rx="0 0 10 10" fill="#38bdf8" opacity="0.4"/>
        <text x="30" y="55" text-anchor="middle" fill="#38bdf8" font-size="10" font-weight="bold">500</text>
        <text x="30" y="68" text-anchor="middle" fill="#94a3b8" font-size="8">ml</text>
      </svg>
      <div class="bottle-label">0.5L</div>
      <div class="bottle-sub">500 ml</div>
    </div>

    <div class="bottle-card" onclick="selectBottle(this, 1000)">
      <svg viewBox="0 0 60 130" width="60" height="130">
        <rect x="20" y="6" width="20" height="12" rx="3" fill="#38bdf8" opacity="0.7"/>
        <rect x="16" y="18" width="28" height="5" rx="2" fill="#1e40af"/>
        <rect x="12" y="23" width="36" height="90" rx="12" fill="#1e3a5f" stroke="#38bdf8" stroke-width="2"/>
        <rect x="12" y="78" width="36" height="35" rx="0 0 12 12" fill="#38bdf8" opacity="0.4"/>
        <text x="30" y="57" text-anchor="middle" fill="#38bdf8" font-size="11" font-weight="bold">1000</text>
        <text x="30" y="70" text-anchor="middle" fill="#94a3b8" font-size="8">ml</text>
      </svg>
      <div class="bottle-label">1L</div>
      <div class="bottle-sub">1000 ml</div>
    </div>

    <div class="bottle-card" onclick="selectBottle(this, 1500)">
      <svg viewBox="0 0 60 150" width="60" height="150">
        <rect x="20" y="5" width="20" height="14" rx="3" fill="#38bdf8" opacity="0.7"/>
        <rect x="15" y="19" width="30" height="5" rx="2" fill="#1e40af"/>
        <rect x="11" y="24" width="38" height="110" rx="12" fill="#1e3a5f" stroke="#38bdf8" stroke-width="2"/>
        <rect x="11" y="89" width="38" height="45" rx="0 0 12 12" fill="#38bdf8" opacity="0.4"/>
        <text x="30" y="65" text-anchor="middle" fill="#38bdf8" font-size="11" font-weight="bold">1500</text>
        <text x="30" y="78" text-anchor="middle" fill="#94a3b8" font-size="8">ml</text>
      </svg>
      <div class="bottle-label">1.5L</div>
      <div class="bottle-sub">1500 ml</div>
    </div>

    <div class="bottle-card" onclick="selectBottle(this, 2000)">
      <svg viewBox="0 0 70 160" width="70" height="160">
        <rect x="22" y="4" width="26" height="15" rx="4" fill="#38bdf8" opacity="0.7"/>
        <rect x="16" y="19" width="38" height="6" rx="2" fill="#1e40af"/>
        <rect x="10" y="25" width="50" height="120" rx="14" fill="#1e3a5f" stroke="#38bdf8" stroke-width="2"/>
        <rect x="10" y="100" width="50" height="45" rx="0 0 14 14" fill="#38bdf8" opacity="0.5"/>
        <text x="35" y="72" text-anchor="middle" fill="#38bdf8" font-size="12" font-weight="bold">2000</text>
        <text x="35" y="87" text-anchor="middle" fill="#94a3b8" font-size="9">ml</text>
      </svg>
      <div class="bottle-label">2L</div>
      <div class="bottle-sub">2000 ml</div>
    </div>

  </div>

  <div class="divider"></div>

  <div class="section-title">Manuel Giriş</div>
  <div class="manual-group">
    <input
      class="ml-input"
      type="number"
      id="mlInput"
      inputmode="numeric"
      pattern="[0-9]*"
      min="1"
      max="9999"
      placeholder="ml girin"
      oninput="onManualInput()"
    >
    <button class="add-btn" id="addBtn" onclick="openModal()">Ekle</button>
  </div>
</div>

<div id="msg"></div>

<div class="modal-overlay" id="modal">
  <div class="modal-box">
    <div class="modal-title">Onay</div>
    <div class="modal-amount" id="modalAmount">—</div>
    <div class="modal-sub">su içtiğinizi onaylıyor musunuz?</div>
    <div class="modal-btns">
      <button class="modal-btn confirm" onclick="confirmIntake()">✓ Evet, Onayla</button>
      <button class="modal-btn cancel"  onclick="closeModal()">✕ İptal</button>
    </div>
  </div>
</div>

<script>
const patientId = "{{ patient_id }}";
let selectedAmount = null;

function selectBottle(el, amount) {
  document.querySelectorAll('.bottle-card').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  selectedAmount = amount;
  document.getElementById('mlInput').value = '';
  updateBtn();
}

function onManualInput() {
  document.querySelectorAll('.bottle-card').forEach(c => c.classList.remove('selected'));
  const v = parseInt(document.getElementById('mlInput').value, 10);
  selectedAmount = (v > 0) ? v : null;
  updateBtn();
}

function updateBtn() {
  const manualVal = parseInt(document.getElementById('mlInput').value, 10);
  const hasValue  = selectedAmount !== null || (manualVal > 0);
  const btn = document.getElementById('addBtn');
  btn.classList.toggle('active', hasValue);
}

function openModal() {
  if (!selectedAmount || selectedAmount <= 0) return;
  document.getElementById('modalAmount').textContent = selectedAmount + ' ml';
  document.getElementById('modal').classList.add('show');
}

function closeModal() {
  document.getElementById('modal').classList.remove('show');
}

async function confirmIntake() {
  const amount = selectedAmount;
  closeModal();
  if (!amount) return;
  document.getElementById('msg').style.display = 'none';
  try {
    const res  = await fetch('/api/intake', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ patient_id: patientId, amount_ml: amount })
    });
    const data = await res.json();
    if (res.ok) {
      showMsg('✓ ' + amount + ' ml kaydedildi!\\nToplam: ' + data.new_total_ml.toFixed(0) + ' ml', true);
      document.querySelectorAll('.bottle-card').forEach(c => c.classList.remove('selected'));
      document.getElementById('mlInput').value = '';
      selectedAmount = null;
      updateBtn();
    } else {
      showMsg(data.error || 'Bir hata oluştu.', false);
    }
  } catch (e) {
    showMsg('Sunucuya ulaşılamadı. Lütfen tekrar deneyin.', false);
  }
}

function showMsg(text, ok) {
  const el = document.getElementById('msg');
  el.textContent = text;
  el.className   = ok ? 'ok' : 'error';
  el.style.display = 'block';
  window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
}
</script>
</body>
</html>
'''

# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────
@app.route('/')
def home():
    return render_template_string(HOME_HTML)

@app.route('/drain/<patient_id>/<int:session_id>')
def drain_detail(patient_id, session_id):
    return render_template_string(DETAIL_HTML, patient_id=patient_id, session_id=session_id)

@app.route('/api/measurement', methods=['POST'])
def receive_measurement():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Veri yok"}), 400

    patient_id    = data.get("patient_id", "BILINMIYOR").strip()
    weight        = data.get("weight", 0)
    hourly_output = data.get("hourly_output", 0)
    # ESP32'den gelen fluid_intake artık dogrudan guveniliyor.
    # ESP32, hem Kinco (40200/40151-40154 register) hem de bir onceki
    # "live" cevabindan aldigi guncel toplami kendi fluidIntake
    # degiskeninde birlestirip buraya en guncel toplami gonderiyor.
    fluid_intake  = data.get("fluid_intake", 0)
    status        = data.get("status", "unknown")
    device_id     = data.get("device_id", "")
    room_number   = data.get("room_number", 0)
    total_output  = data.get("total_output", 0)
    timestamp     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    session = get_active_session(patient_id)
    if not session:
        if status in ('live', 'reset', 'bag_wait'):
            return jsonify({"success": True, "session_id": -1, "fluid_intake": fluid_intake}), 200
        session_id = create_session(patient_id, device_id, room_number, patient_id)
    else:
        session_id = session['id']

    conn = get_db()
    conn.execute('''
        INSERT INTO measurements
        (patient_id, session_id, weight, hourly_output, fluid_intake, room_number, status, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (patient_id, session_id, weight, hourly_output, fluid_intake, room_number, status, timestamp))

    conn.execute('''
        UPDATE sessions SET total_output = ? WHERE id = ?
    ''', (total_output, session_id))

    conn.commit()
    conn.close()

    if status == "reset" and session:
        close_session(session_id)

    print(f"[{timestamp}] {patient_id} ({device_id}) S:{session_id} | {weight}g | {hourly_output}g/h | sivi:{fluid_intake}g | {status}")
    return jsonify({"success": True, "session_id": session_id, "timestamp": timestamp, "fluid_intake": fluid_intake}), 200

@app.route('/api/measurements', methods=['GET'])
def get_measurements():
    patient_id = request.args.get("patient_id")
    session_id = request.args.get("session_id")
    conn = get_db()
    if patient_id and session_id:
        rows = conn.execute('''
            SELECT * FROM measurements WHERE patient_id=? AND session_id=?
            ORDER BY timestamp DESC LIMIT 100
        ''', (patient_id, session_id)).fetchall()
    elif patient_id:
        rows = conn.execute('''
            SELECT * FROM measurements WHERE patient_id=?
            ORDER BY timestamp DESC LIMIT 100
        ''', (patient_id,)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM measurements ORDER BY timestamp DESC LIMIT 100').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows]), 200

@app.route('/api/sessions/<patient_id>', methods=['GET'])
def get_sessions(patient_id):
    conn = get_db()
    rows = conn.execute('SELECT * FROM sessions WHERE patient_id=? ORDER BY id DESC', (patient_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows]), 200

@app.route('/api/devices', methods=['GET'])
def get_devices():
    conn = get_db()
    rows = conn.execute('SELECT DISTINCT device_id FROM sessions ORDER BY device_id').fetchall()
    conn.close()
    return jsonify([r['device_id'] for r in rows]), 200

@app.route('/api/patients/summary', methods=['GET'])
def patients_summary():
    active    = request.args.get("active", "true") == "true"
    device_id = request.args.get("device_id", "")
    conn = get_db()

    device_filter = "AND s.device_id = ?" if device_id else ""
    params_base   = (device_id,) if device_id else ()

    if active:
        rows = conn.execute(f'''
            SELECT s.*,
              (SELECT m.status FROM measurements m WHERE m.session_id=s.id AND m.status NOT IN ('live', 'fluid_update') ORDER BY m.timestamp DESC LIMIT 1) as last_status,
              (SELECT m.weight FROM measurements m WHERE m.session_id=s.id ORDER BY m.timestamp DESC LIMIT 1) as last_weight,
              (SELECT m.hourly_output FROM measurements m WHERE m.session_id=s.id ORDER BY m.timestamp DESC LIMIT 1) as last_hourly,
              (SELECT m.fluid_intake FROM measurements m WHERE m.session_id=s.id ORDER BY m.timestamp DESC LIMIT 1) as last_fluid,
              COALESCE(s.total_output, 0) as total_output
            FROM sessions s
            WHERE s.ended_at IS NULL {device_filter}
            ORDER BY s.started_at DESC
        ''', params_base).fetchall()
    else:
        rows = conn.execute(f'''
            SELECT s.*,
              (SELECT m.status FROM measurements m WHERE m.session_id=s.id ORDER BY m.timestamp DESC LIMIT 1) as last_status,
              (SELECT m.weight FROM measurements m WHERE m.session_id=s.id ORDER BY m.timestamp DESC LIMIT 1) as last_weight,
              (SELECT m.hourly_output FROM measurements m WHERE m.session_id=s.id ORDER BY m.timestamp DESC LIMIT 1) as last_hourly
            FROM sessions s
            WHERE s.ended_at IS NOT NULL {device_filter}
            ORDER BY s.ended_at DESC LIMIT 50
        ''', params_base).fetchall()

    conn.close()
    return jsonify([dict(r) for r in rows]), 200

@app.route('/intake/<patient_id>')
def intake_page(patient_id):
    return render_template_string(INTAKE_HTML, patient_id=patient_id)

@app.route('/api/intake', methods=['POST'])
def post_intake():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Veri gönderilemedi."}), 400

    patient_id = data.get('patient_id', '').strip()
    amount_ml  = data.get('amount_ml')

    if not patient_id or amount_ml is None:
        return jsonify({"error": "patient_id ve amount_ml zorunludur."}), 400

    try:
        amount_ml = float(amount_ml)
        if amount_ml <= 0:
            return jsonify({"error": "Miktar 0'dan büyük olmalıdır."}), 400
    except (ValueError, TypeError):
        return jsonify({"error": "amount_ml geçerli bir sayı değil."}), 400

    session = get_active_session(patient_id)
    if not session:
        return jsonify({"error": f"{patient_id} için aktif seans bulunamadı. Lütfen hemşirenize danışın."}), 404

    session_id = session['id']
    device_id  = session.get('device_id', '')

    conn = get_db()
    last = conn.execute('''
        SELECT weight, hourly_output, fluid_intake FROM measurements
        WHERE session_id = ? ORDER BY timestamp DESC LIMIT 1
    ''', (session_id,)).fetchone()

    if last:
        last_weight  = last['weight']
        last_hourly  = last['hourly_output']
        last_fluid   = last['fluid_intake'] or 0.0
    else:
        last_weight = 0.0
        last_hourly = 0.0
        last_fluid  = 0.0

    new_fluid = last_fluid + amount_ml
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute('''
        INSERT INTO measurements
        (patient_id, session_id, weight, hourly_output, fluid_intake, status, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (patient_id, session_id, last_weight, last_hourly, new_fluid, 'fluid_update', now))
    conn.commit()
    conn.close()

    print(f"[INTAKE] {patient_id} S:{session_id} +{amount_ml}ml → toplam:{new_fluid}ml")

    # ESP32'ye ayrica bildirim gondermeye gerek yok — ESP32 zaten en gec
    # ~2 saniye icinde bir sonraki "live" istegini atacak ve response
    # icinde bu guncel new_fluid degerini alip kendi 40016 register'ini
    # (ve dolayisiyla Kinco ekranini) senkronize edecek.

    return jsonify({"success": True, "new_total_ml": new_fluid}), 200

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"}), 200

# Modul seviyesinde baslatiliyor ki hem "python server.py" (yerel test)
# hem de gunicorn (Render production) altinda calissin. Gunicorn dosyayi
# sadece import ettigi icin "if __name__ == '__main__'" bloğu calismaz.
init_db()
threading.Thread(target=auto_close_sessions, daemon=True).start()
threading.Thread(target=auto_cleanup, daemon=True).start()
print("=== Drain Monitor Sunucu Baslatildi ===")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"Ana Sayfa : http://0.0.0.0:{port}")
    print(f"API       : http://0.0.0.0:{port}/api/measurement")
    print("Veri temizleme  : 24 saat")
    app.run(host='0.0.0.0', port=port, debug=False)
