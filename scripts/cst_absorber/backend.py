"""Original CST preparation and lazy executable-backend facade.

This module never imports the vendor SDK. Prepared commands are review candidates,
not a claim that CST has accepted a model, fitted materials, or solved a case.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
import re


_BLOCKERS = {
    'material_fit_readback': 'Native fitted epsilon/mu and fitting errors have not been read back.',
    'native_geometry_readback': 'Imported shapes, materials, volumes and bounds have not been verified in CST.',
    'cell_domain_origin_readback': 'The native calculation-domain origin and explicit lattice have not been read back.',
    'reference_plane_readback': 'The native Zmax deembedding plane and complex phase have not been verified.',
    'native_mesh_max_edge': 'A native API enforcing the requested maximum edge length has not been verified.',
    'gamma_units': 'Actual Floquet Gamma labels, units and propagation convention have not been verified.',
    'floquet_power_normalization': 'The native complex Floquet S export has not been verified as power-normalized.',
    'independent_power_closure': 'Actual stimulated, outgoing, accepted and material loss curves have not been reconciled.',
    'owned_session_lifecycle': 'Fresh-session creation, bounded solving and verified closure have not been exercised.',
}


def get_capabilities() -> dict:
    """Return capability evidence without probing an installation or process."""
    return {'backend': 'cst', 'status': 'experimental', 'validation': 'not_run',
            'live_supported': False, 'execution_implemented': True,
            'execution_admission': 'explicit_authorization_exclusive_resources_and_acceptance_or_mapping',
            'native_acceptance': 'not_run', 'offline_preparation': True,
            'blocked_capabilities': dict(_BLOCKERS)}


def _number(value, *, positive=False) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError('prepared physical values must be numbers')
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError('prepared physical values must be finite and positive where required')
    return number


def _fmt(value) -> str:
    return format(_number(value), '.15g')


def _identifier(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', value):
        raise ValueError('case/material/region identifiers must be safe ASCII names')
    return value


def _quoted(value: str) -> str:
    if any(c in value for c in '\r\n\x00'):
        raise ValueError('VBA strings may not contain newlines or NUL')
    return '"' + value.replace('"', '""') + '"'


def _line(method: str, *values) -> str:
    return ' .' + method + (' ' + ', '.join(_quoted(str(v)) for v in values) if values else '')


def _check_case(case: dict) -> tuple[dict, dict, list]:
    """Validate the prepared boundary without resampling canonical material data."""
    if not isinstance(case, dict):
        raise ValueError('prepare_cst expects one prepared case')
    _identifier(case.get('id'))
    signature = case.get('signature')
    if not isinstance(signature, str) or not re.fullmatch(r'[0-9a-f]{64}', signature):
        raise ValueError('prepared case signature is missing or invalid')
    audit = case.get('geometry_audit', {})
    if audit.get('overlap_status') != 'verified_no_overlap':
        raise ValueError('unverified geometry overlap prevents native preparation')
    scenario = case.get('scenario', {})
    if scenario.get('profile') != 'periodic-pec':
        raise ValueError('only the periodic-pec profile is supported')
    frequencies = scenario.get('frequencies_Hz', [])
    if not isinstance(frequencies, list) or not frequencies:
        raise ValueError('explicit frequencies are required')
    frequencies = [_number(f, positive=True) for f in frequencies]
    if any(a >= b for a, b in zip(frequencies, frequencies[1:])):
        raise ValueError('frequencies must be strictly increasing')
    if scenario.get('polarization') not in ('TE', 'TM'):
        raise ValueError('one TE or TM excitation is required')
    if not 0 <= _number(scenario.get('theta_deg')) <= 45 or _number(scenario.get('azimuth_deg')) != 0:
        raise ValueError('only theta 0..45 and azimuth 0 are supported')
    air_height = _number(scenario.get('air_height_m'), positive=True)
    reference_offset = _number(scenario.get('reference_plane_m'))
    if not 0 <= reference_offset <= air_height:
        raise ValueError('reference plane must be within the air region above the cell')
    _number(case.get('mesh', {}).get('max_edge_m'), positive=True)
    geometry = case.get('geometry', {})
    cell = geometry.get('cell', {})
    for key in ('Lx_m', 'Ly_m', 'height_m'):
        _number(cell.get(key), positive=True)
    regions = geometry.get('regions')
    samples = case.get('material_samples')
    materials = case.get('materials')
    if not isinstance(regions, list) or not regions or not isinstance(samples, dict) or not isinstance(materials, dict):
        raise ValueError('prepared regions, materials and material_samples are required')
    seen = set()
    for region in regions:
        name = _identifier(region.get('id'))
        if name in seen:
            raise ValueError('duplicate region identifier')
        seen.add(name)
        material = _identifier(region.get('material'))
        if material not in samples or material not in materials:
            raise ValueError('region/material mapping is missing')
        _number(audit.get('volumes_m3', {}).get(name), positive=True)
        if region.get('kind') not in ('brick', 'stl'):
            raise ValueError('unsupported region kind')
    for material, sample in samples.items():
        _identifier(material)
        if sample.get('frequencies_Hz') != frequencies:
            raise ValueError('canonical material frequencies do not match scenario frequencies')
        if _number(sample.get('conductivity_S_m', 0)) != 0:
            raise ValueError('conductivity must already be folded once in the canonical samples')
        if sample.get('time_convention', 'exp(+jωt)') != 'exp(+jωt)':
            raise ValueError('canonical material samples must use exp(+jωt)')
        for key in ('epsilon_real', 'epsilon_imag', 'mu_real', 'mu_imag'):
            values = sample.get(key)
            if not isinstance(values, list) or len(values) != len(frequencies):
                raise ValueError('canonical material sample arrays are incomplete')
            values = [_number(v) for v in values]
            if key.endswith('imag') and any(v > 0 for v in values):
                raise ValueError('canonical passive imaginary parts must be nonpositive')
        if sample.get('density_kg_m3') is not None:
            _number(sample['density_kg_m3'], positive=True)
    return audit, samples, frequencies


def _verify_prepared(case: dict) -> None:
    """Recompute input identity and derived audits; never silently repair tampering."""
    from .contracts import build_plan, canonical_hash
    physical_keys = {'id', 'geometry', 'materials', 'scenario', 'mesh', 'analysis'}
    allowed_keys = physical_keys | {'signature', 'geometry_audit', 'material_samples', 'region_masses_kg', 'runtime', 'required_modes'}
    if set(case) - allowed_keys or not physical_keys.issubset(case):
        raise ValueError('prepared case contains missing or unsupported fields')
    for region in case['geometry']['regions']:
        if region['kind'] == 'stl' and not Path(region['path']).is_absolute():
            raise ValueError('STL path must be resolved by build_plan')
    # These inert values satisfy the shared contract for pure recomputation.
    # They are never used to probe resources, launch a worker or change identity.
    runtime = {'max_cpus': 1, 'max_modes': 1, 'min_available_RAM_GiB': 0,
               'min_free_disk_GiB': 0, 'wall_budget_seconds': 1, 'max_attempts': 1}
    runtime.update(case.get('runtime', {}))
    raw = {'schema_version': '1.0', 'case': {key: case[key] for key in physical_keys}, 'runtime': runtime}
    rebuilt = build_plan(raw, Path('.'))['cases'][0]
    if rebuilt['signature'] != case['signature']:
        raise ValueError('prepared physical signature mismatch: inputs or assets changed')
    for key in ('geometry_audit', 'material_samples', 'region_masses_kg'):
        if key not in case or canonical_hash(case[key]) != canonical_hash(rebuilt[key]):
            raise ValueError('prepared ' + key + ' mismatch: derived data changed')
    if 'required_modes' in case:
        from .modal import required_modes
        if canonical_hash(case['required_modes']) != canonical_hash(required_modes(case)):
            raise ValueError('prepared required_modes mismatch: derived mode data changed')


def _setup(case: dict, modes: list[dict], audit: dict) -> str:
    cell, scenario = case['geometry']['cell'], case['scenario']
    bbox = audit.get('bbox_m')
    if bbox is None:
        boxes = [r['bounds_m'] for r in case['geometry']['regions'] if r['kind'] == 'brick']
        bbox = [[min(b[0][i] for b in boxes) for i in range(3)],
                [max(b[1][i] for b in boxes) for i in range(3)]] if boxes else [[0, 0, 0], [0, 0, cell['height_m']]]
    pad = [bbox[0][0], cell['Lx_m'] - bbox[1][0], bbox[0][1],
           cell['Ly_m'] - bbox[1][1], bbox[0][2],
           cell['height_m'] + scenario['air_height_m'] - bbox[1][2]]
    lines = ["' Original command candidate. Execution and domain/mesh readback remain not_run.",
             'With Units', _line('SetUnit', 'Length', 'mm'), _line('SetUnit', 'Frequency', 'GHz'), 'End With',
             'Solver.FrequencyRange ' + _quoted(_fmt(min(scenario['frequencies_Hz']) / 1e9)) + ', ' +
             _quoted(_fmt(max(scenario['frequencies_Hz']) / 1e9)),
             'With Background', _line('Type', 'Normal'), _line('Epsilon', '1'), _line('Mu', '1'),
             _line('ApplyInAllDirections', 'False')]
    for key, value in zip(('XminSpace', 'XmaxSpace', 'YminSpace', 'YmaxSpace', 'ZminSpace', 'ZmaxSpace'), pad):
        lines.append(_line(key, _fmt(max(0, value) * 1000)))
    lines += ['End With', 'With Boundary']
    for side in ('Xmin', 'Xmax', 'Ymin', 'Ymax'):
        lines.append(_line(side, 'unit cell'))
    lines += [_line('Zmin', 'electric'), _line('Zmax', 'open'),
              _line('Xsymmetry', 'none'), _line('Ysymmetry', 'none'), _line('Zsymmetry', 'none'),
              _line('PeriodicUseConstantAngles', 'True'),
              _line('SetPeriodicBoundaryAngles', _fmt(scenario['theta_deg']), _fmt(scenario['azimuth_deg'])),
              _line('SetPeriodicBoundaryAnglesDirection', 'inward'),
              _line('UnitCellFitToBoundingBox', 'False'),
              _line('UnitCellDs1', _fmt(cell['Lx_m'] * 1000)),
              _line('UnitCellDs2', _fmt(cell['Ly_m'] * 1000)), _line('UnitCellAngle', '90'),
              'End With', 'With FloquetPort', _line('Reset'), _line('Port', 'Zmax'),
              _line('SetDialogTheta', _fmt(scenario['theta_deg'])),
              _line('SetDialogPhi', _fmt(scenario['azimuth_deg'])),
              _line('SetDialogFrequency', _fmt(max(scenario['frequencies_Hz']) / 1e9)),
              _line('SetCustomizedListFlag', 'True')]
    for mode in modes:
        lines.append(_line('AddMode', mode['polarization'], mode['m'], mode['n']))
    lines += [_line('SetNumberOfModesConsidered', len(modes)),
              _line('SetDistanceToReferencePlane', _fmt((scenario['reference_plane_m'] - scenario['air_height_m']) * 1000)),
              _line('ForceLegacyPhaseReference', 'False'), 'End With',
              'Mesh.SetCreator "High Frequency"', 'Mesh.MeshType "Tetrahedral"',
              'ChangeSolverType "HF Frequency Domain"',
              "' Requested max_edge_m is in expected_geometry.json; enforcement requires native API validation.",
              'With FDSolver', _line('Reset'), _line('SetMethod', 'Tetrahedral', 'Discrete samples only'),
              _line('Stimulation', 'List', 'List'), _line('ResetExcitationList'),
              _line('AddToExcitationList', 'Zmax', scenario['polarization'] + '(0,0)'),
              _line('TDCompatibleMaterials', 'False'), _line('CalcPowerLoss', 'True'),
              _line('ResetSampleIntervals', 'all')]
    for frequency in scenario['frequencies_Hz']:
        f = _fmt(frequency / 1e9)
        lines.append(_line('AddSampleInterval', f, f, '1', 'Single', 'False'))
    lines += ['End With']
    return '\n'.join(lines) + '\n'


def _materials(samples: dict) -> str:
    lines = ["' Canonical exp(+j omega t) losses are converted to positive CST loss columns.",
             "' Conductivity is already included in epsilon; native Sigma is zero.",
             "' Nth-order fit is a candidate approximation, not exact table interpolation."]
    for name, sample in samples.items():
        lines += ['With Material', _line('Reset'), _line('Name', 'mat_' + name),
                  _line('Type', 'Normal'), _line('MaterialUnit', 'Frequency', 'GHz'),
                  _line('Epsilon', _fmt(sample['epsilon_real'][0])),
                  _line('Mu', _fmt(sample['mu_real'][0])), _line('Sigma', '0'),
                  _line('TanDGiven', 'False'), _line('TanDMGiven', 'False')]
        if sample.get('density_kg_m3') is not None:
            lines.append(_line('Rho', _fmt(sample['density_kg_m3'])))
        for suffix, real_key, imaginary_key in (('Eps', 'epsilon_real', 'epsilon_imag'), ('Mu', 'mu_real', 'mu_imag')):
            lines += [_line('DispersiveFittingFormat' + suffix, 'Real_Imag'),
                      _line('DispersiveFittingScheme' + suffix, 'Nth Order'),
                      _line('MaximalOrderNthModelFit' + suffix, min(20, max(2, len(sample['frequencies_Hz'])))),
                      _line('UseGeneralDispersion' + suffix, 'True')]
            for f, real, imaginary in zip(sample['frequencies_Hz'], sample[real_key], sample[imaginary_key]):
                lines.append(_line('AddDispersionFittingValue' + suffix,
                                   _fmt(f / 1e9), _fmt(real), _fmt(-imaginary), '1'))
        lines += [_line('Create'), 'End With']
    return '\n'.join(lines) + '\n'


def _stage_geometry(case: dict, audit: dict) -> tuple[str, dict[str, bytes]]:
    lines = ['Component.New "absorber"']
    assets = {}
    for region in case['geometry']['regions']:
        shape, material = 'region_' + region['id'], 'mat_' + region['material']
        if region['kind'] == 'brick':
            bounds = region.get('bounds_m')
            if not isinstance(bounds, list) or len(bounds) != 2 or any(len(b) != 3 for b in bounds):
                raise ValueError('brick bounds must be two three-component vectors')
            lines += ['With Brick', _line('Reset'), _line('Name', shape),
                      _line('Component', 'absorber'), _line('Material', material)]
            for axis, method in enumerate(('Xrange', 'Yrange', 'Zrange')):
                lo, hi = (_number(bounds[j][axis]) for j in (0, 1))
                if lo >= hi:
                    raise ValueError('brick bounds must have positive extents')
                lines.append(_line(method, _fmt(lo * 1000), _fmt(hi * 1000)))
            lines += [_line('Create'), 'End With']
        else:
            source = Path(region.get('path', ''))
            if not source.is_absolute() or not source.is_file():
                raise ValueError('STL path must be resolved by build_plan and exist')
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != audit.get('asset_hashes', {}).get(region['id']):
                raise ValueError('STL content changed after geometry audit')
            unit = {'m': 1., 'mm': .001, 'cm': .01}.get(region.get('source_unit'))
            transform = region.get('transform', {})
            if unit is None or set(transform) != {'scale', 'translation_m'}:
                raise ValueError('unsupported or incomplete STL transform')
            scale = _number(transform['scale'], positive=True)
            translation = transform['translation_m']
            if not isinstance(translation, list) or len(translation) != 3:
                raise ValueError('STL translation must have three components')
            translation = [_number(x) for x in translation]
            import io
            import trimesh
            mesh = trimesh.load_mesh(io.BytesIO(raw), file_type='stl')
            if not isinstance(mesh, trimesh.Trimesh) or not mesh.is_watertight:
                raise ValueError('STL must be one closed mesh')
            mesh.apply_scale(unit * scale)
            mesh.apply_translation(translation)
            expected_volume = audit['volumes_m3'][region['id']]
            if not math.isclose(abs(mesh.volume), expected_volume, rel_tol=1e-6, abs_tol=1e-18):
                raise ValueError('transformed STL volume disagrees with geometry audit')
            mesh.apply_scale(1000)
            asset = 'assets/' + shape + '.stl'
            assets[asset] = mesh.export(file_type='stl')
            lines += ['With STL', _line('Reset'), _line('Name', shape),
                      _line('Component', 'absorber'), _line('FileName', '@@ARTIFACT_ROOT@@/' + asset),
                      _line('ImportToActiveCoordinateSystem', 'False'),
                      _line('ScaleToUnit', 'True'), _line('ImportFileUnits', 'mm'),
                      _line('Read'), 'End With',
                      'Solid.ChangeMaterial ' + _quoted('absorber:' + shape) + ', ' + _quoted(material)]
    return '\n'.join(lines) + '\n', assets


def _geometry_readback(case: dict) -> str:
    lines = ['Sub Main', 'Dim fh As Integer', 'Dim xmin As Double, xmax As Double',
             'Dim ymin As Double, ymax As Double, zmin As Double, zmax As Double',
             'Dim ok As Boolean', 'fh = FreeFile',
             'Open "@@ARTIFACT_ROOT@@/native_geometry.tsv" For Output As #fh',
             'Print #fh, "shape_count" & vbTab & CStr(Solid.GetNumberOfShapes)',
             'Print #fh, "lattice_ds1" & vbTab & CStr(Boundary.GetUnitCellDs1)',
             'Print #fh, "lattice_ds2" & vbTab & CStr(Boundary.GetUnitCellDs2)']
    for region in case['geometry']['regions']:
        shape = _quoted('absorber:region_' + region['id'])
        lines += ['Print #fh, ' + shape + ' & vbTab & Solid.GetMaterialNameForShape(' + shape + ') & vbTab & CStr(Solid.GetVolume(' + shape + '))',
                  'ok = Solid.GetLooseBoundingBoxOfShape(' + shape + ', xmin, xmax, ymin, ymax, zmin, zmax)',
                  'If Not ok Then Err.Raise 1001, "Geometry readback", "Shape bounds unavailable"',
                  'Print #fh, ' + shape + ' & vbTab & CStr(xmin) & vbTab & CStr(xmax) & vbTab & CStr(ymin) & vbTab & CStr(ymax) & vbTab & CStr(zmin) & vbTab & CStr(zmax)']
    return '\n'.join(lines + ['Close #fh', 'End Sub']) + '\n'


def _mode_readback(count: int) -> str:
    return '\n'.join(['Sub Main', 'Dim fh As Integer, i As Long', 'Dim modeName As String, ok As Boolean',
                      'fh = FreeFile', 'Open "@@ARTIFACT_ROOT@@/native_modes.tsv" For Output As #fh',
                      'FloquetPort.Port "Zmax"', 'For i = 1 To ' + str(count),
                      'ok = FloquetPort.GetModeNameByNumber(modeName, i)',
                      'If Not ok Then Err.Raise 1002, "Mode readback", "Mode name unavailable"',
                      'Print #fh, CStr(i) & vbTab & modeName', 'Next i', 'Close #fh', 'End Sub']) + '\n'


def prepare_cst(case: dict, out_dir: Path) -> dict:
    """Write reviewable native-model candidates for exactly one prepared design.

    The placeholder @@ARTIFACT_ROOT@@ deliberately keeps commands portable. A
    future reviewed adapter must bind it to the owned run and escape VBA strings.
    No commands are submitted to CST and no simulation outputs are manufactured.
    """
    audit, samples, frequencies = _check_case(case)
    _verify_prepared(case)
    from .modal import required_modes
    modes = required_modes(case)
    geometry, assets = _stage_geometry(case, audit)
    root = Path(out_dir).resolve()
    files = {'setup.vba': _setup(case, modes, audit), 'materials.vba': _materials(samples),
             'geometry.vba': geometry, 'geometry_readback.vba': _geometry_readback(case),
             'mode_readback.vba': _mode_readback(len(modes))}
    expected = {'units': {'geometry': 'mm', 'volume': 'mm3_candidate_requires_readback'},
                'cell_m': case['geometry']['cell'], 'calculation_domain_m': [[0, 0, 0],
                    [case['geometry']['cell']['Lx_m'], case['geometry']['cell']['Ly_m'],
                     case['geometry']['cell']['height_m'] + case['scenario']['air_height_m']]],
                'mesh_max_edge_m': case['mesh']['max_edge_m'],
                'reference_plane': {'convention': 'air offset above geometry.cell.height_m',
                    'zref_m': case['geometry']['cell']['height_m'] + case['scenario']['reference_plane_m'],
                    'zport_m': case['geometry']['cell']['height_m'] + case['scenario']['air_height_m'],
                    'deembedding_distance_m': case['scenario']['reference_plane_m'] - case['scenario']['air_height_m']},
                'regions': [{'shape': 'absorber:region_' + r['id'], 'material': 'mat_' + r['material'],
                             'volume_m3': audit['volumes_m3'][r['id']],
                             'bbox_m': audit.get('region_bboxes_m', {}).get(r['id'], r.get('bounds_m'))}
                            for r in case['geometry']['regions']]}
    acceptance = {'status': 'not_run', 'backend': 'experimental', 'live_supported': False,
                  'required_acceptance': dict(_BLOCKERS), 'incident_power_assumption': None,
                  'gamma_export_unit': None, 'material_model': 'native_nth_order_fit_candidate',
                  'material_fit_exact_table_interpolation': False,
                  'fit_input_point_count': len(frequencies),
                  'single_point_fit_requires_explicit_native_acceptance': len(frequencies) == 1,
                  'must_retain': ['native material curves and fit errors', 'native region map and bounds',
                      'actual mode-index names', 'complex S and reference impedances',
                      'actual complex Gamma with original unit labels',
                      'independent incident/reflected/absorbed/transmitted power'],
                  'submission_order': ['materials.vba', 'geometry.vba', 'setup.vba'],
                  'coordinate_note': 'Explicit lattice length does not establish native domain origin; read back the domain before solving.',
                  'result_tree_mapping': 'Discover only this fresh project tree; verify Floquet leaves and excitation branch. No guessed success paths.',
                  'sdk_boundary': {'session': 'DesignEnvironment.new; new_mws; save without overwrite',
                      'history': 'project.model3d.add_to_history',
                      'solver': 'start_solver and is_solver_running; owned-session-only shutdown',
                      'results': 'ProjectFile(own saved unpacked path, allow_interactive=False).get_3d()'}}
    files['expected_geometry.json'] = json.dumps(expected, indent=2, allow_nan=False) + '\n'
    files['expected_modes.json'] = json.dumps(modes, indent=2, allow_nan=False) + '\n'
    files['acceptance_requirements.json'] = json.dumps(acceptance, indent=2, allow_nan=False) + '\n'
    # Empty native-output schemas are templates, never synthetic solved data.
    files['spectra_export.schema.csv'] = 'frequency_Hz,m,n,polarization,s_re,s_im,gamma_re_per_m,gamma_im_per_m,normalization\n'
    files['power_export.schema.csv'] = 'frequency_Hz,incident_W,reflected_W,absorbed_W,transmitted_W\n'
    import io
    table = io.StringIO(newline='')
    writer = csv.writer(table)
    writer.writerow(['material', 'frequency_Hz', 'epsilon_real', 'epsilon_loss', 'mu_real', 'mu_loss',
                     'input_sigma_S_m', 'native_sigma_S_m', 'density_kg_m3'])
    for material, sample in samples.items():
        for i, frequency in enumerate(frequencies):
            writer.writerow(['mat_' + material, frequency, sample['epsilon_real'][i], -sample['epsilon_imag'][i],
                             sample['mu_real'][i], -sample['mu_imag'][i],
                             sample.get('source_conductivity_S_m', case['materials'][material].get('conductivity_S_m', 0)),
                             0, sample.get('density_kg_m3')])
    files['expected_materials.csv'] = table.getvalue()
    contents = {name: value.encode('utf-8') for name, value in files.items()}
    contents.update(assets)
    if any((root / name).exists() for name in [*contents, 'preparation.json']):
        raise FileExistsError('CST preparation artifacts already exist; use a fresh output directory')
    root.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    result = {'status': 'prepared', 'validation': 'not_run', 'backend': 'experimental',
              'live_supported': False, 'case_id': case['id'], 'case_signature': case['signature'],
              'artifacts': [*contents, 'preparation.json'],
              'artifact_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
              'blocked_capabilities': dict(_BLOCKERS)}
    (root / 'preparation.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return result


class CstBackend:
    """Runtime facade; its delegate admits only explicitly authorized execution.

    Construction and unapproved dispatch never import the vendor SDK. Native
    acceptance remains separate from having an executable implementation.
    """

    def __init__(self, **options):
        from .live_backend import ExecutableCstBackend
        self._delegate = ExecutableCstBackend(**options)

    @property
    def last_receipt(self):
        return self._delegate.last_receipt

    def ownership(self, attempt_dir):
        return self._delegate.ownership(attempt_dir)

    def cleanup_identity(self, recorded):
        return self._delegate.cleanup_identity(recorded)

    def __call__(self, case: dict, run_dir: Path, stop_event=None) -> dict:
        return self._delegate(case, run_dir, stop_event)
