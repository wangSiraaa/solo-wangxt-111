import { Component, OnInit } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import {
  ApiService, Scenario, SolutionDto, SolveResponse,
} from '../api.service';
import { MixResultComponent } from './mix-result.component';
import { ConflictsComponent } from './conflicts.component';
import { ScenarioEditorComponent } from './scenario-editor.component';

@Component({
  selector: 'app-solver',
  standalone: true,
  imports: [DecimalPipe, MixResultComponent, ConflictsComponent, ScenarioEditorComponent],
  templateUrl: './solver.component.html',
  styleUrl: './solver.component.scss',
})
export class SolverComponent implements OnInit {
  scenarios: Scenario[] = [];
  selectedId = 1;
  loading = false;
  error = '';
  base: SolveResponse | null = null;
  rain: SolveResponse | null = null;
  activeProfile: 'base' | 'rain' = 'base';
  editorOpen = false;
  editorTarget: Scenario | null = null;
  history: SolutionDto[] = [];
  historyOpen = false;

  constructor(private api: ApiService) {}

  ngOnInit() {
    this.loadScenarios(true);
  }

  loadScenarios(autoselectFirst = false, preferredId?: number) {
    this.api.scenarios().subscribe(ss => {
      const existed = this.scenarios.find(s => s.id === this.selectedId);
      this.scenarios = ss;
      if (preferredId && ss.some(s => s.id === preferredId)) {
        this.selectedId = preferredId;
      } else if (autoselectFirst || !existed) {
        this.selectedId = ss.find(s => s.id === this.selectedId)?.id ?? ss[0]?.id ?? 1;
      }
      if (preferredId || existed || autoselectFirst) this.reload();
    });
  }

  get scenario(): Scenario | undefined {
    return this.scenarios.find(s => s.id === this.selectedId);
  }

  get current(): SolveResponse | null {
    return this.activeProfile === 'base' ? this.base : this.rain;
  }

  get hasRain(): boolean {
    return Object.keys(this.scenario?.rain_overrides ?? {}).length > 0;
  }

  select(id: number) {
    if (id === this.selectedId) return;
    this.selectedId = id;
    this.reload();
  }

  reload() {
    this.base = null; this.rain = null; this.error = '';
    this.history = []; this.historyOpen = false;
    this.run('base');
  }

  run(profile: 'base' | 'rain') {
    this.activeProfile = profile;
    if ((profile === 'base' ? this.base : this.rain)) return;
    this.loading = true; this.error = '';
    this.api.solve(this.selectedId, profile).subscribe({
      next: r => {
        if (profile === 'base') this.base = r; else this.rain = r;
        this.loading = false;
      },
      error: e => {
        this.error = ApiService.errText(e);
        this.loading = false;
      },
    });
  }

  openCreate() { this.editorTarget = null; this.editorOpen = true; }
  openEdit() { this.editorTarget = this.scenario ?? null; this.editorOpen = true; }
  closeEditor() { this.editorOpen = false; this.editorTarget = null; }

  onSaved(s: Scenario) {
    this.editorOpen = false; this.editorTarget = null;
    this.loadScenarios(false, s.id);
    this.selectedId = s.id;
    this.reload();
  }

  deleteCurrent() {
    const s = this.scenario;
    if (!s || s.built_in) return;
    if (!confirm(`确定删除自定义场景「${s.name}」及其全部历史解？此操作不可恢复。`)) return;
    this.api.deleteScenario(s.id).subscribe(() => {
      this.loadScenarios(true);
    });
  }

  loadHistory() {
    this.historyOpen = !this.historyOpen;
    if (this.historyOpen && !this.history.length) {
      this.api.solutions(this.selectedId).subscribe(rows => this.history = rows);
    }
  }

  feasible(resp: SolveResponse | null): SolutionDto[] {
    return (resp?.solutions ?? []).filter(s => s.status === 'feasible');
  }

  /** 旱季 vs 雨季方案对比（按 mode 对齐）。 */
  compareRows(): Array<{ mode: string; label: string; base?: SolutionDto; rain?: SolutionDto }> {
    const labels: Record<string, string> = {
      min_cost: '最低成本', target_center: '指标居中', max_cheap: '廉价原料最大化',
    };
    const modes = ['min_cost', 'target_center', 'max_cheap'];
    return modes.map(mode => ({
      mode, label: labels[mode],
      base: this.feasible(this.base).find(s => s.mode === mode),
      rain: this.feasible(this.rain).find(s => s.mode === mode),
    }));
  }

  cheapCodes(): string[] {
    return (this.scenario?.materials ?? []).filter(m => m.preferred_cheap).map(m => m.code);
  }

  cheapPct(s: SolutionDto | undefined): number {
    if (!s) return 0;
    return this.cheapCodes().reduce((acc, c) => acc + (s.mix[c] || 0), 0);
  }

  rainEntries(): Array<[string, number]> {
    return Object.entries(this.scenario?.rain_overrides ?? {});
  }

  modeLabel(mode: string): string {
    return ({ min_cost: '💰 最低成本', target_center: '🎯 指标居中',
              max_cheap: '🏷 廉价最大化', diagnosis: '🚫 冲突诊断' } as Record<string, string>)[mode] ?? mode;
  }

  deltaCost(b?: SolutionDto, r?: SolutionDto): number {
    return (r?.cost_dry_t ?? 0) - (b?.cost_dry_t ?? 0);
  }

  arrow(a?: number | null, b?: number | null, digits = '1.3-3'): string {
    const fmt = (v: number) => v.toFixed(digits === '1.2-2' ? 2 : digits === '1.3-3' ? 3 : 3);
    if (a === undefined || a === null || b === undefined || b === null) return '—';
    const arrowChar = b > a + 1e-9 ? ' ↑' : b < a - 1e-9 ? ' ↓' : ' →';
    return `${fmt(a)} → ${fmt(b)}${arrowChar}`;
  }

  pctArrow(a: number, b: number): string {
    const arrowChar = b > a + 1e-9 ? ' ↑' : b < a - 1e-9 ? ' ↓' : ' →';
    return `${a.toFixed(2)}% → ${b.toFixed(2)}%${arrowChar}`;
  }

  inKh(v?: number | null) { return v != null && v >= this.scenario!.targets.kh[0] && v <= this.scenario!.targets.kh[1]; }
  inSm(v?: number | null) { return v != null && v >= this.scenario!.targets.sm[0] && v <= this.scenario!.targets.sm[1]; }
  inIm(v?: number | null) { return v != null && v >= this.scenario!.targets.im[0] && v <= this.scenario!.targets.im[1]; }

  /** 从历史解 trace 中取快照化验版本（旧解保留旧版本，不会随当前生效版本改变）。 */
  snapshotAssays(h: SolutionDto): string {
    const av = h.trace?.provenance?.assay_versions ?? {};
    const entries = Object.values(av).slice(0, 3)
      .map(v => `${v.material}:${v.assay_version}#${v.assay_id}`);
    const more = Object.keys(av).length > 3 ? ` +${Object.keys(av).length - 3}` : '';
    return entries.join('，') + more;
  }
}
