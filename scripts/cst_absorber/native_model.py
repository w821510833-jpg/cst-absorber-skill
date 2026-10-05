"""Original native query macros and strict report readers; no SDK invocation.

Readers check consistency of supplied readback files with the requested model.
They cannot establish which engine produced a file or certify numerical physics.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
import re


_LENGTH = {'nm': 1e-9, 'um': 1e-6, 'mm': .001, 'cm': .01, 'm': 1.,
           'mil': 25.4e-6, 'in': .0254, 'ft': .3048}
_FREQUENCY = {'Hz': 1., 'kHz': 1e3, 'MHz': 1e6, 'GHz': 1e9, 'THz': 1e12, 'PHz': 1e15}
_BOUNDARIES = {'Xmin': 'unit cell', 'Xmax': 'unit cell', 'Ymin': 'unit cell',
               'Ymax': 'unit cell', 'Zmin': 'electric', 'Zmax': 'open'}
_MAX_REPORT_BYTES = 8 * 1024 * 1024
_MESH_QUERIES = {'mesh_type': 'Mesh.GetMeshType',
                 'mesh_cells': 'Mesh.GetNumberOfMeshCells',
                 'min_edge': 'Mesh.GetMinimumEdgeLength',
                 'max_edge': 'Mesh.GetMaximumEdgeLength',
                 'mesher_mode': 'Mesh.GetParallelMesherMode("Tet")',
                 'mesher_threads': 'Mesh.GetMaxParallelMesherThreads("Tet")'}


def _number(value, label, *, positive=False):
    if isinstance(value, bool):
        raise ValueError(label + ' must be finite numeric data')
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(label + ' must be finite numeric data') from error
    if not math.isfinite(result) or positive and result <= 0:
        raise ValueError(label + ' must be finite' + (' and positive' if positive else ''))
    return result


def _integer(value, label, *, positive=False):
    if not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]*', value):
        raise ValueError(label + ' must be an integer')
    result = int(value)
    if positive and result <= 0:
        raise ValueError(label + ' must be positive')
    return result


def _fmt(value):
    return format(_number(value, 'VBA numeric value'), '.15g')


def _q(value):
    value = str(value)
    if any(c in value for c in '\r\n\x00'):
        raise ValueError('VBA strings cannot contain newlines or NUL')
    return '"' + value.replace('"', '""') + '"'


def _shape(region):
    for key in ('id', 'material'):
        if not isinstance(region.get(key), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', region[key]):
            raise ValueError('region identifiers must match the shared safe identifier contract')
    return 'absorber:region_' + region['id']


def bind_artifact_text(text, artifact_root):
    """Bind only explicit portable path placeholders and reject relative escape."""
    if not isinstance(text, str):
        raise ValueError('artifact text must be a string')
    root = Path(artifact_root).resolve().as_posix()
    if any(c in root for c in '\r\n\x00'):
        raise ValueError('artifact root is invalid')
    for match in re.finditer(r'@@ARTIFACT_ROOT@@([^"\r\n]*)', text):
        suffix = match.group(1).replace('\\', '/')
        if suffix and not suffix.startswith('/'):
            raise ValueError('artifact path suffix is not relative to the owned root')
        parts = suffix.split('/')[1:]
        if any(part in ('.', '..') or ':' in part for part in parts):
            raise ValueError('artifact path traversal may escape the owned root')
    return text.replace('@@ARTIFACT_ROOT@@', root.replace('"', '""'))


def _print(tag, *expressions):
    return 'Print #fh, ' + _q(tag) + ''.join(' & vbTab & ' + expression for expression in expressions)


def model_readback_vba(case, artifact_root):
    """Generate actual unit/domain/shape/mode queries into native_model.tsv."""
    from .modal import required_modes
    expected_shape_count = len(case['geometry']['regions'])
    expected_mode_count = len(required_modes(case))
    lines = ["' Original native-model-v1 readback; this generator does not execute CST.",
             'Sub Main', 'Dim fh As Integer, i As Long, count As Long',
             'Dim xmin As Double, xmax As Double, ymin As Double, ymax As Double',
             'Dim zmin As Double, zmax As Double, ok As Boolean',
             'Dim modeName As String, shapeName As String', 'fh = FreeFile',
             'Open "@@ARTIFACT_ROOT@@/native_model.tsv" For Output As #fh',
             _print('schema', _q('native-model-v1')),
             _print('unit', _q('length'), 'Units.GetUnit("Length")'),
             _print('unit', _q('frequency'), 'Units.GetUnit("Frequency")')]
    for key, query in [('geometry_to_SI', 'GetGeometryUnitToSI'),
                       ('geometry_SI_to_unit', 'GetGeometrySIToUnit'),
                       ('frequency_to_SI', 'GetFrequencyUnitToSI'),
                       ('frequency_SI_to_unit', 'GetFrequencySIToUnit')]:
        lines.append(_print('factor', _q(key), 'Str$(Units.' + query + ')'))
    for tag, query in [('domain', 'GetCalculationBox'), ('structure', 'GetStructureBox')]:
        lines += ['Boundary.' + query + ' xmin, xmax, ymin, ymax, zmin, zmax',
                  _print(tag, *['Str$(' + name + ')' for name in ('xmin', 'xmax', 'ymin', 'ymax', 'zmin', 'zmax')])]
    lines.append(_print('lattice', 'Str$(Boundary.GetUnitCellDs1)',
                        'Str$(Boundary.GetUnitCellDs2)', 'Str$(Boundary.GetUnitCellAngle)'))
    for side in _BOUNDARIES:
        lines.append(_print('boundary', _q(side), 'Boundary.Get' + side))
    lines += ['count = Solid.GetNumberOfShapes', _print('shape_count', 'CStr(count)'),
              'If count <> ' + str(expected_shape_count) + ' Then Err.Raise 1014, "Native model readback", "Unexpected shape count"',
              'For i = 0 To count', 'shapeName = Solid.GetNameOfShapeFromIndex(i)',
              'If Len(shapeName) > 0 Then', _print('shape_name', 'CStr(i)', 'shapeName'),
              'End If', 'Next i']
    for region in case['geometry']['regions']:
        shape = _q(_shape(region))
        lines += ['ok = Solid.GetLooseBoundingBoxOfShape(' + shape + ', xmin, xmax, ymin, ymax, zmin, zmax)',
                  'If Not ok Then Err.Raise 1011, "Native model readback", "Shape bounds unavailable"',
                  _print('shape', shape, 'Solid.GetMaterialNameForShape(' + shape + ')',
                         'Str$(Solid.GetVolume(' + shape + '))',
                         *['Str$(' + name + ')' for name in ('xmin', 'xmax', 'ymin', 'ymax', 'zmin', 'zmax')])]
    lines += ['FloquetPort.Port "Zmax"', 'If FloquetPort.IsPortAtZmax Then',
              _print('mode_port', _q('Zmax')), 'ElseIf FloquetPort.IsPortAtZmin Then',
              _print('mode_port', _q('Zmin')), 'Else',
              'Err.Raise 1012, "Native model readback", "Unknown Floquet port"', 'End If',
              'count = FloquetPort.GetNumberOfModes', _print('mode_count', 'CStr(count)'),
              'If count <> ' + str(expected_mode_count) + ' Then Err.Raise 1015, "Native model readback", "Unexpected mode count"',
              'For i = 1 To count', 'ok = FloquetPort.GetModeNameByNumber(modeName, i)',
              'If Not ok Then Err.Raise 1013, "Native model readback", "Mode name unavailable"',
              _print('mode', 'CStr(i)', 'modeName'), 'Next i', 'Close #fh', 'End Sub']
    return bind_artifact_text('\n'.join(lines) + '\n', artifact_root)


def _read_records(root, filename, schema):
    root = Path(root).resolve()
    path = root / filename
    if not path.is_file():
        raise ValueError('native report missing: ' + filename)
    if path.resolve().parent != root:
        raise ValueError('native report path escapes the owned artifact root')
    if path.stat().st_size > _MAX_REPORT_BYTES:
        raise ValueError('native report exceeds the bounded parsing limit')
    try:
        raw = list(csv.reader(path.read_text(encoding='utf-8').splitlines(), delimiter='\t'))
    except (UnicodeError, csv.Error) as error:
        raise ValueError('native report is not valid UTF-8 TSV') from error
    records = [[part.strip() for part in row] for row in raw if row]
    if not records or records[0] != ['schema', schema]:
        raise ValueError('native report schema is missing or invalid')
    return records[1:], raw


def _check_length(record, count):
    if len(record) != count:
        raise ValueError('native ' + (record[0] if record else 'empty') + ' record has invalid field count')


def _once(target, key, value):
    if key in target:
        raise ValueError('duplicate native record: ' + str(key))
    target[key] = value


def _bbox(values, factor):
    values = [_number(v, 'bounding-box coordinate') * factor for v in values]
    result = [[values[0], values[2], values[4]], [values[1], values[3], values[5]]]
    if any(b <= a for a, b in zip(*result)):
        raise ValueError('native bounding box must have positive extents')
    return result


def _close(actual, expected, label, *, rel=1e-6, absolute=0):
    if not math.isclose(actual, expected, rel_tol=rel, abs_tol=absolute):
        raise ValueError('native ' + label + ' mismatch')


def _check_bbox(actual, expected, label):
    for axis in range(3):
        expected_extent = _number(expected[1][axis] - expected[0][axis],
                                  label + ' expected extent', positive=True)
        actual_extent = _number(actual[1][axis] - actual[0][axis],
                                label + ' actual extent', positive=True)
        # Normalize to each axis length, so an offset coordinate or a fixed SI
        # floor cannot conceal a thin feature. Keep the previous coordinate
        # check as an additional ceiling rather than relaxing origin accuracy.
        tolerance = expected_extent * 1e-6
        for row in range(2):
            a, e = actual[row][axis], expected[row][axis]
            _close(a, e, label, absolute=1e-10)
            _close(a, e, label, rel=0, absolute=tolerance)
        # Endpoint errors can oppose each other; check size independently.
        _close(actual_extent, expected_extent, label + ' extent')


def _units(units, factors):
    if set(units) != {'length', 'frequency'}:
        raise ValueError('native unit records are incomplete')
    required = {'geometry_to_SI', 'geometry_SI_to_unit', 'frequency_to_SI', 'frequency_SI_to_unit'}
    if set(factors) != required or units['length'] not in _LENGTH or units['frequency'] not in _FREQUENCY:
        raise ValueError('native units or factors are unsupported/incomplete')
    expected = {'geometry_to_SI': _LENGTH[units['length']],
                'geometry_SI_to_unit': 1 / _LENGTH[units['length']],
                'frequency_to_SI': _FREQUENCY[units['frequency']],
                'frequency_SI_to_unit': 1 / _FREQUENCY[units['frequency']]}
    for key in required:
        _close(factors[key], expected[key], 'unit factor ' + key, rel=1e-10, absolute=0)
    return dict(units, **factors)


def read_model_report(case, artifact_root):
    """Validate complete actual model records while retaining acceptance gaps."""
    records, raw = _read_records(artifact_root, 'native_model.tsv', 'native-model-v1')
    scalar, units, factors, boundaries, shapes, shape_indices, modes = {}, {}, {}, {}, {}, {}, {}
    for row in records:
        tag = row[0]
        if tag in ('unit', 'factor', 'boundary'):
            _check_length(row, 3)
            target = {'unit': units, 'factor': factors, 'boundary': boundaries}[tag]
            value = _number(row[2], 'unit factor', positive=True) if tag == 'factor' else row[2]
            _once(target, row[1], value)
        elif tag in ('domain', 'structure', 'lattice', 'shape_count', 'mode_count', 'mode_port'):
            expected_size = {'domain': 7, 'structure': 7, 'lattice': 4,
                             'shape_count': 2, 'mode_count': 2, 'mode_port': 2}[tag]
            _check_length(row, expected_size)
            _once(scalar, tag, row[1:])
        elif tag == 'shape_name':
            _check_length(row, 3)
            index = _integer(row[1], 'shape index')
            _once(shape_indices, index, row[2])
        elif tag == 'shape':
            _check_length(row, 10)
            _once(shapes, row[1], row[2:])
        elif tag == 'mode':
            _check_length(row, 3)
            index = _integer(row[1], 'mode index', positive=True)
            _once(modes, index, row[2])
        else:
            raise ValueError('unknown native model record: ' + tag)
    expected_scalar = {'domain', 'structure', 'lattice', 'shape_count', 'mode_count', 'mode_port'}
    if set(scalar) != expected_scalar:
        raise ValueError('native model report is missing required records')
    actual_units = _units(units, factors)
    factor = actual_units['geometry_to_SI']
    cell = case['geometry']['cell']
    domain = _bbox(scalar['domain'], factor)
    expected_domain = [[0, 0, 0], [cell['Lx_m'], cell['Ly_m'], cell['height_m'] + case['scenario']['air_height_m']]]
    _check_bbox(domain, expected_domain, 'calculation domain')
    structure = _bbox(scalar['structure'], factor)
    _check_bbox(structure, case['geometry_audit']['bbox_m'], 'structure bounds')
    lattice = [_number(v, 'lattice value', positive=True) for v in scalar['lattice']]
    _close(lattice[0] * factor, cell['Lx_m'], 'lattice Lx')
    _close(lattice[1] * factor, cell['Ly_m'], 'lattice Ly')
    _close(lattice[2], 90, 'lattice angle')
    if boundaries != _BOUNDARIES:
        raise ValueError('native periodic-PEC boundaries mismatch')
    expected_shapes = {_shape(region): region for region in case['geometry']['regions']}
    count = _integer(scalar['shape_count'][0], 'shape count', positive=True)
    if count != len(expected_shapes) or set(shapes) != set(expected_shapes):
        raise ValueError('native shape count/name mismatch')
    if any(index > count for index in shape_indices):
        raise ValueError('native shape query index mismatch')
    if len(shape_indices) != count or len(set(shape_indices.values())) != count or set(shape_indices.values()) != set(expected_shapes):
        raise ValueError('native actual shape-name enumeration mismatch')
    actual_shapes = []
    for name, values in shapes.items():
        region = expected_shapes[name]
        if values[0] != 'mat_' + region['material']:
            raise ValueError('native shape material mapping mismatch')
        volume = _number(_number(values[1], 'shape volume', positive=True) * factor ** 3,
                         'converted shape volume', positive=True)
        expected_volume = _number(case['geometry_audit']['volumes_m3'][region['id']],
                                  'expected shape volume', positive=True)
        # The dimensionless 10 ppm consistency budget scales with volume;
        # there is no absolute cubic-metre allowance or minimum model scale.
        _close(volume, expected_volume, 'shape volume', rel=1e-5)
        bbox = _bbox(values[2:], factor)
        _check_bbox(bbox, case['geometry_audit']['region_bboxes_m'][region['id']], 'shape bounds')
        actual_shapes.append({'name': name, 'material': values[0], 'volume_m3': volume, 'bbox_m': bbox})
    mode_count = _integer(scalar['mode_count'][0], 'mode count', positive=True)
    if scalar['mode_port'] != ['Zmax'] or mode_count != len(modes) or set(modes) != set(range(1, mode_count + 1)):
        raise ValueError('native Floquet port/count/index mismatch')
    actual_modes = []
    descriptors = set()
    for index, name in sorted(modes.items()):
        match = re.fullmatch(r'(TE|TM)\((-?[0-9]+),\s*(-?[0-9]+)\)', name)
        if match is None:
            raise ValueError('native Floquet mode name is unsupported')
        pol, m, n = match[1], int(match[2]), int(match[3])
        descriptor = (m, n, pol)
        if descriptor in descriptors:
            raise ValueError('duplicate native physical mode')
        descriptors.add(descriptor)
        actual_modes.append({'index': index, 'name': name, 'm': m, 'n': n, 'polarization': pol, 'port': 'Zmax'})
    from .modal import required_modes
    required = {(mode['m'], mode['n'], mode['polarization']) for mode in required_modes(case)}
    if descriptors != required:
        raise ValueError('native Floquet physical mode coverage mismatch')
    return {'schema_version': '1.0', 'status': 'model_report_validated',
            'native_validation': 'not_established', 'numerically_qualified': False,
            'evidence_kind': 'readback_file', 'source_file': 'native_model.tsv', 'raw_records': raw,
            'units': actual_units, 'domain_m': domain, 'structure_bbox_m': structure,
            'boundaries': boundaries, 'lattice': {'Lx_m': lattice[0] * factor, 'Ly_m': lattice[1] * factor, 'angle_deg': lattice[2]},
            'shape_indices': [{'index': i, 'name': name} for i, name in sorted(shape_indices.items())],
            'shapes': actual_shapes, 'modes': actual_modes, 'modes_port': 'Zmax',
            'readback_limitations': {
                'reference_plane_readback': {
                    'status': 'unsupported_public_getter', 'port': 'Zmax',
                    'requested_zref_m': cell['height_m'] + case['scenario']['reference_plane_m'],
                    'requested_deembedding_distance_m': case['scenario']['reference_plane_m'] - case['scenario']['air_height_m'],
                    'configured_value_verified': False, 'phase_benchmark_verified': False,
                    'documentation': 'CST Studio Suite 2025 public VBA FloquetPort object',
                    'reason': 'SetDistanceToReferencePlane is documented; a corresponding distance getter is not documented.'},
                'solver_cpu_enforcement': {
                    'status': 'unsupported_public_getter', 'runtime_enforcement_verified': False,
                    'documentation': 'CST Studio Suite 2025 public VBA FDSolver object',
                    'reason': 'MaxCPUs and MaximumNumberOfCPUDevices configure resources; no corresponding CPU getter or actual-use query is documented.'}},
            'unresolved_gates': ['material_fit_readback', 'reference_plane_readback', 'solver_cpu_enforcement']}


def resource_mesh_vba(case, runtime, report):
    """Apply requested resources and nominal tet sizing, without a hard edge guarantee."""
    from .contracts import resolve_mesh_policy
    cpus = runtime.get('max_cpus')
    if isinstance(cpus, bool) or not isinstance(cpus, int) or cpus <= 0:
        raise ValueError('max_cpus must be a positive integer')
    edge = resolve_mesh_policy(case['mesh'])['target_edge_m']
    domain = report['domain_m']
    diagonal = math.sqrt(sum((b - a) ** 2 for a, b in zip(*domain)))
    ratio = _number(diagonal / edge, 'domain diagonal to mesh-edge ratio', positive=True)
    steps = math.ceil(ratio)
    factor = _number(report['units']['geometry_SI_to_unit'], 'geometry conversion', positive=True)
    lines = ["' Nominal size controls allow quality-related edge variation; independent acceptance requires readback.",
             'With FDSolver', ' .UseParallelization "True"', ' .MaxCPUs ' + _q(cpus),
             ' .MaximumNumberOfCPUDevices "1"', 'End With',
             'Mesh.SetParallelMesherMode "Tet", "user-defined"',
             'Mesh.SetMaxParallelMesherThreads "Tet", ' + _q(cpus),
             'Mesh.MinimumStepNumberTet ' + _q(_fmt(steps)),
             'Mesh.DelaunayPropagationFactor "1"']
    for region in case['geometry']['regions']:
        lines.append('Solid.SetMeshStepwidthTet ' + _q(_shape(region)) + ', ' + _q(_fmt(edge * factor)))
    return '\n'.join(lines) + '\n'


def mesh_readback_vba(artifact_root):
    """Query selected mesh and configured mesher values without assigning a unit."""
    lines = ["' GetMaximumEdgeLength's result unit/freshness remains a declared acceptance gap.",
             'Sub Main', 'Dim fh As Integer', 'fh = FreeFile',
             'Open "@@ARTIFACT_ROOT@@/native_mesh.tsv" For Output As #fh',
             _print('schema', _q('native-mesh-v1')),
             _print('unit', _q('length'), 'Units.GetUnit("Length")'),
             _print('factor', _q('geometry_to_SI'), 'Str$(Units.GetGeometryUnitToSI)'),
             _print('mesh_type', 'Mesh.GetMeshType'),
             _print('mesh_cells', 'CStr(Mesh.GetNumberOfMeshCells)'),
             _print('min_edge', 'Str$(Mesh.GetMinimumEdgeLength)'),
             _print('max_edge', 'Str$(Mesh.GetMaximumEdgeLength)'),
             _print('mesher_mode', 'Mesh.GetParallelMesherMode("Tet")'),
             _print('mesher_threads', 'CStr(Mesh.GetMaxParallelMesherThreads("Tet"))')]
    for key, getter in _MESH_QUERIES.items():
        lines.append(_print('source', _q(key), _q(getter)))
    lines += ['Close #fh', 'End Sub']
    return bind_artifact_text('\n'.join(lines) + '\n', artifact_root)


def read_mesh_report(case, root, model_report):
    """Return checked diagnostics, including acceptance failure, under a unit assumption.

    Invalid readback data raises. A valid longest-edge overshoot is retained in
    the report so the caller can save evidence before applying its failure gate.
    """
    from .contracts import resolve_mesh_policy
    records, raw = _read_records(root, 'native_mesh.tsv', 'native-mesh-v1')
    values, sources = {}, {}
    for row in records:
        tag = row[0]
        if tag in ('unit', 'factor'):
            _check_length(row, 3)
            if (tag, row[1]) not in (('unit', 'length'), ('factor', 'geometry_to_SI')):
                raise ValueError('unknown native mesh unit/factor record')
            _once(values, tag, row[2])
        elif tag in _MESH_QUERIES:
            _check_length(row, 2)
            _once(values, tag, row[1])
        elif tag == 'source':
            _check_length(row, 3)
            _once(sources, row[1], row[2])
        else:
            raise ValueError('unknown native mesh record')
    if set(values) != {'unit', 'factor', *_MESH_QUERIES}:
        raise ValueError('native mesh report is incomplete')
    if sources != _MESH_QUERIES:
        raise ValueError('native mesh getter provenance is missing or unsupported')
    if values['unit'] != model_report['units']['length']:
        raise ValueError('native mesh project-unit record mismatch')
    factor = _number(values['factor'], 'mesh geometry unit factor', positive=True)
    _close(factor, model_report['units']['geometry_to_SI'], 'mesh unit factor', rel=1e-10, absolute=0)
    if values['mesher_mode'] != 'user-defined':
        raise ValueError('native mesher thread mode mismatch')
    if values['mesh_type'] != 'Tetrahedral':
        raise ValueError('native selected mesh type mismatch')
    mesh_cells = _integer(values['mesh_cells'], 'mesh cell count', positive=True)
    threads = _integer(values['mesher_threads'], 'mesher threads', positive=True)
    expected_threads = case.get('runtime', {}).get('max_cpus')
    if expected_threads is not None and threads != expected_threads:
        raise ValueError('native mesher thread count mismatch')
    raw_edge = _number(values['max_edge'], 'maximum mesh edge', positive=True)
    min_edge = _number(values['min_edge'], 'minimum mesh edge', positive=True)
    if min_edge > raw_edge:
        raise ValueError('native minimum mesh edge exceeds maximum edge')
    converted = _number(raw_edge * factor, 'converted maximum mesh edge', positive=True)
    policy = resolve_mesh_policy(case['mesh'])
    target, limit = policy['target_edge_m'], policy['acceptance_max_edge_m']
    target_exceeded = converted > target * (1 + 1e-6)
    within_limit = converted <= limit * (1 + 1e-6) if limit is not None else None
    acceptance_failed = within_limit is False
    acceptance_status = ('not_requested' if limit is None else
                         'failed_under_unit_assumption' if acceptance_failed else
                         'within_limit_under_unit_assumption')
    return {'schema_version': '1.0', 'status': 'mesh_report_checked_with_unit_assumption',
            'source_file': 'native_mesh.tsv', 'raw_records': raw, 'evidence_kind': 'readback_file',
            'max_edge_raw': raw_edge, 'project_length_unit': values['unit'],
            'min_edge_raw': min_edge, 'mesh_type': values['mesh_type'], 'mesh_cells': mesh_cells,
            'query_provenance': {key: {'getter': getter,
                'documentation': 'CST Studio Suite 2025 public VBA Mesh object',
                'producer_verified': False} for key, getter in sources.items()},
            'assumed_geometry_to_SI': factor,
            'unit_assumption': 'GetMaximumEdgeLength is assumed to use the project length unit; not established by public API evidence.',
            'max_edge_m_under_assumption': converted, 'requested_max_edge_m': limit,
            'target_edge_m': target, 'acceptance_max_edge_m': limit,
            'mesh_policy_origin': policy['policy_origin'],
            'sizing_target_semantics': 'Nominal unstructured cell sizing; mesh quality adjustments may produce larger edges.',
            'target_exceeded_under_assumption': target_exceeded,
            'measurement_acceptance_required': limit is not None,
            'measurement_acceptance_failed': acceptance_failed,
            'measurement_acceptance': {'criterion': 'selected_mesh_longest_edge',
                'status': acceptance_status, 'accepted': False, 'limit_m': limit,
                'comparison_relative_tolerance': 1e-6,
                'unit_confirmed': False, 'freshness_confirmed': False},
            'edge_within_limit_under_assumption': within_limit, 'mesh_unit_confirmed': False,
            'mesh_freshness_confirmed': False,
            'strict_mesher_size_guarantee': False, 'mesher_mode': values['mesher_mode'],
            'mesher_threads': threads, 'mesher_threads_evidence': 'configured_process_count',
            'mesher_thread_enforcement_verified': False, 'solver_cpu_enforcement_verified': False,
            'native_validation': 'not_established', 'numerically_qualified': False,
            'unresolved_gates': ['mesh_edge_unit', 'mesh_edge_freshness',
                                 'mesher_thread_enforcement', 'solver_cpu_enforcement']}
