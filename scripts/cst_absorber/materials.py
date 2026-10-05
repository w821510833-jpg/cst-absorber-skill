"""Passive scalar material tables; interpolation is not a causal material fit."""
from bisect import bisect_right
import math

EPSILON_0 = 8.8541878128e-12
UNITS = {'Hz': 1.0, 'MHz': 1e6, 'GHz': 1e9}
CONVENTIONS = {'exp(+jωt)': 1, 'exp(-jωt)': -1}


def _number(value, label, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label} must be a finite number')
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f'{label} must be a finite number')
    if positive and value <= 0 or nonnegative and value < 0:
        raise ValueError(f'{label} has an invalid sign')
    return float(value)


def _object(value, allowed, required, label):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError(f'{label} has missing or unsupported fields')


def _range(value, label):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f'{label} must contain two frequencies')
    values = [_number(v, label, positive=True) for v in value]
    if values[0] >= values[1]:
        raise ValueError(f'{label} must increase')
    return values


def sample_material(material: dict, frequencies_Hz: list) -> dict:
    """Return SI samples with exp(+jωt) loss sign and conductivity folded once.

    The output conductivity is zero because the returned epsilon already contains
    its loss. No causal fitting or certification is implied by these samples.
    """
    required = {'time_convention', 'frequency_unit', 'epsilon_mu_table', 'measured_ranges_Hz'}
    _object(material, required | {'conductivity_S_m', 'epsilon_includes_conductivity',
                                  'density_kg_m3', 'extrapolation'}, required, 'material')
    convention = material['time_convention']
    unit = material['frequency_unit']
    if not isinstance(convention, str) or convention not in CONVENTIONS:
        raise ValueError('unsupported time convention')
    if not isinstance(unit, str) or unit not in UNITS:
        raise ValueError('unsupported frequency unit')
    sigma = _number(material.get('conductivity_S_m', 0), 'conductivity', nonnegative=True)
    includes = material.get('epsilon_includes_conductivity', False)
    if not isinstance(includes, bool):
        raise ValueError('epsilon_includes_conductivity must be boolean')
    density = material.get('density_kg_m3')
    if 'density_kg_m3' in material:
        density = _number(density, 'density', positive=True)
    rows = material['epsilon_mu_table']
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError('material table needs at least two rows')
    keys = {'frequency', 'epsilon_real', 'epsilon_imag', 'mu_real', 'mu_imag'}
    table = []
    for row in rows:
        _object(row, keys, keys, 'table row')
        converted = {key: _number(row[key], key, positive=(key == 'frequency')) for key in keys}
        converted['frequency'] *= UNITS[unit]
        if not math.isfinite(converted['frequency']):
            raise ValueError('converted frequency is not finite')
        for key in ('epsilon_imag', 'mu_imag'):
            converted[key] *= CONVENTIONS[convention]
            if converted[key] > 0:
                raise ValueError('active loss sign is unsupported')
        table.append(converted)
    grid = [row['frequency'] for row in table]
    if any(b <= a for a, b in zip(grid, grid[1:])):
        raise ValueError('table frequencies must strictly increase')
    ranges = material['measured_ranges_Hz']
    _object(ranges, {'epsilon', 'mu'}, {'epsilon', 'mu'}, 'measured ranges')
    coverage = {key: _range(ranges[key], key) for key in ('epsilon', 'mu')}
    if any(lo < grid[0] or hi > grid[-1] for lo, hi in coverage.values()):
        raise ValueError('measured ranges must lie within the material table')
    extrapolation = material.get('extrapolation', {'allowed': False})
    _object(extrapolation, {'allowed', 'range_Hz', 'method', 'authorization'}, {'allowed'}, 'extrapolation')
    allowed = extrapolation['allowed']
    if not isinstance(allowed, bool):
        raise ValueError('extrapolation.allowed must be boolean')
    extrapolation_range = None
    if allowed:
        _object(extrapolation, {'allowed', 'range_Hz', 'method', 'authorization'},
                {'allowed', 'range_Hz', 'method', 'authorization'}, 'extrapolation')
        if extrapolation['method'] != 'linear' or not isinstance(extrapolation['authorization'], str) or not extrapolation['authorization'].strip():
            raise ValueError('linear extrapolation needs explicit authorization')
        extrapolation_range = _range(extrapolation['range_Hz'], 'extrapolation range')
    elif set(extrapolation) != {'allowed'}:
        raise ValueError('disabled extrapolation must not contain authorization fields')
    if not isinstance(frequencies_Hz, list) or not frequencies_Hz:
        raise ValueError('frequencies must be a finite nonempty list')
    frequencies = [_number(value, 'frequency', positive=True) for value in frequencies_Hz]
    if any(b <= a for a, b in zip(frequencies, frequencies[1:])):
        raise ValueError('sample frequencies must strictly increase')
    result = {'frequencies_Hz': frequencies, 'time_convention': 'exp(+jωt)',
              'epsilon_real': [], 'epsilon_imag': [], 'mu_real': [], 'mu_imag': [],
              'conductivity_S_m': 0.0, 'source_conductivity_S_m': sigma,
              'density_kg_m3': density, 'mass_available': density is not None,
              'interpolation': 'piecewise-linear', 'causal_fit': False,
              'extrapolated_frequencies_Hz': [],
              'conductivity_treatment': 'already_in_epsilon' if includes else 'folded_into_epsilon'}
    for frequency in frequencies:
        outside = frequency < grid[0] or frequency > grid[-1] or any(
            frequency < lo or frequency > hi for lo, hi in coverage.values())
        if outside:
            if not allowed or not extrapolation_range[0] <= frequency <= extrapolation_range[1]:
                raise ValueError('frequency lacks measured coverage or authorized extrapolation')
            result['extrapolated_frequencies_Hz'].append(frequency)
        index = max(0, min(bisect_right(grid, frequency) - 1, len(grid) - 2))
        fraction = (frequency - grid[index]) / (grid[index + 1] - grid[index])
        for key in ('epsilon_real', 'epsilon_imag', 'mu_real', 'mu_imag'):
            value = table[index][key] + fraction * (table[index + 1][key] - table[index][key])
            if key == 'epsilon_imag' and not includes:
                value -= sigma / (2 * math.pi * frequency * EPSILON_0)
            if not math.isfinite(value) or key.endswith('imag') and value > 0:
                raise ValueError('sampling produced nonfinite or active material data')
            result[key].append(value)
    return result
