"""Independent vacuum Floquet lattice coverage; no solver-specific API."""
import math

C0 = 299792458.0
CUTOFF_RTOL = 1e-12


def _parameters(case):
    cell, scenario = case['geometry']['cell'], case['scenario']
    lx, ly = float(cell['Lx_m']), float(cell['Ly_m'])
    theta, phi = float(scenario.get('theta_deg', 0)), float(scenario.get('azimuth_deg', 0))
    frequencies = [float(x) for x in scenario['frequencies_Hz']]
    if not all(math.isfinite(x) for x in [lx, ly, theta, phi, *frequencies]) or lx <= 0 or ly <= 0:
        raise ValueError('finite positive lattice dimensions and frequencies required')
    if not frequencies or any(x <= 0 for x in frequencies) or any(b <= a for a, b in zip(frequencies, frequencies[1:])):
        raise ValueError('frequencies_Hz must be positive and strictly increasing')
    if not 0 <= theta <= 45 or phi != 0 or scenario.get('polarization') not in ('TE', 'TM'):
        raise ValueError('supported incidence: theta 0..45, azimuth 0, TE or TM')
    return lx, ly, math.radians(theta), math.radians(phi), frequencies


def mode_state(case, frequency_Hz, m, n):
    """Classify a rectangular-lattice order without reference to configured modes."""
    lx, ly, theta, phi, _ = _parameters(case)
    k0 = 2 * math.pi * float(frequency_Hz) / C0
    kx = k0 * math.sin(theta) * math.cos(phi) + 2 * math.pi * m / lx
    ky = k0 * math.sin(theta) * math.sin(phi) + 2 * math.pi * n / ly
    delta = k0*k0 - kx*kx - ky*ky
    tolerance = CUTOFF_RTOL * k0*k0
    if abs(delta) <= tolerance:
        return {'state': 'cutoff', 'kz_per_m': 0.0, 'alpha_per_m': 0.0}
    if delta > 0:
        return {'state': 'propagating', 'kz_per_m': math.sqrt(delta), 'alpha_per_m': 0.0}
    return {'state': 'evanescent', 'kz_per_m': 0.0, 'alpha_per_m': math.sqrt(-delta)}


def required_modes(case):
    """Union of both polarizations of all propagating/cutoff orders at requested points.

    Bounds derive from |k_parallel+G| <= k0. Configured truncations never influence
    enumeration. Export this union at every requested frequency (including orders
    which become evanescent at lower frequencies).
    """
    lx, ly, theta, phi, frequencies = _parameters(case)
    maximum = max(frequencies)
    # Conservative rectangular bound, independent of the configured modal set.
    m_bound = math.ceil(maximum * lx / C0 * (1 + abs(math.sin(theta)*math.cos(phi)))) + 1
    n_bound = math.ceil(maximum * ly / C0 * (1 + abs(math.sin(theta)*math.sin(phi)))) + 1
    limit = case.get('runtime', {}).get('max_modes')
    if limit is not None and (isinstance(limit, bool) or int(limit) != limit or limit < 1):
        raise ValueError('max_modes must be a positive integer')
    # Guard an unreasonable input before allocating or iterating a huge lattice.
    if (2*m_bound+1)*(2*n_bound+1)*len(frequencies) > 5_000_000:
        raise ValueError('independent enumeration exceeds safety bound; reduce domain or frequencies')
    modes = []
    for m in range(-m_bound, m_bound+1):
        for n in range(-n_bound, n_bound+1):
            if any(mode_state(case, f, m, n)['state'] != 'evanescent' for f in frequencies):
                modes.extend({'m': m, 'n': n, 'polarization': p} for p in ('TE', 'TM'))
                if limit is not None and len(modes) > limit:
                    raise ValueError('physical modal coverage exceeds max_modes; no truncation permitted')
    return sorted(modes, key=lambda x: (abs(x['m'])+abs(x['n']), x['m'], x['n'], x['polarization']))
