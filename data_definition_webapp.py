#!/usr/bin/env python3
"""
Data Definition Webapp — FastAPI web UI for the Data Definition Agent.

Usage:
    python data_definition_webapp.py    # serves at http://localhost:8001
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = FastAPI(title="Data Definition Agent")

# ---------------------------------------------------------------------------
# Re-import agent logic from data_definition_agent (without running main)
# ---------------------------------------------------------------------------

from data_definition_agent import (
    ColumnMeta,
    TableMeta,
    generate_table_definitions,
    generate_column_definitions,
    quality_review,
)

# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class ColumnInput(BaseModel):
    table_schema: str = ""
    table_name: str = ""
    column_name: str
    data_type: str = ""
    is_nullable: str = ""
    existing_description: str = ""
    sample_values: str = ""


class TableInput(BaseModel):
    table_catalog: str = ""
    table_schema: str = ""
    table_name: str
    table_type: str = "TABLE"
    existing_description: str = ""
    notes: str = ""
    columns: list[ColumnInput] = []


class GenerateRequest(BaseModel):
    tables: list[TableInput] = []
    columns: list[ColumnInput] = []   # standalone columns (schema.table inferred)
    skip_columns: bool = False
    skip_review: bool = False


class ColumnResult(BaseModel):
    column_name: str
    data_type: str
    is_nullable: str
    business_term: str
    data_classification: str
    business_definition: str
    existing_description: str
    sample_values: str


class TableResult(BaseModel):
    table_catalog: str
    table_schema: str
    table_name: str
    table_type: str
    business_name: str
    data_domain: str
    primary_use_case: str
    business_definition: str
    existing_description: str
    columns: list[ColumnResult]


class GenerateResponse(BaseModel):
    tables: list[TableResult]
    total_tables: int
    total_columns: int


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.post("/api/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    if not req.tables and not req.columns:
        raise HTTPException(status_code=400, detail="Provide at least one table or column.")

    # Build TableMeta objects
    tables: list[TableMeta] = []
    for ti in req.tables:
        t = TableMeta(
            table_catalog=ti.table_catalog,
            table_schema=ti.table_schema,
            table_name=ti.table_name,
            table_type=ti.table_type or "TABLE",
            existing_description=ti.existing_description,
            notes=ti.notes,
        )
        for ci in ti.columns:
            t.columns.append(ColumnMeta(
                table_schema=ti.table_schema,
                table_name=ti.table_name,
                column_name=ci.column_name,
                data_type=ci.data_type,
                is_nullable=ci.is_nullable,
                existing_description=ci.existing_description,
                sample_values=ci.sample_values,
            ))
        tables.append(t)

    # Handle standalone columns — group by schema.table
    if req.columns:
        seen: dict[tuple, TableMeta] = {(t.table_schema, t.table_name): t for t in tables}
        for ci in req.columns:
            key = (ci.table_schema, ci.table_name)
            if key not in seen:
                seen[key] = TableMeta(
                    table_catalog="",
                    table_schema=ci.table_schema,
                    table_name=ci.table_name,
                )
                tables.append(seen[key])
            seen[key].columns.append(ColumnMeta(
                table_schema=ci.table_schema,
                table_name=ci.table_name,
                column_name=ci.column_name,
                data_type=ci.data_type,
                is_nullable=ci.is_nullable,
                existing_description=ci.existing_description,
                sample_values=ci.sample_values,
            ))

    if not tables:
        raise HTTPException(status_code=400, detail="No tables resolved from input.")

    # Run agents
    generate_table_definitions(tables)

    flat_cols = [c for t in tables for c in t.columns]
    if not req.skip_columns and flat_cols:
        generate_column_definitions(tables)

    if not req.skip_review:
        quality_review(tables)

    # Build response
    result_tables: list[TableResult] = []
    for t in tables:
        cols = [
            ColumnResult(
                column_name=c.column_name,
                data_type=c.data_type,
                is_nullable=c.is_nullable,
                business_term=c.business_term,
                data_classification=c.data_classification,
                business_definition=c.business_definition,
                existing_description=c.existing_description,
                sample_values=c.sample_values,
            )
            for c in t.columns
        ]
        result_tables.append(TableResult(
            table_catalog=t.table_catalog,
            table_schema=t.table_schema,
            table_name=t.table_name,
            table_type=t.table_type,
            business_name=t.business_name,
            data_domain=t.data_domain,
            primary_use_case=t.primary_use_case,
            business_definition=t.business_definition,
            existing_description=t.existing_description,
            columns=cols,
        ))

    total_cols = sum(len(t.columns) for t in result_tables)
    return GenerateResponse(tables=result_tables, total_tables=len(result_tables), total_columns=total_cols)


# ---------------------------------------------------------------------------
# HTML UI
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Data Definition Agent</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f1f5f9; color: #1e293b; min-height: 100vh; }

  /* Layout */
  .header { background: #1e293b; color: #fff; padding: 18px 32px;
            display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 1.3rem; font-weight: 600; }
  .header .badge { background: #6366f1; padding: 2px 10px; border-radius: 20px;
                   font-size: 0.72rem; font-weight: 600; letter-spacing: .04em; }
  .main { display: grid; grid-template-columns: 1fr 1fr; gap: 24px;
          padding: 28px 32px; max-width: 1400px; margin: 0 auto; }
  @media(max-width:900px){ .main { grid-template-columns: 1fr; } }

  /* Panels */
  .panel { background: #fff; border-radius: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.07);
           overflow: hidden; }
  .panel-header { padding: 16px 20px; border-bottom: 1px solid #e2e8f0;
                  display: flex; align-items: center; justify-content: space-between; }
  .panel-header h2 { font-size: 1rem; font-weight: 600; }
  .panel-body { padding: 20px; }

  /* Forms */
  .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .form-grid.triple { grid-template-columns: 1fr 1fr 1fr; }
  .form-grid.full { grid-template-columns: 1fr; }
  label { font-size: 0.8rem; font-weight: 500; color: #64748b; display: block;
          margin-bottom: 3px; }
  input, select, textarea {
    width: 100%; border: 1px solid #e2e8f0; border-radius: 7px;
    padding: 8px 10px; font-size: 0.875rem; color: #1e293b;
    background: #f8fafc; transition: border .15s;
  }
  input:focus, select:focus, textarea:focus {
    outline: none; border-color: #6366f1; background: #fff;
  }
  textarea { resize: vertical; min-height: 60px; }
  .field { display: flex; flex-direction: column; }

  /* Buttons */
  .btn { padding: 8px 16px; border-radius: 8px; font-size: 0.875rem; font-weight: 500;
         cursor: pointer; border: none; transition: opacity .15s, transform .1s; }
  .btn:active { transform: scale(.97); }
  .btn-primary { background: #6366f1; color: #fff; }
  .btn-primary:hover { background: #4f46e5; }
  .btn-sm { padding: 5px 12px; font-size: 0.8rem; }
  .btn-danger { background: #fee2e2; color: #dc2626; }
  .btn-danger:hover { background: #fecaca; }
  .btn-ghost { background: #f1f5f9; color: #475569; }
  .btn-ghost:hover { background: #e2e8f0; }
  .btn-success { background: #10b981; color: #fff; }
  .btn-success:hover { background: #059669; }
  .btn-full { width: 100%; padding: 12px; font-size: 0.95rem; }
  .btn:disabled { opacity: .5; cursor: not-allowed; }

  /* Entry cards */
  .entry-list { display: flex; flex-direction: column; gap: 8px; margin-top: 14px; }
  .entry-card { border: 1px solid #e2e8f0; border-radius: 9px; overflow: hidden; }
  .entry-card-head { padding: 9px 14px; background: #f8fafc;
                     display: flex; align-items: center; justify-content: space-between;
                     cursor: pointer; user-select: none; }
  .entry-card-head .name { font-size: 0.875rem; font-weight: 600; }
  .entry-card-head .meta { font-size: 0.75rem; color: #94a3b8; }
  .entry-card-body { padding: 14px; border-top: 1px solid #e2e8f0; display: none; }
  .entry-card-body.open { display: block; }

  /* Tabs */
  .tabs { display: flex; gap: 2px; margin-bottom: 16px; background: #f1f5f9;
          border-radius: 9px; padding: 4px; }
  .tab { flex: 1; padding: 7px; text-align: center; border-radius: 7px;
         font-size: 0.85rem; font-weight: 500; cursor: pointer; color: #64748b;
         border: none; background: transparent; transition: all .15s; }
  .tab.active { background: #fff; color: #6366f1; box-shadow: 0 1px 3px rgba(0,0,0,.08); }

  /* Results */
  .result-table-card { border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden;
                        margin-bottom: 18px; }
  .result-table-head { background: #ede9fe; padding: 12px 16px;
                       display: flex; align-items: flex-start; gap: 12px; }
  .result-table-head .domain-badge { background: #6366f1; color: #fff; border-radius: 6px;
                                      padding: 2px 9px; font-size: 0.72rem; font-weight: 600;
                                      white-space: nowrap; flex-shrink: 0; margin-top: 2px; }
  .result-table-head .table-title { font-weight: 700; font-size: 1rem; }
  .result-table-head .table-sub { font-size: 0.8rem; color: #6d28d9; margin-top: 2px; }
  .result-table-def { padding: 12px 16px; font-size: 0.875rem; line-height: 1.5;
                       color: #374151; border-bottom: 1px solid #e2e8f0; }
  .result-meta { display: flex; gap: 18px; padding: 10px 16px;
                  font-size: 0.78rem; color: #64748b; border-bottom: 1px solid #ede9fe;
                  flex-wrap: wrap; }
  .result-meta span strong { color: #374151; }
  .col-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  .col-table th { background: #f8fafc; padding: 8px 12px; text-align: left;
                   font-size: 0.75rem; font-weight: 600; color: #64748b;
                   border-bottom: 2px solid #e2e8f0; white-space: nowrap; }
  .col-table td { padding: 9px 12px; border-bottom: 1px solid #f1f5f9;
                   vertical-align: top; }
  .col-table tr:last-child td { border-bottom: none; }
  .col-table tr:hover td { background: #fafafe; }
  .classification { padding: 2px 8px; border-radius: 12px; font-size: 0.72rem;
                     font-weight: 600; white-space: nowrap; }
  .cls-Public     { background: #dcfce7; color: #16a34a; }
  .cls-Internal   { background: #dbeafe; color: #1d4ed8; }
  .cls-Confidential { background: #fef3c7; color: #d97706; }
  .cls-PII        { background: #fee2e2; color: #dc2626; }
  .cls-Sensitive  { background: #fce7f3; color: #db2777; }

  /* Options row */
  .options-row { display: flex; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }
  .checkbox-label { display: flex; align-items: center; gap: 6px; font-size: 0.85rem;
                     color: #475569; cursor: pointer; }
  .checkbox-label input[type=checkbox] { width: auto; }

  /* Spinner */
  .spinner { display: inline-block; width: 18px; height: 18px; border: 2px solid #fff;
             border-top-color: transparent; border-radius: 50%; animation: spin .7s linear infinite;
             vertical-align: middle; margin-right: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Empty / error state */
  .empty-state { text-align: center; padding: 48px 24px; color: #94a3b8; }
  .empty-state .icon { font-size: 2.5rem; margin-bottom: 12px; }
  .empty-state p { font-size: 0.9rem; }
  .error-box { background: #fee2e2; border: 1px solid #fca5a5; border-radius: 8px;
               padding: 12px 16px; color: #dc2626; font-size: 0.875rem; margin-top: 12px; }

  /* Export */
  .export-bar { display: flex; gap: 10px; padding: 14px 20px;
                border-top: 1px solid #e2e8f0; background: #f8fafc; }

  /* Divider */
  hr { border: none; border-top: 1px solid #e2e8f0; margin: 16px 0; }
  .section-label { font-size: 0.78rem; font-weight: 600; color: #94a3b8;
                   text-transform: uppercase; letter-spacing: .06em; margin-bottom: 10px; }
  .count-badge { background: #ede9fe; color: #6366f1; border-radius: 20px;
                  padding: 1px 9px; font-size: 0.75rem; font-weight: 600; }
  .col-sub-head { display: flex; align-items: center; justify-content: space-between;
                  padding: 8px 16px; background: #f8fafc; border-top: 1px solid #e2e8f0; }
  .col-sub-head span { font-size: 0.78rem; font-weight: 600; color: #64748b;
                        text-transform: uppercase; letter-spacing: .06em; }
  .no-columns { padding: 14px 16px; font-size: 0.82rem; color: #94a3b8; }
</style>
</head>
<body>

<div class="header">
  <div>
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#a5b4fc" stroke-width="2">
      <rect x="2" y="3" width="20" height="5" rx="1"/><rect x="2" y="10" width="20" height="5" rx="1"/>
      <rect x="2" y="17" width="20" height="5" rx="1"/>
    </svg>
  </div>
  <h1>Data Definition Agent</h1>
  <span class="badge">AI-Powered</span>
</div>

<div class="main">

  <!-- LEFT: Input panel -->
  <div class="panel">
    <div class="panel-header">
      <h2>Define Your Tables &amp; Columns</h2>
    </div>
    <div class="panel-body">

      <!-- Tabs -->
      <div class="tabs">
        <button class="tab active" onclick="switchTab('tables')">Tables</button>
        <button class="tab" onclick="switchTab('columns')">Columns</button>
      </div>

      <!-- TABLE TAB -->
      <div id="tab-tables">
        <div class="section-label">Add a Table</div>
        <div class="form-grid triple">
          <div class="field">
            <label>Schema *</label>
            <input id="t-schema" placeholder="e.g. sales">
          </div>
          <div class="field">
            <label>Table Name *</label>
            <input id="t-name" placeholder="e.g. customer_orders">
          </div>
          <div class="field">
            <label>Catalog</label>
            <input id="t-catalog" placeholder="e.g. my_catalog">
          </div>
        </div>
        <div class="form-grid" style="margin-top:10px">
          <div class="field">
            <label>Type</label>
            <select id="t-type">
              <option>TABLE</option>
              <option>VIEW</option>
              <option>MATERIALIZED VIEW</option>
              <option>EXTERNAL TABLE</option>
            </select>
          </div>
          <div class="field">
            <label>Notes (optional)</label>
            <input id="t-notes" placeholder="e.g. Sourced from SAP">
          </div>
        </div>
        <div class="form-grid full" style="margin-top:10px">
          <div class="field">
            <label>Existing Description (optional)</label>
            <textarea id="t-desc" rows="2" placeholder="Any existing description to improve upon…"></textarea>
          </div>
        </div>
        <button class="btn btn-primary btn-sm" style="margin-top:12px" onclick="addTable()">+ Add Table</button>

        <div class="entry-list" id="table-list"></div>
      </div>

      <!-- COLUMN TAB -->
      <div id="tab-columns" style="display:none">
        <div class="section-label">Add a Column</div>
        <div class="form-grid triple">
          <div class="field">
            <label>Schema *</label>
            <input id="c-schema" placeholder="e.g. sales">
          </div>
          <div class="field">
            <label>Table *</label>
            <input id="c-table" placeholder="e.g. customer_orders">
          </div>
          <div class="field">
            <label>Column Name *</label>
            <input id="c-col" placeholder="e.g. order_id">
          </div>
        </div>
        <div class="form-grid triple" style="margin-top:10px">
          <div class="field">
            <label>Data Type</label>
            <input id="c-dtype" placeholder="e.g. INTEGER">
          </div>
          <div class="field">
            <label>Nullable</label>
            <select id="c-nullable">
              <option value="">—</option>
              <option>YES</option>
              <option>NO</option>
            </select>
          </div>
          <div class="field">
            <label>Sample Values</label>
            <input id="c-samples" placeholder="e.g. 1001, 1002">
          </div>
        </div>
        <div class="form-grid full" style="margin-top:10px">
          <div class="field">
            <label>Existing Description (optional)</label>
            <textarea id="c-desc" rows="2" placeholder="Any existing description…"></textarea>
          </div>
        </div>
        <button class="btn btn-primary btn-sm" style="margin-top:12px" onclick="addColumn()">+ Add Column</button>

        <div class="entry-list" id="column-list"></div>
      </div>

      <hr>

      <!-- Options -->
      <div class="section-label">Options</div>
      <div class="options-row">
        <label class="checkbox-label">
          <input type="checkbox" id="opt-skip-cols"> Skip column definitions
        </label>
        <label class="checkbox-label">
          <input type="checkbox" id="opt-skip-review"> Skip quality review
        </label>
      </div>

      <button class="btn btn-primary btn-full" id="gen-btn" onclick="generate()">
        Generate Definitions
      </button>
      <div id="error-msg"></div>
    </div>
  </div>

  <!-- RIGHT: Results panel -->
  <div class="panel">
    <div class="panel-header">
      <h2>Generated Definitions</h2>
      <span class="count-badge" id="result-count">—</span>
    </div>
    <div id="results-body">
      <div class="empty-state">
        <div class="icon">🗂️</div>
        <p>Add tables and/or columns on the left,<br>then click <strong>Generate Definitions</strong>.</p>
      </div>
    </div>
    <div class="export-bar" id="export-bar" style="display:none">
      <button class="btn btn-ghost btn-sm" onclick="exportJSON()">Export JSON</button>
      <button class="btn btn-ghost btn-sm" onclick="exportText()">Export Text Report</button>
    </div>
  </div>

</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let tables = [];   // { schema, name, catalog, type, desc, notes, columns: [] }
let columns = [];  // standalone columns: { schema, table, col, dtype, nullable, desc, samples }
let lastResult = null;

// ── Tab switching ──────────────────────────────────────────────────────────
function switchTab(tab) {
  document.getElementById('tab-tables').style.display  = tab === 'tables'  ? '' : 'none';
  document.getElementById('tab-columns').style.display = tab === 'columns' ? '' : 'none';
  document.querySelectorAll('.tab').forEach((el, i) => {
    el.classList.toggle('active', (i === 0 && tab === 'tables') || (i === 1 && tab === 'columns'));
  });
}

// ── Add Table ──────────────────────────────────────────────────────────────
function addTable() {
  const schema = document.getElementById('t-schema').value.trim();
  const name   = document.getElementById('t-name').value.trim();
  if (!name) { alert('Table name is required.'); return; }

  tables.push({
    id: Date.now(),
    schema, name,
    catalog: document.getElementById('t-catalog').value.trim(),
    type:    document.getElementById('t-type').value,
    desc:    document.getElementById('t-desc').value.trim(),
    notes:   document.getElementById('t-notes').value.trim(),
    columns: [],
  });

  // clear
  ['t-schema','t-name','t-catalog','t-desc','t-notes'].forEach(id => {
    document.getElementById(id).value = '';
  });
  document.getElementById('t-type').value = 'TABLE';
  renderTableList();
}

function removeTable(id) {
  tables = tables.filter(t => t.id !== id);
  renderTableList();
}

function renderTableList() {
  const el = document.getElementById('table-list');
  if (!tables.length) { el.innerHTML = ''; return; }
  el.innerHTML = tables.map(t => `
    <div class="entry-card">
      <div class="entry-card-head" onclick="toggleCard(this)">
        <div>
          <span class="name">${t.schema ? t.schema + '.' : ''}${t.name}</span>
          <span class="meta">&nbsp;·&nbsp;${t.type}${t.columns.length ? ' · ' + t.columns.length + ' col(s)' : ''}</span>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <button class="btn btn-sm btn-ghost" style="padding:3px 8px;font-size:.72rem"
            onclick="event.stopPropagation();addColToTable(${t.id})">+ Col</button>
          <button class="btn btn-sm btn-danger" style="padding:3px 8px;font-size:.72rem"
            onclick="event.stopPropagation();removeTable(${t.id})">✕</button>
        </div>
      </div>
      <div class="entry-card-body" id="tcard-${t.id}">
        ${t.desc ? '<p style="font-size:.8rem;color:#64748b;margin-bottom:8px">'+escHtml(t.desc)+'</p>' : ''}
        ${t.notes ? '<p style="font-size:.78rem;color:#94a3b8;margin-bottom:8px">Notes: '+escHtml(t.notes)+'</p>' : ''}
        ${t.columns.length ? renderInlineColList(t) : '<p style="font-size:.8rem;color:#94a3b8">No columns yet. Click "+ Col" to add.</p>'}
      </div>
    </div>
  `).join('');
}

function renderInlineColList(t) {
  return '<table style="width:100%;font-size:.78rem;border-collapse:collapse">'
    + '<tr style="color:#94a3b8"><th style="text-align:left;padding:3px 6px">Column</th>'
    + '<th style="text-align:left;padding:3px 6px">Type</th>'
    + '<th style="text-align:left;padding:3px 6px">Nullable</th>'
    + '<th></th></tr>'
    + t.columns.map((c, i) => `
      <tr>
        <td style="padding:3px 6px">${escHtml(c.col)}</td>
        <td style="padding:3px 6px;color:#94a3b8">${escHtml(c.dtype)}</td>
        <td style="padding:3px 6px;color:#94a3b8">${c.nullable || '—'}</td>
        <td style="padding:3px 6px">
          <button class="btn btn-danger" style="padding:2px 7px;font-size:.7rem"
            onclick="removeColFromTable(${t.id},${i})">✕</button>
        </td>
      </tr>`).join('')
    + '</table>';
}

function addColToTable(tableId) {
  const t = tables.find(t => t.id === tableId);
  if (!t) return;
  const col = prompt('Column name:');
  if (!col) return;
  const dtype = prompt('Data type (e.g. INTEGER):', '') || '';
  const nullable = prompt('Nullable? (YES/NO):', '') || '';
  const samples = prompt('Sample values (comma-separated):', '') || '';
  t.columns.push({ col: col.trim(), dtype: dtype.trim(), nullable: nullable.trim(), desc: '', samples: samples.trim() });
  renderTableList();
}

function removeColFromTable(tableId, idx) {
  const t = tables.find(t => t.id === tableId);
  if (t) { t.columns.splice(idx, 1); renderTableList(); }
}

// ── Add Standalone Column ──────────────────────────────────────────────────
function addColumn() {
  const col = document.getElementById('c-col').value.trim();
  if (!col) { alert('Column name is required.'); return; }

  columns.push({
    id: Date.now(),
    schema:   document.getElementById('c-schema').value.trim(),
    table:    document.getElementById('c-table').value.trim(),
    col,
    dtype:    document.getElementById('c-dtype').value.trim(),
    nullable: document.getElementById('c-nullable').value,
    desc:     document.getElementById('c-desc').value.trim(),
    samples:  document.getElementById('c-samples').value.trim(),
  });

  ['c-schema','c-table','c-col','c-dtype','c-desc','c-samples'].forEach(id => {
    document.getElementById(id).value = '';
  });
  document.getElementById('c-nullable').value = '';
  renderColumnList();
}

function removeColumn(id) {
  columns = columns.filter(c => c.id !== id);
  renderColumnList();
}

function renderColumnList() {
  const el = document.getElementById('column-list');
  if (!columns.length) { el.innerHTML = ''; return; }
  el.innerHTML = columns.map(c => `
    <div class="entry-card">
      <div class="entry-card-head" onclick="toggleCard(this)">
        <div>
          <span class="name">${c.schema ? c.schema+'.' : ''}${c.table ? c.table+'.' : ''}${c.col}</span>
          <span class="meta">&nbsp;·&nbsp;${c.dtype || '—'}</span>
        </div>
        <button class="btn btn-sm btn-danger" style="padding:3px 8px;font-size:.72rem"
          onclick="event.stopPropagation();removeColumn(${c.id})">✕</button>
      </div>
      <div class="entry-card-body" id="ccard-${c.id}">
        <p style="font-size:.8rem;color:#64748b">
          Nullable: ${c.nullable || '—'} &nbsp;|&nbsp;
          Samples: ${c.samples || '—'}
        </p>
        ${c.desc ? '<p style="font-size:.78rem;color:#94a3b8;margin-top:4px">'+escHtml(c.desc)+'</p>' : ''}
      </div>
    </div>
  `).join('');
}

function toggleCard(head) {
  const body = head.nextElementSibling;
  body.classList.toggle('open');
}

// ── Generate ───────────────────────────────────────────────────────────────
async function generate() {
  if (!tables.length && !columns.length) {
    showError('Add at least one table or column before generating.');
    return;
  }
  clearError();

  const btn = document.getElementById('gen-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Generating…';

  const payload = {
    tables: tables.map(t => ({
      table_catalog: t.catalog,
      table_schema:  t.schema,
      table_name:    t.name,
      table_type:    t.type,
      existing_description: t.desc,
      notes: t.notes,
      columns: t.columns.map(c => ({
        table_schema: t.schema,
        table_name:   t.name,
        column_name:  c.col,
        data_type:    c.dtype,
        is_nullable:  c.nullable,
        existing_description: c.desc,
        sample_values: c.samples,
      })),
    })),
    columns: columns.map(c => ({
      table_schema: c.schema,
      table_name:   c.table,
      column_name:  c.col,
      data_type:    c.dtype,
      is_nullable:  c.nullable,
      existing_description: c.desc,
      sample_values: c.samples,
    })),
    skip_columns: document.getElementById('opt-skip-cols').checked,
    skip_review:  document.getElementById('opt-skip-review').checked,
  };

  try {
    const res = await fetch('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Server error');
    }
    const data = await res.json();
    lastResult = data;
    renderResults(data);
  } catch(e) {
    showError('Error: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'Generate Definitions';
  }
}

// ── Render Results ─────────────────────────────────────────────────────────
function renderResults(data) {
  document.getElementById('result-count').textContent =
    data.total_tables + ' table' + (data.total_tables !== 1 ? 's' : '')
    + ', ' + data.total_columns + ' col' + (data.total_columns !== 1 ? 's' : '');

  const html = data.tables.map(t => `
    <div class="result-table-card">
      <div class="result-table-head">
        <span class="domain-badge">${escHtml(t.data_domain || '—')}</span>
        <div>
          <div class="table-title">${escHtml(t.business_name || t.table_schema + '.' + t.table_name)}</div>
          <div class="table-sub">${escHtml(t.table_schema)}.${escHtml(t.table_name)} &nbsp;·&nbsp; ${escHtml(t.table_type)}</div>
        </div>
      </div>
      <div class="result-table-def">${escHtml(t.business_definition || '—')}</div>
      <div class="result-meta">
        <span><strong>Use case:</strong> ${escHtml(t.primary_use_case || '—')}</span>
        ${t.table_catalog ? '<span><strong>Catalog:</strong> ' + escHtml(t.table_catalog) + '</span>' : ''}
      </div>
      ${t.columns && t.columns.length ? renderColTable(t.columns) : '<div class="no-columns">No columns.</div>'}
    </div>
  `).join('');

  document.getElementById('results-body').innerHTML =
    '<div style="padding:20px">' + html + '</div>';
  document.getElementById('export-bar').style.display = 'flex';
}

function renderColTable(cols) {
  const rows = cols.map(c => `
    <tr>
      <td><strong>${escHtml(c.column_name)}</strong>
          ${c.business_term ? '<br><span style="font-size:.72rem;color:#6366f1">'+escHtml(c.business_term)+'</span>' : ''}</td>
      <td style="color:#64748b">${escHtml(c.data_type || '—')}</td>
      <td style="color:#64748b">${c.is_nullable || '—'}</td>
      <td>${classificationBadge(c.data_classification)}</td>
      <td>${escHtml(c.business_definition || '—')}</td>
    </tr>
  `).join('');

  return `
    <div class="col-sub-head">
      <span>Columns (${cols.length})</span>
    </div>
    <div style="overflow-x:auto">
      <table class="col-table">
        <thead>
          <tr>
            <th>Column / Business Term</th>
            <th>Type</th>
            <th>Nullable</th>
            <th>Classification</th>
            <th>Business Definition</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function classificationBadge(cls) {
  if (!cls) return '<span style="color:#94a3b8">—</span>';
  const safe = cls.replace(/[^a-zA-Z]/g, '');
  return `<span class="classification cls-${safe}">${escHtml(cls)}</span>`;
}

// ── Export ─────────────────────────────────────────────────────────────────
function exportJSON() {
  if (!lastResult) return;
  download('definitions.json', JSON.stringify(lastResult, null, 2), 'application/json');
}

function exportText() {
  if (!lastResult) return;
  const lines = [
    'DATA DEFINITION REPORT',
    'Generated : ' + new Date().toLocaleString(),
    'Tables    : ' + lastResult.total_tables,
    'Columns   : ' + lastResult.total_columns,
    '',
  ];
  const sep80 = '='.repeat(80);
  const sep40 = '-'.repeat(40);
  for (const t of lastResult.tables) {
    lines.push(sep80);
    lines.push('TABLE: ' + t.table_schema + '.' + t.table_name);
    lines.push(sep80);
    lines.push('  business_name       : ' + (t.business_name || ''));
    lines.push('  data_domain         : ' + (t.data_domain || ''));
    lines.push('  primary_use_case    : ' + (t.primary_use_case || ''));
    lines.push('  business_definition : ' + (t.business_definition || ''));
    lines.push('  existing_description: ' + (t.existing_description || ''));
    if (t.columns && t.columns.length) {
      lines.push('');
      lines.push('  COLUMNS (' + t.columns.length + '):');
      for (const c of t.columns) {
        lines.push('  ' + sep40);
        lines.push('  COLUMN: ' + c.column_name);
        lines.push('    data_type           : ' + (c.data_type || ''));
        lines.push('    nullable            : ' + (c.is_nullable || ''));
        lines.push('    business_term       : ' + (c.business_term || ''));
        lines.push('    data_classification : ' + (c.data_classification || ''));
        lines.push('    business_definition : ' + (c.business_definition || ''));
        lines.push('    sample_values       : ' + (c.sample_values || ''));
      }
      lines.push('  ' + sep40);
    }
    lines.push('');
  }
  download('definitions.txt', lines.join('\\n'), 'text/plain');
}

function download(filename, content, mime) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([content], { type: mime }));
  a.download = filename;
  a.click();
}

// ── Helpers ────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s || '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}
function showError(msg) {
  document.getElementById('error-msg').innerHTML = '<div class="error-box">' + escHtml(msg) + '</div>';
}
function clearError() {
  document.getElementById('error-msg').innerHTML = '';
}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


if __name__ == "__main__":
    uvicorn.run("data_definition_webapp:app", host="0.0.0.0", port=8001, reload=True)
