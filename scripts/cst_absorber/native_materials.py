"""Original formal material queries and fail-closed offline report validation.

No CST module is imported and no query is executed here. A supplied file can
establish parameter consistency, but cannot prove its producer or solver use.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re
import stat

from .native_model import _number, _print, _q, _shape, bind_artifact_text


_SCHEMA = 'native-materials-v1'
_FILENAME = 'native_materials.tsv'
_MAX_BYTES = 8 * 1024 * 1024
_QUERIES = {'material_binding': 'Solid.GetMaterialNameForShape',
            'conductivity_xyz': 'Material.GetSigma', 'density': 'Material.GetRho'}
_UNITS = {'conductivity': 'S/m', 'density': 'kg/m3'}
_UNIT_METHODS = {'conductivity': 'Material.Sigma/SigmaX/SigmaY/SigmaZ',
                 'density': 'Material.Rho'}
_MATERIAL_DOC = ('CST Studio Suite 2025 public VBA Material Object; '
                 'mergedProjects/VBA_3D/special_vbalayer/special_vbalayerolayer_object.htm')


def _request(case, operation_id):
    if (not isinstance(operation_id, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', operation_id)):
        raise ValueError('material readback operation_id must be a safe nonempty identifier')
    if (not isinstance(case, dict) or not isinstance(case.get('signature'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', case['signature'])):
        raise ValueError('material readback requires the current case signature')
    regions = case.get('geometry', {}).get('regions')
    if not isinstance(regions, list) or not regions:
        raise ValueError('material readback requires complete case regions')
    expected = {}
    for region in regions:
        shape = _shape(region)
        if shape in expected:
            raise ValueError('material readback has duplicate case shapes')
        expected[shape] = region['material']
    return expected


def material_readback_vba(case, artifact_root, *, operation_id):
    """Generate fixed documented GetSigma/GetRho calls; never invoke CST."""
    expected = _request(case, operation_id)
    if any(ord(c) < 32 for c in str(artifact_root)):
        raise ValueError('artifact root contains unsupported control characters')
    lines = ["' Original native-materials-v1 parameter readback; not solver-use evidence.",
             'Sub Main', 'Dim fh As Integer, actualMaterial As String',
             'Dim sigmaX As Double, sigmaY As Double, sigmaZ As Double, rho As Double',
             'fh = FreeFile',
             'Open "@@ARTIFACT_ROOT@@/native_materials.tsv" For Output As #fh',
             _print('schema', _q(_SCHEMA)),
             _print('case_signature', _q(case['signature'])),
             _print('operation_id', _q(operation_id)),
             _print('shape_count', _q(len(expected)))]
    for key, unit in _UNITS.items():
        lines.append(_print('unit', _q(key), _q(unit)))
    for key, method in _UNIT_METHODS.items():
        lines.append(_print('unit_source', _q(key), _q(method)))
    for key, getter in _QUERIES.items():
        lines.append(_print('source', _q(key), _q(getter)))
    for shape in expected:
        lines += ['actualMaterial = Solid.GetMaterialNameForShape(' + _q(shape) + ')',
                  'If Len(actualMaterial) = 0 Then Err.Raise 1016, "Native material readback", "Actual material unavailable"',
                  'Material.GetSigma actualMaterial, sigmaX, sigmaY, sigmaZ',
                  'Material.GetRho actualMaterial, rho',
                  _print('material', _q(shape), 'actualMaterial', 'Str$(sigmaX)',
                         'Str$(sigmaY)', 'Str$(sigmaZ)', 'Str$(rho)')]
    lines += ['Close #fh', 'End Sub']
    return bind_artifact_text('\n'.join(lines) + '\n', artifact_root)


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            getattr(value, 'st_birthtime_ns', value.st_ctime_ns), value.st_nlink,
            getattr(value, 'st_file_attributes', 0))


def _alias(value):
    return (stat.S_ISLNK(value.st_mode)
            or bool(getattr(value, 'st_file_attributes', 0) & 0x400))


def _root_chain(root):
    if '..' in root.parts:
        raise ValueError('material report root traversal is unsupported')
    chain = []
    for node in [*reversed(root.parents), root]:
        value = node.lstat()
        if _alias(value) or not stat.S_ISDIR(value.st_mode):
            raise ValueError('material report root ancestor alias/reparse link is unsupported')
        # Sibling creation can update a parent's mtime/size. Its directory
        # identity and reparse status must stay fixed, not unrelated contents.
        chain.append((node, value.st_dev, value.st_ino, value.st_mode,
                      getattr(value, 'st_birthtime_ns', None),
                      getattr(value, 'st_file_attributes', 0)))
    return chain


def _read_report(artifact_root):
    """Read one bounded regular file with identity checks around the read."""
    root = Path(artifact_root).absolute()
    try:
        original_root = root
        chain = _root_chain(original_root)
        root_before = root.lstat()
        if _alias(root_before):
            raise ValueError('material report root alias is unsupported')
        # Windows may resolve an ordinary 8.3 path to its long spelling. Check
        # the directory identity rather than treating that spelling as a link.
        root = root.resolve()
        if _stamp(root_before) != _stamp(root.lstat()):
            raise ValueError('material report root identity changed')
        path = root / _FILENAME
        before = path.lstat()
        if _alias(before) or before.st_nlink != 1 or not stat.S_ISREG(before.st_mode):
            raise ValueError('material report must be a regular file without alias links')
        if before.st_size > _MAX_BYTES:
            raise ValueError('material report exceeds the bounded parsing limit')
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if _stamp(before) != _stamp(opened):
                raise ValueError('material report identity changed before read')
            data = stream.read(_MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(data) > _MAX_BYTES or len(data) != before.st_size
                or _stamp(opened) != _stamp(after) or opened.st_ctime_ns != after.st_ctime_ns
                or _stamp(before) != _stamp(path.lstat())
                or _stamp(root_before) != _stamp(root.lstat())
                or chain != _root_chain(original_root)):
            raise ValueError('material report changed during bounded stable read')
    except OSError as error:
        raise ValueError('native material report missing or unreadable') from error
    try:
        text = data.decode('utf-8')
        if '\x00' in text:
            raise ValueError('native material report contains NUL')
        # VBA Print writes literal TSV; CSV quote/unescape semantics could
        # normalize a forged multiline nonce or parameter into a matching one.
        lines = text.split('\n')
        if lines[-1] == '':
            lines.pop()
        raw = []
        for line in lines:
            line = line.removesuffix('\r')
            if any(ord(c) < 32 and c != '\t' for c in line):
                raise ValueError('native material report contains unsupported control characters')
            raw.append(line.split('\t'))
    except UnicodeError as error:
        raise ValueError('native material report is not valid UTF-8 TSV') from error
    if not raw or raw[0] != ['schema', _SCHEMA] or any(not row for row in raw):
        raise ValueError('native material report schema or records are invalid')
    return raw[1:], raw, hashlib.sha256(data).hexdigest()


def _once(target, key, value):
    if key in target:
        raise ValueError('duplicate native material record: ' + str(key))
    target[key] = value


def _model_shapes(model_report, expected, signature):
    if (not isinstance(model_report, dict)
            or model_report.get('status') != 'model_report_validated'):
        raise ValueError('validated actual model report is required for material readback')
    if model_report.get('case_signature', signature) != signature:
        raise ValueError('native material/model case signature mismatch')
    actual = {}
    shapes = model_report.get('shapes')
    if not isinstance(shapes, list):
        raise ValueError('actual model shape/material identity is missing')
    for shape in shapes:
        if not isinstance(shape, dict) or not isinstance(shape.get('name'), str):
            raise ValueError('actual model shape/material identity is invalid')
        _once(actual, shape['name'], shape.get('material'))
    if actual != {shape: 'mat_' + material for shape, material in expected.items()}:
        raise ValueError('actual model shape/material mapping mismatch')
    return actual


def read_material_report(case, artifact_root, model_report, *, operation_id):
    """Validate actual parameter records without establishing producer/solver use."""
    expected = _request(case, operation_id)
    actual_shapes = _model_shapes(model_report, expected, case['signature'])
    records, raw, digest = _read_report(artifact_root)
    scalar, units, unit_sources, sources, states = {}, {}, {}, {}, {}
    for row in records:
        tag = row[0]
        if tag in ('case_signature', 'operation_id', 'shape_count'):
            if len(row) != 2:
                raise ValueError('native material scalar has invalid field count')
            _once(scalar, tag, row[1])
        elif tag in ('unit', 'unit_source', 'source'):
            if len(row) != 3:
                raise ValueError('native material source/unit has invalid field count')
            _once({'unit': units, 'unit_source': unit_sources, 'source': sources}[tag], row[1], row[2])
        elif tag == 'material':
            if len(row) != 7:
                raise ValueError('native material state has invalid field count')
            _once(states, row[1], row[2:])
        else:
            raise ValueError('unknown native material record: ' + tag)
    if set(scalar) != {'case_signature', 'operation_id', 'shape_count'}:
        raise ValueError('native material report is incomplete')
    if scalar['case_signature'] != case['signature'] or scalar['operation_id'] != operation_id:
        raise ValueError('native material case signature or operation nonce mismatch')
    if (not re.fullmatch(r'[1-9][0-9]*', scalar['shape_count'])
            or int(scalar['shape_count']) != len(expected) or set(states) != set(expected)):
        raise ValueError('native material actual shape coverage/count mismatch')
    if sources != _QUERIES or units != _UNITS or unit_sources != _UNIT_METHODS:
        raise ValueError('native material getter or unit provenance is missing/unsupported')
    materials = []
    shared = {}
    for shape, material_id in expected.items():
        values = states[shape]
        if values[0] != actual_shapes[shape]:
            raise ValueError('native material actual shape material mapping mismatch')
        sigma = [_number(value, 'native conductivity') for value in values[1:4]]
        rho = _number(values[4], 'native density')
        sample = case.get('material_samples', {}).get(material_id)
        if not isinstance(sample, dict):
            raise ValueError('prepared material samples are required')
        native_sigma = _number(sample.get('conductivity_S_m'), 'prepared native conductivity')
        if native_sigma != 0 or any(value != native_sigma for value in sigma):
            raise ValueError('native conductivity must be zero on all axes because loss is included in epsilon')
        expected_rho = sample.get('density_kg_m3')
        matches = None
        if expected_rho is not None:
            expected_rho = _number(expected_rho, 'prepared density', positive=True)
            if rho <= 0 or not math.isclose(rho, expected_rho, rel_tol=1e-6, abs_tol=0):
                raise ValueError('native material density mismatch')
            matches = True
        observation = (sigma, rho)
        if material_id in shared and shared[material_id] != observation:
            raise ValueError('inconsistent native parameters for a shared actual material')
        shared[material_id] = observation
        materials.append({
            'shape': shape, 'material_id': material_id, 'actual_material_name': values[0],
            'conductivity': {'raw_xyz': sigma, 'unit': 'S/m', 'value_xyz_S_m': sigma,
                             'expected_xyz_S_m': [native_sigma] * 3, 'matches_prepared': True,
                             'unit_documented': True,
                             'unit_source': {'method': _UNIT_METHODS['conductivity'],
                                             'documentation': _MATERIAL_DOC, 'lines': [390, 407]},
                             'conductivity_treatment': 'included_in_epsilon'},
            'density': {'raw': rho, 'unit': 'kg/m3', 'value_kg_m3': rho,
                        'expected_kg_m3': expected_rho, 'matches_requested': matches,
                        'status': 'matches_requested' if matches else 'observed_unrequested',
                        'unit_documented': True,
                        'unit_source': {'method': _UNIT_METHODS['density'],
                                        'documentation': _MATERIAL_DOC, 'lines': [288, 289]},
                        'relative_tolerance': 1e-6, 'absolute_tolerance': 0.0,
                        'mass_qualified': False},
            'getter_provenance': {key: {'getter': getter, 'producer_verified': False}
                                  for key, getter in _QUERIES.items()}})
    limitations = {
        'fd_material_policy_readback': {
            'status': 'unsupported_public_getter', 'getter': None, 'observed_value': None,
            'documented_setter': 'FDSolver.TDCompatibleMaterials',
            'reason': 'The public FDSolver object documents the setter, without a material-policy getter.'},
        'fd_material_source_readback': {
            'status': 'unsupported_public_getter', 'getter': None, 'observed_value': None,
            'reason': 'No formal public getter binding an FD material response source to this solver run is established.'},
        'reference_plane_readback': {
            'status': 'unsupported_public_getter', 'getter': None, 'observed_value': None,
            'documented_setter': 'FloquetPort.SetDistanceToReferencePlane',
            'reason': 'The public FloquetPort object documents the setter, without a reference-plane distance getter.'}}
    return {'schema_version': 'cst-material-readback/1', 'status': 'material_report_validated',
            'case_signature': case['signature'], 'operation_id': operation_id,
            'source_file': _FILENAME, 'source_sha256': digest, 'raw_records': raw,
            'evidence_kind': 'readback_file', 'producer_verified': False,
            'parameter_evidence': 'readback_consistency_validated', 'materials': materials,
            'native_validation': 'not_established', 'native_acceptance': 'not_run',
            'solver_response_linkage_verified': False, 'numerically_qualified': False,
            'physical_accepted': False, 'readback_limitations': limitations,
            'unresolved_gates': ['material_solver_response_linkage', 'material_fit_readback',
                                 'fd_material_policy_readback', 'fd_material_source_readback',
                                 'reference_plane_readback']}
