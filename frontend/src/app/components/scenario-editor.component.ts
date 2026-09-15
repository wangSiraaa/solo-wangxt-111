import { Component, EventEmitter, Input, OnInit, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  ApiService, Assay, Material, Scenario, ScenarioInput, RevisionDto,
} from '../api.service';

interface MatRow {
  code: string; name: string; category: string;
  selected: boolean; min: number; max: number | null; cheap: boolean;
  activeAssay: Assay | null; activeAssayId: number | null;
  activeCostId: number | null;
  availabilityMax: number; // -1 不限
  assayCount: number;
}

export type EditorMode = 'create' | 'draft';

@Component({
  selector: 'app-scenario-editor',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './scenario-editor.component.html',
  styleUrl: './scenario-editor.component.scss',
})
export class ScenarioEditorComponent implements OnInit {
  /** create=建单即发布 rev1；draft=编辑既有自定义场景的草稿 */
  @Input() mode: EditorMode = 'create';
  @Input() scenario: Scenario | null = null;
  /** draft 模式：从某个修订（旧发布版/当前草稿）复制内容 */
  @Input() sourceRevision: RevisionDto | null = null;
  @Output() saved = new EventEmitter<{ scenarioId: number; revision: RevisionDto | null }>();
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
  publishing = false;
  serverErrors: Record<string, string> = {};
  formErrors: string[] = [];
  conflictError = '';
  savedDraft: RevisionDto | null = null;

  constructor(private api: ApiService) {}

  get title(): string {
    if (this.mode === 'create') return '＋ 新建自定义场景（保存即发布 rev1）';
    if (this.sourceRevision && this.sourceRevision.status === 'draft') {
      const bn = this.sourceRevision.branch_name;
      return bn ? `编辑分支草稿 r${this.sourceRevision.revision_no} · ${bn}`
                : `编辑草稿 r${this.sourceRevision.revision_no}`;
    }
    const src = this.sourceRevision ? `（复制自 r${this.sourceRevision.revision_no}）` : '';
    return `为「${this.scenario?.name ?? ''}」创建修订草稿 ${src}`;
  }

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
  }

  private fillRows(ms: Material[], assaysByCode: Map<string, Assay[]>) {
    this.rows = ms.map(m => {
      const as = assaysByCode.get(m.code) ?? [];
      return {
        code: m.code, name: m.name, category: m.category,
        selected: false, min: 0, max: null, cheap: false,
        activeAssay: as.find(a => a.id === m.active_assay_id) ?? null,
        activeAssayId: m.active_assay_id,
        activeCostId: m.active_cost_id,
        availabilityMax: -1, assayCount: as.length,
      };
    });
    this.rows.forEach(r => this.api.availability(r.code).subscribe(av => {
      r.availabilityMax = av?.max_fraction_pct ?? -1;
    }));
    this.rainRows = this.rows.map(r => ({ code: r.code, name: r.name, override: null, extra: 0 }));
    this.loadInitial();
  }

  private loadInitial() {
    // draft 模式优先用传入修订 payload；create 模式为空表单
    const p = this.mode === 'draft' && this.sourceRevision
      ? this.sourceRevision.payload
      : null;
    if (!p) return;
    this.name = p.name;
    this.description = p.description;
    [this.khMin, this.khMax] = [p.kh_min, p.kh_max];
    [this.smMin, this.smMax] = [p.sm_min, p.sm_max];
    [this.imMin, this.imMax] = [p.im_min, p.im_max];
    this.mgoMax = p.mgo_max; this.so3Max = p.so3_max;
    this.alkaliMax = p.alkali_eq_max; this.clMax = p.cl_max;
    this.denomFloor = p.denom_floor;
    // 修订 payload 与建单入参的材料键名兼容（code / material_code）
    for (const mat of p.materials) {
      const code = (mat as any).code ?? mat.material_code;
      const row = this.rows.find(r => r.code === code);
      if (row) {
        row.selected = true; row.min = mat.min_pct;
        row.max = mat.max_pct; row.cheap = mat.preferred_cheap;
        // 草稿沿用修订钉住的化验/成本（可能已非当前生效版本）
        if (mat.assay_id) row.activeAssayId = mat.assay_id;
        if (mat.cost_id) row.activeCostId = mat.cost_id;
      }
    }
    for (const rr of this.rainRows) {
      rr.override = p.rain_overrides[rr.code] ?? null;
      rr.extra = p.rain_extra_cost[rr.code] ?? 0;
    }
  }

  get minSum(): number {
    return this.rows.filter(r => r.selected).reduce((a, r) => a + (Number(r.min) || 0), 0);
  }
  get selectedCount(): number { return this.rows.filter(r => r.selected).length; }
  get selectedRows(): MatRow[] { return this.rows.filter(r => r.selected); }
  get lockVersion(): number | null {
    return this.savedDraft?.lock_version ??
      (this.sourceRevision?.status === 'draft' ? this.sourceRevision.lock_version : null);
  }

  isSelected(code: string): boolean {
    return this.rows.find(r => r.code === code)?.selected ?? false;
  }
  availText(r: MatRow): string {
    return r.availabilityMax >= 0 ? `可供上限 ${r.availabilityMax}%` : '可供量不限';
  }
  effectiveMax(r: MatRow): number {
    const scene = r.max === null ? 100 : r.max;
    return r.availabilityMax >= 0 ? Math.min(scene, r.availabilityMax) : scene;
  }
  matError(code: string): string[] {
    return Object.entries(this.serverErrors)
      .filter(([k]) => k.startsWith(`materials[${code}]`)).map(([, v]) => v);
  }
  get rainErrors(): Array<[string, string]> {
    return Object.entries(this.serverErrors)
      .filter(([k]) => k.startsWith('rain_overrides.') || k.startsWith('rain_extra_cost.'));
  }
  get otherServerErrors(): Array<[string, string]> {
    const known = new Set(['name', 'kh_min', 'sm_min', 'im_min', 'denom_floor',
      'mgo_max', 'so3_max', 'alkali_eq_max', 'cl_max', 'materials',
      'materials.min_sum', '_']);
    return Object.entries(this.serverErrors).filter(([k]) =>
      !known.has(k) && !k.startsWith('rain_overrides.')
      && !k.startsWith('rain_extra_cost.') && !k.startsWith('materials['));
  }

  validateClient(): boolean {
    const errs: string[] = [];
    if (!this.name.trim()) errs.push('名称必填');
    if (!(this.khMin < this.khMax)) errs.push('KH 下限必须小于上限');
    if (!(this.smMin < this.smMax)) errs.push('SM 下限必须小于上限');
    if (!(this.imMin < this.imMax)) errs.push('IM 下限必须小于上限');
    if (!(this.denomFloor > 0 && this.denomFloor <= 1)) errs.push('分母地板须在 (0,1]% 内');
    for (const [v, lbl] of [[this.mgoMax, 'MgO'], [this.so3Max, 'SO₃'],
                            [this.alkaliMax, '碱当量'], [this.clMax, 'Cl⁻']] as const) {
      if (!(v > 0)) errs.push(`${lbl} 上限必须为正`);
    }
    if (this.selectedCount < 2) errs.push('至少选择两种参与原料');
    if (this.minSum > 100 + 1e-9) errs.push(`最低掺量之和 ${this.minSum.toFixed(1)}% 超过 100%`);
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

  private buildBody(): ScenarioInput & { lock_version?: number | null;
                                        source_revision_no?: number | null;
                                        revision_no?: number | null } {
    const selectedCodes = new Set(this.rows.filter(r => r.selected).map(r => r.code));
    const src = this.sourceRevision;
    return {
      name: this.name.trim(), description: this.description,
      kh_min: this.khMin, kh_max: this.khMax, sm_min: this.smMin, sm_max: this.smMax,
      im_min: this.imMin, im_max: this.imMax,
      mgo_max: this.mgoMax, so3_max: this.so3Max,
      alkali_eq_max: this.alkaliMax, cl_max: this.clMax,
      denom_floor: this.denomFloor,
      rain_overrides: Object.fromEntries(
        this.rainRows.filter(r => r.override !== null && selectedCodes.has(r.code))
          .map(r => [r.code, r.override!])),
      rain_extra_cost: Object.fromEntries(
        this.rainRows.filter(r => r.extra > 0 && selectedCodes.has(r.code))
          .map(r => [r.code, r.extra])),
      materials: this.rows.filter(r => r.selected).map(r => ({
        material_code: r.code, min_pct: r.min, max_pct: r.max,
        preferred_cheap: r.cheap,
        // 草稿模式下钉住当前选择行的化验/成本 ID（后端缺失时补当前生效）
        assay_id: this.mode === 'draft' ? r.activeAssayId : null,
        cost_id: this.mode === 'draft' ? r.activeCostId : null,
      })),
      lock_version: this.mode === 'draft' ? this.lockVersion : null,
      // 编辑既有草稿/候选/分支：按修订号更新；只有首次从发布版分叉才带来源号
      revision_no: this.mode === 'draft' && src && src.status !== 'published'
        ? src.revision_no : null,
      source_revision_no: this.mode === 'draft' && src && src.status === 'published'
        ? src.revision_no : null,
    };
  }

  /** 建单：POST 即发布 rev1；草稿：PUT /draft（乐观锁 + 幂等键）。 */
  save(publishAfter = false) {
    this.serverErrors = {}; this.conflictError = '';
    if (!this.validateClient()) return;

    if (this.mode === 'create') {
      this.saving = true;
      this.api.createScenario(this.buildBody()).subscribe({
        next: sc => {
          this.saving = false;
          this.saved.emit({ scenarioId: sc.id, revision: null });
        },
        error: e => this.handleError(e),
      });
      return;
    }

    this.saving = true;
    const body = this.buildBody();
    const key = this.draftKey();
    this.api.saveDraft(this.scenario!.id, body, key).subscribe({
      next: rev => {
        this.saving = false;
        this.savedDraft = rev;
        // 继续编辑基于刚保存的修订（最新 lock_version），保证乐观链不断
        this.sourceRevision = rev;
        this._draftKey = null;
        if (publishAfter) this.publish();
      },
      error: e => this.handleError(e),
    });
  }

  private _draftKey: string | null = null;
  private draftKey(): string {
    // 每次“保存”动作为一个幂等单元；保存成功后清除以允许下一次修改
    if (!this._draftKey) this._draftKey = ApiService.idemKey();
    return this._draftKey;
  }

  publish() {
    if (!this.savedDraft) {
      this.save(true);
      return;
    }
    this.publishing = true;
    this.api.publishDraft(this.scenario!.id, this.savedDraft.lock_version,
                         ApiService.idemKey()).subscribe({
      next: rev => {
        this.publishing = false;
        this.savedDraft = null;
        // 发布冻结：关闭编辑器并通知父组件刷新、按新发布版重算
        this.saved.emit({ scenarioId: this.scenario!.id, revision: rev });
      },
      error: e => {
        this.publishing = false;
        if (e.status === 409) {
          this.conflictError = (e.error?.detail?.message
            ?? e.error?.detail ?? '发布冲突') + ' —— 请关闭后重新从最新草稿打开';
        } else this.handleError(e);
      },
    });
  }

  private handleError(e: any) {
    this.saving = false; this.publishing = false;
    if (e.status === 409) {
      this.conflictError = e.error?.detail?.message ?? '乐观并发冲突：内容已被其他会话修改，请刷新';
      return;
    }
    this.serverErrors = e.error?.detail?.fields ?? { _: ApiService.errText(e) };
  }

  cancel() { this.cancelled.emit(); }
}
