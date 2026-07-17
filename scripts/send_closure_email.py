#!/usr/bin/env python3
"""
send_closure_email.py — builds and sends the monthly closure summary email.

Runs as the last step of the "Process Monthly Closure Files" GitHub Action,
right after convert_closure.py has synced the freshly-uploaded month into
the `monthly_closure` table on Supabase. Reads the already-parsed rows back
from Supabase (same numbers the dashboard's Overview/Insights tabs use).

Required environment variables (RESEND_* are NEW, same names as the MTD
email script — reuse the same secrets):
  SUPABASE_URL, SUPABASE_SERVICE_KEY   (existing)
  RESEND_API_KEY                       (NEW)
  MTD_EMAIL_TO                         (NEW — same recipient list as MTD email)

Usage:
  python scripts/send_closure_email.py
"""
import os
import json
import base64
import subprocess
import urllib.request

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
MTD_EMAIL_TO = [e.strip() for e in os.environ.get('MTD_EMAIL_TO', '').split(',') if e.strip()]
RESEND_FROM = os.environ.get('RESEND_FROM', 'FAKHARANY360 <onboarding@resend.dev>')

WATCHLIST_SIZE = 5

PRODUCTS = [
    {'key': 'mobile', 'label': 'Mobile', 'tKey': 'mobileTarget', 'aKey': 'mobileSubs', 'color': '#5E2D91', 'bg': '#f5f0fa'},
    {'key': 'gold',   'label': 'WE Gold', 'tKey': 'goldTarget',  'aKey': 'goldSubs',   'color': '#f5a623', 'bg': '#fff8ec'},
    {'key': 'fbb',    'label': 'FBB',     'tKey': 'fbbTarget',   'aKey': 'fbbSubs',    'color': '#2563eb', 'bg': '#eef4ff'},
    {'key': 'fixed',  'label': 'Fixed',   'tKey': 'fixedTarget', 'aKey': 'fixedSubs',  'color': '#16a34a', 'bg': '#eefbf1'},
    {'key': 'wallet', 'label': 'Wallet',  'tKey': 'walletTarget','aKey': 'walletSales','color': '#e91e8c', 'bg': '#fdeef6'},
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


def fetch_latest_month():
    latest = supabase_get('monthly_closure?select=month&order=month.desc&limit=1')
    if not latest:
        raise RuntimeError('No rows found in monthly_closure — has a closure file been processed yet?')
    month = latest[0]['month']
    rows = supabase_get(f'monthly_closure?month=eq.{month}&select=*')
    return month, rows


def num(row, key):
    v = (row.get('row') or {}).get(key)
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def pct(a, t):
    return (a / t * 100) if t else 0.0


def status_color(p):
    if p >= 95:
        return '#16a34a'
    if p >= 80:
        return '#f5a623'
    return '#c0392b'


def build_product_grid(rows):
    cells = []
    for p in PRODUCTS:
        t = sum(num(r, p['tKey']) for r in rows)
        a = sum(num(r, p['aKey']) for r in rows)
        cells.append({**p, 'achPct': pct(a, t)})
    return cells


def build_tariff_mix(rows):
    low = sum(num(r, 'lowT') for r in rows)
    mid = sum(num(r, 'midT') for r in rows)
    high = sum(num(r, 'highT') for r in rows)
    total = low + mid + high
    if total == 0:
        return None
    return {
        'lowPct': low / total * 100, 'midPct': mid / total * 100, 'highPct': high / total * 100,
    }


def status_label(p):
    return 'ممتاز' if p >= 95 else 'متابعة' if p >= 80 else 'تدخل عاجل'


def store_name(r):
    return (r.get('row') or {}).get('store') or r.get('store_code') or '-'


def build_regional_manager_table(rows):
    """Top-level grouping = regional_manager (the small set of senior Area
    Managers) — this is what the email body calls "المناطق"."""
    groups = {}
    for r in rows:
        rm = r.get('regional_manager') or r.get('area_manager') or 'غير محدد'
        groups.setdefault(rm, []).append(r)
    out = []
    for rm, grs in groups.items():
        t = sum(num(r, 'mobileTarget') for r in grs)
        a = sum(num(r, 'mobileSubs') for r in grs)
        achPct = pct(a, t)
        stores = len(set(r.get('store_code') for r in grs))
        out.append({'name': rm, 'stores': stores, 'achPct': achPct, 'color': status_color(achPct)})
    out.sort(key=lambda x: x['achPct'])
    return out


def build_branch_detail(rows):
    """Full branch-level list, grouped by regional_manager — feeds the PDF."""
    groups = {}
    for r in rows:
        t = num(r, 'mobileTarget')
        if t <= 0:
            continue
        rm = r.get('regional_manager') or r.get('area_manager') or 'غير محدد'
        a = num(r, 'mobileSubs')
        groups.setdefault(rm, []).append({
            'store': store_name(r),
            'supervisor': r.get('supervisor') or '-',
            'achPct': pct(a, t),
        })
    for rm in groups:
        groups[rm].sort(key=lambda x: x['achPct'])
    return groups


def build_watchlist(rows):
    items = []
    for r in rows:
        t = num(r, 'mobileTarget')
        if t <= 0:
            continue
        a = num(r, 'mobileSubs')
        items.append({
            'store': (r.get('row') or {}).get('store', r.get('store_code')),
            'supervisor': r.get('supervisor') or '-',
            'achPct': pct(a, t),
        })
    items.sort(key=lambda x: x['achPct'])
    return items[:WATCHLIST_SIZE]


def render_html(month, rows, region_label='Cairo & Canal', pdf_attached=False):
    grid = build_product_grid(rows)
    tariff = build_tariff_mix(rows)
    rms = build_regional_manager_table(rows)
    watch = build_watchlist(rows)
    mobile = next(p for p in grid if p['key'] == 'mobile')

    grid_cells = ''.join(f"""
        <td width="20%" align="center" style="background:{p['bg']};border-radius:8px;padding:10px 4px;">
          <div style="font-size:11px;color:{p['color']};font-weight:700;">{p['label']}</div>
          <div style="font-size:9px;color:{p['color']};opacity:0.7;font-weight:700;">Ach%</div>
          <div style="font-size:17px;font-weight:800;color:{p['color']};">{p['achPct']:.0f}%</div>
        </td>""" for p in grid)

    tariff_block = ''
    if tariff:
        tariff_block = f"""
  <tr><td style="padding:18px 28px 4px 28px;">
    <div style="color:#1a1a2e;font-size:14px;font-weight:800;margin-bottom:8px;">🎯 توزيع الباقات (Tariff Mix)</div>
    <table role="presentation" width="100%" style="border-radius:8px;overflow:hidden;"><tr style="height:22px;">
      <td width="{tariff['highPct']:.0f}%" style="background:#16a34a;"></td>
      <td width="{tariff['midPct']:.0f}%" style="background:#f5a623;"></td>
      <td width="{tariff['lowPct']:.0f}%" style="background:#c0392b;"></td>
    </tr></table>
    <table role="presentation" width="100%" style="margin-top:4px;"><tr>
      <td style="font-size:11px;color:#16a34a;font-weight:700;">■ High {tariff['highPct']:.0f}%</td>
      <td align="center" style="font-size:11px;color:#f5a623;font-weight:700;">■ Mid {tariff['midPct']:.0f}%</td>
      <td align="left" style="font-size:11px;color:#c0392b;font-weight:700;">■ Low {tariff['lowPct']:.0f}%</td>
    </tr></table>
  </td></tr>"""

    rm_rows = ''.join(f"""
        <tr style="background:{'#fff5f5' if rm['color']=='#c0392b' else '#ffffff'};">
          <td style="padding:8px 10px;color:#1a1a2e;">{rm['name']}</td>
          <td style="padding:8px 10px;" align="center">{rm['stores']}</td>
          <td style="padding:8px 10px;font-weight:800;color:{rm['color']};" align="center">{rm['achPct']:.0f}%</td>
          <td align="center"><span style="background:{rm['color']};color:#fff;font-size:10px;padding:2px 8px;border-radius:10px;">{status_label(rm['achPct'])}</span></td>
        </tr>""" for rm in rms)

    watch_rows = ''.join(f"""
        <tr style="background:#fdf3ea;">
          <td style="padding:8px 10px;color:#1a1a2e;">{w['store']}</td>
          <td style="padding:8px 10px;color:{status_color(w['achPct'])};font-weight:800;" align="center">{w['achPct']:.0f}%</td>
          <td style="padding:8px 10px;color:#7a2020;font-size:11px;" align="left">{w['supervisor']}</td>
        </tr>""" for w in watch)

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background-color:#f0eef7;font-family:'Segoe UI',Tahoma,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0eef7;padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 2px 10px rgba(94,45,145,0.08);">
  <tr><td style="background:linear-gradient(135deg,#5E2D91,#e91e8c);padding:22px 28px;">
    <table role="presentation" width="100%"><tr>
      <td style="color:#ffffff;font-size:19px;font-weight:800;">📅 FAKHARANY360 — إغلاق الشهر</td>
      <td align="left" style="color:#ffffff;font-size:12px;opacity:0.9;">{month}</td>
    </tr></table>
    <div style="color:#f3e8ff;font-size:12px;margin-top:4px;">تقرير محمد الفخراني — منطقة {region_label}</div>
  </td></tr>

  <tr><td style="padding:20px 28px 4px 28px;">
    <table role="presentation" width="100%" style="background:#f3e8ff;border-right:4px solid #5E2D91;border-radius:8px;">
      <tr><td style="padding:14px 16px;" dir="ltr" align="left">
        <div style="color:#5E2D91;font-size:13px;font-weight:800;margin-bottom:4px;">💡 Final Result</div>
        <div style="color:#3a1d5c;font-size:13px;line-height:1.7;">
          Mobile closed the month at <b style="color:{status_color(mobile['achPct'])};">{mobile['achPct']:.0f}%</b> of target across {sum(a['stores'] for a in rms)} branches.
        </div>
      </td></tr>
    </table>
  </td></tr>

  <tr><td style="padding:18px 28px 4px 28px;">
    <div style="color:#1a1a2e;font-size:14px;font-weight:800;margin-bottom:8px;">📦 نتيجة الشهر النهائية حسب المنتج</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="6"><tr>{grid_cells}</tr></table>
  </td></tr>
  {tariff_block}

  <tr><td style="padding:20px 28px 4px 28px;">
    <div style="color:#1a1a2e;font-size:14px;font-weight:800;margin-bottom:8px;">🗺️ نتيجة المناطق ({len(rms)})</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eee;border-radius:8px;overflow:hidden;font-size:12px;">
      <tr style="background:#5E2D91;color:#fff;">
        <td style="padding:8px 10px;font-weight:700;">المنطقة</td>
        <td style="padding:8px 10px;font-weight:700;" align="center">فروع</td>
        <td style="padding:8px 10px;font-weight:700;" align="center">Ach%</td>
        <td style="padding:8px 10px;font-weight:700;" align="center">الحالة</td>
      </tr>
      {rm_rows}
    </table>
  </td></tr>

  <tr><td style="padding:20px 28px 4px 28px;">
    <div style="color:#1a1a2e;font-size:14px;font-weight:800;margin-bottom:8px;">⚠️ أضعف {WATCHLIST_SIZE} فروع الشهر ده (من إجمالي الفروع)</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eee;border-radius:8px;overflow:hidden;font-size:12px;">
      <tr style="background:#5E2D91;">
        <td style="padding:6px 10px;color:#fff;font-size:10px;font-weight:700;">الفرع</td>
        <td style="padding:6px 10px;color:#fff;font-size:10px;font-weight:700;" align="center">Ach%</td>
        <td style="padding:6px 10px;color:#fff;font-size:10px;font-weight:700;" align="left">المشرف</td>
      </tr>
      {watch_rows}
    </table>
  </td></tr>
  {"<tr><td style='padding:22px 28px;'><span style='display:inline-block;background:#5E2D91;color:#fff;font-size:12px;font-weight:700;padding:9px 18px;border-radius:8px;'>📎 تفاصيل كل الفروع (PDF مرفق)</span></td></tr>" if pdf_attached else ""}

  <tr><td style="padding:0 28px 22px 28px;">
    <div style="color:#9ca3af;font-size:10px;">FAKHARANY360 · تقرير إغلاق شهري تلقائي · لا ترد على هذا الإيميل</div>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


def build_pdf(month, rows, region_label='Cairo & Canal'):
    """Full branch-level breakdown, grouped by regional manager. Returns raw
    PDF bytes, or None if the PDF/Arabic-text dependencies aren't installed."""
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
        return get_display(arabic_reshaper.reshape(str(text)))

    branches = build_branch_detail(rows)
    buf_path = '/tmp/fakharany360_closure_detail.pdf'
    c = canvas.Canvas(buf_path, pagesize=A4)
    W, H = A4
    y = H - 20 * mm

    def header():
        nonlocal y
        c.setFillColorRGB(0.37, 0.18, 0.57)
        c.rect(0, H - 25 * mm, W, 25 * mm, fill=1, stroke=0)
        c.setFillColorRGB(1, 1, 1)
        c.setFont('Amiri', 16)
        c.drawRightString(W - 15 * mm, H - 12 * mm, ar(f'تفاصيل الفروع — إغلاق {month}'))
        c.setFont('Amiri', 10)
        c.drawRightString(W - 15 * mm, H - 19 * mm, ar(f'منطقة {region_label} — محمد الفخراني'))
        c.setFillColorRGB(0, 0, 0)
        y = H - 32 * mm

    def new_page():
        c.showPage()
        header()

    def row_line(cells, bg=None, text_color=(0, 0, 0)):
        nonlocal y
        if y < 20 * mm:
            new_page()
        if bg:
            c.setFillColorRGB(*bg)
            c.rect(15 * mm, y - 5.5 * mm, W - 30 * mm, 7 * mm, fill=1, stroke=0)
        c.setFillColorRGB(*text_color)
        c.setFont('Amiri', 10)
        x = W - 18 * mm
        widths = [90 * mm, 40 * mm, 30 * mm]
        for text, wdt in zip(cells, widths):
            c.drawRightString(x, y, ar(text) if any('\u0600' <= ch <= '\u06FF' for ch in str(text)) else str(text))
            x -= wdt
        y -= 7 * mm

    header()
    for rm_name, brs in branches.items():
        row_line([f'المنطقة: {rm_name}  ({len(brs)} فرع)', '', ''], bg=(0.95, 0.94, 0.98))
        row_line(['الفرع', 'المشرف', 'Ach%'], bg=(0.37, 0.18, 0.57), text_color=(1, 1, 1))
        for b in brs:
            bg = (1, 0.96, 0.96) if b['achPct'] < 80 else None
            row_line([b['store'], b['supervisor'], f"{b['achPct']:.0f}%"], bg=bg)
        y -= 3 * mm

    c.save()
    with open(buf_path, 'rb') as f:
        return f.read()


def send_via_resend(html, subject, pdf_bytes=None, pdf_filename='fakharany360_branches.pdf'):
    if not RESEND_API_KEY:
        print('  ⚠️  RESEND_API_KEY not set — skipping send.')
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
    month, rows = fetch_latest_month()
    if not rows:
        print('  ℹ️  No rows for latest month — skipping email.')
        return
    pdf_bytes = build_pdf(month, rows)
    html = render_html(month, rows, pdf_attached=bool(pdf_bytes))
    subject = f'📅 FAKHARANY360 — تقرير إغلاق شهر {month}'
    send_via_resend(html, subject, pdf_bytes=pdf_bytes, pdf_filename=f'fakharany360_branches_{month}.pdf')


if __name__ == '__main__':
    main()
