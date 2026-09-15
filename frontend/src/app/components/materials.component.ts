import { Component, OnInit } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { ApiService, Assay, Cost, Material } from '../api.service';

interface MaterialRow extends Material {
  assays: Assay[];
  costs: Cost[];
  expanded: boolean;
}

@Component({
  selector: 'app-materials',
  standalone: true,
  imports: [DecimalPipe],
  template: `
    <div class="panel">
      <h2 style="margin-top:0">原料库 · 检测成分 / 干湿基 / 成本</h2>
      <p class="muted">
        所有数值为<b>虚构工艺边界数据</b>。成分按干基质量百分数存储；NULL=缺测（红色，拒绝当零用），
        0.00=显式未检出（灰色）。点击原料行查看全部化验版本并切换生效版本。
      </p>
      <div class="scroll-x">
        <table>
          <thead>
            <tr>
              <th>原料</th><th>类别</th><th>含水率%</th><th>湿吨价</th>
              <th>CaO</th><th>SiO₂</th><th>Al₂O₃</th><th>Fe₂O₃</th>
              <th>MgO</th><th>SO₃</th><th>K₂O</th><th>Na₂O</th><th>Cl⁻</th><th>LOI</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            @for (r of rows; track r.code) {
              <ng-container>
                <tr (click)="toggle(r)" style="cursor:pointer">
                  <td>{{ r.name }} <span class="muted mono" style="font-size:11px">{{ r.code }}</span></td>
                  <td>{{ r.category }}</td>
                  <td>{{ active(r)?.moisture_pct | number:'1.1-1' }}</td>
                  <td>{{ activeCost(r)?.price_wet_t | number:'1.0-0' }}</td>
                  @for (o of oxides; track o) {
                    <td [class.missing-cell]="active(r)?.[o]===null"
                        [class.zero-cell]="active(r)?.[o]===0">
                      @if (active(r)?.[o] !== null && active(r)?.[o] !== undefined) {
                        {{ active(r)?.[o] | number:'1.2-2' }}
                      } @else { 缺测 }
                    </td>
                  }
                  <td><button (click)="toggle(r);$event.stopPropagation()">版本</button></td>
                </tr>
                @if (r.expanded) {
                  <tr class="detail-row">
                    <td colspan="15">
                      <h4>化验版本（干基成分）</h4>
                      <table>
                        <thead>
                          <tr>
                            <th>版本</th><th>备注</th><th>含水率%</th>
                            @for (o of oxides; track o) { <th>{{ oxideLabel(o) }}</th> }
                            <th>状态</th>
                          </tr>
                        </thead>
                        <tbody>
                          @for (a of r.assays; track a.id) {
                            <tr>
                              <td class="mono">{{ a.version }} <span class="muted">#{{ a.id }}</span></td>
                              <td style="text-align:left;white-space:normal">{{ a.lab_note }}</td>
                              <td>{{ a.moisture_pct | number:'1.1-1' }}</td>
                              @for (o of oxides; track o) {
                                <td [class.missing-cell]="a[o]===null" [class.zero-cell]="a[o]===0">
                                  @if (a[o] !== null && a[o] !== undefined) {
                                    {{ a[o] | number:'1.2-2' }}
                                  } @else { 缺测 }
                                </td>
                              }
                              <td>
                                @if (a.id === r.active_assay_id) {
                                  <span class="badge good">生效中</span>
                                } @else {
                                  <button (click)="activate(r, a.id); $event.stopPropagation()">切为生效</button>
                                }
                              </td>
                            </tr>
                          }
                        </tbody>
                      </table>
                      <h4>成本（到厂价，元/湿吨）</h4>
                      <table>
                        <thead><tr><th>#</th><th>价格</th><th>备注</th></tr></thead>
                        <tbody>
                          @for (c of r.costs; track c.id) {
                            <tr>
                              <td class="mono">{{ c.id }} @if (c.id === r.active_cost_id) { <span class="badge good">生效</span> }</td>
                              <td>{{ c.price_wet_t | number:'1.2-2' }}</td>
                              <td style="text-align:left">{{ c.note }}</td>
                            </tr>
                          }
                        </tbody>
                      </table>
                    </td>
                  </tr>
                }
              </ng-container>
            }
          </tbody>
        </table>
      </div>
      <p class="muted" style="margin-bottom:0">
        换算关系：干基吨成本 = 湿吨价 ÷（1−含水率）。例如粉煤灰 18% 含水时，1 干吨需 1.2195 湿吨。
      </p>
    </div>
  `,
  styles: [`
    .detail-row td { background: var(--panel-2); padding: 12px 16px; }
    h4 { margin: 10px 0 6px; }
  `],
})
export class MaterialsComponent implements OnInit {
  rows: MaterialRow[] = [];
  oxides = ['cao','sio2','al2o3','fe2o3','mgo','so3','k2o','na2o','cl','loi'] as const;
  private labels: Record<string, string> = {
    cao:'CaO', sio2:'SiO₂', al2o3:'Al₂O₃', fe2o3:'Fe₂O₃', mgo:'MgO', so3:'SO₃',
    k2o:'K₂O', na2o:'Na₂O', cl:'Cl⁻', loi:'LOI',
  };

  constructor(private api: ApiService) {}

  ngOnInit() {
    this.api.materials().subscribe(ms => {
      this.rows = ms.map(m => ({ ...m, assays: [], costs: [], expanded: false }));
      for (const r of this.rows) {
        this.api.assays(r.code).subscribe(a => r.assays = a);
        this.api.costs(r.code).subscribe(c => r.costs = c);
      }
    });
  }

  oxideLabel(o: string) { return this.labels[o]; }
  active(r: MaterialRow): Assay | undefined {
    return r.assays.find(a => a.id === r.active_assay_id);
  }
  activeCost(r: MaterialRow): Cost | undefined {
    return r.costs.find(c => c.id === r.active_cost_id);
  }
  toggle(r: MaterialRow) { r.expanded = !r.expanded; }
  activate(r: MaterialRow, assayId: number) {
    this.api.activateAssay(r.code, assayId).subscribe(() => {
      r.active_assay_id = assayId;
    });
  }
}
