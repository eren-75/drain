from flask import Flask, request, jsonify, render_template_string, send_file
from datetime import datetime, timedelta
import sqlite3
import threading
import time
import io

app = Flask(__name__)

# Kalici disk (volume) yolu ortam degiskeninden okunuyor.
# Railway'de: Volume ekleyip mount path'i DATA_DIR olarak Variables'a yazarsin.
# Hicbir volume/env yoksa "." (calisma dizini) kullanilir — ama bu durumda
# yeniden deploy'da veritabani sifirlanabilir (ephemeral disk).
import os
_DATA_DIR = os.environ.get("DATA_DIR", ".")
if not os.path.isdir(_DATA_DIR):
    os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, "drain_monitor.db")

# PDF raporlarin kaydedilecegi klasor — ayni kalici diskin altinda
REPORTS_DIR = os.path.join(_DATA_DIR, "reports")
if not os.path.isdir(REPORTS_DIR):
    os.makedirs(REPORTS_DIR, exist_ok=True)

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
            patient_name TEXT    DEFAULT '',
            last_report_hour INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER NOT NULL,
            patient_id   TEXT    NOT NULL,
            nurse_name   TEXT    NOT NULL,
            comment_text TEXT    NOT NULL,
            timestamp    TEXT    NOT NULL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS reports (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER NOT NULL,
            patient_id   TEXT    NOT NULL,
            report_type  TEXT    NOT NULL,
            hour_mark    INTEGER DEFAULT 0,
            file_path    TEXT    NOT NULL,
            created_at   TEXT    NOT NULL
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
    try:
        c.execute('ALTER TABLE sessions ADD COLUMN last_report_hour INTEGER DEFAULT 0')
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

# ─────────────────────────────────────────
# PDF RAPOR URETIMI
# ─────────────────────────────────────────

def generate_pdf_report(session_id, report_type="manual", hour_mark=0):
    """Bir session icin PDF rapor uretir, diske kaydeder ve dosya yolunu dondurur.
    report_type: 'manual' (hemsire istegiyle) veya 'auto' (24 saatlik otomatik)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                     Paragraph, Spacer, Image as RLImage)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    import matplotlib
    matplotlib.use('Agg')  # Sunucu tarafinda ekran gerektirmeyen backend
    import matplotlib.pyplot as plt

    conn = get_db()
    session = conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
    if not session:
        conn.close()
        return None

    session = dict(session)
    patient_id = session['patient_id']

    measurements = conn.execute('''
        SELECT * FROM measurements WHERE session_id=?
        ORDER BY id ASC
    ''', (session_id,)).fetchall()
    measurements = [dict(m) for m in measurements]

    comments = conn.execute('''
        SELECT * FROM comments WHERE session_id=?
        ORDER BY id ASC
    ''', (session_id,)).fetchall()
    comments = [dict(c) for c in comments]
    conn.close()

    # Sadece gercek saatlik olcumler (grafik ve tablo icin)
    hourly_rows = [m for m in measurements if m['status'] in ('normal', 'blocked', 'full')]

    total_output = session.get('total_output', 0) or 0
    latest_fluid = 0
    for m in reversed(measurements):
        if m.get('fluid_intake'):
            latest_fluid = m['fluid_intake']
            break
    diff = latest_fluid - total_output
    avg_hourly = (total_output / len(hourly_rows)) if hourly_rows else 0

    # ── Grafik uret (matplotlib) ──
    chart_path = None
    if hourly_rows:
        fig, ax = plt.subplots(figsize=(7, 3))
        labels = [m['timestamp'][11:16] for m in hourly_rows]
        values = [m['hourly_output'] for m in hourly_rows]
        bar_colors = [
            '#facc15' if m['status'] == 'blocked' else
            '#f87171' if m['status'] == 'full' else '#4ade80'
            for m in hourly_rows
        ]
        ax.bar(range(len(values)), values, color=bar_colors)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=45, ha='right', fontsize=7)
        ax.set_ylabel('ml')
        ax.set_title('Saatlik Drain Çıkışı')
        fig.tight_layout()

        chart_path = os.path.join(REPORTS_DIR, f"_chart_{session_id}_{int(time.time())}.png")
        fig.savefig(chart_path, dpi=110)
        plt.close(fig)

    # ── PDF olustur ──
    filename = f"rapor_{patient_id}_{session_id}_{report_type}_{int(time.time())}.pdf"
    filepath = os.path.join(REPORTS_DIR, filename)

    doc = SimpleDocTemplate(filepath, pagesize=A4,
                             topMargin=1.5*cm, bottomMargin=1.5*cm,
                             leftMargin=1.5*cm, rightMargin=1.5*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('TitleTR', parent=styles['Title'], fontSize=16)
    h2_style = ParagraphStyle('H2TR', parent=styles['Heading2'], fontSize=12,
                               spaceBefore=10, spaceAfter=6)
    normal_style = styles['Normal']

    elements = []
    elements.append(Paragraph("Drain Takip Raporu", title_style))
    elements.append(Spacer(1, 0.3*cm))

    report_label = "Otomatik Rapor (24 Saatlik)" if report_type == "auto" else "Manuel Rapor"
    elements.append(Paragraph(f"<b>Rapor Türü:</b> {report_label}", normal_style))
    elements.append(Paragraph(f"<b>Oluşturulma:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", normal_style))
    elements.append(Spacer(1, 0.4*cm))

    # ── Hasta bilgileri tablosu ──
    info_data = [
        ["Hasta ID", session.get('patient_id', '—')],
        ["Oda No", str(session.get('room_number') or '—')],
        ["Cihaz ID", session.get('device_id') or '—'],
        ["Başlangıç", session.get('started_at') or '—'],
        ["Bitiş", session.get('ended_at') or 'Devam ediyor'],
        ["İşlem No", f"#{session_id}"],
    ]
    info_table = Table(info_data, colWidths=[4*cm, 11*cm])
    info_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,-1), colors.HexColor('#1e293b')),
        ('TEXTCOLOR', (0,0), (0,-1), colors.white),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 0.5*cm))

    # ── Ozet kartlar ──
    elements.append(Paragraph("Özet Değerler", h2_style))
    summary_data = [
        ["Toplam Çekilen", "İçilen Sıvı", "Fark", "Saatlik Ortalama"],
        [f"{total_output:.1f} ml", f"{latest_fluid:.1f} ml",
         f"{diff:+.1f} ml", f"{avg_hourly:.1f} ml"],
    ]
    summary_table = Table(summary_data, colWidths=[3.75*cm]*4)
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#334155')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('FONTNAME', (0,1), (-1,1), 'Helvetica-Bold'),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 0.5*cm))

    # ── Grafik ──
    if chart_path and os.path.exists(chart_path):
        elements.append(Paragraph("Saatlik Drain Çıkışı Grafiği", h2_style))
        elements.append(RLImage(chart_path, width=16*cm, height=6.5*cm))
        elements.append(Spacer(1, 0.4*cm))

    # ── Olcum tablosu ──
    elements.append(Paragraph("Ölçüm Geçmişi", h2_style))
    table_data = [["Zaman", "Miktar (ml)", "Saatlik (ml)", "İçilen Sıvı (ml)", "Durum"]]
    status_labels = {
        'normal': 'Normal', 'blocked': 'Tıkalı', 'full': 'Dolu', 'reset': 'Reset'
    }
    display_rows = [m for m in measurements if m['status'] not in ('live', 'fluid_update')]
    for m in display_rows:
        table_data.append([
            m['timestamp'],
            f"{m['weight']:.1f}",
            f"{m['hourly_output']:.1f}",
            f"{(m.get('fluid_intake') or 0):.1f}",
            status_labels.get(m['status'], m['status'])
        ])

    if len(table_data) > 1:
        meas_table = Table(table_data, colWidths=[3.8*cm, 2.8*cm, 2.8*cm, 3.2*cm, 2.4*cm], repeatRows=1)
        meas_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#334155')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTSIZE', (0,0), (-1,-1), 8),
            ('GRID', (0,0), (-1,-1), 0.4, colors.grey),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f1f5f9')]),
            ('TOPPADDING', (0,0), (-1,-1), 3),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ]))
        elements.append(meas_table)
    else:
        elements.append(Paragraph("Henüz ölçüm kaydı yok.", normal_style))

    elements.append(Spacer(1, 0.5*cm))

    # ── Hemsire yorumlari ──
    elements.append(Paragraph("Hemşire Notları", h2_style))
    if comments:
        comment_data = [["Zaman", "Hemşire", "Not"]]
        for c in comments:
            comment_data.append([c['timestamp'], c['nurse_name'], c['comment_text']])
        comment_table = Table(comment_data, colWidths=[3.5*cm, 3.5*cm, 8*cm], repeatRows=1)
        comment_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#334155')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTSIZE', (0,0), (-1,-1), 8),
            ('GRID', (0,0), (-1,-1), 0.4, colors.grey),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ]))
        elements.append(comment_table)
    else:
        elements.append(Paragraph("Kayıtlı not bulunmuyor.", normal_style))

    doc.build(elements)

    # Gecici grafik dosyasini temizle
    if chart_path and os.path.exists(chart_path):
        os.remove(chart_path)

    # Veritabanina kaydet
    conn = get_db()
    conn.execute('''
        INSERT INTO reports (session_id, patient_id, report_type, hour_mark, file_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (session_id, patient_id, report_type, hour_mark, filepath,
          datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

    print(f"[PDF] Rapor uretildi: {filename} (session {session_id}, tip: {report_type})")
    return filepath

def check_and_generate_auto_report(session_id):
    """Bir session'daki gercek saatlik olcum sayisi 24'un katina ulastiginda
    ve o dilim icin daha once rapor uretilmediyse otomatik PDF olusturur.

    Es zamanli gelen birden fazla istek (ayni anda birkac olcum POST edilmesi)
    ayni anda bu fonksiyonu tetikleyebilir. Sadece TEK thread'in rapor
    uretmesini garanti etmek icin atomik bir "claim" (UPDATE...WHERE) yapiyoruz:
    UPDATE sadece last_report_hour hala eski degerdeyse satiri gunceller ve
    etkilenen satir sayisi 1 donerse "hakkimizi kazandik" demektir."""
    conn = get_db()
    session = conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
    if not session:
        conn.close()
        return

    hourly_count = conn.execute('''
        SELECT COUNT(*) as cnt FROM measurements
        WHERE session_id=? AND status IN ('normal','blocked','full')
    ''', (session_id,)).fetchone()['cnt']

    if hourly_count == 0:
        conn.close()
        return

    current_block = hourly_count // 24  # 24=1, 48=2, 72=3 ...
    last_report_hour = session['last_report_hour'] or 0
    last_block = last_report_hour // 24

    if hourly_count % 24 == 0 and current_block > last_block:
        # Atomik claim: sadece last_report_hour hala eski degerdeyse guncelle.
        # SQLite tek yazicili oldugu icin bu islem gercekten atomiktir —
        # ayni anda birden fazla thread calissa bile sadece biri basarili olur.
        cur = conn.execute('''
            UPDATE sessions SET last_report_hour = ?
            WHERE id = ? AND last_report_hour = ?
        ''', (hourly_count, session_id, last_report_hour))
        conn.commit()
        won_claim = cur.rowcount == 1
        conn.close()

        if not won_claim:
            # Baska bir thread bizden once hakki kazandi, biz cikiyoruz
            return

        try:
            generate_pdf_report(session_id, report_type="auto", hour_mark=hourly_count)
        except Exception as e:
            print(f"[PDF] Otomatik rapor hatasi (session {session_id}): {e}")

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
# icinde guncel fluid_intake degerini okuyup kendi register'ini
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
      <span class="timer" id="timer">Yenileniyor: 5s</span>
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
let countdown = 5;
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
        ${d.device_id ? `<span onclick="event.stopPropagation(); window.open('/qr/' + d.device_id, '_blank')" style="display:inline-block;margin-top:4px;background:#0f172a;color:#38bdf8;border:1px solid #334155;border-radius:6px;padding:3px 8px;font-size:11px;font-weight:bold;cursor:pointer;">📱 QR</span>` : ''}
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

  countdown = 5;
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
    .chart-box { background:#1e293b; border-radius:12px; padding:20px; border:1px solid #334155; margin-bottom:24px; overflow-x:scroll; overflow-y:hidden; white-space:nowrap; }
    .chart-box h2 { font-size:15px; color:#94a3b8; margin-bottom:16px; }
    .table-box { background:#1e293b; border-radius:12px; padding:20px; border:1px solid #334155; max-height:400px; overflow-y:auto; }
    .table-box h2 { font-size:15px; color:#94a3b8; margin-bottom:16px; }

    .report-box {
      background:#1e293b; border-radius:12px; padding:20px;
      border:1px solid #334155; margin-top:24px;
    }
    .report-box h2 { font-size:15px; color:#94a3b8; margin-bottom:16px; display:flex; justify-content:space-between; align-items:center; }
    .gen-report-btn {
      background:#a78bfa; color:#1e1b3a; border:none;
      border-radius:8px; padding:8px 16px; font-size:13px;
      font-weight:bold; cursor:pointer;
    }
    .gen-report-btn:hover { background:#c4b5fd; }
    .gen-report-btn:disabled { background:#475569; color:#94a3b8; cursor:wait; }
    .report-list { display:flex; flex-direction:column; gap:8px; }
    .report-item {
      display:flex; justify-content:space-between; align-items:center;
      background:#0f172a; border-radius:8px; padding:10px 14px;
      font-size:13px; border:1px solid #334155;
    }
    .report-item .r-info { display:flex; flex-direction:column; gap:2px; }
    .report-item .r-type { font-weight:bold; color:#e2e8f0; }
    .report-item .r-date { font-size:11px; color:#64748b; }
    .report-item .r-badge {
      font-size:11px; padding:2px 8px; border-radius:10px;
      background:#312e81; color:#a5b4fc;
    }
    .report-item .r-badge.manual { background:#312e81; color:#a5b4fc; }
    .report-item .r-badge.auto   { background:#164e63; color:#67e8f9; }
    .report-item a.dl-btn {
      background:#38bdf8; color:#0f172a; text-decoration:none;
      padding:6px 12px; border-radius:6px; font-size:12px; font-weight:bold;
    }
    .report-item a.dl-btn:hover { background:#7dd3fc; }

    .comment-box {
      background:#1e293b; border-radius:12px; padding:20px;
      border:1px solid #334155; margin-top:24px;
    }
    .comment-box h2 { font-size:15px; color:#94a3b8; margin-bottom:16px; }
    .comment-form { display:flex; flex-direction:column; gap:10px; margin-bottom:16px; }
    .comment-form input, .comment-form textarea {
      background:#0f172a; color:#e2e8f0; border:1px solid #334155;
      border-radius:8px; padding:10px 12px; font-size:13px; font-family:inherit;
      outline:none;
    }
    .comment-form input:focus, .comment-form textarea:focus { border-color:#38bdf8; }
    .comment-form textarea { resize:vertical; min-height:60px; }
    .comment-submit-btn {
      background:#38bdf8; color:#0f172a; border:none;
      border-radius:8px; padding:10px 16px; font-size:13px;
      font-weight:bold; cursor:pointer; align-self:flex-start;
    }
    .comment-submit-btn:hover { background:#7dd3fc; }
    .comment-list { display:flex; flex-direction:column; gap:10px; }
    .comment-item {
      background:#0f172a; border-radius:8px; padding:12px 14px;
      border:1px solid #334155; font-size:13px;
    }
    .comment-item .c-header {
      display:flex; justify-content:space-between; margin-bottom:6px;
    }
    .comment-item .c-nurse { font-weight:bold; color:#38bdf8; }
    .comment-item .c-time { font-size:11px; color:#64748b; }
    .comment-item .c-text { color:#cbd5e1; line-height:1.5; }
    .no-items { color:#64748b; font-size:13px; text-align:center; padding:16px; }
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
    <span class="timer" id="timer">Yenileniyor: 5s</span>
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
    <div class="card" id="diffCard" style="border-color:#f472b6">
      <div class="label" id="diffLabel" style="color:#f472b6">FARK (İÇİLEN - ÇEKİLEN)</div>
      <div class="value" id="diff_value" style="color:#f472b6">—</div>
    </div>
    <div class="card" style="border-color:#facc15">
      <div class="label" style="color:#facc15">SAATLİK ORTALAMA</div>
      <div class="value" id="avg_value" style="color:#facc15">—</div>
    </div>
    <div class="card info">
      <div class="label">SAATLİK ÖLÇÜM</div>
      <div class="value" id="total">—</div>
    </div>
    <div class="card info">
      <div class="label">ÖLÇÜM SÜRESİ</div>
      <div class="value" id="duration">—</div>
    </div>
    <div class="card" id="statusCard">
      <div class="label">DURUM</div>
      <div class="value" id="status">—</div>
    </div>
  </div>

  <div class="chart-box">
    <h2>📈 Saatlik Drain Çıkışı (ml)</h2>
    <div id="chartWrap" style="display:inline-block; min-width:100%;">
      <canvas id="chart"></canvas>
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

  <div class="report-box">
    <h2>
      <span>📄 PDF Raporlar</span>
      <button class="gen-report-btn" id="genReportBtn" onclick="generateReportNow()">Şimdi Rapor Oluştur</button>
    </h2>
    <div class="report-list" id="reportList">
      <div class="no-items">Yükleniyor...</div>
    </div>
  </div>

  <div class="comment-box">
    <h2>📝 Hemşire Notları</h2>
    <div class="comment-form">
      <input type="text" id="nurseNameInput" placeholder="Hemşire adı">
      <textarea id="commentTextInput" placeholder="Not... (örn. Hastanın idrarında kan görüldü, ölçümü durduruyorum)"></textarea>
      <button class="comment-submit-btn" onclick="submitComment()">Notu Ekle</button>
    </div>
    <div class="comment-list" id="commentList">
      <div class="no-items">Yükleniyor...</div>
    </div>
  </div>
</div>
<script>
const patientId = "{{ patient_id }}";
const sessionId = "{{ session_id }}";
let chart = null;
let countdown = 5;
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
  countdown = 5;
  try {
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
  const totalOutput = session ? (session.total_output || 0) : 0;

  const latestFluid = latest.fluid_intake || 0;

  document.getElementById('weight').textContent       = latest.weight.toFixed(1) + ' ml';
  document.getElementById('hourly').textContent       = latest.hourly_output.toFixed(1) + ' ml';
  document.getElementById('total_output').textContent = totalOutput.toFixed(1) + ' ml';
  document.getElementById('fluid_intake').textContent = latestFluid.toFixed(1) + ' ml';

  // Fark negatif olabilir: cekilen, icilenden fazlaysa bu klinik acidan
  // onemli bir bilgi, gizlenmemeli.
  const diff = latestFluid - totalOutput;
  const diffCard  = document.getElementById('diffCard');
  const diffLabel = document.getElementById('diffLabel');
  const diffValue = document.getElementById('diff_value');
  if (diff < 0) {
    diffCard.style.borderColor  = '#22d3ee';
    diffLabel.style.color       = '#22d3ee';
    diffValue.style.color       = '#22d3ee';
  } else {
    diffCard.style.borderColor  = '#f472b6';
    diffLabel.style.color       = '#f472b6';
    diffValue.style.color       = '#f472b6';
  }
  diffValue.textContent = (diff >= 0 ? '+' : '') + diff.toFixed(1) + ' ml';

  const saatlikCount = data.filter(d => ['normal', 'blocked', 'full'].includes(d.status)).length;
  document.getElementById('total').textContent = saatlikCount;
  const avg = saatlikCount > 0 ? totalOutput / saatlikCount : 0;
  document.getElementById('avg_value').textContent = avg.toFixed(1) + ' ml';
  if (session && session.started_at) {
    const start = new Date(session.started_at.replace(' ', 'T'));
    const now = new Date();
    const diffMs = now - start;
    const hours = Math.floor(diffMs / 3600000);
    const mins = Math.floor((diffMs % 3600000) / 60000);
    document.getElementById('duration').textContent = hours + 's ' + mins + 'dk';
  }

  const s = statusMap[latest.status] || { text: latest.status, cls: 'info' };
  document.getElementById('status').textContent = s.text;
  document.getElementById('statusCard').className = 'card ' + s.cls;

  const filtered = data
    .filter(d => ['normal', 'blocked', 'full'].includes(d.status))
    .reverse();
  const barWidth = 60;
  const minWidth = Math.max(document.querySelector('.chart-box').clientWidth - 40, filtered.length * barWidth);
  const canvas = document.getElementById('chart');
  canvas.style.width  = minWidth + 'px';
  canvas.style.height = '300px';
  canvas.width  = minWidth;
  canvas.height = 300;
  document.getElementById('chartWrap').style.width = minWidth + 'px';
  const labels = filtered.map(d => d.timestamp.slice(11, 16));
  const values = filtered.map(d => d.hourly_output);
  const colors = filtered.map(d =>
    d.status === 'blocked' ? '#facc15' :
    d.status === 'full'    ? '#f87171' : '#4ade80');
  if (chart) {
    chart.data.labels = labels;
    chart.data.datasets[0].data = values;
    chart.data.datasets[0].backgroundColor = colors;
    chart.update('none');
  } else {
    chart = new Chart(document.getElementById('chart'), {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [{ label: 'Saatlik Çıkış (ml)', data: values, backgroundColor: colors, borderRadius: 6 }]
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
  }

  document.getElementById('tbody').innerHTML = data
    .filter(d => !['live', 'fluid_update'].includes(d.status))
    .map(d => `
    <tr>
      <td>${d.timestamp}</td>
      <td>${d.weight.toFixed(1)} ml</td>
      <td>${d.hourly_output.toFixed(1)} ml</td>
      <td style="color:#fb923c">${(d.fluid_intake||0).toFixed(1)} ml</td>
      <td>${badgeMap[d.status] || d.status}</td>
    </tr>
  `).join('');

  } catch(e) {
    console.error('loadData hata:', e);
    document.getElementById('statusCard').className = 'card danger';
    document.getElementById('status').textContent = 'Hata: ' + e.message;
  }
}

// ─── PDF Raporlar ───
async function loadReports() {
  try {
    const res = await fetch(`/api/reports/${sessionId}`);
    const reports = await res.json();
    const listEl = document.getElementById('reportList');
    if (!reports.length) {
      listEl.innerHTML = '<div class="no-items">Henüz rapor oluşturulmadı.</div>';
      return;
    }
    listEl.innerHTML = reports.map(r => {
      const typeLabel = r.report_type === 'auto'
        ? `Otomatik Rapor (${r.hour_mark}. Saat)`
        : 'Manuel Rapor';
      const badgeCls = r.report_type === 'auto' ? 'auto' : 'manual';
      return `
        <div class="report-item">
          <div class="r-info">
            <span class="r-type">${typeLabel} <span class="r-badge ${badgeCls}">${r.report_type}</span></span>
            <span class="r-date">${r.created_at}</span>
          </div>
          <a class="dl-btn" href="/report/download/${r.id}" target="_blank">⬇ İndir</a>
        </div>
      `;
    }).join('');
  } catch(e) {
    console.error('loadReports hata:', e);
  }
}

async function generateReportNow() {
  const btn = document.getElementById('genReportBtn');
  btn.disabled = true;
  btn.textContent = 'Oluşturuluyor...';
  try {
    const res = await fetch(`/api/reports/generate/${sessionId}`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      await loadReports();
    } else {
      alert(data.error || 'Rapor oluşturulamadı.');
    }
  } catch(e) {
    alert('Sunucuya ulaşılamadı.');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Şimdi Rapor Oluştur';
  }
}

// ─── Hemşire Notları ───
async function loadComments() {
  try {
    const res = await fetch(`/api/comments/${sessionId}`);
    const comments = await res.json();
    const listEl = document.getElementById('commentList');
    if (!comments.length) {
      listEl.innerHTML = '<div class="no-items">Henüz not eklenmedi.</div>';
      return;
    }
    listEl.innerHTML = comments.map(c => `
      <div class="comment-item">
        <div class="c-header">
          <span class="c-nurse">👤 ${c.nurse_name}</span>
          <span class="c-time">${c.timestamp}</span>
        </div>
        <div class="c-text">${c.comment_text}</div>
      </div>
    `).join('');
  } catch(e) {
    console.error('loadComments hata:', e);
  }
}

async function submitComment() {
  const nurseName = document.getElementById('nurseNameInput').value.trim();
  const commentText = document.getElementById('commentTextInput').value.trim();
  if (!nurseName || !commentText) {
    alert('Lütfen hemşire adı ve not metnini girin.');
    return;
  }
  try {
    const res = await fetch('/api/comments', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, nurse_name: nurseName, comment_text: commentText })
    });
    const data = await res.json();
    if (res.ok) {
      document.getElementById('commentTextInput').value = '';
      await loadComments();
    } else {
      alert(data.error || 'Not eklenemedi.');
    }
  } catch(e) {
    alert('Sunucuya ulaşılamadı.');
  }
}

setInterval(() => {
  countdown--;
  document.getElementById('timer').textContent = 'Yenileniyor: ' + countdown + 's';
  if (countdown <= 0) { loadData(); loadReports(); }
}, 1000);
loadData();
loadReports();
loadComments();
</script>
</body>
</html>
'''

PATIENT_STATUS_HTML = '''
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ patient_id }} — Durum</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
  <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body { font-family:Arial,sans-serif; background:#0f172a; color:#e2e8f0; min-height:100vh; }
    header {
      background:#1e293b; padding:16px;
      border-bottom:2px solid #334155; text-align:center;
    }
    header h1 { font-size:20px; color:#38bdf8; }
    header p { font-size:12px; color:#64748b; margin-top:4px; }
    .container { padding:16px; max-width:500px; margin:auto; }
    .grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:16px; }
    .card {
      background:#1e293b; border-radius:12px; padding:14px;
      border:1px solid #334155;
    }
    .card .label { font-size:10px; color:#64748b; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:6px; }
    .card .value { font-size:22px; font-weight:bold; }
    .card.blue   .value { color:#38bdf8; }
    .card.green  .value { color:#4ade80; }
    .card.purple .value { color:#a78bfa; }
    .card.orange .value { color:#fb923c; }
    .card.diff-pos .value { color:#4ade80; }
    .card.diff-neg .value { color:#f87171; }
    .card.full { grid-column: span 2; }
    .chart-box {
      background:#1e293b; border-radius:12px; padding:16px;
      border:1px solid #334155; margin-bottom:16px;
      overflow-x:auto;
    }
    .chart-box h2 { font-size:13px; color:#64748b; margin-bottom:12px; }
    .intake-btn {
      display:block; width:100%; padding:18px;
      background:#38bdf8; color:#0f172a;
      border:none; border-radius:12px;
      font-size:18px; font-weight:bold;
      cursor:pointer; text-align:center;
      text-decoration:none; margin-bottom:16px;
    }
    .intake-btn:hover { background:#7dd3fc; }
    .badge { display:inline-block; padding:4px 10px; border-radius:20px; font-size:12px; font-weight:bold; }
    .badge.normal  { background:#14532d; color:#4ade80; }
    .badge.blocked { background:#713f12; color:#facc15; }
    .badge.full    { background:#7f1d1d; color:#f87171; }
    .badge.live    { background:#0f3460; color:#38bdf8; border:1px solid #38bdf8; }
    .timer { text-align:center; font-size:11px; color:#475569; margin-bottom:12px; }
  </style>
</head>
<body>
<header>
  <h1>🩺 {{ patient_id }}</h1>
  <p id="subtitle">Yükleniyor...</p>
</header>
<div class="container">
  <div class="timer" id="timer">Yenileniyor: 10s</div>
  <div class="grid">
    <div class="card blue">
      <div class="label">ANLIK MİKTAR</div>
      <div class="value" id="weight">—</div>
    </div>
    <div class="card green">
      <div class="label">SON SAATLİK</div>
      <div class="value" id="hourly">—</div>
    </div>
    <div class="card purple">
      <div class="label">TOPLAM ÇEKİLEN</div>
      <div class="value" id="total">—</div>
    </div>
    <div class="card orange">
      <div class="label">İÇİLEN SIVI</div>
      <div class="value" id="fluid">—</div>
    </div>
    <div class="card full" id="diffCard">
      <div class="label">FARK (İÇİLEN - ÇEKİLEN)</div>
      <div class="value" id="diff">—</div>
    </div>
  </div>
  <div class="chart-box" style="overflow:hidden;">
    <h2>📊 Sıvı Dengesi</h2>
    <canvas id="chart" height="220"></canvas>
  </div>
  <a class="intake-btn" href="/intake/device/{{ device_id }}?tab=intake">
    💧 Su / İçecek Ekle
  </a>
</div>
<script>
const patientId = "{{ patient_id }}";
const sessionId = "{{ session_id }}";
let chart = null;
let countdown = 10;
async function loadData() {
  countdown = 10;
  try {
    const res = await fetch(`/api/measurements?patient_id=${patientId}&session_id=${sessionId}`);
    const data = await res.json();
    if (!data.length) return;
    const latest = data[0];
    const totalOutput = {{ total_output }};
    const latestFluid = latest.fluid_intake || 0;
    const diff = latestFluid - totalOutput;

    document.getElementById('subtitle').textContent =
      'Oda: {{ room_number }} • {{ started_at }}';
    document.getElementById('weight').textContent = latest.weight.toFixed(1) + ' ml';
    document.getElementById('hourly').textContent = latest.hourly_output.toFixed(1) + ' ml';
    document.getElementById('total').textContent  = totalOutput.toFixed(1) + ' ml';
    document.getElementById('fluid').textContent  = latestFluid.toFixed(1) + ' ml';
    const diffEl   = document.getElementById('diff');
    const diffCard = document.getElementById('diffCard');
    diffEl.textContent = (diff >= 0 ? '+' : '') + diff.toFixed(1) + ' ml';
    diffCard.className = 'card full ' + (diff >= 0 ? 'diff-pos' : 'diff-neg');

    const drainVal  = totalOutput;
    const fluidVal  = latestFluid;
    const pieLabels = ['Toplam Çekilen', 'İçilen Sıvı'];
    const pieData   = [drainVal, fluidVal];
    const pieColors = ['#a78bfa', '#fb923c'];
    if (chart) {
      chart.data.labels = pieLabels;
      chart.data.datasets[0].data = pieData;
      chart.update('none');
    } else {
      chart = new Chart(document.getElementById('chart'), {
        type: 'doughnut',
        data: {
          labels: pieLabels,
          datasets: [{ data: pieData, backgroundColor: pieColors, borderWidth: 2, borderColor: '#1e293b' }]
        },
        options: {
          responsive: true,
          plugins: {
            legend: { position: 'bottom', labels: { color: '#94a3b8', font: { size: 12 } } },
            tooltip: {
              callbacks: {
                label: ctx => ctx.label + ': ' + ctx.parsed.toFixed(1) + ' ml'
              }
            }
          }
        }
      });
    }
  } catch(e) { console.error(e); }
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
  <p id="fluidTotal" style="font-size:16px;color:#4ade80;margin-top:6px;font-weight:bold;"></p>
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
const deviceId = "{{ device_id }}";
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
  let res, retryCount = 0;
  while (retryCount < 3) {
    try {
      res = await fetch('/api/intake', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ patient_id: patientId, device_id: deviceId, amount_ml: amount })
      });
      break;
    } catch(err) {
      retryCount++;
      if (retryCount >= 3) { showMsg('Sunucuya ulaşılamadı. Tekrar deneyin.', false); return; }
      await new Promise(r => setTimeout(r, 1000));
    }
  }
  try {
    const data = await res.json();
    if (res.ok) {
      showMsg('✓ ' + amount + ' ml kaydedildi!\\nToplam: ' + data.new_total_ml.toFixed(0) + ' ml', true);
      document.querySelectorAll('.bottle-card').forEach(c => c.classList.remove('selected'));
      document.getElementById('mlInput').value = '';
      selectedAmount = null;
      updateBtn();
      loadFluidTotal();
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

async function loadFluidTotal() {
  try {
    const res = await fetch('/api/intake_status?patient_id=' + patientId);
    const d = await res.json();
    if (d.fluid_intake !== undefined) {
      document.getElementById('fluidTotal').textContent =
        'İçilen Sıvı: ' + d.fluid_intake.toFixed(0) + ' ml';
    }
  } catch(e) {}
}
loadFluidTotal();
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
    # ESP32'den gelen fluid_intake dogrudan guveniliyor.
    fluid_intake_from_esp = data.get("fluid_intake", 0)
    fluid_intake          = fluid_intake_from_esp
    status                = data.get("status", "unknown")
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

    # Live ölçümde ESP32'nin eski değeri DB'yi düşürmesin
    if status == 'live' and session:
        conn_tmp = get_db()
        last_row = conn_tmp.execute(
            'SELECT fluid_intake FROM measurements WHERE session_id=? ORDER BY id DESC LIMIT 1',
            (session['id'] if session else -1,)
        ).fetchone()
        conn_tmp.close()
        if last_row and (last_row['fluid_intake'] or 0) > fluid_intake_from_esp:
            fluid_intake = last_row['fluid_intake']

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

    # Gercek saatlik olcum ise (normal/blocked/full) 24 saatlik otomatik
    # rapor esigine ulasildi mi kontrol et. PDF uretimi zaman alabilir,
    # bu yuzden ESP32'nin cevabini bekletmemek icin ayri thread'de calistir.
    if status in ('normal', 'blocked', 'full'):
        threading.Thread(
            target=check_and_generate_auto_report,
            args=(session_id,),
            daemon=True
        ).start()

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
            ORDER BY id DESC LIMIT 1500
        ''', (patient_id, session_id)).fetchall()
    elif patient_id:
        rows = conn.execute('''
            SELECT * FROM measurements WHERE patient_id=?
            ORDER BY id DESC LIMIT 1500
        ''', (patient_id,)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM measurements ORDER BY id DESC LIMIT 1500').fetchall()
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
              (SELECT m.status FROM measurements m WHERE m.session_id=s.id AND m.status NOT IN ('live', 'fluid_update') ORDER BY m.id DESC LIMIT 1) as last_status,
              (SELECT m.weight FROM measurements m WHERE m.session_id=s.id ORDER BY m.id DESC LIMIT 1) as last_weight,
              (SELECT m.hourly_output FROM measurements m WHERE m.session_id=s.id ORDER BY m.id DESC LIMIT 1) as last_hourly,
              (SELECT m.fluid_intake FROM measurements m WHERE m.session_id=s.id ORDER BY m.id DESC LIMIT 1) as last_fluid,
              COALESCE(s.total_output, 0) as total_output,
              (SELECT CASE WHEN COUNT(*) > 0 THEN s.total_output / COUNT(*) ELSE 0 END
               FROM measurements m2 WHERE m2.session_id=s.id
               AND m2.status IN ('normal','blocked','full') AND m2.hourly_output > 0) as hourly_avg
            FROM sessions s
            WHERE s.ended_at IS NULL {device_filter}
            ORDER BY s.started_at DESC
        ''', params_base).fetchall()
    else:
        rows = conn.execute(f'''
            SELECT s.*,
              (SELECT m.status FROM measurements m WHERE m.session_id=s.id ORDER BY m.id DESC LIMIT 1) as last_status,
              (SELECT m.weight FROM measurements m WHERE m.session_id=s.id ORDER BY m.id DESC LIMIT 1) as last_weight,
              (SELECT m.hourly_output FROM measurements m WHERE m.session_id=s.id ORDER BY m.id DESC LIMIT 1) as last_hourly
            FROM sessions s
            WHERE s.ended_at IS NOT NULL {device_filter}
            ORDER BY s.ended_at DESC LIMIT 50
        ''', params_base).fetchall()

    conn.close()
    return jsonify([dict(r) for r in rows]), 200

@app.route('/intake/<patient_id>')
def intake_page(patient_id):
    return render_template_string(INTAKE_HTML, patient_id=patient_id, device_id='')

@app.route('/api/intake', methods=['POST'])
def post_intake():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Veri gönderilemedi."}), 400

    patient_id    = data.get('patient_id', '').strip()
    req_device_id = data.get('device_id', '').strip()
    amount_ml     = data.get('amount_ml')

    if (not patient_id and not req_device_id) or amount_ml is None:
        return jsonify({"error": "patient_id veya device_id, ve amount_ml zorunludur."}), 400

    try:
        amount_ml = float(amount_ml)
        if amount_ml <= 0:
            return jsonify({"error": "Miktar 0'dan büyük olmalıdır."}), 400
    except (ValueError, TypeError):
        return jsonify({"error": "amount_ml geçerli bir sayı değil."}), 400

    session = get_active_session(patient_id)
    if not session and req_device_id:
        conn_tmp = get_db()
        row_tmp = conn_tmp.execute(
            'SELECT patient_id FROM sessions WHERE device_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1',
            (req_device_id,)
        ).fetchone()
        conn_tmp.close()
        if row_tmp:
            patient_id = row_tmp['patient_id']
            session = get_active_session(patient_id)
    if not session:
        return jsonify({"error": "Aktif seans bulunamadı. Hemşireye danışın."}), 404

    session_id = session['id']

    conn = get_db()
    last = conn.execute('''
        SELECT weight, hourly_output, fluid_intake FROM measurements
        WHERE session_id = ? ORDER BY id DESC LIMIT 1
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

    # Not: ESP32'ye ayrica bildirim gonderilmiyor (bulut ortaminda ESP32'ye
    # disaridan ulasilamaz). ESP32 zaten en gec ~2 saniye icinde bir sonraki
    # "live" istegini atacak ve response icinde bu guncel new_fluid degerini
    # alip kendi register'ini (ve dolayisiyla Kinco ekranini) senkronize edecek.

    return jsonify({"success": True, "new_total_ml": new_fluid}), 200

@app.route('/intake/device/<device_id>')
def intake_device(device_id):
    tab = request.args.get('tab', 'status')
    conn = get_db()
    row = conn.execute(
        '''SELECT s.patient_id, s.id, s.room_number, s.started_at, s.total_output
           FROM sessions s WHERE s.device_id=? AND s.ended_at IS NULL
           ORDER BY s.id DESC LIMIT 1''',
        (device_id,)
    ).fetchone()
    conn.close()

    if not row:
        return """<!DOCTYPE html><html lang="tr"><head>
        <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
        <meta http-equiv="refresh" content="10">
        <title>Bekleniyor</title>
        <style>body{font-family:Arial,sans-serif;background:#0f172a;color:#e2e8f0;
          display:flex;flex-direction:column;align-items:center;justify-content:center;
          min-height:100vh;gap:16px;text-align:center;padding:24px;}
          h1{font-size:22px;color:#38bdf8;}p{color:#94a3b8;font-size:14px;}</style>
        </head><body>
        <div style="font-size:64px">⏳</div>
        <h1>Henüz Hasta Kaydı Yok</h1>
        <p>Hemşire ölçümü başlattığında bu sayfa otomatik yenilenir.</p>
        <p style="font-size:12px;color:#475569">Cihaz: """ + device_id + """</p>
        </body></html>""", 200
    if tab == 'intake':
        return render_template_string(INTAKE_HTML, patient_id=row['patient_id'], device_id=device_id)
    return render_template_string(PATIENT_STATUS_HTML,
        patient_id=row['patient_id'],
        device_id=device_id,
        session_id=row['id'],
        room_number=row['room_number'] or '—',
        started_at=(row['started_at'] or '')[:16],
        total_output=row['total_output'] or 0
    )

@app.route('/qr/<device_id>')
def qr_page(device_id):
    from flask import request as freq
    server_url = freq.host_url.rstrip('/')
    intake_url = f"{server_url}/intake/device/{device_id}"
    html = f"""<!DOCTYPE html><html lang="tr"><head>
    <meta charset="UTF-8"><title>QR — {device_id}</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
    <style>
      body{{font-family:Arial,sans-serif;background:#fff;color:#111;display:flex;flex-direction:column;align-items:center;justify-content:center;
        min-height:100vh;gap:12px;text-align:center;padding:24px;}}
      h1{{font-size:20px;color:#0f172a;}}
      .sub{{color:#555;font-size:14px;}}
      .url{{font-size:11px;color:#888;word-break:break-all;max-width:260px;margin-top:8px;}}
      .device{{font-size:12px;color:#aaa;}}
      .print-btn{{margin-top:16px;padding:10px 28px;background:#0f172a;color:#fff;
        border:none;border-radius:8px;font-size:15px;cursor:pointer;}}
      @media print{{.print-btn{{display:none;}}}}
    </style></head><body>
    <h1>&#x1F4A7; Sıvı Takibi</h1>
    <p class="sub">QR kodu okutarak içtiğiniz sıvıyı kaydedin</p>
    <div id="qr" style="margin:8px auto;"></div>
    <div class="url">{intake_url}</div>
    <p class="device">Cihaz: {device_id}</p>
    <button class="print-btn" onclick="window.print()">&#x1F5A8;&#xFE0F; Yazdır</button>
    <script>
    new QRCode(document.getElementById("qr"), {{
        text: "{intake_url}",
        width: 240, height: 240,
        colorDark: "#000000", colorLight: "#ffffff",
        correctLevel: QRCode.CorrectLevel.H
    }});
    </script>
    </body></html>"""
    return html

@app.route('/api/intake_status', methods=['GET'])
def intake_status():
    patient_id = request.args.get('patient_id', '').strip()
    if not patient_id:
        return jsonify({"error": "patient_id gerekli"}), 400
    conn = get_db()
    row = conn.execute('''
        SELECT fluid_intake FROM measurements
        WHERE patient_id=? ORDER BY id DESC LIMIT 1
    ''', (patient_id,)).fetchone()
    conn.close()
    if row:
        return jsonify({"fluid_intake": row['fluid_intake'] or 0}), 200
    return jsonify({"fluid_intake": 0}), 200

# ─────────────────────────────────────────
# YORUMLAR (HEMSIRE NOTLARI)
# ─────────────────────────────────────────

@app.route('/api/comments', methods=['POST'])
def add_comment():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Veri gönderilemedi."}), 400

    session_id = data.get('session_id')
    nurse_name = (data.get('nurse_name') or '').strip()
    comment_text = (data.get('comment_text') or '').strip()

    if not session_id or not nurse_name or not comment_text:
        return jsonify({"error": "session_id, nurse_name ve comment_text zorunludur."}), 400

    conn = get_db()
    session = conn.execute('SELECT patient_id FROM sessions WHERE id=?', (session_id,)).fetchone()
    if not session:
        conn.close()
        return jsonify({"error": "Session bulunamadı."}), 404

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute('''
        INSERT INTO comments (session_id, patient_id, nurse_name, comment_text, timestamp)
        VALUES (?, ?, ?, ?, ?)
    ''', (session_id, session['patient_id'], nurse_name, comment_text, now))
    conn.commit()
    conn.close()

    print(f"[YORUM] Session {session_id} — {nurse_name}: {comment_text}")
    return jsonify({"success": True}), 200

@app.route('/api/comments/<int:session_id>', methods=['GET'])
def get_comments(session_id):
    conn = get_db()
    rows = conn.execute('''
        SELECT * FROM comments WHERE session_id=? ORDER BY id DESC
    ''', (session_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows]), 200

# ─────────────────────────────────────────
# PDF RAPORLAR
# ─────────────────────────────────────────

@app.route('/api/reports/<int:session_id>', methods=['GET'])
def list_reports(session_id):
    conn = get_db()
    rows = conn.execute('''
        SELECT * FROM reports WHERE session_id=? ORDER BY id DESC
    ''', (session_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows]), 200

@app.route('/api/reports/generate/<int:session_id>', methods=['POST'])
def generate_report_now(session_id):
    """Hemsirenin 'Şimdi Rapor Oluştur' butonuyla tetiklenen manuel rapor."""
    conn = get_db()
    session = conn.execute('SELECT id FROM sessions WHERE id=?', (session_id,)).fetchone()
    conn.close()
    if not session:
        return jsonify({"error": "Session bulunamadı."}), 404

    try:
        filepath = generate_pdf_report(session_id, report_type="manual")
        if not filepath:
            return jsonify({"error": "Rapor oluşturulamadı."}), 500
        report_id = None
        conn = get_db()
        row = conn.execute(
            'SELECT id FROM reports WHERE file_path=? ORDER BY id DESC LIMIT 1',
            (filepath,)
        ).fetchone()
        conn.close()
        if row:
            report_id = row['id']
        return jsonify({"success": True, "report_id": report_id}), 200
    except Exception as e:
        print(f"[PDF] Manuel rapor hatasi: {e}")
        return jsonify({"error": f"Rapor oluşturulurken hata: {e}"}), 500

@app.route('/report/download/<int:report_id>', methods=['GET'])
def download_report(report_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM reports WHERE id=?', (report_id,)).fetchone()
    conn.close()
    if not row or not os.path.exists(row['file_path']):
        return "Rapor bulunamadı.", 404

    filename = os.path.basename(row['file_path'])
    return send_file(row['file_path'], as_attachment=True, download_name=filename,
                      mimetype='application/pdf')

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"}), 200

# Modul seviyesinde baslatiliyor ki hem "python server.py" (yerel test)
# hem de gunicorn (Railway production) altinda calissin. Gunicorn dosyayi
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
