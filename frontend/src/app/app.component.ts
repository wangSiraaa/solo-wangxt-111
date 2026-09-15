import { Component } from '@angular/core';
import { SolverComponent } from './components/solver.component';
import { MaterialsComponent } from './components/materials.component';
import { ManualComponent } from './components/manual.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [SolverComponent, MaterialsComponent, ManualComponent],
  template: `
    <header class="app-header">
      <div class="title">
        <h1 style="margin:0">离线生料配比试算工作台</h1>
        <span class="badge warn">虚构工艺边界 · 仅供离线研究 · 不构成任何生产设备指令</span>
      </div>
      <nav class="tab-row">
        <button class="tab" [class.active]="tab==='solver'" (click)="tab='solver'">场景试算与方案对比</button>
        <button class="tab" [class.active]="tab==='manual'" (click)="tab='manual'">手工配比试算</button>
        <button class="tab" [class.active]="tab==='materials'" (click)="tab='materials'">原料库 / 化验版本</button>
      </nav>
    </header>
    <main class="app-main">
      @switch (tab) {
        @case ('solver') { <app-solver /> }
        @case ('manual') { <app-manual /> }
        @case ('materials') { <app-materials /> }
      }
    </main>
    <footer class="app-footer muted">
      计算口径：xᵢ=干生料质量分数，Σxᵢ=1；合成成分=Σxᵢ·cᵢ（质量守恒）；
      SM=SiO₂/(Al₂O₃+Fe₂O₃)，IM=Al₂O₃/Fe₂O₃，KH=(CaO−1.65Al₂O₃−0.35Fe₂O₃)/(2.8SiO₂)；
      湿吨=干吨/(1−含水率)。必需氧化物缺测（NULL）拒绝计算，分母低于保护阈值明确报错。
    </footer>
  `,
  styles: [`
    .app-header {
      padding: 18px 24px 0; background: linear-gradient(180deg, #141c28, #0f141b);
      border-bottom: 1px solid var(--line);
    }
    .title { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
    .app-main { padding: 18px 24px; max-width: 1500px; margin: 0 auto; }
    .app-footer { padding: 16px 24px; font-size: 12px; line-height: 1.7; max-width: 1500px; margin: 0 auto; }
  `],
})
export class AppComponent {
  tab: 'solver' | 'manual' | 'materials' = 'solver';
}
