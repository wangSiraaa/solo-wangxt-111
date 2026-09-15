import { Component, OnInit } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  ApiService, Material, TraceBody,
} from '../api.service';

const OXIDES: Array<{ key: string; label: string; color: string }> = [
  { key: 'cao', label: 'CaO', color: '#4da3ff' },
  { key: 'sio2', label: 'SiO₂', color: '#f0b429' },
  { key: 'al2o3', label: 'Al₂O₃', color: '#38c172' },
  { key: 'fe2o3', label: 'Fe₂O₃', color: '#ef5350' },
  { key: 'mgo', label: 'MgO', color: '#26c6da' },
  { key: 'so3', label: 'SO₃', color: '#ff8a65' },
  { key: 'k2o', label: 'K₂O', color: '#b39ddb' },
  { key: 'na2o', label: 'Na₂O', color: '#f48fb1' },
  { key: 'cl', label: 'Cl⁻', color: '#aed581' },
  { key: 'loi', label: 'LOI', color: '#78909c' },
];

interface Row {
  code: string; name: string; category: string;
  pct: number; moistureOverride: number | null; extraCost: number;
  baseMoisture: number | null;
}

@Component({
  selector: 'app-manual',
  standalone: true,
  imports: [DecimalPipe, FormsModule],
  templateUrl: './manual.component.html',
  styleUrl: './manual.component.scss',
})
export class ManualComponent implements OnInit {
  materials: Material[] = [];
  rows: Row[] = [];
  denomFloor = 0.05;
  trace: TraceBody | null = null;
  provenance: unknown = null;
  error = '';
  oxideRows = OXIDES;
  private timer: any = null;

  constructor(private api: ApiService) {}

  ngOnInit() {
    this.api.materials().subscribe(ms => {
      this.materials = ms;
      // 默认载入 S1 的六个常规原料，初始即可行的干基配比
      const defaultCodes = ['LS_H', 'LS_L', 'SST', 'SH', 'FA', 'FE'];
      const initMix: Record<string, number> = {
        LS_H: 62, LS_L: 10, SST: 13, SH: 6, FA: 6, FE: 3,
      };
      this.rows = ms.filter(m => defaultCodes.includes(m.code)).map(m => ({
        code: m.code, name: m.name, category: m.category,
        pct: initMix[m.code] ?? 0,
        moistureOverride: null, extraCost: 0, baseMoisture: null,
      }));
      // 拉取含水率用于显示
      for (const r of this.rows) {
        this.api.assays(r.code).subscribe(as => {
          const a = as.find(x => x.id === this.materials.find(m => m.code === r.code)?.active_assay_id)
                    ?? as[0];
          r.baseMoisture = a?.moisture_pct ?? null;
        });
      }
      this.calc();
    });
  }

  addMaterial(code: string, after?: () => void) {
    if (!code || this.rows.some(r => r.code === code)) { after?.(); return; }
    const m = this.materials.find(x => x.code === code)!;
    const row: Row = { code: m.code, name: m.name, category: m.category,
                       pct: 0, moistureOverride: null, extraCost: 0, baseMoisture: null };
    this.rows.push(row);
    this.api.assays(m.code).subscribe(as => {
      row.baseMoisture = as.find(a => a.id === m.active_assay_id)?.moisture_pct ?? as[0]?.moisture_pct ?? null;
      after?.();
    });
  }

  remove(code: string) {
    this.rows = this.rows.filter(r => r.code !== code);
    this.calc();
  }

  get sumPct() { return this.rows.reduce((a, r) => a + r.pct, 0); }
  get sumBad() { return Math.abs(this.sumPct - 100) > 0.01; }
  get addableMaterials(): Material[] {
    return this.materials.filter(x => !this.rows.some(r => r.code === x.code));
  }
  get activeMaterialRows() {
    return (this.trace?.materials ?? []).filter(x => x.fraction_dry_pct > 0.001);
  }

  ignVal(key: string): string {
    const v = this.trace?.composition_ignited_pct[key];
    return v === undefined || v === null ? '—' : v.toFixed(3);
  }

  onInput() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.calc(), 250);
  }

  calc() {
    const items = this.rows
      .filter(r => r.pct > 0)
      .map(r => ({
        code: r.code, pct: r.pct,
        moisture_override: r.moistureOverride,
        extra_cost_wet_t: r.extraCost || undefined,
      }));
    if (!items.length) { this.trace = null; this.error = ''; return; }
    this.api.manualBlend(items, this.denomFloor).subscribe({
      next: resp => { this.trace = resp.trace; this.provenance = resp.provenance; this.error = ''; },
      error: e => { this.trace = null; this.error = ApiService.errText(e); },
    });
  }

  /** 一键演示：92% 石英砂 → 零分母错误；雨季含水率覆盖。 */
  demo(kind: 'zero' | 'rain') {
    if (kind === 'zero') {
      const apply = () => {
        const codes = new Set(['QZ', 'LS_H']);
        this.rows.forEach(r => {
          r.pct = r.code === 'QZ' ? 92 : r.code === 'LS_H' ? 8 : 0;
          r.moistureOverride = null;
        });
        this.rows = this.rows.filter(r => codes.has(r.code) || r.pct > 0);
        this.calc();
      };
      this.ensure('QZ', apply);
    } else {
      const needed = ['LS_H', 'LS_L', 'SST', 'SH', 'FA', 'FE'];
      let pending = needed.filter(c => !this.rows.some(r => r.code === c)).length;
      const apply = () => {
        if (pending > 0) return;
        const mix: Record<string, number> = { LS_H: 60, LS_L: 12, SST: 12, SH: 6, FA: 7, FE: 3 };
        const rain: Record<string, number> = { LS_L: 7, SST: 9, SH: 14, FA: 26, FE: 19 };
        this.rows.forEach(r => {
          if (mix[r.code] !== undefined) {
            r.pct = mix[r.code];
            r.moistureOverride = rain[r.code] ?? null;
          } else {
            r.pct = 0;
            r.moistureOverride = null;
          }
        });
        this.calc();
      };
      for (const c of needed) {
        this.ensure(c, () => { pending--; apply(); });
      }
    }
  }

  private ensure(code: string, after?: () => void) {
    if (!this.rows.some(r => r.code === code)) this.addMaterial(code, after);
    else after?.();
  }

  /** 某氧化物按原料贡献的堆叠来源段（占该氧化物总量的比例）。 */
  sourceStack(key: string) {
    if (!this.trace) return [];
    return this.trace.materials
      .map((m, i) => ({
        code: m.material, name: m.name,
        value: m.oxide_contribution_pct[key] || 0,
        share: this.sourceShare(key, i),
        color: this.palette[i % this.palette.length],
      }))
      .filter(s => s.value > 1e-6);
  }

  sourceShare(key: string, i: number): number {
    const total = this.trace!.composition_dry_pct[key] || 0;
    const v = this.trace!.materials[i].oxide_contribution_pct[key] || 0;
    return total ? 100 * v / total : 0;
  }

  palette = ['#4da3ff', '#f0b429', '#38c172', '#ef5350', '#26c6da',
             '#b39ddb', '#ff8a65', '#f48fb1', '#aed581'];
}
