#!/usr/bin/env python3
"""
send_mtd_email.py — builds and sends the daily MTD summary email.

Runs as the last step of the "Process Daily MTD Files" GitHub Action, right
after convert_mtd.js has synced the freshly-uploaded file into the
`mtd_mobile` table on Supabase. This script does NOT touch the .xlsb file
itself — it reads the already-parsed rows straight from Supabase, so the
numbers here are guaranteed to match what the dashboard's MTD tab shows
(same `row.mobileProj` / `row.mobileProjPct` fields the browser uses).

Hierarchy in the data (3 levels — see convert_mtd.js):
  regional_manager  -> the small number of senior Area Managers (what the
                        email calls "المناطق" — this is the level the email
                        body summarizes).
  area_manager      -> a mid-level supervisor covering several branches.
  store              -> the actual branch (this is the level the attached
                        PDF lists in full, grouped under its regional_manager).

Required environment variables (all already exist as GitHub Secrets, except
the two new ones marked NEW):
  SUPABASE_URL, SUPABASE_SERVICE_KEY   (existing)
  RESEND_API_KEY                       (NEW — from resend.com, free tier)
  MTD_EMAIL_TO                         (NEW — comma-separated recipient list)

Usage:
  python scripts/send_mtd_email.py
"""
import os
import json
import base64
import subprocess
import urllib.request
from datetime import datetime

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
MTD_EMAIL_TO = [e.strip() for e in os.environ.get('MTD_EMAIL_TO', '').split(',') if e.strip()]
RESEND_FROM = os.environ.get('RESEND_FROM', 'FAKHARANY360 <onboarding@resend.dev>')

WATCHLIST_SIZE = 5  # weakest branches shown in the email body (full list goes in the PDF)

PRODUCTS = [
    {'key': 'mobile', 'label': 'Mobile', 'tKey': 'mobileTarget', 'aKey': 'mobileSubs', 'projKey': 'mobileProj', 'color': '#5E2D91', 'bg': '#f5f0fa'},
    {'key': 'gold',   'label': 'WE Gold', 'tKey': 'goldTarget',  'aKey': 'goldSubs',   'projKey': 'goldProj',   'color': '#f5a623', 'bg': '#fff8ec'},
    {'key': 'fbb',    'label': 'FBB',     'tKey': 'fbbTarget',   'aKey': 'fbbSubs',    'projKey': 'fbbProj',    'color': '#2563eb', 'bg': '#eef4ff'},
    {'key': 'fixed',  'label': 'Fixed',   'tKey': 'fixedTarget', 'aKey': 'fixedSubs',  'projKey': 'fixedProj',  'color': '#16a34a', 'bg': '#eefbf1'},
    {'key': 'fwa',    'label': 'WE Air',  'tKey': 'fwaTarget',   'aKey': 'fwaSubs',    'projKey': 'fwaProj',    'color': '#0891b2', 'bg': '#ecfafe'},
]


def supabase_get(path):
    url = f'{SUPABASE_URL}/rest/v1/{path}'
    req = urllib.request.Request(url, headers={
        'apikey': SUPABASE_SERVICE_KEY,
        'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
        'User-Agent': 'Mozilla/5.0 (compatible; FAKHARANY360-bot/1.0)',
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode('utf-8'))


def fetch_latest_snapshot():
    latest = supabase_get('mtd_mobile?select=snapshot_date&order=snapshot_date.desc&limit=1')
    if not latest:
        raise RuntimeError('No rows found in mtd_mobile — has any MTD file been processed yet?')
    date_iso = latest[0]['snapshot_date']
    rows = supabase_get(f'mtd_mobile?snapshot_date=eq.{date_iso}&select=*')
    return date_iso, rows


def num(row, key):
    v = (row.get('row') or {}).get(key)
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def pct(a, t):
    return (a / t * 100) if t else 0.0


def status_label_color(p):
    if p >= 95:
        return 'ممتاز', '#16a34a'
    if p >= 80:
        return 'متابعة', '#f5a623'
    return 'تدخل عاجل', '#c0392b'


def status_color(p):
    return status_label_color(p)[1]


def store_name(r):
    return (r.get('row') or {}).get('store') or r.get('store_code') or '-'


def build_product_grid(rows):
    cells = []
    for p in PRODUCTS:
        t = sum(num(r, p['tKey']) for r in rows)
        proj = sum(num(r, p['projKey']) for r in rows)
        cells.append({**p, 'projPct': pct(proj, t)})
    return cells


def build_regional_manager_table(rows):
    """Top-level grouping = regional_manager (the small set of senior Area
    Managers). This is what the email body calls "المناطق"."""
    groups = {}
    for r in rows:
        rm = r.get('regional_manager') or r.get('area_manager') or 'غير محدد'
        groups.setdefault(rm, []).append(r)

    out = []
    for rm, grs in groups.items():
        t = sum(num(r, 'mobileTarget') for r in grs)
        a = sum(num(r, 'mobileSubs') for r in grs)
        proj = sum(num(r, 'mobileProj') for r in grs)
        achPct, projPct = pct(a, t), pct(proj, t)
        status, color = status_label_color(projPct)
        stores = len(set(r.get('store_code') for r in grs))
        out.append({'name': rm, 'stores': stores, 'achPct': achPct, 'projPct': projPct, 'status': status, 'color': color})
    out.sort(key=lambda x: x['projPct'])
    return out


def build_branch_detail(rows):
    """Full branch-level list, grouped by regional_manager — this feeds the PDF."""
    groups = {}
    for r in rows:
        t = num(r, 'mobileTarget')
        if t <= 0:
            continue
        rm = r.get('regional_manager') or r.get('area_manager') or 'غير محدد'
        a = num(r, 'mobileSubs')
        proj = num(r, 'mobileProj')
        groups.setdefault(rm, []).append({
            'store': store_name(r),
            'area_manager': r.get('area_manager') or '-',
            'supervisor': r.get('supervisor') or '-',
            'achPct': pct(a, t),
            'projPct': pct(proj, t),
        })
    for rm in groups:
        groups[rm].sort(key=lambda x: x['projPct'])
    return groups


def build_watchlist(rows):
    items = []
    for r in rows:
        t = num(r, 'mobileTarget')
        if t <= 0:
            continue
        a = num(r, 'mobileSubs')
        proj = num(r, 'mobileProj')
        items.append({
            'store': store_name(r),
            'supervisor': r.get('supervisor') or '-',
            'achPct': pct(a, t),
            'projPct': pct(proj, t),
        })
    items.sort(key=lambda x: x['projPct'])
    return items[:WATCHLIST_SIZE]


def render_html(date_iso, rows, region_label='Cairo & Canal', pdf_attached=False):
    grid = build_product_grid(rows)
    rms = build_regional_manager_table(rows)
    watch = build_watchlist(rows)

    mobile = next(p for p in grid if p['key'] == 'mobile')
    total_t = sum(num(r, 'mobileTarget') for r in rows)
    total_a = sum(num(r, 'mobileSubs') for r in rows)
    ach_pct = pct(total_a, total_t)
    day_num = datetime.fromisoformat(date_iso).day

    grid_cells = ''.join(f"""
        <td width="20%" align="center" style="background:{p['bg']};border-radius:8px;padding:10px 4px;">
          <div style="font-size:11px;color:{p['color']};font-weight:700;">{p['label']}</div>
          <div style="font-size:9px;color:{p['color']};opacity:0.7;font-weight:700;">Proj%</div>
          <div style="font-size:17px;font-weight:800;color:{p['color']};">{p['projPct']:.0f}%</div>
        </td>""" for p in grid)

    rm_rows = ''.join(f"""
        <tr style="background:{'#fff5f5' if rm['color']=='#c0392b' else '#ffffff'};">
          <td style="padding:8px 10px;color:#1a1a2e;">{rm['name']}</td>
          <td style="padding:8px 10px;" align="center">{rm['stores']}</td>
          <td style="padding:8px 10px;font-weight:700;" align="center">{rm['achPct']:.0f}%</td>
          <td style="padding:8px 10px;font-weight:800;color:{rm['color']};" align="center">{rm['projPct']:.0f}%</td>
          <td align="center"><span style="background:{rm['color']};color:#fff;font-size:10px;padding:2px 8px;border-radius:10px;">{rm['status']}</span></td>
        </tr>""" for rm in rms)

    watch_rows = ''.join(f"""
        <tr style="background:#fdf3ea;">
          <td style="padding:8px 10px;color:#1a1a2e;">{w['store']}</td>
          <td style="padding:8px 10px;color:#c0392b;font-weight:800;" align="center">{w['achPct']:.0f}%</td>
          <td style="padding:8px 10px;color:{status_color(w['projPct'])};font-weight:800;" align="center">{w['projPct']:.0f}%</td>
          <td style="padding:8px 10px;color:#7a2020;font-size:11px;" align="left">{w['supervisor']}</td>
        </tr>""" for w in watch)

    pdf_btn = """
  <tr><td style="padding:22px 28px;">
    <table role="presentation" width="100%"><tr>
      <td>
        <span style="display:inline-block;background:#5E2D91;color:#fff;font-size:12px;font-weight:700;padding:9px 18px;border-radius:8px;">📎 تفاصيل كل الفروع (PDF مرفق)</span>
      </td>
    </tr></table>
  </td></tr>""" if pdf_attached else ""

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background-color:#f0eef7;font-family:'Segoe UI',Tahoma,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0eef7;padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 2px 10px rgba(94,45,145,0.08);">
  <tr><td style="background:linear-gradient(135deg,#5E2D91,#e91e8c);padding:22px 28px;">
    <table role="presentation" width="100%"><tr>
      <td style="color:#ffffff;font-size:19px;font-weight:800;">📊 FAKHARANY360 — MTD يومي</td>
      <td align="left" style="color:#ffffff;font-size:12px;opacity:0.9;">{date_iso}</td>
    </tr></table>
    <div style="color:#f3e8ff;font-size:12px;margin-top:4px;">تقرير محمد الفخراني — منطقة {region_label}</div>
  </td></tr>

  <tr><td style="padding:20px 28px 4px 28px;">
    <table role="presentation" width="100%" style="background:#f3e8ff;border-right:4px solid #5E2D91;border-radius:8px;">
      <tr><td style="padding:14px 16px;" dir="ltr" align="left">
        <div style="color:#5E2D91;font-size:13px;font-weight:800;margin-bottom:4px;">💡 Month-End Projection</div>
        <div style="color:#3a1d5c;font-size:13px;line-height:1.7;">
          Achieved <b>{ach_pct:.0f}%</b> of Mobile target through Day {day_num}. At the current pace, we're projected to close the month at
          <b style="color:{status_color(mobile['projPct'])};">{mobile['projPct']:.0f}%</b> (Target: 100%).
        </div>
      </td></tr>
    </table>
  </td></tr>

  <tr><td style="padding:18px 28px 4px 28px;">
    <div style="color:#1a1a2e;font-size:14px;font-weight:800;margin-bottom:2px;">📦 الأداء حسب المنتج</div>
    <div style="color:#9ca3af;font-size:10px;margin-bottom:8px;">القيم = Projected (توقع الإغلاق)</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="6"><tr>{grid_cells}</tr></table>
  </td></tr>

  <tr><td style="padding:20px 28px 4px 28px;">
    <div style="color:#1a1a2e;font-size:14px;font-weight:800;margin-bottom:8px;">🗺️ أداء المناطق ({len(rms)})</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eee;border-radius:8px;overflow:hidden;font-size:12px;">
      <tr style="background:#5E2D91;color:#fff;">
        <td style="padding:8px 10px;font-weight:700;">المنطقة</td>
        <td style="padding:8px 10px;font-weight:700;" align="center">فروع</td>
        <td style="padding:8px 10px;font-weight:700;" align="center">Ach%</td>
        <td style="padding:8px 10px;font-weight:700;" align="center">Proj%</td>
        <td style="padding:8px 10px;font-weight:700;" align="center">الحالة</td>
      </tr>
      {rm_rows}
    </table>
  </td></tr>

  <tr><td style="padding:20px 28px 4px 28px;">
    <div style="color:#1a1a2e;font-size:14px;font-weight:800;margin-bottom:8px;">⚠️ فروع تحتاج نظرك (أضعف {WATCHLIST_SIZE} من إجمالي الفروع)</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eee;border-radius:8px;overflow:hidden;font-size:12px;">
      <tr style="background:#5E2D91;">
        <td style="padding:6px 10px;color:#fff;font-size:10px;font-weight:700;">الفرع</td>
        <td style="padding:6px 10px;color:#fff;font-size:10px;font-weight:700;" align="center">Ach%</td>
        <td style="padding:6px 10px;color:#fff;font-size:10px;font-weight:700;" align="center">Proj%</td>
        <td style="padding:6px 10px;color:#fff;font-size:10px;font-weight:700;" align="left">المشرف</td>
      </tr>
      {watch_rows}
    </table>
  </td></tr>
  {pdf_btn}

  <tr><td style="padding:0 28px 22px 28px;">
    <div style="color:#9ca3af;font-size:10px;">FAKHARANY360 · تقرير تلقائي يومي · لا ترد على هذا الإيميل</div>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


def build_pdf(date_iso, rows, region_label='Cairo & Canal'):
    """Full branch-level breakdown, grouped by regional manager. Returns raw
    PDF bytes, or None if the PDF/Arabic-text dependencies aren't installed
    (in that case the email is still sent, just without the attachment)."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        import arabic_reshaper
        from bidi.algorithm import get_display
    except ImportError as e:
        print(f'  ⚠️  PDF skipped — missing dependency: {e}')
        return None

    font_path = os.path.join(os.path.dirname(__file__), 'assets', 'Amiri-Regular.ttf')
    pdfmetrics.registerFont(TTFont('Amiri', font_path))

    def ar(text):
        """Reshape + reorder Arabic text for correct rendering in the PDF."""
        return get_display(arabic_reshaper.reshape(str(text)))

    branches = build_branch_detail(rows)
    buf_path = '/tmp/fakharany360_mtd_detail.pdf'
    c = canvas.Canvas(buf_path, pagesize=A4)
    W, H = A4
    y = H - 20 * mm

    def header():
        nonlocal y
        c.setFillColorRGB(0.37, 0.18, 0.57)
        c.rect(0, H - 25 * mm, W, 25 * mm, fill=1, stroke=0)
        c.setFillColorRGB(1, 1, 1)
        c.setFont('Amiri', 16)
        c.drawRightString(W - 15 * mm, H - 12 * mm, ar(f'تفاصيل الفروع — MTD {date_iso}'))
        c.setFont('Amiri', 10)
        c.drawRightString(W - 15 * mm, H - 19 * mm, ar(f'منطقة {region_label} — محمد الفخراني'))
        c.setFillColorRGB(0, 0, 0)
        y = H - 32 * mm

    def new_page():
        c.showPage()
        header()

    def row_line(cells, bold=False, bg=None, text_color=(0, 0, 0)):
        nonlocal y
        if y < 20 * mm:
            new_page()
        if bg:
            c.setFillColorRGB(*bg)
            c.rect(15 * mm, y - 5.5 * mm, W - 30 * mm, 7 * mm, fill=1, stroke=0)
        c.setFillColorRGB(*text_color)
        c.setFont('Amiri', 10)
        x = W - 18 * mm
        widths = [70 * mm, 30 * mm, 30 * mm, 30 * mm]
        for text, wdt in zip(cells, widths):
            c.drawRightString(x, y, ar(text) if any('\u0600' <= ch <= '\u06FF' for ch in str(text)) else str(text))
            x -= wdt
        y -= 7 * mm

    header()
    for rm_name, brs in branches.items():
        row_line([f'المنطقة: {rm_name}  ({len(brs)} فرع)', '', '', ''], bg=(0.95, 0.94, 0.98))
        row_line(['الفرع', 'المشرف', 'Ach%', 'Proj%'], bg=(0.37, 0.18, 0.57), text_color=(1, 1, 1))
        for b in brs:
            bg = (1, 0.96, 0.96) if b['projPct'] < 80 else None
            row_line([b['store'], b['supervisor'], f"{b['achPct']:.0f}%", f"{b['projPct']:.0f}%"], bg=bg)
        y -= 3 * mm

    c.save()
    with open(buf_path, 'rb') as f:
        return f.read()


def send_via_resend(html, subject, pdf_bytes=None, pdf_filename='fakharany360_branches.pdf'):
    if not RESEND_API_KEY:
        print('  ⚠️  RESEND_API_KEY not set — skipping send, printing HTML length only.')
        print(f'  (HTML length: {len(html)} chars)')
        return
    if not MTD_EMAIL_TO:
        print('  ⚠️  MTD_EMAIL_TO not set — no recipients, skipping send.')
        return

    email_payload = {
        'from': RESEND_FROM,
        'to': MTD_EMAIL_TO,
        'subject': subject,
        'html': html,
    }
    if pdf_bytes:
        email_payload['attachments'] = [{
            'filename': pdf_filename,
            'content': base64.b64encode(pdf_bytes).decode('ascii'),
        }]

    payload = json.dumps(email_payload)
    # Using curl instead of urllib: Cloudflare (which fronts api.resend.com) blocks
    # plain Python urllib requests based on their TLS/HTTP client fingerprint
    # (HTTP 403 / Cloudflare error 1010), regardless of headers set. curl's
    # fingerprint is not flagged, and it's what Resend's own docs use.
    result = subprocess.run(
        ['curl', '-sS', '-w', '\n%{http_code}', '-X', 'POST', 'https://api.resend.com/emails',
         '-H', f'Authorization: Bearer {RESEND_API_KEY}',
         '-H', 'Content-Type: application/json',
         '--data-binary', '@-'],
        input=payload, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f'curl failed to run: {result.stderr}')
    body, _, status_code = result.stdout.rpartition('\n')
    if not status_code.strip().isdigit() or not (200 <= int(status_code.strip()) < 300):
        raise RuntimeError(f'Resend send failed (HTTP {status_code.strip()}): {body}')
    print('  ✅ Email sent:', body)


def main():
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        print('  ℹ️  SUPABASE_URL/SUPABASE_SERVICE_KEY not set — nothing to email, skipping.')
        return
    date_iso, rows = fetch_latest_snapshot()
    if not rows:
        print('  ℹ️  No rows for latest snapshot — skipping email.')
        return

    pdf_bytes = build_pdf(date_iso, rows)
    html = render_html(date_iso, rows, pdf_attached=bool(pdf_bytes))
    subject = f'📊 FAKHARANY360 — ملخص MTD ليوم {date_iso}'
    send_via_resend(html, subject, pdf_bytes=pdf_bytes, pdf_filename=f'fakharany360_branches_{date_iso}.pdf')


if __name__ == '__main__':
    main()
