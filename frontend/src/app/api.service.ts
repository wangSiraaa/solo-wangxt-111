import { Injectable } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Observable } from 'rxjs';

export interface Material {
  id: number; code: string; name: string; category: string;
  active_assay_id: number | null; active_cost_id: number | null;
}

export interface Assay {
  id: number; material_id: number; version: string; sampled_at: string | null;
  lab_note: string; moisture_pct: number;
  cao: number | null; sio2: number | null; al2o3: number | null; fe2o3: number | null;
  mgo: number | null; so3: number | null; k2o: number | null; na2o: number | null;
  cl: number | null; loi: number | null;
}

export interface Cost { id: number; material_id: number; price_wet_t: number; note: string; }

export interface ScenarioMaterial {
  code: string; name: string; min_pct: number; max_pct: number | null; preferred_cheap: boolean;
}

export interface Scenario {
  id: number; name: string; description: string;
  built_in?: boolean; created_at?: string;
  targets: { kh: [number, number]; sm: [number, number]; im: [number, number] };
  hazards: { mgo_max: number; so3_max: number; alkali_eq_max: number; cl_max: number };
  denom_floor: number;
  rain_overrides: Record<string, number>;
  rain_extra_cost: Record<string, number>;
  materials: ScenarioMaterial[];
}

export interface ScenarioMaterialInput {
  material_code: string; min_pct: number; max_pct: number | null; preferred_cheap: boolean;
}

export interface ScenarioInput {
  name: string; description: string;
  kh_min: number; kh_max: number; sm_min: number; sm_max: number;
  im_min: number; im_max: number;
  mgo_max: number; so3_max: number; alkali_eq_max: number; cl_max: number;
  denom_floor: number;
  rain_overrides: Record<string, number>;
  rain_extra_cost: Record<string, number>;
  materials: ScenarioMaterialInput[];
}

export interface AvailabilityInfo {
  material_id: number; max_fraction_pct: number; supply_note: string;
}

export interface Conflict {
  kind: string; constraint?: string; detail: string;
  constraints?: string[]; achievable?: { label: string; achievable: [number, number] } | null;
  violated_constraints?: Array<{
    constraint: string; group: string; actual: number; rhs: number;
    sense: string; shortfall_or_excess: number; text: string;
  }>;
  least_bad_mix_pct?: Record<string, number>;
}

export interface MaterialTraceRow {
  material: string; name: string;
  fraction_dry_pct: number; moisture_pct: number; base_moisture_pct: number;
  moisture_overridden: boolean;
  dry_factor_t_per_t: number; wet_mass_t_per_t_dry_blend: number;
  fraction_wet_pct: number; price_wet_t: number; extra_cost_wet_t: number;
  price_dry_t: number; cost_contribution: number;
  oxide_contribution_pct: Record<string, number>;
}

export interface TraceBody {
  composition_dry_pct: Record<string, number>;
  composition_ignited_pct: Record<string, number>;
  loi_scaling_factor: number;
  indicators: { kh: number; sm: number; im: number };
  alkali_eq_pct: number; kh_warning: string | null;
  denominator_checks: Array<{ name: string; value: number; floor: number; ok: boolean }>;
  materials: MaterialTraceRow[];
  cost_dry_t: number; cost_wet_equiv_t: number;
  total_wet_mass_t_per_t_dry: number;
  mass_balance: { basis: string; dry_input_kg: number; wet_input_kg: number;
                  water_kg: number; ignited_kg: number };
}

export interface SolutionDto {
  solution_id: number; scenario_id?: number; profile?: string;
  mode: string; status: string;
  cost_dry_t: number | null; cost_wet_t: number | null;
  kh: number | null; sm: number | null; im: number | null;
  created_at?: string | null;
  mix: Record<string, number>; conflicts: Conflict[];
  trace: {
    provenance?: {
      scenario: { id: number; name: string; description: string };
      profile: string; profile_note: string;
      assay_versions: Record<string, {
        material: string; name: string; assay_version: string; assay_id: number;
        lab_note: string; cost_id: number; price_wet_t: number;
        min_pct: number; max_pct: number; availability_note: string;
        composition_dry_pct: Record<string, number | null>;
      }>;
      basis: Record<string, string>;
      targets: Record<string, unknown>;
      linear_constraints: Array<{ name: string; sense: string; rhs: number;
                                  coef: Record<string, number> }>;
    };
    trace?: TraceBody;
    reason?: string; error_code?: string;
    details?: { missing?: Array<{ material: string; analyte: string; label: string }> };
    duplicate?: boolean;
  };
}

export interface SolveResponse {
  status: 'feasible' | 'infeasible' | 'failed';
  profile: string;
  conflicts: Conflict[];
  solutions: SolutionDto[];
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  constructor(private http: HttpClient) {}

  materials(): Observable<Material[]> { return this.http.get<Material[]>('/api/materials'); }

  assays(code: string): Observable<Assay[]> {
    return this.http.get<Assay[]>(`/api/materials/${code}/assays`);
  }

  costs(code: string): Observable<Cost[]> {
    return this.http.get<Cost[]>(`/api/materials/${code}/costs`);
  }

  addAssay(code: string, body: Partial<Assay>, activate = true): Observable<Assay> {
    return this.http.post<Assay>(`/api/materials/${code}/assays?activate=${activate}`, body);
  }

  activateAssay(code: string, assayId: number): Observable<Assay> {
    return this.http.post<Assay>(`/api/materials/${code}/assays/${assayId}/activate`, {});
  }

  addCost(code: string, body: { price_wet_t: number; note: string }): Observable<Cost> {
    return this.http.post<Cost>(`/api/materials/${code}/costs`, body);
  }

  scenarios(): Observable<Scenario[]> { return this.http.get<Scenario[]>('/api/scenarios'); }

  createScenario(body: ScenarioInput): Observable<Scenario> {
    return this.http.post<Scenario>('/api/scenarios', body);
  }

  updateScenario(id: number, body: ScenarioInput): Observable<Scenario> {
    return this.http.put<Scenario>(`/api/scenarios/${id}`, body);
  }

  deleteScenario(id: number): Observable<unknown> {
    return this.http.delete(`/api/scenarios/${id}`);
  }

  availability(code: string): Observable<AvailabilityInfo | null> {
    return this.http.get<AvailabilityInfo | null>(`/api/materials/${code}/availability`);
  }

  solve(id: number, profile: 'base' | 'rain'): Observable<SolveResponse> {
    return this.http.post<SolveResponse>(`/api/scenarios/${id}/solve?profile=${profile}`, {});
  }

  solutions(id: number): Observable<SolutionDto[]> {
    return this.http.get<SolutionDto[]>(`/api/scenarios/${id}/solutions`);
  }

  manualBlend(items: Array<{ code: string; pct: number;
                             moisture_override?: number | null;
                             extra_cost_wet_t?: number }>,
              denomFloor: number): Observable<{ trace: TraceBody;
                                                provenance: { assay_versions: unknown[] } }> {
    return this.http.post('/api/manual-blend', { items, denom_floor: denomFloor }) as
      Observable<{ trace: TraceBody; provenance: { assay_versions: unknown[] } }>;
  }

  static errText(e: HttpErrorResponse): string {
    const d = e.error?.detail;
    if (d && typeof d === 'object') {
      let s = `[${d.code}] ${d.message}`;
      const miss = d.details?.missing;
      if (Array.isArray(miss) && miss.length) {
        s += '；缺测项：' + miss.map((m: any) => `${m.material}·${m.label ?? m.analyte}`).join('、');
      }
      return s;
    }
    return typeof d === 'string' ? d : e.message;
  }
}
