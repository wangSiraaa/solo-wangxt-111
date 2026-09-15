import { Component, EventEmitter, Input, OnInit, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  ApiService, Assay, Material, Scenario, ScenarioInput,
} from '../api.service';

interface MatRow {
  code: string; name: string; category: string;
  selected: boolean; min: number; max: number | null; cheap: boolean;
  activeAssay: Assay | null; activeAssayId: number | null;
  availabilityMax: number; // -1 不限
  assayCount: number;
}

@Component({
  selector: 'app-scenario-editor',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './scenario-editor.component.html',
  styleUrl: './scenario-editor.component.scss',
})
export class ScenarioEditorComponent implements OnInit {
  @Input() editing: Scenario | null = null;
  @Output() saved = new EventEmitter<Scenario>();
  @Output() cancelled = new EventEmitter<void>();

  name = '';
  description = '';
  khMin = 0.86; khMax = 0.96;
  smMin = 2.3; smMax = 2.8;
  imMin = 1.2; imMax = 1.8;
  mgoMax = 5.0; so3Max = 1.5; alkaliMax = 1.0; clMax = 0.03;
  denomFloor = 0.05;
  rows: MatRow[] = [];
  rainRows: Array<{ code: string; name: string; override: number | null; extra: number }> = [];
  saving = false;
  serverErrors: Record<string, string> = {};
  formErrors: string[] = [];

  constructor(private api: ApiService) {}

  ngOnInit() {
    this.api.materials().subscribe(ms => {
      const assaysByCode = new Map<string, Assay[]>();
      let pending = ms.length;
      ms.forEach(m => this.api.assays(m.code).subscribe(as => {
        assaysByCode.set(m.code, as);
        pending--;
        if (pending === 0) this.fillRows(ms, assaysByCode);
      }));
    });
    if (this.editing) this.loadFrom(this.editing);
  }

  private fillRows(ms: Material[], assaysByCode: Map<string, Assay[]>) {
    this.rows = ms.map(m => {
      const as = assaysByCode.get(m.code) ?? [];
      return {
        code: m.code, name: m.name, category: m.category,
        selected: false, min: 0, max: null, cheap: false,
        activeAssay: as.find(a => a.id === m.active_assay_id) ?? null,
        activeAssayId: m.active_assay_id,
        availabilityMax: -1, assayCount: as.length,
      };
    });
    // 可用量
    this.rows.forEach(r => this.api.availability(r.code).subscribe(av => {
      r.availabilityMax = av?.max_fraction_pct ?? -1;
    }));
    // 雨季行（全部原料可选）
    this.rainRows = this.rows.map(r => ({ code: r.code, name: r.name, override: null, extra: 0 }));
    if (this.editing) this.applyEditingToRows();
  }

  private loadFrom(s: Scenario) {
    this.name = s.name;
    this.description = s.description;
    [this.khMin, this.khMax] = s.targets.kh;
    [this.smMin, this.smMax] = s.targets.sm;
    [this.imMin, this.imMax] = s.targets.im;
    this.mgoMax = s.hazards.mgo_max; this.so3Max = s.hazards.so3_max;
    this.alkaliMax = s.hazards.alkali_eq_max; this.clMax = s.hazards.cl_max;
    this.denomFloor = s.denom_floor;
    if (this.rows.length) this.applyEditingToRows();
  }

  private applyEditingToRows() {
    const s = this.editing!;
    for (const r of this.rows) {
      const it = s.materials.find(m => m.code === r.code);
      if (it) {
        r.selected = true; r.min = it.min_pct;
        r.max = it.max_pct; r.cheap = it.preferred_cheap;
      }
    }
    for (const rr of this.rainRows) {
      rr.override = s.rain_overrides[rr.code] ?? null;
      rr.extra = s.rain_extra_cost[rr.code] ?? 0;
    }
  }

  get minSum(): number {
    return this.rows.filter(r => r.selected).reduce((a, r) => a + (Number(r.min) || 0), 0);
  }

  get otherServerErrors(): Array<[string, string]> {
    const known = new Set([
      'name', 'kh_min', 'sm_min', 'im_min', 'denom_floor', 'mgo_max', 'so3_max',
      'alkali_eq_max', 'cl_max', 'materials', 'materials.min_sum', '_',
    ]);
    return Object.entries(this.serverErrors).filter(([k]) =>
      !known.has(k) && !k.startsWith('rain_overrides.')
      && !k.startsWith('rain_extra_cost.') && !k.startsWith('materials['));
  }

  get selectedCount(): number { return this.rows.filter(r => r.selected).length; }

  get selectedRows(): MatRow[] { return this.rows.filter(r => r.selected); }

  isSelected(code: string): boolean {
    return this.rows.find(r => r.code === code)?.selected ?? false;
  }

  matError(code: string): string[] {
    return Object.entries(this.serverErrors)
      .filter(([k]) => k.startsWith(`materials[${code}]`))
      .map(([, v]) => v);
  }

  get rainErrors(): Array<[string, string]> {
    return Object.entries(this.serverErrors)
      .filter(([k]) => k.startsWith('rain_overrides.') || k.startsWith('rain_extra_cost.'));
  }

  availText(r: MatRow): string {
    return r.availabilityMax >= 0 ? `可供上限 ${r.availabilityMax}%` : '可供量不限';
  }

  effectiveMax(r: MatRow): number {
    const scene = r.max === null ? 100 : r.max;
    return r.availabilityMax >= 0 ? Math.min(scene, r.availabilityMax) : scene;
  }

  validateClient(): boolean {
    const errs: string[] = [];
    if (!this.name.trim()) errs.push('名称必填');
    if (!(this.khMin < this.khMax)) errs.push('KH 下限必须小于上限');
    if (!(this.smMin < this.smMax)) errs.push('SM 下限必须小于上限');
    if (!(this.imMin < this.imMax)) errs.push('IM 下限必须小于上限');
    for (const [lo, hi, lbl] of [
      [this.khMin, this.khMax, 'KH'], [this.smMin, this.smMax, 'SM'],
      [this.imMin, this.imMax, 'IM']] as const) {
      if (lo <= 0 || hi <= 0) errs.push(`${lbl} 必须为正数`);
    }
    if (!(this.denomFloor > 0 && this.denomFloor <= 1)) errs.push('分母地板须在 (0,1]% 内');
    for (const [v, lbl] of [[this.mgoMax, 'MgO'], [this.so3Max, 'SO₃'],
                            [this.alkaliMax, '碱当量'], [this.clMax, 'Cl⁻']] as const) {
      if (!(v > 0)) errs.push(`${lbl} 上限必须为正`);
    }
    if (this.selectedCount < 2) errs.push('至少选择两种参与原料');
    if (this.minSum > 100 + 1e-9) errs.push(`最低掺量之和 ${this.minSum.toFixed(1)}% 超过 100%`);
    // 化验/成本是否存在等引用完整性由服务端判定，错误按 .assay/.cost 分列回显，
    // 避免客户端用单一提示覆盖两类原因
    for (const r of this.rows.filter(x => x.selected)) {
      if (r.min < 0 || r.min > 100) errs.push(`${r.code} 最低掺量非法`);
      if (r.max !== null && (r.max < 0 || r.max > 100 || r.min > r.max)) {
        errs.push(`${r.code} 场景上限非法或低于最低掺量`);
      }
      if (r.availabilityMax >= 0 && r.min > r.availabilityMax + 1e-9) {
        errs.push(`${r.code} 最低掺量高于可供上限 ${r.availabilityMax}%`);
      }
    }
    for (const rr of this.rainRows.filter(x => this.isSelected(x.code))) {
      if (rr.override !== null && !(rr.override >= 0 && rr.override < 100)) {
        errs.push(`${rr.code} 雨季含水率须在 [0,100)% 内`);
      }
      if (rr.extra < 0) errs.push(`${rr.code} 附加成本不能为负`);
    }
    this.formErrors = errs;
    return errs.length === 0;
  }

  save() {
    this.serverErrors = {};
    if (!this.validateClient()) return;
    const selectedCodes = new Set(this.rows.filter(r => r.selected).map(r => r.code));
    const body: ScenarioInput = {
      name: this.name.trim(),
      description: this.description,
      kh_min: this.khMin, kh_max: this.khMax,
      sm_min: this.smMin, sm_max: this.smMax,
      im_min: this.imMin, im_max: this.imMax,
      mgo_max: this.mgoMax, so3_max: this.so3Max,
      alkali_eq_max: this.alkaliMax, cl_max: this.clMax,
      denom_floor: this.denomFloor,
      // 只提交已选原料的雨季配置，杜绝未知编码
      rain_overrides: Object.fromEntries(
        this.rainRows.filter(r => r.override !== null && selectedCodes.has(r.code))
          .map(r => [r.code, r.override!])),
      rain_extra_cost: Object.fromEntries(
        this.rainRows.filter(r => r.extra > 0 && selectedCodes.has(r.code))
          .map(r => [r.code, r.extra])),
      materials: this.rows.filter(r => r.selected).map(r => ({
        material_code: r.code, min_pct: r.min,
        max_pct: r.max, preferred_cheap: r.cheap,
      })),
    };
    this.saving = true;
    const req = this.editing
      ? this.api.updateScenario(this.editing.id, body)
      : this.api.createScenario(body);
    req.subscribe({
      next: s => { this.saving = false; this.saved.emit(s); },
      error: e => {
        this.saving = false;
        this.serverErrors = e.error?.detail?.fields ?? { _: ApiService.errText(e) };
      },
    });
  }

  cancel() { this.cancelled.emit(); }
}
