"""Lossless, injected CST result access; mappings remain diagnostic declarations.

This module never imports the vendor SDK. An authorized caller must inject the
results module for the exact saved, completed project it owns.
"""
import csv
import hashlib
import json
import math
import numbers
import re
from pathlib import Path


def _number(value):
    if isinstance(value, bool) or not isinstance(value, numbers.Number):
        raise ValueError('result values must be numeric')
    if isinstance(value, numbers.Real):
        number = float(value)
        if not math.isfinite(number):
            return {'nonfinite': 'nan' if math.isnan(number) else ('+inf' if number > 0 else '-inf')}
        return int(value) if isinstance(value, numbers.Integral) else number
    return {'real': _number(value.real), 'imag': _number(value.imag)}


def _sequence(value, limit):
    if isinstance(value, (str, bytes, dict)) or value is None:
        raise ValueError('result array must be a numeric sequence')
    result = []
    for entry in value:
        if len(result) >= limit:
            raise ValueError('array length exceeds declared limit')
        result.append(entry)
    return result


def _bounded_sequence(value, limit):
    """Retain a bounded snapshot even when a source exceeds the read budget."""
    if isinstance(value, (str, bytes, dict)) or value is None:
        raise ValueError('result array must be a numeric sequence')
    try:
        source_length = len(value)
    except TypeError:
        source_length = None
    result, iterator = [], iter(value)
    for _ in range(limit):
        try:
            result.append(next(iterator))
        except StopIteration:
            return result, False
    # Do not consume a further numeric value merely to prove truncation. For
    # unknown-length iterables, reaching the bound is unresolved completeness.
    return result, source_length is None or source_length > limit


def _encode(value, limit, depth=0):
    if depth > 12:
        raise ValueError('raw metadata nesting exceeds limit')
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, numbers.Number):
        return _number(value)
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError('metadata keys must be strings')
        return {key: _encode(entry, limit, depth+1) for key, entry in value.items()}
    return [_encode(entry, limit, depth+1) for entry in _sequence(value, limit)]


def _finite(value):
    if isinstance(value, dict):
        return 'nonfinite' not in value and all(_finite(entry) for entry in value.values())
    if isinstance(value, list):
        return all(_finite(entry) for entry in value)
    return True


def _metadata_equal(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) or isinstance(right, dict):
        return (isinstance(left, dict) and isinstance(right, dict) and set(left) == set(right)
                and all(_metadata_equal(left[key], right[key]) for key in left))
    if isinstance(left, list) or isinstance(right, list):
        return (isinstance(left, list) and isinstance(right, list) and len(left) == len(right)
                and all(_metadata_equal(a, b) for a, b in zip(left, right)))
    if isinstance(left, numbers.Real) and isinstance(right, numbers.Real):
        return left == right
    return type(left) is type(right) and left == right


def _parts(value):
    if isinstance(value, dict) and set(value) == {'real', 'imag'}:
        return value['real'], value['imag']
    return value, 0


def _numeric_equal(left, right):
    left_re, left_im = _parts(left)
    right_re, right_im = _parts(right)
    return _metadata_equal(left_re, right_re) and _metadata_equal(left_im, right_im)


def _cell(value):
    return json.dumps(value, allow_nan=False, ensure_ascii=False) if isinstance(value, (dict, list)) else value


def _write_csv(path, columns, rows):
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def export_raw_results(project_path, output_dir, *, results_module=None,
                       stop_event=None, max_curves=2000, max_total_points=1000000):
    """Read each actual leaf/run, retaining errors and original numeric evidence.

    Output must be empty to prevent old evidence from surviving a partial read.
    ``completed`` describes this export only; validation always stays ``not_run``.
    """
    out = Path(output_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError('raw output directory must be empty')
    out.mkdir(parents=True, exist_ok=True)
    raw = {'schema_version': 'cst-raw-results/1', 'status': 'failed',
           'validation': 'not_run', 'native_acceptance': 'not_run',
           'project_path': str(Path(project_path).resolve()), 'tree_items': [],
           'curves': [], 'errors': [], 'artifacts': ['resulttree.json'],
           'limits': {'max_curves': max_curves, 'max_total_points': max_total_points},
           'counts': {'curves': 0, 'points': 0}}
    cancelled = False

    def stopped():
        return stop_event is not None and stop_event.is_set()

    def error(stage, exc, path=None, run=None):
        raw['errors'].append({'stage': stage, 'treepath': path, 'run_id': run,
                              'type': type(exc).__name__, 'message': str(exc)})

    try:
        if stopped():
            cancelled = True
        else:
            if results_module is None:
                raise ValueError('module_required: authorized caller must inject results_module')
            for limit in (max_curves, max_total_points):
                if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                    raise ValueError('export limits must be positive integers')
            project = Path(project_path).resolve(strict=True)
            if project.suffix.lower() != '.cst' or not project.is_file():
                raise ValueError('an explicit saved .cst project file is required')
            result_project = results_module.ProjectFile(str(project), allow_interactive=False)
            module = result_project.get_3d()
            paths = list(module.get_tree_items())
            if not paths:
                raise ValueError('missing: result tree is empty')
            if any(not isinstance(path, str) or not path for path in paths) or len(set(paths)) != len(paths):
                raise ValueError('result tree contains invalid or duplicate paths')
            raw['tree_items'] = paths
            (out / 'curves').mkdir()
            attempts = 0
            for path in paths:
                if stopped():
                    cancelled = True
                    break
                try:
                    ids = list(module.get_run_ids(path))
                    if not ids:
                        raise ValueError('missing: leaf has no actual run ids')
                    if any(isinstance(run, bool) or not isinstance(run, numbers.Integral) or run < 0 for run in ids) or len(set(ids)) != len(ids):
                        raise ValueError('invalid or duplicate run ids')
                except Exception as exc:
                    error('run_ids', exc, path)
                    continue
                for actual_run in ids:
                    run = int(actual_run)
                    if stopped():
                        cancelled = True
                        break
                    if attempts >= max_curves:
                        error('limit', ValueError('max_curves limit exceeded'), path, run)
                        break
                    attempts += 1
                    try:
                        item = module.get_result_item(path, run_id=run, load_impedances=True)
                        if item is None:
                            raise ValueError('missing: returned result item is None')
                        length = item.length
                        if isinstance(length, bool) or not isinstance(length, numbers.Integral) or length < 1:
                            raise ValueError('result length must be a positive integer')
                        length = int(length)
                        if raw['counts']['points'] + length > max_total_points:
                            raise ValueError('max_total_points limit exceeded')
                        data = item.get_data()
                        params = _encode(item.get_parameter_combination(), max_total_points)
                        module_params = _encode(module.get_parameter_combination(run), max_total_points)
                        if not isinstance(params, dict) or not isinstance(module_params, dict):
                            raise ValueError('parameter combinations must be dictionaries')
                        data_consistent = True
                        invalid_reasons = []
                        data_tuples_raw = None
                        snapshot_truncated = {'x': False, 'y': False, 'data': False, 'reference_impedance': False}
                        declared_length = length
                        if isinstance(data, numbers.Number):
                            if length != 1:
                                raise ValueError('0D result length must be one')
                            kind, x, y, refs, row_refs = '0D', None, _number(data), None, [None]
                            ydata_raw = _encode(item.get_ydata(), max_total_points)
                            getter_value = ydata_raw[0] if isinstance(ydata_raw, list) and len(ydata_raw) == 1 else ydata_raw
                            data_consistent = _numeric_equal(getter_value, y)
                            reference_consistency = 'not_applicable'
                            if not data_consistent:
                                invalid_reasons.append('0D data/getter sources disagree')
                        else:
                            kind = '1D'
                            # One extra observed position detects a false declared
                            # length. Its invalid snapshot is retained as evidence;
                            # longer sources retain this bounded prefix only.
                            read_limit = max_total_points - raw['counts']['points'] + 1
                            x_values, snapshot_truncated['x'] = _bounded_sequence(item.get_xdata(), read_limit)
                            y_values, snapshot_truncated['y'] = _bounded_sequence(item.get_ydata(), read_limit)
                            rows_data, snapshot_truncated['data'] = _bounded_sequence(data, read_limit)
                            x, y = [_number(value) for value in x_values], [_number(value) for value in y_values]
                            data_tuples_raw = []
                            for row in rows_data:
                                values, row_truncated = _bounded_sequence(row, 4)
                                snapshot_truncated['data'] = snapshot_truncated['data'] or row_truncated
                                data_tuples_raw.append([_number(value) for value in values])
                            length = max(len(x), len(y), len(data_tuples_raw))
                            if raw['counts']['points'] + length > max_total_points:
                                invalid_reasons.append('actual raw samples exceed max_total_points limit')
                            if len(x) != declared_length or len(y) != declared_length or len(data_tuples_raw) != declared_length:
                                invalid_reasons.append('x/y/data length differs from declared result length')
                            if any(len(row) not in (2, 3) for row in data_tuples_raw):
                                invalid_reasons.append('unsupported 1D data tuple shape')
                            tuples_x = [row[0] if row else None for row in data_tuples_raw]
                            tuples_y = [row[1] if len(row) > 1 else None for row in data_tuples_raw]
                            if (len(tuples_x) != len(x) or len(tuples_y) != len(y)
                                    or not all(_numeric_equal(a, b) for a, b in zip(tuples_x, x))
                                    or not all(_numeric_equal(a, b) for a, b in zip(tuples_y, y))):
                                invalid_reasons.append('raw data tuples and x/y getters disagree')
                            row_refs = [row[2] if len(row) >= 3 else None for row in data_tuples_raw]
                            refs_source = item.get_ref_imp_data()
                            if refs_source is None or isinstance(refs_source, (str, bool, dict, numbers.Number)):
                                refs = _encode(refs_source, max_total_points)
                            else:
                                refs_values, snapshot_truncated['reference_impedance'] = _bounded_sequence(refs_source, read_limit)
                                refs = [_encode(value, max_total_points) for value in refs_values]
                            if any(snapshot_truncated.values()):
                                invalid_reasons.append('raw numeric source exceeds snapshot limit and is truncated')
                            if isinstance(refs, list) and len(refs) > max_total_points - raw['counts']['points']:
                                invalid_reasons.append('reference impedance samples exceed max_total_points limit')
                            no_references = (refs is None or refs == []) and all(value is None for value in row_refs)
                            reference_consistency = 'not_present' if no_references else 'not_established'
                            if (isinstance(refs, list) and len(refs) == length
                                    and all((isinstance(value, numbers.Real) and not isinstance(value, bool))
                                            or isinstance(value, dict) and set(value) in ({'real', 'imag'}, {'nonfinite'})
                                            for value in refs)):
                                references_match = (len(refs) == len(row_refs)
                                                    and all(_numeric_equal(value, row_ref) for value, row_ref in zip(refs, row_refs)))
                                reference_consistency = 'verified' if references_match else 'conflict'
                                if not references_match:
                                    invalid_reasons.append('reference getter and data tuple sources disagree')
                            elif not no_references:
                                invalid_reasons.append('reference impedance shape or missing source prevents consistency verification')
                            data_consistent = not invalid_reasons
                            ydata_raw = y
                        reported_run = item.run_id
                        reported_path = item.treepath
                        identity_valid = (not isinstance(reported_run, bool)
                                          and isinstance(reported_run, numbers.Integral) and reported_run == run
                                          and reported_path == path and _metadata_equal(params, module_params))
                        curve = {'treepath': path, 'reported_treepath': reported_path,
                                 'requested_run_id': run, 'run_id': _encode(reported_run, max_total_points),
                                 'title': item.title, 'xlabel': item.xlabel, 'ylabel': item.ylabel,
                                 'parameters': params, 'module_parameters': module_params,
                                 'kind': kind, 'x': x, 'y': y, 'reference_impedance': refs,
                                 'row_reference_impedance': row_refs, 'point_count': length,
                                 'declared_point_count': declared_length, 'data_tuples_raw': data_tuples_raw,
                                 'snapshot_truncated': snapshot_truncated,
                                 'identity_valid': identity_valid, 'ydata_raw': ydata_raw,
                                 'data_consistent': data_consistent,
                                 'reference_impedance_consistency': reference_consistency,
                                 'invalid_reasons': invalid_reasons}
                        if not all(isinstance(curve[key], str) for key in ('reported_treepath', 'title', 'xlabel', 'ylabel')):
                            raise ValueError('original tree/title/axis labels must be strings')
                        curve['finite'] = _finite(curve)
                        if not identity_valid:
                            invalid_reasons.append('requested/reported identity or parameters disagree')
                        if not curve['finite']:
                            invalid_reasons.append('nonfinite values')
                        curve['analysis_eligible'] = not invalid_reasons
                        relative = 'curves/{:06d}.csv'.format(len(raw['curves']))
                        curve['data_csv'] = relative
                        csv_rows = []
                        for index in range(length):
                            value = y if kind == '0D' else y[index] if index < len(y) else None
                            re, im = _parts(value) if value is not None else (None, None)
                            ref = row_refs[index] if index < len(row_refs) else None
                            ref_re, ref_im = _parts(ref) if ref is not None else (None, None)
                            data_row = data_tuples_raw[index] if data_tuples_raw is not None and index < len(data_tuples_raw) else []
                            data_y = data_row[1] if len(data_row) > 1 else None
                            data_re, data_im = _parts(data_y) if data_y is not None else (None, None)
                            csv_rows.append({'sample_index': index, 'kind': kind, 'treepath': path,
                                             'run_id': reported_run, 'title': item.title,
                                             'xlabel': item.xlabel, 'ylabel': item.ylabel,
                                             'x': _cell(x[index] if x is not None and index < len(x) else None),
                                             'y_real': _cell(re), 'y_imag': _cell(im),
                                             'reference_impedance_real': _cell(ref_re),
                                             'reference_impedance_imag': _cell(ref_im),
                                             'data_x': _cell(data_row[0] if data_row else None),
                                             'data_y_real': _cell(data_re), 'data_y_imag': _cell(data_im),
                                             'analysis_eligible': curve['analysis_eligible'],
                                             'invalid_reasons': _cell(invalid_reasons)})
                        columns = ['sample_index', 'kind', 'treepath', 'run_id', 'title', 'xlabel', 'ylabel',
                                   'x', 'y_real', 'y_imag', 'reference_impedance_real', 'reference_impedance_imag',
                                   'data_x', 'data_y_real', 'data_y_imag', 'analysis_eligible', 'invalid_reasons']
                        _write_csv(out / relative, columns, csv_rows)
                        raw['curves'].append(curve)
                        raw['artifacts'].append(relative)
                        raw['counts']['curves'] += 1
                        raw['counts']['points'] += length
                        if not identity_valid:
                            error('identity', ValueError('requested/reported path, run or parameters disagree'), path, run)
                        if not curve['finite']:
                            error('nonfinite', ValueError('tagged nonfinite raw values are not analyzable'), path, run)
                        if not data_consistent:
                            error('data_consistency', ValueError('; '.join(invalid_reasons)), path, run)
                    except Exception as exc:
                        error('result', exc, path, run)
                if cancelled or attempts >= max_curves:
                    if attempts >= max_curves and path != paths[-1]:
                        error('limit', ValueError('max_curves limit reached before remaining leaves'), path)
                    break
    except Exception as exc:
        error('open_or_inventory', exc)
    cancelled = cancelled or stopped()
    raw['status'] = ('cancelled' if cancelled else
                     ('partial' if raw['curves'] else 'failed') if raw['errors'] else
                     'completed' if raw['curves'] else 'failed')
    raw['artifact_sha256'] = {name: hashlib.sha256((out / name).read_bytes()).hexdigest()
                              for name in raw['artifacts'] if name != 'resulttree.json'}
    (out / 'resulttree.json').write_text(json.dumps(raw, ensure_ascii=False, indent=2,
                                                   allow_nan=False), encoding='utf-8')
    return raw


def _strict_number(value):
    if isinstance(value, bool) or not isinstance(value, numbers.Real) or not math.isfinite(float(value)):
        raise ValueError('canonical values must be finite real numbers')
    return float(value)


def _complex_value(value):
    if isinstance(value, dict) and set(value) == {'real', 'imag'}:
        return complex(_strict_number(value['real']), _strict_number(value['imag']))
    return complex(_strict_number(value))


def _integer(value):
    number = _strict_number(value)
    if number != int(number):
        raise ValueError('mode/run identities must be integers')
    return int(number)


def _unit_in_label(label, unit):
    return label.endswith(' / ' + unit) or label.endswith('[' + unit + ']')


class _IdentityUnresolved(ValueError):
    def __init__(self, gate, message):
        super().__init__(message)
        self.gate = gate


def _identity_rule(rule, required, gate, *, prefix=False):
    """Compile supported complete identity atoms under literal actual tree parents.

    Arbitrary placeholder positions cannot prove which S column or excitation a
    result belongs to. Unknown identity grammars therefore remain unresolved.
    """
    fields = {'treepath'} if prefix else {'treepath', 'title'}
    if not isinstance(rule, dict) or rule.get('field') not in fields:
        raise _IdentityUnresolved(gate, 'actual excitation identity source field is missing or unsupported')
    template = rule.get('template')
    if not isinstance(template, str) or not template or len(template) > 4096:
        raise _IdentityUnresolved(gate, 'explicit actual-label excitation template is required')
    atom = ('Excitation [{incident_port}({incident_mode})]' if prefix else
            'S{receive_port}({receive_mode}),{incident_port}({incident_mode})')
    if not template.endswith(atom):
        raise _IdentityUnresolved(gate, 'unsupported or incomplete actual excitation identity grammar')
    parents = template[:-len(atom)]
    if (rule['field'] == 'title' and parents or parents and parents[-1] not in ('\\', '/')
            or '{' in parents or '}' in parents):
        raise _IdentityUnresolved(gate, 'identity atom must occupy a complete title or tree segment')
    if parents:
        segments = re.split(r'[\\/]', parents[:-1])
        if any(not segment or 'Excitation [' in segment
               or re.search(r'[A-Za-z0-9_.-]+\([0-9]+\)', segment) for segment in segments):
            raise _IdentityUnresolved(gate, 'additional fixed identities make actual tree ancestry ambiguous')
    pattern = re.escape(parents) + re.escape(atom)
    for slot in required:
        expression = r'[0-9]+' if slot.endswith('_mode') else r'[A-Za-z0-9_.-]+'
        pattern = pattern.replace(re.escape('{' + slot + '}'), '(?P<' + slot + '>' + expression + ')')
    return rule['field'], template, re.compile(pattern)


def _extract_identity(compiled, curve, gate, *, prefix=False):
    field, template, pattern = compiled
    value = curve[field]
    if not isinstance(value, str) or len(value) > 65536:
        raise _IdentityUnresolved(gate, 'actual excitation identity label is unavailable or exceeds limit')
    match = pattern.match(value) if prefix else pattern.fullmatch(value)
    if match is None:
        raise _IdentityUnresolved(gate, 'actual result label cannot establish excitation identity')
    groups = match.groupdict()
    for name in groups:
        if name.endswith('_mode'):
            groups[name] = int(groups[name])
            if groups[name] < 1:
                raise _IdentityUnresolved(gate, 'actual excitation mode number must be positive')
    evidence = {'source_field': field, 'source_value': value, 'template': template,
                'treepath': curve['treepath'], 'run_id': curve['run_id'], **groups}
    if prefix:
        tail = value[match.end():]
        if len(tail) < 2 or tail[0] not in ('\\', '/') or not tail[1:]:
            raise _IdentityUnresolved(gate, 'actual power excitation branch needs a complete path separator and quantity')
        if any('Excitation [' in segment for segment in re.split(r'[\\/]', tail[1:])):
            raise _IdentityUnresolved(gate, 'multiple actual power excitation branches are ambiguous')
        evidence['branch'], evidence['quantity_path'] = match.group(), tail[1:]
    else:
        # A second recognizable matrix identity must agree with the selected
        # field. Choosing a profile field cannot conceal contradictory labels.
        atom = re.compile(r'(?<![A-Za-z0-9_.-])S(?P<receive_port>[A-Za-z0-9_.-]+)\((?P<receive_mode>[0-9]+)\),'
                          r'(?P<incident_port>[A-Za-z0-9_.-]+)\((?P<incident_mode>[0-9]+)\)(?![A-Za-z0-9_.-])')
        other_field = 'title' if field == 'treepath' else 'treepath'
        for other_match in atom.finditer(curve[other_field]):
            other_groups = other_match.groupdict()
            for key in ('receive_mode', 'incident_mode'):
                other_groups[key] = int(other_groups[key])
            if other_groups != groups:
                raise _IdentityUnresolved(gate, 'actual S title and tree identity disagree')
    return evidence


def canonicalize_raw_results(raw, profile, case, model_report, out_dir):
    """Apply an explicit exact mapping; never certify its declared physical roles.

    Selectors require treepath/run_id/title/xlabel/ylabel, x_unit/x_to_Hz and
    y_unit/y_scale. No frequency interpolation, assumed Gamma, or power residual
    supplies a missing source. Material roles and normalization remain declared.
    """
    out = Path(out_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError('canonical output directory must be empty')
    out.mkdir(parents=True, exist_ok=True)
    receipt = {'schema_version': 'cst-canonical-mapping/1', 'status': 'failed',
               'spectra_path': None, 'power_path': None, 'physical_accepted': False,
               'numerically_qualified': False, 'native_acceptance': 'not_run',
               'mapping_evidence': 'profile_declared_not_native_accepted',
               'normalization_evidence': None, 'unresolved_gates': [],
               'excitation_identity': {'status': 'unresolved', 'incident_mode': None,
                                       's_columns': [], 'power_branches': []},
               'material_fit_readback': {'status': 'unresolved', 'materials': []},
               'errors': [], 'sources': [], 'artifacts': ['mapping-receipt.json']}
    try:
        if not isinstance(raw, dict):
            raw = json.loads(Path(raw).read_text(encoding='utf-8'))
        if raw.get('schema_version') != 'cst-raw-results/1' or raw.get('status') != 'completed' or raw.get('errors'):
            raise ValueError('only a complete error-free raw inventory may be mapped')
        if profile.get('schema_version') != 'cst-result-profile/1' or profile.get('confirmed') is not True:
            raise ValueError('an explicit confirmed result profile is required')
        if not case.get('signature') or profile.get('case_signature') != case['signature']:
            raise ValueError('profile/case signature mismatch')
        run = _integer(profile['run_id'])
        if run < 0 or not isinstance(profile.get('expected_parameters'), dict) or not _finite(profile['expected_parameters']):
            raise ValueError('explicit finite run parameters are required')
        if profile.get('normalization') != 'power':
            raise ValueError('explicit power normalization is required')
        evidence = profile.get('normalization_evidence')
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError('normalization_evidence must be a nonempty source string')
        receipt['normalization_evidence'] = evidence
        receipt['profile_sha256'] = hashlib.sha256(json.dumps(profile, sort_keys=True,
                                                             allow_nan=False).encode()).hexdigest()
        receipt['raw_sha256'] = hashlib.sha256(json.dumps(raw, sort_keys=True,
                                                         allow_nan=False).encode()).hexdigest()
        if model_report.get('status') != 'model_report_validated':
            raise ValueError('validated actual model readback is required')
        frequencies = [_strict_number(value) for value in case['scenario']['frequencies_Hz']]
        if not frequencies or any(value <= 0 for value in frequencies) or any(b <= a for a, b in zip(frequencies, frequencies[1:])):
            raise ValueError('planned frequencies must be positive and increasing')
        by_key = {}
        for curve in raw['curves']:
            key = (curve['treepath'], _integer(curve['run_id']))
            if key in by_key:
                raise ValueError('duplicate actual raw tree/run key')
            by_key[key] = curve
        used = set()

        def select(selector, role):
            selected_run = _integer(selector['run_id'])
            key = (selector['treepath'], selected_run)
            if selected_run != run or key not in by_key:
                raise ValueError('exact actual tree/run selector does not exist for requested run')
            if key in used:
                raise ValueError('duplicate source curve mapped to multiple quantities')
            curve = by_key[key]
            for field in ('treepath', 'run_id', 'title', 'xlabel', 'ylabel'):
                if selector[field] != curve[field]:
                    raise ValueError('exact original selector label/identity mismatch: ' + field)
            if (curve.get('kind') != '1D' or curve.get('finite') is not True or not _finite(curve)
                    or curve.get('analysis_eligible') is not True or curve.get('data_consistent') is not True
                    or curve.get('invalid_reasons')
                    or curve.get('identity_valid') is not True or curve.get('reported_treepath') != curve['treepath']
                    or curve.get('requested_run_id') != run or not _metadata_equal(curve.get('parameters'), profile['expected_parameters'])
                    or not _metadata_equal(curve.get('module_parameters'), profile['expected_parameters'])):
                raise ValueError('curve is nonfinite, not 1D, or actual identity/parameters disagree')
            x_unit, y_unit = selector['x_unit'], selector['y_unit']
            x_factor = {'Hz': 1.0, 'kHz': 1e3, 'MHz': 1e6, 'GHz': 1e9}.get(x_unit)
            y_factors = {'1/m': 1.0, '1/mm': 1000.0} if role == 'gamma' else ({'W': 1.0} if role == 'power' else {'1': 1.0})
            y_factor = y_factors.get(y_unit)
            if (x_factor is None or y_factor is None or _strict_number(selector['x_to_Hz']) != x_factor
                    or _strict_number(selector['y_scale']) != y_factor
                    or not _unit_in_label(curve['xlabel'], x_unit) or not _unit_in_label(curve['ylabel'], y_unit)):
                raise ValueError('actual axis units and declared conversion factors must agree')
            x = [_strict_number(value) * x_factor for value in curve['x']]
            y = [_complex_value(value) * y_factor for value in curve['y']]
            if len(x) != curve['point_count'] or len(y) != len(x) or x != frequencies:
                raise ValueError('actual frequencies/row lengths must exactly match planned frequencies')
            if any(not math.isfinite(value.real) or not math.isfinite(value.imag) for value in y):
                raise ValueError('converted values must remain finite')
            used.add(key)
            receipt['sources'].append({'role': role, **{field: curve[field] for field in ('treepath', 'run_id', 'title', 'xlabel', 'ylabel')},
                                       'x_unit': x_unit, 'x_to_Hz': x_factor, 'y_unit': y_unit, 'y_scale': y_factor})
            return y, curve

        def mode_identity(mode, mapped=False):
            index = _integer(mode['native_mode_index'] if mapped else mode['index'])
            if index < 1:
                raise ValueError('native mode index must be positive')
            port = mode.get('port', model_report.get('modes_port'))
            name = mode['mode_name'] if mapped else mode['name']
            if not isinstance(port, str) or not port or not isinstance(name, str) or not name:
                raise ValueError('actual mode port/name is missing')
            return (port, index, name, _integer(mode['m']), _integer(mode['n']), mode['polarization'])

        actual_modes = [mode_identity(mode) for mode in model_report['modes']]
        mapped_modes = [mode_identity(mode, True) for mode in profile['modes']]
        if (len({mode[:2] for mode in actual_modes}) != len(actual_modes)
                or len({mode[:2] for mode in mapped_modes}) != len(mapped_modes)):
            raise ValueError('conflicting or duplicate actual native mode key')
        if not actual_modes or len(set(actual_modes)) != len(actual_modes) or len(set(mapped_modes)) != len(mapped_modes) or set(actual_modes) != set(mapped_modes):
            raise ValueError('profile mode map must match actual model mode readback exactly')
        excitation = profile.get('excitation')
        if not isinstance(excitation, dict) or not isinstance(excitation.get('incident_mode'), dict):
            raise _IdentityUnresolved('incident_excitation_identity', 'actual incident excitation identity is missing')
        incident_mode = excitation['incident_mode']
        try:
            incident_identity = mode_identity(incident_mode, True)
        except (KeyError, TypeError, ValueError) as exc:
            raise _IdentityUnresolved('incident_excitation_identity', 'complete actual incident mode identity is required') from exc
        requested_polarization = case['scenario'].get('polarization')
        if requested_polarization not in ('TE', 'TM'):
            raise _IdentityUnresolved('incident_excitation_identity', 'requested case incidence polarization is missing')
        if (incident_identity not in actual_modes or incident_identity[3:5] != (0, 0)
                or incident_identity[5] != requested_polarization
                or incident_identity[0] != model_report.get('modes_port')
                or model_report['boundaries'].get(incident_identity[0]) != 'open'):
            raise ValueError('incident excitation must match the actual open-port fundamental mode and requested polarization')
        receipt['excitation_identity']['incident_mode'] = {
            'port': incident_identity[0], 'native_mode_index': incident_identity[1], 'mode_name': incident_identity[2],
            'm': incident_identity[3], 'n': incident_identity[4], 'polarization': incident_identity[5]}
        s_rule = _identity_rule(excitation.get('s_identity'),
                                {'receive_port', 'receive_mode', 'incident_port', 'incident_mode'},
                                's_matrix_incident_column')
        power_rule = _identity_rule(excitation.get('power_identity'), {'incident_port', 'incident_mode'},
                                    'power_excitation_identity', prefix=True)
        spectra = []
        for mode in profile['modes']:
            if mode['polarization'] not in ('TE', 'TM'):
                raise ValueError('only explicitly mapped TE/TM spectra are supported')
            coefficients, source = select(mode['reflection'], 'reflection')
            actual_column = _extract_identity(s_rule, source, 's_matrix_incident_column')
            receipt['excitation_identity']['s_columns'].append(actual_column)
            receiving_identity = mode_identity(mode, True)
            if ((actual_column['receive_port'], actual_column['receive_mode']) != receiving_identity[:2]
                    or receiving_identity[0] != incident_identity[0]
                    or (actual_column['incident_port'], actual_column['incident_mode']) != incident_identity[:2]):
                receipt['excitation_identity']['status'] = 'mismatch'
                raise ValueError('actual S receiving mode or incident column disagrees with requested excitation')
            gamma, _ = select(mode['gamma'], 'gamma')
            for index, frequency in enumerate(frequencies):
                spectra.append({'frequency_Hz': frequency, 'm': mode['m'], 'n': mode['n'],
                                'polarization': mode['polarization'], 's_re': coefficients[index].real,
                                's_im': coefficients[index].imag, 'gamma_re_per_m': gamma[index].real,
                                'gamma_im_per_m': gamma[index].imag, 'normalization': 'power'})

        power_branch = None

        def real_power(selector, material_loss=False, quantity_role=None):
            nonlocal power_branch
            values, curve = select(selector, 'power')
            identity = _extract_identity(power_rule, curve, 'power_excitation_identity', prefix=True)
            identity['quantity_role'] = quantity_role
            receipt['excitation_identity']['power_branches'].append(identity)
            if ((identity['incident_port'], identity['incident_mode']) != incident_identity[:2]
                    or power_branch is not None and identity['branch'] != power_branch):
                receipt['excitation_identity']['status'] = 'mismatch'
                raise ValueError('actual independent power excitation branch disagrees with S incident excitation')
            power_branch = identity['branch']
            if material_loss:
                names = ' '.join(str(curve[field]) for field in ('treepath', 'title', 'ylabel')).lower()
                if 'accepted' in names or ('absorb' in names and 'port' in names):
                    raise ValueError('Accepted or port absorption cannot supply material absorbed loss')
            if any(value.imag != 0 or value.real < 0 for value in values):
                raise ValueError('power curves must be real and nonnegative')
            return [value.real for value in values]

        power_profile = profile['power']
        incident = real_power(power_profile['stimulated'], quantity_role='stimulated')
        reflected = real_power(power_profile['reflected'], quantity_role='reflected')
        if any(value <= 0 for value in incident):
            raise ValueError('actual stimulated power must be positive')
        loss_entries = power_profile['material_absorbed']
        material_ids = [entry['material_id'] for entry in loss_entries]
        if (not case.get('materials') or len(set(material_ids)) != len(material_ids)
                or set(material_ids) != set(case['materials'])):
            raise ValueError('independent material-loss mapping must cover each case material exactly once')
        losses = [real_power(entry['selector'], True, 'material_absorbed:' + entry['material_id']) for entry in loss_entries]
        receipt['excitation_identity']['status'] = 'validated_against_actual_labels'
        assumption = profile['boundary_assumption']
        if (case['scenario'].get('profile') != 'periodic-pec' or assumption != {'kind': 'PEC', 'boundary': 'Zmin', 'value': 'electric'}
                or model_report['boundaries'].get('Zmin') != 'electric'):
            raise ValueError('zero transmission requires the explicit PEC assumption and actual boundary readback')
        receipt['transmission_source'] = 'read_back_PEC_boundary_assumption'
        powers = [{'frequency_Hz': frequency, 'incident_W': incident[index], 'reflected_W': reflected[index],
                   'absorbed_W': math.fsum(loss[index] for loss in losses), 'transmitted_W': 0.0}
                  for index, frequency in enumerate(frequencies)]

        fit_report = receipt['material_fit_readback']
        if not profile.get('materials'):
            receipt['unresolved_gates'].append('material_fit_readback')
        else:
            fit_entries = profile['materials']
            ids = [entry['material_id'] for entry in fit_entries]
            if len(set(ids)) != len(ids) or set(ids) != set(case['materials']):
                raise ValueError('fitted-response mapping must cover each case material exactly once')
            fit_report['source_role'] = 'profile_declared_fitted_response'
            fit_report['native_acceptance'] = 'pending'
            all_match = True
            for entry in fit_entries:
                if entry.get('role') != 'fitted_response':
                    raise ValueError('original material response is not fitted-response evidence')
                convention = entry['source_time_convention']
                if convention not in ('exp(+jωt)', 'exp(-jωt)'):
                    raise ValueError('material complex time convention must be explicit')
                rtol = _strict_number(entry['relative_tolerance'])
                atol = _strict_number(entry['absolute_tolerance'])
                if min(rtol, atol) < 0 or max(rtol, atol) <= 0:
                    raise ValueError('fit comparison requires declared nonnegative finite tolerances')
                material_id = entry['material_id']
                expected = case['material_samples'][material_id]
                if expected['frequencies_Hz'] != frequencies or expected.get('time_convention', 'exp(+jωt)') != 'exp(+jωt)':
                    raise ValueError('prepared material sample axes/convention disagree')
                material_report = {'material_id': material_id, 'role': entry['role'],
                                   'source_role': 'profile_declared_fitted_response',
                                   'source_time_convention': convention, 'relative_tolerance': rtol,
                                   'absolute_tolerance': atol, 'max_absolute_error': 0.0, 'matched': None,
                                   'comparison_complete': False,
                                   'components': {}}
                fit_report['materials'].append(material_report)
                material_matched = True
                for component in ('epsilon', 'mu'):
                    values, source = select(entry[component], 'material')
                    if 'original' in (source['title'] + ' ' + source['treepath']).lower():
                        raise ValueError('original-data curve cannot be called fitted-response evidence')
                    if convention == 'exp(-jωt)':
                        values = [value.conjugate() for value in values]
                    reals, imags = expected[component+'_real'], expected[component+'_imag']
                    if len(reals) != len(frequencies) or len(imags) != len(frequencies):
                        raise ValueError('prepared material component lengths disagree')
                    points = []
                    for index, value in enumerate(values):
                        target = complex(_strict_number(reals[index]), _strict_number(imags[index]))
                        difference = abs(value-target)
                        if not math.isfinite(difference):
                            raise ValueError('material comparison overflowed')
                        matched = difference <= atol + rtol*abs(target)
                        material_matched &= matched
                        material_report['max_absolute_error'] = max(material_report['max_absolute_error'], difference)
                        points.append({'frequency_Hz': frequencies[index], 'actual_real': value.real, 'actual_imag': value.imag,
                                       'expected_real': target.real, 'expected_imag': target.imag,
                                       'absolute_error': difference, 'within_declared_tolerance': matched})
                    material_report['components'][component] = {'treepath': source['treepath'], 'title': source['title'],
                                                                 'xlabel': source['xlabel'], 'ylabel': source['ylabel'],
                                                                 'run_id': source['run_id'], 'points': points}
                material_report['matched'] = material_matched
                material_report['comparison_complete'] = True
                all_match &= material_matched
            fit_report['status'] = 'profile_declared_fit_matches_samples' if all_match else 'fit_sample_mismatch'
            (out / 'material-fit-readback.json').write_text(json.dumps(fit_report, indent=2, ensure_ascii=False,
                                                                      allow_nan=False), encoding='utf-8')
            receipt['artifacts'].append('material-fit-readback.json')
            if not all_match:
                raise ValueError('declared fitted response differs from prepared samples beyond tolerances')
        _write_csv(out / 'spectra.csv', list(spectra[0]), spectra)
        _write_csv(out / 'power.csv', list(powers[0]), powers)
        receipt.update(status='diagnostic_only', spectra_path=str(out / 'spectra.csv'), power_path=str(out / 'power.csv'))
        receipt['artifacts'].extend(['spectra.csv', 'power.csv'])
    except Exception as exc:
        if isinstance(exc, _IdentityUnresolved):
            receipt['unresolved_gates'].append(exc.gate)
        receipt['errors'].append({'type': type(exc).__name__, 'message': str(exc)})
    receipt['artifact_sha256'] = {name: hashlib.sha256((out / name).read_bytes()).hexdigest()
                                  for name in receipt['artifacts'] if name != 'mapping-receipt.json'}
    (out / 'mapping-receipt.json').write_text(json.dumps(receipt, indent=2, ensure_ascii=False,
                                                       allow_nan=False), encoding='utf-8')
    return receipt
