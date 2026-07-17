#!/usr/bin/env python3
"""
send_mtd_email.py — builds and sends the daily MTD summary email.

Runs as the last step of the "Process Daily MTD Files" GitHub Action, right
after convert_mtd.js has synced the freshly-uploaded file into the
`mtd_mobile` table on Supabase. This script does NOT touch the .xlsb file
itself — it reads the already-parsed rows straight from Supabase, so the
numbers here are guaranteed to match what the dashboard's MTD tab shows
(same `row.mobileProj` / `row.mobileProjPct` fields the browser uses).

Required environment variables (all already exist as GitHub Secrets, except
the two new ones marked NEW):
  SUPABASE_URL, SUPABASE_SERVICE_KEY   (existing)
  RESEND_API_KEY                       (NEW — from resend.com, free tier)
  MTD_EMAIL_TO                         (NEW — comma-separated recipient list)

Usage:
  python scripts/send_mtd_email.py
"""
import os
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
MTD_EMAIL_TO = [e.strip() for e in os.environ.get('MTD_EMAIL_TO', '').split(',') if e.strip()]
RESEND_FROM = os.environ.get('RESEND_FROM', 'FAKHARANY360 <onboarding@resend.dev>')

WATCHLIST_SIZE = 5  # how many weakest branches to show in the email body

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
    """Find the most recent snapshot_date in mtd_mobile, then fetch all its rows."""
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


def build_product_grid(rows):
    cells = []
    for p in PRODUCTS:
        t = sum(num(r, p['tKey']) for r in rows)
        proj = sum(num(r, p['projKey']) for r in rows)
        projPct = pct(proj, t)
        cells.append({**p, 'projPct': projPct})
    return cells


def build_area_manager_table(rows):
    groups = {}
    for r in rows:
        am = r.get('area_manager') or 'Unassigned'
        groups.setdefault(am, []).append(r)

    out = []
    for am, grs in groups.items():
        t = sum(num(r, 'mobileTarget') for r in grs)
        a = sum(num(r, 'mobileSubs') for r in grs)
        proj = sum(num(r, 'mobileProj') for r in grs)
        achPct = pct(a, t)
        projPct = pct(proj, t)
        stores = len(set(r.get('store_code') for r in grs))
        if projPct >= 95:
            status, color = 'ممتاز', '#16a34a'
        elif projPct >= 80:
            status, color = 'متابعة', '#f5a623'
        else:
            status, color = 'تدخل عاجل', '#c0392b'
        out.append({'name': am, 'stores': stores, 'achPct': achPct, 'projPct': projPct, 'status': status, 'color': color})
    out.sort(key=lambda x: x['projPct'])
    return out


def build_watchlist(rows):
    items = []
    for r in rows:
        t = num(r, 'mobileTarget')
        if t <= 0:
            continue
        a = num(r, 'mobileSubs')
        proj = num(r, 'mobileProj')
        items.append({
            'store': (r.get('row') or {}).get('store', r.get('store_code')),
            'supervisor': r.get('supervisor') or '-',
            'achPct': pct(a, t),
            'projPct': pct(proj, t),
        })
    items.sort(key=lambda x: x['projPct'])
    return items[:WATCHLIST_SIZE]


def status_color(p):
    if p >= 95:
        return '#16a34a'
    if p >= 80:
        return '#f5a623'
    return '#c0392b'


def render_html(date_iso, rows, region_label='Cairo & Canal'):
    grid = build_product_grid(rows)
    ams = build_area_manager_table(rows)
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

    am_rows = ''.join(f"""
        <tr style="background:{'#fff5f5' if am['color']=='#c0392b' else '#ffffff'};">
          <td style="padding:8px 10px;color:#1a1a2e;">{am['name']}</td>
          <td style="padding:8px 10px;" align="center">{am['stores']}</td>
          <td style="padding:8px 10px;font-weight:700;" align="center">{am['achPct']:.0f}%</td>
          <td style="padding:8px 10px;font-weight:800;color:{am['color']};" align="center">{am['projPct']:.0f}%</td>
          <td align="center"><span style="background:{am['color']};color:#fff;font-size:10px;padding:2px 8px;border-radius:10px;">{am['status']}</span></td>
        </tr>""" for am in ams)

    watch_rows = ''.join(f"""
        <tr style="background:#fdf3ea;">
          <td style="padding:8px 10px;color:#1a1a2e;">{w['store']}</td>
          <td style="padding:8px 10px;color:#c0392b;font-weight:800;" align="center">{w['achPct']:.0f}%</td>
          <td style="padding:8px 10px;color:{status_color(w['projPct'])};font-weight:800;" align="center">{w['projPct']:.0f}%</td>
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
    <div style="color:#1a1a2e;font-size:14px;font-weight:800;margin-bottom:8px;">🗺️ أداء المناطق</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eee;border-radius:8px;overflow:hidden;font-size:12px;">
      <tr style="background:#5E2D91;color:#fff;">
        <td style="padding:8px 10px;font-weight:700;">المنطقة</td>
        <td style="padding:8px 10px;font-weight:700;" align="center">فروع</td>
        <td style="padding:8px 10px;font-weight:700;" align="center">Ach%</td>
        <td style="padding:8px 10px;font-weight:700;" align="center">Proj%</td>
        <td style="padding:8px 10px;font-weight:700;" align="center">الحالة</td>
      </tr>
      {am_rows}
    </table>
  </td></tr>

  <tr><td style="padding:20px 28px 4px 28px;">
    <div style="color:#1a1a2e;font-size:14px;font-weight:800;margin-bottom:8px;">⚠️ فروع تحتاج نظرك (أضعف {WATCHLIST_SIZE})</div>
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

  <tr><td style="padding:22px 28px;">
    <div style="color:#9ca3af;font-size:10px;">FAKHARANY360 · تقرير تلقائي يومي · لا ترد على هذا الإيميل</div>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


def send_via_resend(html, subject):
    if not RESEND_API_KEY:
        print('  ⚠️  RESEND_API_KEY not set — skipping send, printing HTML length only.')
        print(f'  (HTML length: {len(html)} chars)')
        return
    if not MTD_EMAIL_TO:
        print('  ⚠️  MTD_EMAIL_TO not set — no recipients, skipping send.')
        return
    payload = json.dumps({
        'from': RESEND_FROM,
        'to': MTD_EMAIL_TO,
        'subject': subject,
        'html': html,
    }).encode('utf-8')
    req = urllib.request.Request('https://api.resend.com/emails', data=payload, method='POST', headers={
        'Authorization': f'Bearer {RESEND_API_KEY}',
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (compatible; FAKHARANY360-bot/1.0)',
    })
    try:
        with urllib.request.urlopen(req) as resp:
            print('  ✅ Email sent:', resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Resend send failed (HTTP {e.code}): {body}')


def main():
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        print('  ℹ️  SUPABASE_URL/SUPABASE_SERVICE_KEY not set — nothing to email, skipping.')
        return
    date_iso, rows = fetch_latest_snapshot()
    if not rows:
        print('  ℹ️  No rows for latest snapshot — skipping email.')
        return
    html = render_html(date_iso, rows)
    subject = f'📊 FAKHARANY360 — ملخص MTD ليوم {date_iso}'
    send_via_resend(html, subject)


if __name__ == '__main__':
    main()
