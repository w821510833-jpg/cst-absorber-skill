"""SI volume auditing of bricks and closed STL assets without solver execution."""
import hashlib
import math
import re
from pathlib import Path


def _number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label} must be a finite scalar number')
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or positive and value <= 0:
        raise ValueError(f'{label} must be finite' + (' and positive' if positive else ''))
    return float(value)


def _object(value, allowed, required, label):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError(f'{label} has missing or unsupported fields')


def _vector(value, label):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f'{label} must contain three coordinates')
    return [_number(item, label) for item in value]


def audit_geometry(geometry: dict, base_dir: Path) -> dict:
    """Audit volumes, bounds and overlaps; intersecting STL bboxes stay unverified.

    The cell origin is [0,0,0]. No boolean intersection package is required.
    This audit never certifies a nontrivial STL intersection as disjoint.
    """
    _object(geometry, {'cell', 'regions'}, {'cell', 'regions'}, 'geometry')
    cell = geometry['cell']
    _object(cell, {'Lx_m', 'Ly_m', 'height_m'}, {'Lx_m', 'Ly_m', 'height_m'}, 'cell')
    upper = [_number(cell[key], key, positive=True) for key in ('Lx_m', 'Ly_m', 'height_m')]
    regions = geometry['regions']
    if not isinstance(regions, list) or not regions:
        raise ValueError('regions must be a nonempty list')
    volumes, bboxes, hashes, kinds = {}, {}, {}, {}
    for region in regions:
        _object(region, {'id', 'material', 'kind', 'bounds_m', 'path', 'source_unit', 'transform'},
                {'id', 'material', 'kind'}, 'region')
        region_id = region['id']
        if not isinstance(region_id, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', region_id) is None or region_id in volumes:
            raise ValueError('region ids must be safe and unique')
        if not isinstance(region['material'], str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', region['material']) is None:
            raise ValueError('region needs a safe material id')
        kind = region['kind']
        if kind == 'brick':
            _object(region, {'id', 'material', 'kind', 'bounds_m'}, {'id', 'material', 'kind', 'bounds_m'}, 'brick')
            bounds = region['bounds_m']
            if not isinstance(bounds, list) or len(bounds) != 2:
                raise ValueError('brick bounds need lower and upper coordinates')
            bbox = [_vector(row, 'brick bounds') for row in bounds]
            if any(b <= a for a, b in zip(*bbox)):
                raise ValueError('brick must have positive extent on every axis')
            volume = math.prod(b - a for a, b in zip(*bbox))
        elif kind == 'stl':
            _object(region, {'id', 'material', 'kind', 'path', 'source_unit', 'transform'},
                    {'id', 'material', 'kind', 'path', 'source_unit'}, 'STL region')
            unit = region['source_unit']
            if not isinstance(unit, str) or unit not in {'m', 'mm', 'cm'}:
                raise ValueError('STL source_unit must be m, mm or cm')
            transform = region.get('transform', {})
            _object(transform, {'scale', 'translation_m'}, set(), 'STL transform')
            scale = _number(transform.get('scale', 1), 'scale', positive=True)
            translation = _vector(transform.get('translation_m', [0, 0, 0]), 'translation')
            if not isinstance(region['path'], str) or not region['path'].strip():
                raise ValueError('STL path must be a nonempty string')
            path = (Path(base_dir) / region['path']).resolve()
            if path.suffix.lower() != '.stl' or not path.is_file():
                raise ValueError('STL file is missing or has unsupported format')
            hashes[region_id] = hashlib.sha256(path.read_bytes()).hexdigest()
            import trimesh
            mesh = trimesh.load_mesh(path, file_type='stl', process=True)
            if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0 or not mesh.is_watertight or not mesh.is_winding_consistent:
                raise ValueError('STL must be a closed consistently oriented volume')
            if len(mesh.split(only_watertight=False)) != 1:
                raise ValueError('multipart or nested STL shells are unsupported; use separate regions')
            factor = {'m': 1, 'mm': .001, 'cm': .01}[unit] * scale
            mesh.apply_scale(factor)
            mesh.apply_translation(translation)
            bbox = mesh.bounds.tolist()
            volume = float(mesh.volume)
            if not math.isfinite(volume) or volume <= 0 or not all(math.isfinite(v) for row in bbox for v in row):
                raise ValueError('STL must have a finite positive oriented volume')
        else:
            raise ValueError('unsupported region kind')
        tolerance = max(upper) * 1e-9
        if any(lo < -tolerance or hi > limit + tolerance for lo, hi, limit in zip(*bbox, upper)):
            raise ValueError('region lies outside the cell')
        if not math.isfinite(volume) or volume <= 0:
            raise ValueError('region volume must be finite and positive')
        volumes[region_id], bboxes[region_id], kinds[region_id] = volume, bbox, kind
    unverified = []
    ids = list(volumes)
    for index, first in enumerate(ids):
        for second in ids[index + 1:]:
            overlap = all(min(bboxes[first][1][axis], bboxes[second][1][axis]) >
                          max(bboxes[first][0][axis], bboxes[second][0][axis]) for axis in range(3))
            if overlap:
                if kinds[first] == kinds[second] == 'brick':
                    raise ValueError('brick regions overlap')
                unverified.append([first, second])
    bbox = [[min(box[0][axis] for box in bboxes.values()) for axis in range(3)],
            [max(box[1][axis] for box in bboxes.values()) for axis in range(3)]]
    return {'volumes_m3': volumes, 'bbox_m': bbox, 'region_bboxes_m': bboxes,
            'coverage_limits_m': [[0.0, 0.0, 0.0], upper], 'asset_hashes': hashes,
            'stl_self_intersection_status': 'unverified' if hashes else 'not_applicable',
            'overlap_status': 'unverified' if unverified else 'verified_no_overlap',
            'unverified_intersections': unverified}
