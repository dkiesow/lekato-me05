#!/usr/bin/env python3
"""Reads docs/pedal_models.xlsx (filled in by hand from the app) and writes data/models.json for the tools. Run with a python that has openpyxl."""
import json
from openpyxl import load_workbook
wb = load_workbook('docs/pedal_models.xlsx'); out = {}
FIX = {'Teble': 'Treble', 'Leel': 'Level', 'Fiter': 'Filter', 'Distorton': 'Distortion'}   # spelling slips in the hand-entered sheet; the sheet itself is left as typed
for sheet in ('Gate', 'OD-FX', 'Amp', 'Cab', 'EQ', 'Mod', 'Delay', 'Reverb'):
    ws = wb[sheet]; head = [c.value for c in ws[2]]
    kcols = [i for i, h in enumerate(head) if h and h.startswith('Knob ')]; dcols = [i for i, h in enumerate(head) if h and h.startswith('Default ')]
    ncol = len(head) - 1; models = []
    for row in ws.iter_rows(min_row=3, values_only=True):
        if row[0] is None: continue
        n = row[3]
        models.append({'app': row[0], 'value': row[1], 'name': row[2], 'knobs': [FIX.get(row[i], row[i]) for i in kcols[:n]], 'defaults': [row[i] for i in dcols[:n]], 'notes': row[ncol] if len(row) > ncol else None})
    out[sheet] = models
ws = wb['Drum']
out['Drum'] = {'genres': [r[2] for r in ws.iter_rows(min_row=3, max_row=32, values_only=True) if r[2]], 'notes': [r[4] for r in ws.iter_rows(min_row=3, max_row=32, values_only=True) if r[4]]}
g = out['Drum']['genres']; out['Drum']['patterns'] = ['%s %02d' % (x, i) for x in g for i in range(1, 11)]; out['Drum']['knobs'] = ['Drum (level)', 'BPM']
json.dump(out, open('data/models.json', 'w'), indent=1); print({k: len(v) for k, v in out.items()})
