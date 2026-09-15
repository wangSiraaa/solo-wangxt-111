import { Component, Input } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { Conflict } from '../api.service';

@Component({
  selector: 'app-conflicts',
  standalone: true,
  imports: [DecimalPipe],
  template: `
    <div class="panel conflict-panel">
      <h3 style="margin-top:0">🚫 求解失败：约束冲突诊断（{{ conflicts.length }} 项）</h3>
      <p class="muted">
        已用 HiGHS 线性规划验证可行性，并通过「逐项放松 / 成对放松 / 最小违约弹性规划」定位冲突。
        以下边界在当前原料体系与最低掺量、可用量下无法同时成立：
      </p>
      @for (c of conflicts; track $index) {
        <div class="conflict-item">
          @switch (c.kind) {
            @case ('conflict_pair') {
              <div><b>冲突对：</b></div>
              <ul>@for (n of c.constraints; track n) { <li>{{ n }}</li> }</ul>
            }
            @case ('joint_conflict') {
              <div><b>{{ c.detail }}</b></div>
              <table>
                <thead>
                  <tr><th>被违反约束</th><th>类型</th>
                      <th>实际值（率值/组分%）</th><th>边界</th><th>线性缺口/超出</th></tr>
                </thead>
                <tbody>
                  @for (v of c.violated_constraints; track v.constraint) { <tr>
                    <td style="text-align:left">{{ v.constraint }}</td>
                    <td><span class="badge">{{ v.group }}</span></td>
                    <td class="bad-num">{{ v.actual | number:(v.group === 'indicator' ? '1.3-3' : '1.4-4') }}</td>
                    <td>{{ v.rhs | number:(v.group === 'indicator' ? '1.3-3' : '1.4-4') }}</td>
                    <td>{{ v.shortfall_or_excess | number:'1.4-4' }}</td>
                  </tr> }
                </tbody>
              </table>
              <details>
                <summary>最小总违约的配比（仅供定位冲突，并非可行解）</summary>
                <div class="mono muted" style="font-size:12px;line-height:1.8">
                  @for (e of mixEntries(c.least_bad_mix_pct); track e[0]) {
                    {{ e[0] }}: {{ e[1] | number:'1.2-2' }}% &nbsp;
                  }
                </div>
              </details>
            }
            @default {
              <div>
                <b class="bad-num">{{ c.constraint }}</b>
                <span class="badge" [class.bad]="c.kind==='indicator'"
                      [class.warn]="c.kind==='floor' || c.kind==='hazard'">{{ c.kind }}</span>
                <div class="muted">{{ c.detail }}</div>
              </div>
            }
          }
          @if (c.achievable) {
            <div class="warn-box" style="font-size:13px">
              仅保留配比界与分母保护时，{{ c.achievable.label }} 的可达区间为
              <b class="mono">[{{ c.achievable.achievable[0] }}, {{ c.achievable.achievable[1] }}]</b>
              —— 目标边界需落到此区间内才有解。
            </div>
          }
        </div>
      }
    </div>
  `,
  styles: [`
    .conflict-panel { border-color: var(--bad); }
    .conflict-item {
      background: var(--panel-2); border-radius: 8px; padding: 10px 14px; margin: 8px 0;
    }
    .bad-num { color: var(--bad); font-weight: 600; }
    ul { margin: 4px 0; padding-left: 20px; }
  `],
})
export class ConflictsComponent {
  @Input() conflicts: Conflict[] = [];
  mixEntries(m?: Record<string, number>): Array<[string, number]> {
    return m ? Object.entries(m) : [];
  }
}
