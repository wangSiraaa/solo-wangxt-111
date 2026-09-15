import { Component, EventEmitter, Input, OnInit, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  ApiService, RevisionDto, MergePreview, MergeConflict,
} from '../api.service';

@Component({
  selector: 'app-merge-wizard',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './merge-wizard.component.html',
  styleUrl: './merge-wizard.component.scss',
})
export class MergeWizardComponent implements OnInit {
  @Input() scenarioId!: number;
  @Input() revisions: RevisionDto[] = [];
  /** 预选分支（从时间线点击合并） */
  @Input() preselectA: number | null = null;
  @Output() merged = new EventEmitter<number>();
  @Output() cancelled = new EventEmitter<void>();

  aNo: number | null = null;
  bNo: number | null = null;
  baseNo: number | null = null;
  preview: MergePreview | null = null;
  resolutions: Record<string, unknown> = {};
  busy = false;
  error = '';

  constructor(private api: ApiService) {}

  ngOnInit() {
    this.aNo = this.preselectA;
  }

  get branchable(): RevisionDto[] {
    return this.revisions.filter(r => r.status === 'draft' && r.kind === 'branch');
  }

  get baseOptions(): RevisionDto[] {
    return this.revisions.filter(r => r.status === 'published');
  }

  get autoMerged() { return this.preview?.auto ?? []; }
  get conflicts(): MergeConflict[] { return this.preview?.conflicts ?? []; }
  get resolvedCount(): number { return Object.keys(this.resolutions).length; }
  get allConflictsResolved(): boolean {
    return this.conflicts.length > 0 && this.resolvedCount >= this.conflicts.length;
  }

  revisionOf(no: number | null): RevisionDto | undefined {
    return this.revisions.find(r => r.revision_no === no);
  }

  /** 共同来源默认：两分支 created_from 相同则自动填 */
  private inferBase() {
    const a = this.revisionOf(this.aNo);
    const b = this.revisionOf(this.bNo);
    if (a && b && a.created_from_revision_no &&
        a.created_from_revision_no === b.created_from_revision_no) {
      this.baseNo = a.created_from_revision_no;
    } else {
      this.baseNo = null;
    }
  }

  onSelect() {
    this.inferBase();
    if (this.aNo && this.bNo && this.baseNo) this.runPreview();
  }

  runPreview() {
    if (!this.aNo || !this.bNo || !this.baseNo) return;
    this.busy = true; this.error = '';
    this.api.mergePreview(this.scenarioId, this.baseNo, this.aNo, this.bNo,
                          this.resolutions).subscribe({
      next: p => { this.preview = p; this.busy = false; },
      error: e => { this.busy = false; this.error = e.error?.detail?.message ?? ApiService.errText(e); },
    });
  }

  chooseResolution(field: string, side: 'a' | 'b' | 'base') {
    const c = this.conflicts.find(x => x.field === field);
    if (!c) return;
    this.resolutions[field] = side === 'a' ? c.a : side === 'b' ? c.b : c.base;
  }

  commitMerge() {
    if (!this.aNo || !this.bNo || !this.baseNo) return;
    this.busy = true; this.error = '';
    this.api.mergeBranches(this.scenarioId, {
      a: this.aNo, b: this.bNo, base: this.baseNo,
      attempt_id: this.preview?.attempt_id,
      resolutions: this.resolutions,
    }, ApiService.idemKey()).subscribe({
      next: p => {
        this.busy = false;
        if (p.status === 'conflict') {
          this.preview = p;
          return;
        }
        if (p.status === 'merged') {
          this.merged.emit(p.revision_no!);
        }
      },
      error: e => {
        this.busy = false;
        this.error = e.error?.detail?.message ?? ApiService.errText(e);
      },
    });
  }

  fmt(v: unknown): string {
    if (v === null || v === undefined) return '—';
    if (typeof v === 'boolean') return v ? '是' : '否';
    if (typeof v === 'object') return JSON.stringify(v);
    return String(v);
  }

  cancel() { this.cancelled.emit(); }
}
