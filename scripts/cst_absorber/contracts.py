"""Strict explicit-case configuration and deterministic content identities."""
import copy
import hashlib
import json
import math
import re
from pathlib import Path
from .geometry import audit_geometry
from .materials import sample_material


def canonical_hash(value) -> str:
    """Hash JSON content independent of object key order; reject NaN/Infinity."""
    def normalize(item):
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError('hash content must be finite JSON')
            return int(item) if item.is_integer() else item
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError('hash object keys must be strings')
            return {key: normalize(content) for key, content in item.items()}
        if isinstance(item, list):
            return [normalize(content) for content in item]
        if item is None or isinstance(item, (str, bool, int)):
            return item
        raise ValueError('hash content must be finite JSON')
    try:
        encoded = json.dumps(normalize(value), sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                             allow_nan=False).encode('utf-8')
    except (ValueError, TypeError) as error:
        raise ValueError('hash content must be finite JSON') from error
    return hashlib.sha256(encoded).hexdigest()


def _object(value, allowed, required, label):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError(f'{label} has missing or unsupported fields')


def _number(value, label, positive=False, nonnegative=False, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label} must be a finite scalar number')
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f'{label} must be a finite scalar number')
    if positive and value <= 0 or nonnegative and value < 0 or integer and (not isinstance(value, int)):
        raise ValueError(f'{label} has an invalid value')
    return value if integer else float(value)


def _identifier(value, label):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', value) is None:
        raise ValueError(f'{label} must be a safe 1..100 character identifier')
    return value


def normalize_config(raw: dict, base_dir: Path) -> dict:
    """Validate explicit inputs and return one normalized single/batch contract."""
    _object(raw, {'schema_version', 'execution', 'case', 'cases', 'runtime'},
            {'schema_version', 'runtime'}, 'configuration')
    if raw['schema_version'] != '1.0':
        raise ValueError('unsupported schema_version')
    execution = raw.get('execution', {'mode': 'single'})
    _object(execution, {'mode', 'confirmed'}, set(), 'execution')
    mode = execution.get('mode', 'single')
    if mode not in ('single', 'batch'):
        raise ValueError('execution.mode must be single or batch')
    if 'confirmed' in execution and not isinstance(execution['confirmed'], bool):
        raise ValueError('execution.confirmed must be boolean')
    if mode == 'single':
        if 'case' not in raw or 'cases' in raw:
            raise ValueError('single execution requires exactly one case')
        cases = [raw['case']]
    else:
        if execution.get('confirmed') is not True or 'case' in raw or 'cases' not in raw:
            raise ValueError('batch requires confirmed:true and explicit cases')
        cases = raw['cases']
        if not isinstance(cases, list) or not cases:
            raise ValueError('batch cases must be a finite nonempty list')
    runtime_keys = {'max_cpus', 'min_available_RAM_GiB', 'min_free_disk_GiB',
                    'max_modes', 'wall_budget_seconds', 'max_attempts'}
    _object(raw['runtime'], runtime_keys, runtime_keys - {'max_attempts'}, 'runtime')
    runtime = copy.deepcopy(raw['runtime'])
    runtime.setdefault('max_attempts', 1)
    for key in runtime_keys:
        runtime[key] = _number(runtime[key], key, positive=key not in ('min_available_RAM_GiB', 'min_free_disk_GiB'),
                               nonnegative=key in ('min_available_RAM_GiB', 'min_free_disk_GiB'),
                               integer=key in ('max_cpus', 'max_modes', 'max_attempts'))
    normalized, ids = [], set()
    for source in cases:
        keys = {'id', 'geometry', 'materials', 'scenario', 'mesh', 'analysis'}
        _object(source, keys, keys, 'case')
        case = copy.deepcopy(source)
        case_id = _identifier(case['id'], 'case id')
        if case_id in ids:
            raise ValueError('case ids must be unique')
        ids.add(case_id)
        scenario = case['scenario']
        scenario_keys = {'profile', 'frequencies_Hz', 'theta_deg', 'azimuth_deg',
                         'polarization', 'air_height_m', 'reference_plane_m'}
        _object(scenario, scenario_keys, scenario_keys, 'scenario')
        if scenario['profile'] != 'periodic-pec' or scenario['polarization'] not in ('TE', 'TM'):
            raise ValueError('unsupported scenario profile or polarization')
        frequencies = scenario['frequencies_Hz']
        if not isinstance(frequencies, list) or not frequencies:
            raise ValueError('frequencies_Hz must be a nonempty list')
        frequencies = [_number(value, 'frequency', positive=True) for value in frequencies]
        if any(b <= a for a, b in zip(frequencies, frequencies[1:])):
            raise ValueError('frequencies_Hz must strictly increase')
        scenario['frequencies_Hz'] = frequencies
        scenario['theta_deg'] = _number(scenario['theta_deg'], 'theta_deg', nonnegative=True)
        if scenario['theta_deg'] > 45 or _number(scenario['azimuth_deg'], 'azimuth_deg') != 0:
            raise ValueError('supported incidence requires theta 0..45 and azimuth 0')
        scenario['azimuth_deg'] = 0.0
        scenario['air_height_m'] = _number(scenario['air_height_m'], 'air_height_m', positive=True)
        scenario['reference_plane_m'] = _number(scenario['reference_plane_m'], 'reference_plane_m', nonnegative=True)
        if scenario['reference_plane_m'] > scenario['air_height_m']:
            raise ValueError('reference plane must lie in the air interval')
        _object(case['mesh'], {'max_edge_m'}, {'max_edge_m'}, 'mesh')
        case['mesh']['max_edge_m'] = _number(case['mesh']['max_edge_m'], 'max_edge_m', positive=True)
        analysis = case['analysis']
        _object(analysis, {'band_Hz', 'max_gap_Hz', 'threshold_R'}, {'band_Hz', 'max_gap_Hz'}, 'analysis')
        band = analysis['band_Hz']
        if not isinstance(band, list) or len(band) != 2:
            raise ValueError('analysis band_Hz must contain two values')
        analysis['band_Hz'] = [_number(value, 'band frequency', positive=True) for value in band]
        if band[0] >= band[1] or band[0] < frequencies[0] or band[1] > frequencies[-1]:
            raise ValueError('analysis band must increase and lie within scenario frequencies')
        analysis['max_gap_Hz'] = _number(analysis['max_gap_Hz'], 'max_gap_Hz', positive=True)
        analysis['threshold_R'] = _number(analysis.get('threshold_R', .1), 'threshold_R', positive=True)
        if analysis['threshold_R'] > 1:
            raise ValueError('threshold_R must not exceed 1')
        materials = case['materials']
        if not isinstance(materials, dict) or not materials:
            raise ValueError('materials must be a nonempty id mapping')
        for material_id, material in materials.items():
            _identifier(material_id, 'material id')
            sample_material(material, frequencies)
            material.setdefault('conductivity_S_m', 0.0)
            material.setdefault('epsilon_includes_conductivity', False)
            material.setdefault('extrapolation', {'allowed': False})
        # Normalization also rejects unknown geometry fields and unsafe shapes.
        _object(case['geometry'], {'cell', 'regions'}, {'cell', 'regions'}, 'geometry')
        regions = case['geometry']['regions']
        if not isinstance(regions, list) or not regions:
            raise ValueError('regions must be a nonempty list')
        for region in regions:
            if not isinstance(region, dict) or not isinstance(region.get('material'), str) or region['material'] not in materials:
                raise ValueError('region material mapping is missing')
        audit_geometry(case['geometry'], base_dir)
        for region in regions:
            if region['kind'] == 'stl':
                region['path'] = str((Path(base_dir) / region['path']).resolve())
                transform = region.setdefault('transform', {})
                transform['scale'] = _number(transform.get('scale', 1), 'scale', positive=True)
                transform['translation_m'] = [_number(value, 'translation_m')
                                              for value in transform.get('translation_m', [0, 0, 0])]
        normalized.append(case)
    result = {'schema_version': '1.0', 'execution': {'mode': mode}, 'runtime': runtime}
    if mode == 'batch':
        result['execution']['confirmed'] = True
        result['cases'] = normalized
    else:
        result['case'] = normalized[0]
    return result


def build_plan(raw: dict, base_dir: Path) -> dict:
    """Prepare the same offline audits and material samples for every explicit case."""
    normalized = normalize_config(raw, base_dir)
    cases = normalized.get('cases', [normalized.get('case')])
    prepared = []
    for case in cases:
        audit = audit_geometry(case['geometry'], base_dir)
        samples = {key: sample_material(material, case['scenario']['frequencies_Hz'])
                   for key, material in case['materials'].items()}
        masses = {region['id']: (audit['volumes_m3'][region['id']] * samples[region['material']]['density_kg_m3']
                                if samples[region['material']]['mass_available'] else None)
                  for region in case['geometry']['regions']}
        identity = copy.deepcopy(case)
        for region in identity['geometry']['regions']:
            if region['kind'] == 'stl':
                region.pop('path')
                region['asset_sha256'] = audit['asset_hashes'][region['id']]
        entry = copy.deepcopy(case)
        entry.update(signature=canonical_hash(identity), geometry_audit=audit,
                     material_samples=samples, region_masses_kg=masses)
        prepared.append(entry)
    scope = 'batch' if len(prepared) > 1 else 'single'
    # Content identity excludes local paths and operational resource budgets.
    content_hash = canonical_hash({'schema_version': '1.0', 'scope_mode': scope,
                                   'case_signatures': [case['signature'] for case in prepared]})
    return {'schema_version': '1.0', 'scope_mode': scope, 'cases': prepared,
            'runtime': normalized['runtime'], 'content_hash': content_hash}
