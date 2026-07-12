#!/usr/bin/env python3
"""
backfill_supabase.py — ONE-TIME migration script.

Reads the existing data/manifest.json + data/*.json files (the old public
JSON dataset) and pushes every month's rows into Supabase's
monthly_closure table. Run this once via the "Backfill Historical Data to
Supabase" GitHub Action, then it's safe to delete data/*.json entirely.

Safe to re-run: uses the same upsert (merge-duplicates) logic as the
regular monthly closure sync, so running it twice just re-writes the same
rows instead of duplicating them.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import convert_closure as cc  # reuses sync_month_to_supabase / SUPABASE_ENABLED


def main():
    if not cc.SUPABASE_ENABLED:
        print('❌ SUPABASE_URL / SUPABASE_SERVICE_KEY not set — nothing to do.')
        sys.exit(1)

    manifest_path = os.path.join('data', 'manifest.json')
    if not os.path.exists(manifest_path):
        print(f'❌ {manifest_path} not found — nothing to backfill.')
        sys.exit(1)

    with open(manifest_path, encoding='utf-8') as f:
        manifest = json.load(f)
    months = manifest.get('months', [])
    print(f'Found {len(months)} month(s) in manifest: {months}')

    total_rows = 0
    for month in months:
        path = os.path.join('data', month + '.json')
        if not os.path.exists(path):
            print(f'  ⚠️  Missing {path} — skipped')
            continue
        with open(path, encoding='utf-8') as f:
            rows = json.load(f)
        try:
            cc.sync_month_to_supabase(rows)
            total_rows += len(rows)
        except Exception as e:
            print(f'  ❌ Failed to sync {month}: {e}')

    print(f'✅ Backfill complete — {total_rows} row(s) synced across {len(months)} month(s).')


if __name__ == '__main__':
    main()
