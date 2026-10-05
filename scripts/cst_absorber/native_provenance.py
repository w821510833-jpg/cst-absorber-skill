"""Associate actual observations without inventing missing native identifiers.

An invocation association is weaker than an authenticated curve-to-solver or
material-source link. No CST module is imported and no native gates are cleared.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .contracts import canonical_hash


def _path_guards(path):
    guards = []
    for component in (path, *path.parents):
        value = component.lstat()
        if (stat.S_ISLNK(value.st_mode) or
                getattr(value, 'st_file_attributes', 0) & 0x400):
            raise ValueError('evidence path is linked or reparse aliased')
        guards.append((str(component), value.st_dev, value.st_ino))
    return guards


def _snapshot(path, *, capture_limit=None, checkpoint=None):
    check = checkpoint if checkpoint is not None else lambda: None
    check()
    path = Path(path).absolute()
    guards = _path_guards(path)
    digest = hashlib.sha256()
    chunks = [] if capture_limit is not None else None
    fields = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    with path.open('rb') as source:
        check()
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError('evidence file is not regular and unaliased')
        if capture_limit is not None and before.st_size > capture_limit:
            raise ValueError('input evidence exceeds size bound')
        total = 0
        for block in iter(lambda: source.read(1024*1024), b''):
            check()
            total += len(block)
            if capture_limit is not None and total > capture_limit:
                raise ValueError('input evidence exceeds size bound')
            digest.update(block)
            check()
            if chunks is not None:
                chunks.append(block)
        check()
        after = os.fstat(source.fileno())
        current = path.stat()
        if (not stat.S_ISREG(after.st_mode) or after.st_nlink != 1 or
                not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or
                fields(before) != fields(after) or fields(after) != fields(current) or
                before.st_ctime_ns != after.st_ctime_ns or guards != _path_guards(path)):
            raise ValueError('evidence file changed or became aliased during snapshot')
    snapshot = {'sha256': digest.hexdigest(), 'identity': list(fields(before))}
    return (snapshot, b''.join(chunks)) if chunks is not None else snapshot


def make_solver_binding(case, artifact_root, solver_info, *, completion=None,
                        process_identity=None, evidence_kind='unknown_transport', checkpoint=None):
    """Bind a retained SDK observation to this immutable case and invocation.

    Public get_solver_run_info documents a dictionary but no native run-id
    schema. A SUCCESS value therefore cannot populate a native curve run id.
    """
    if checkpoint is not None:
        checkpoint()
    original_root = Path(artifact_root).absolute()
    _path_guards(original_root)
    root = original_root.resolve(strict=True)
    signature = case.get('signature')
    if not isinstance(signature, str) or re.fullmatch(r'[0-9a-f]{64}', signature) is None:
        raise ValueError('case signature is invalid')
    if not isinstance(solver_info, dict):
        raise ValueError('actual solver information must be a dictionary')
    completion = copy.deepcopy(completion or {})
    gates = ['native_solver_curve_run_linkage', 'material_solver_linkage',
             'fd_material_policy_readback', 'reference_plane_readback']
    inputs = root/'inputs.json'
    input_snapshot = {'status':'missing', 'matches_prepared_case':None}
    if inputs.exists():
        input_snapshot, input_bytes = _snapshot(inputs, capture_limit=1024*1024, checkpoint=checkpoint)
        observed = json.loads(input_bytes.decode('utf-8'))
        if canonical_hash(observed) != canonical_hash(case):
            raise ValueError('immutable input snapshot differs from prepared case')
        if _snapshot(inputs, checkpoint=checkpoint) != input_snapshot:
            raise ValueError('input snapshot changed during case comparison')
        input_snapshot.update(status='observed', matches_prepared_case=True)
    else:
        gates.append('immutable_input_association')
    archive = root/'model.cst'
    archive_snapshot = _snapshot(archive, checkpoint=checkpoint)
    expected_digest = completion.get('archive_integrity', {}).get('sha256')
    if expected_digest is not None and archive_snapshot['sha256'] != expected_digest:
        raise ValueError('archive digest differs from completed operation pin')
    if not expected_digest:
        gates.append('archive_completion_binding')
    operation_id = completion.get('operation_id')
    if operation_id is not None and (not isinstance(operation_id, str) or
                                    re.fullmatch(r'[0-9a-f]{32}', operation_id) is None):
        raise ValueError('completion operation identity is invalid')
    if completion.get('completion_method') != 'Model3D.run_solver':
        gates.append('postprocessing_completion')
    binding = {
        'schema_version':'cst-solver-binding/1', 'case_id':case['id'],
        'case_signature':signature, 'project_path':str(archive),
        'execution_operation_id':operation_id, 'evidence_kind':evidence_kind,
        'process_identity':copy.deepcopy(process_identity), 'solver_info':copy.deepcopy(solver_info),
        'solver_info_source':'Model3D.get_solver_run_info',
        'completion_method':completion.get('completion_method'),
        'input_snapshot':input_snapshot, 'archive_snapshot':archive_snapshot,
        'native_run_id':None, 'native_run_linkage_verified':False,
        'solver_material_linkage_verified':False, 'actual_fd_policy':None,
        'actual_reference_plane_m':None, 'unresolved_gates':gates,
        'association_scope':'same_owned_invocation_not_native_curve_run_authentication',
        'native_acceptance':'not_run', 'numerically_qualified':False, 'physical_certification':False}
    runtime = root/'runtime.json'
    binding['runtime_snapshot'] = _snapshot(runtime, checkpoint=checkpoint) if runtime.exists() else None
    return binding


def bind_raw_provenance(raw, binding, case, *, checkpoint=None):
    """Retain curve identities and source roles; no configured value is a getter."""
    from .native_results import _material_identity
    if checkpoint is not None:
        checkpoint()
    if binding.get('case_signature') != case.get('signature'):
        raise ValueError('solver binding belongs to a different prepared case')
    expected_project = Path(binding['project_path']).absolute()
    actual_path = raw.get('project_path')
    same_project = (isinstance(actual_path,str) and bool(actual_path)
                    and Path(actual_path).is_absolute() and Path(actual_path).resolve() == expected_project.resolve())
    if same_project:
        try:
            _path_guards(Path(actual_path).absolute())
        except (OSError, ValueError):
            same_project = False
    gates = list(binding.get('unresolved_gates', []))
    observed_snapshot = None
    try:
        observed_snapshot = _snapshot(expected_project, checkpoint=checkpoint)
        archive_current = observed_snapshot == binding.get('archive_snapshot')
    except (OSError, ValueError):
        archive_current = False
    if not archive_current:
        same_project = False
        gates.append('archive_binding_drift')
    if not same_project:
        gates.append('raw_project_association')
    curves = []
    for curve in raw.get('curves', []):
        if checkpoint is not None:
            checkpoint()
        identity = _material_identity(curve)
        frequencies = None
        if curve.get('xlabel') == 'Frequency / GHz':
            values = curve.get('x', [])
            frequencies = []
            for index, value in enumerate(values):
                if checkpoint is not None and index % 1024 == 0:
                    checkpoint()
                if isinstance(value,bool) or not isinstance(value,(int,float)):
                    raise ValueError('raw frequency grid is not numeric')
                frequencies.append(float(value)*1e9)
            canonical_hash(frequencies)  # Reject nonfinite/overflow without inventing samples.
        curves.append({
            'treepath':curve.get('treepath'), 'reported_treepath':curve.get('reported_treepath'),
            'run_id':curve.get('run_id'), 'parameters':copy.deepcopy(curve.get('parameters')),
            'module_parameters':copy.deepcopy(curve.get('module_parameters')),
            'source_role':identity['source_role'] if identity else 'other_actual_result',
            'actual_material_identity':identity, 'actual_frequencies_Hz':frequencies,
            'planned_frequency_subset_present':(all(f in frequencies for f in case['scenario']['frequencies_Hz'])
                                                if frequencies is not None else None),
            'native_solver_run_linkage_verified':False})
        if checkpoint is not None:
            checkpoint()
    if checkpoint is not None:
        checkpoint()
    return {
        'schema_version':'cst-result-provenance/1', 'case_signature':case['signature'],
        'execution_operation_id':binding.get('execution_operation_id'),
        'same_owned_project':same_project, 'association_scope':binding['association_scope'],
        'solver_info':copy.deepcopy(binding['solver_info']),
        'archive_snapshot':observed_snapshot if archive_current else None,
        'expected_archive_snapshot':copy.deepcopy(binding['archive_snapshot']),
        'archive_binding_current':archive_current, 'curves':curves,
        'native_run_linkage_verified':False, 'solver_material_linkage_verified':False,
        'actual_fd_policy':None, 'actual_reference_plane_m':None,
        'unresolved_gates':sorted(set(gates)), 'native_acceptance':'not_run',
        'numerically_qualified':False, 'physical_certification':False}
