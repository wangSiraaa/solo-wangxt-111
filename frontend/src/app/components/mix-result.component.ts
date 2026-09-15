import { Component, Input } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import {
  Scenario, SolutionDto, TraceBody, MaterialTraceRow,
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

interface VersionMeta {
  material: string; name: string; assay_version: string; assay_id: number;
  lab_note: string; cost_id: number; price_wet_t: number;
  min_pct: number; max_pct: number; availability_note: string;
  composition_dry_pct: Record<string, number | null>;
}

@Component({
  selector: 'app-mix-result',
  standalone: true,
  imports: [DecimalPipe],
  templateUrl: './mix-result.component.html',
  styleUrl: './mix-result.component.scss',
})
export class MixResultComponent {
  @Input() sol!: SolutionDto;
  @Input() scenario: Scenario | null = null;
  oxides = OXIDES;

  get t(): TraceBody | null { return this.sol.trace.trace ?? null; }
  get prov() { return this.sol.trace.provenance ?? null; }
  get ind() { return this.t?.indicators; }
  get materialsMeta(): Record<string, VersionMeta> {
    return (this.prov?.assay_versions ?? {}) as Record<string, VersionMeta>;
  }
  get materialRows(): VersionMeta[] { return Object.values(this.materialsMeta); }
  get basisEntries(): string[] { return Object.values(this.prov?.basis ?? {}); }

  get mixEntries(): Array<{ code: string; pct: number; name: string; cheap: boolean }> {
    const mix = this.sol.mix || {};
    return Object.entries(mix)
      .map(([code, pct]) => ({
        code, pct: pct as number,
        name: this.materialsMeta[code]?.name ?? code,
        cheap: !!this.scenario?.materials.find(m => m.code === code)?.preferred_cheap,
      }))
      .sort((a, b) => b.pct - a.pct);
  }

  stackedSegmentsFor(code: string) {
    const row = this.t?.materials.find(r => r.material === code);
    if (!row) return [];
    return OXIDES.map(o => ({
      ...o,
      pctOfBlend: row.oxide_contribution_pct[o.key] || 0,
    })).filter(s => s.pctOfBlend > 0.005);
  }

  sourceShare(row: MaterialTraceRow, key: string): number {
    const total = this.t!.composition_dry_pct[key] || 0;
    return total ? 100 * (row.oxide_contribution_pct[key] || 0) / total : 0;
  }

  coefEntries(coef: Record<string, number>): Array<[string, number]> {
    return Object.entries(coef).filter(([, v]) => Math.abs(v) > 1e-9);
  }

  ignVal(key: string): string {
    const v = this.t?.composition_ignited_pct[key];
    return v === undefined || v === null ? '—' : v.toFixed(3);
  }

  inRange(v: number | undefined | null, lo: number, hi: number) {
    return v !== undefined && v !== null && v >= lo - 1e-9 && v <= hi + 1e-9;
  }

  isCheap(code: string): boolean {
    return !!this.scenario?.materials.find(m => m.code === code)?.preferred_cheap;
  }

  assayVersion(code: string): string {
    return this.materialsMeta[code]?.assay_version ?? '?';
  }
}
