import { Component, OnInit } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  ApiService, Scenario, SolutionDto, SolveResponse, RevisionDto,
} from '../api.service';
import { MixResultComponent } from './mix-result.component';
import { ConflictsComponent } from './conflicts.component';
import { ScenarioEditorComponent } from './scenario-editor.component';
import { MergeWizardComponent } from './merge-wizard.component';

@Component({
  selector: 'app-solver',
  standalone: true,
  imports: [DecimalPipe, FormsModule, MixResultComponent, ConflictsComponent,
            ScenarioEditorComponent, MergeWizardComponent],
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
  editorMode: 'create' | 'draft' = 'create';
  editorScenario: Scenario | null = null;
  editorSource: RevisionDto | null = null;
  mergeOpen = false;
  mergePreselectA: number | null = null;
  history: SolutionDto[] = [];
  historyOpen = false;
  timeline: RevisionDto[] = [];
  timelineOpen = false;
  newBranchName = '';
  newBranchSourceNo: number | null = null;

  constructor(private api: ApiService) {}

  ngOnInit() { this.loadScenarios(true); }

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
    this.timeline = []; this.timelineOpen = false;
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
      error: e => { this.error = ApiService.errText(e); this.loading = false; },
    });
  }

  openCreate() {
    this.editorMode = 'create';
    this.editorScenario = null;
    this.editorSource = null;
    this.editorOpen = true;
  }

  // ---------------- 并行分支与合并 ----------------

  get publishedRevisions(): RevisionDto[] {
    return this.timeline.filter(r => r.status === 'published');
  }
  get branchDrafts(): RevisionDto[] {
    return this.timeline.filter(r => r.status === 'draft' && r.kind === 'branch');
  }

  refreshTimeline(open = true): Promise<RevisionDto[]> {
    return new Promise(resolve => {
      this.api.revisions(this.selectedId).subscribe(rs => {
        this.timeline = rs;
        this.timelineOpen = open;
        if (this.newBranchSourceNo === null) {
          const pub = [...rs].reverse().find(r => r.status === 'published');
          this.newBranchSourceNo = pub?.revision_no ?? null;
        }
        resolve(rs);
      });
    });
  }

  createBranchFromPublished(revNo: number) {
    const name = (this.newBranchName || '').trim();
    if (!name) { alert('请填写分支名称'); return; }
    this.api.createBranch(this.selectedId, revNo, name, ApiService.idemKey()).subscribe({
      next: () => {
        this.newBranchName = '';
        this.loadScenarios(false, this.selectedId);
        this.refreshTimeline();
      },
      error: e => alert(e.error?.detail?.message ?? '创建分支失败'),
    });
  }

  editBranch(rev: RevisionDto) {
    if (rev.status === 'published') return;
    this.editorMode = 'draft';
    this.editorScenario = this.scenario ?? null;
    this.editorSource = rev;
    this.editorOpen = true;
  }

  openMerge(aNo: number | null = null) {
    if (!this.timeline.length) {
      this.api.revisions(this.selectedId).subscribe(rs => {
        this.timeline = rs;
        this.mergePreselectA = aNo;
        this.mergeOpen = true;
      });
    } else {
      this.mergePreselectA = aNo;
      this.mergeOpen = true;
    }
  }

  onMerged(candidateNo: number) {
    this.mergeOpen = false;
    this.loadScenarios(false, this.selectedId);
    this.refreshTimeline();
    // 直接打开候选发布确认
    setTimeout(() => {
      const cand = this.timeline.find(r => r.revision_no === candidateNo);
      if (cand && confirm(`合并候选 r${candidateNo} 已生成。立即发布？`)) {
        this.publishRevision(cand);
      }
    }, 400);
  }

  publishBranch(rev: RevisionDto) {
    this.publishRevision(rev);
  }

  continueDraft() {
    if (!this.scenario?.draft_revision_id) return;
    this.api.revisions(this.selectedId).subscribe(rs => {
      const draft = rs.find(r => r.status === 'draft');
      if (draft) this.openEditDraft(draft);
    });
  }

  continueDraftRevision(rev: RevisionDto) {
    this.openEditDraft(rev);
  }

  fmtVal(v: unknown): string {
    if (v === null || v === undefined) return '—';
    if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(3);
    if (typeof v === 'boolean') return v ? '是' : '否';
    return String(v);
  }

  openEditDraft(source: RevisionDto | null = null) {
    if (!this.scenario || this.scenario.built_in) return;
    this.editorMode = 'draft';
    this.editorScenario = this.scenario;
    if (source) {
      this.editorSource = source;
      this.editorOpen = true;
      return;
    }
    // 从当前发布版复制：拉取发布修订后再打开，保证编辑器初始化时已有源数据
    this.editorSource = null;
    this.api.revisions(this.selectedId).subscribe(rs => {
      const pub = rs.find(r => r.revision_no === this.scenario!.published_revision_no)
        ?? [...rs].reverse().find(r => r.status === 'published') ?? null;
      this.editorSource = pub;
      this.editorOpen = true;
    });
  }

  closeEditor() {
    this.editorOpen = false;
    this.editorScenario = null;
    this.editorSource = null;
  }

  onSaved(e: { scenarioId: number; revision: RevisionDto | null }) {
    this.editorOpen = false;
    this.loadScenarios(false, e.scenarioId || this.selectedId);
  }

  deleteCurrent() {
    const s = this.scenario;
    if (!s || s.built_in) return;
    if (!confirm(`确定删除自定义场景「${s.name}」及其全部修订与历史解？此操作不可恢复。`)) return;
    this.api.deleteScenario(s.id).subscribe(() => this.loadScenarios(true));
  }

  loadHistory() {
    this.historyOpen = !this.historyOpen;
    if (this.historyOpen && !this.history.length) {
      this.api.solutions(this.selectedId).subscribe(rows => this.history = rows);
    }
  }

  loadTimeline() {
    this.timelineOpen = !this.timelineOpen;
    if (this.timelineOpen && !this.timeline.length) {
      this.api.revisions(this.selectedId).subscribe(rs => {
        this.timeline = rs;
        // 默认从当前发布版分叉
        if (this.newBranchSourceNo === null) {
          const pub = [...rs].reverse().find(r => r.status === 'published');
          this.newBranchSourceNo = pub?.revision_no ?? null;
        }
      });
    }
  }

  publishRevision(rev: RevisionDto) {
    this.api.publishRevision(this.selectedId, rev.revision_no, rev.lock_version,
                              ApiService.idemKey()).subscribe({
      next: () => {
        this.timeline = [];
        this.loadScenarios(false, this.selectedId);
      },
      error: e => alert(e.error?.detail?.message ?? '发布失败（可能已被其他会话修改）'),
    });
  }

  discardRevision(rev: RevisionDto) {
    if (!confirm(`放弃 r${rev.revision_no}${rev.branch_name ? '（' + rev.branch_name + '）' : ''}？`)) return;
    this.api.discardDraft(this.selectedId, rev.revision_no).subscribe({
      next: () => { this.timeline = []; this.loadScenarios(false, this.selectedId); },
      error: e => alert(e.error?.detail?.message ?? '放弃失败'),
    });
  }

  rollbackTo(revNo: number) {
    this.api.rollbackDraft(this.selectedId, revNo, ApiService.idemKey()).subscribe({
      next: d => {
        this.timeline = [];
        this.loadScenarios(false, this.selectedId);
        // 用回滚产生的草稿修订直接打开编辑器（来源标记为被复制的旧发布版）
        this.editorMode = 'draft';
        this.editorScenario = this.scenario ?? null;
        this.editorSource = d;
        this.editorOpen = true;
      },
      error: e => alert(e.error?.detail?.message ?? '回滚草稿失败'),
    });
  }

  replayRevision(revNo: number) {
    // 重放旧发布修订：切换该修订求解（产生绑定 revNo 的新审计解）
    this.loading = true;
    this.api.solve(this.selectedId, this.activeProfile, revNo).subscribe({
      next: r => {
        this.loading = false;
        if (this.activeProfile === 'base') this.base = r; else this.rain = r;
        this.history = [];
      },
      error: e => { this.loading = false; alert(ApiService.errText(e)); },
    });
  }

  feasible(resp: SolveResponse | null): SolutionDto[] {
    return (resp?.solutions ?? []).filter(s => s.status === 'feasible');
  }

  /** 旱季 vs 雨季方案对比（按 mode 对齐）。 */
  compareRows(): Array<{ mode: string; label: string; base?: SolutionDto; rain?: SolutionDto }> {
    const labels: Record<string, string> = {
      min_cost: '最低成本', target_center: '指标居中', max_cheap: '廉价原料最大化',
    };
    return ['min_cost', 'target_center', 'max_cheap'].map(mode => ({
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
    const fmt = (v: number) => v.toFixed(digits === '1.2-2' ? 2 : 3);
    if (a === undefined || a === null || b === undefined || b === null) return '—';
    const c = b > a + 1e-9 ? ' ↑' : b < a - 1e-9 ? ' ↓' : ' →';
    return `${fmt(a)} → ${fmt(b)}${c}`;
  }
  pctArrow(a: number, b: number): string {
    const c = b > a + 1e-9 ? ' ↑' : b < a - 1e-9 ? ' ↓' : ' →';
    return `${a.toFixed(2)}% → ${b.toFixed(2)}%${c}`;
  }
  inKh(v?: number | null) { return v != null && v >= this.scenario!.targets.kh[0] && v <= this.scenario!.targets.kh[1]; }
  inSm(v?: number | null) { return v != null && v >= this.scenario!.targets.sm[0] && v <= this.scenario!.targets.sm[1]; }
  inIm(v?: number | null) { return v != null && v >= this.scenario!.targets.im[0] && v <= this.scenario!.targets.im[1]; }

  snapshotAssays(h: SolutionDto): string {
    const av = h.trace?.provenance?.assay_versions ?? {};
    const entries = Object.values(av).slice(0, 3)
      .map(v => `${v.material}:${v.assay_version}#${v.assay_id}`);
    const more = Object.keys(av).length > 3 ? ` +${Object.keys(av).length - 3}` : '';
    return entries.join('，') + more;
  }

  fmtTime(s: string | null | undefined): string {
    return (s ?? '').replace('T', ' ').slice(0, 19);
  }
}
