"""Fail-closed CSV spectral checks and linear-R sampled-band metrics."""
import csv
import hashlib
import math
from pathlib import Path
from .modal import required_modes, mode_state


def _finite(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f'{name} must be numeric') from error
    if not math.isfinite(number):
        raise ValueError(f'{name} must be finite')
    return number


def _db(value):
    return 10*math.log10(value) if value > 0 else None


def band_metrics(frequencies, reflection, band_Hz, max_gap_Hz, threshold_R=.1):
    """Integrate piecewise-linear R only across declared valid adjacent intervals.

    Widths are computed on the covered subset; an incomplete band has no average.
    Minima describe actual in-band samples, never an interpolated optimization.
    """
    f = [_finite(x, 'frequency') for x in frequencies]
    r = [_finite(x, 'R') for x in reflection]
    lo, hi = [_finite(x, 'band') for x in band_Hz]
    gap, threshold = _finite(max_gap_Hz, 'max_gap_Hz'), _finite(threshold_R, 'threshold_R')
    if len(f) != len(r) or not f or hi <= lo or gap <= 0 or threshold < 0 or any(x < 0 for x in r) or any(b <= a for a,b in zip(f,f[1:])):
        raise ValueError('invalid spectral band, reflection, frequency axis or gap policy')
    integral = coverage = 0.0
    intervals, invalid_gaps = [], []
    for a,b,ra,rb in zip(f,f[1:],r,r[1:]):
        left,right = max(a,lo),min(b,hi)
        if right <= left: continue
        if b-a > gap:
            invalid_gaps.append([left,right]); continue
        rl = ra+(rb-ra)*(left-a)/(b-a)
        rr = ra+(rb-ra)*(right-a)/(b-a)
        coverage += right-left
        integral += (rl+rr)*(right-left)/2
        if rl <= threshold and rr <= threshold:
            intervals.append([left,right])
        elif (rl <= threshold) != (rr <= threshold):
            crossing = left+(threshold-rl)*(right-left)/(rr-rl)
            intervals.append([left,crossing] if rl <= threshold else [crossing,right])
    merged = []
    for left,right in intervals:
        if merged and math.isclose(merged[-1][1],left,rel_tol=0,abs_tol=max(1e-12,(hi-lo)*1e-12)):
            merged[-1][1] = right
        else: merged.append([left,right])
    widths = [right-left for left,right in merged]
    fully = math.isclose(coverage,hi-lo,rel_tol=1e-12,abs_tol=0)
    samples = [(value,frequency) for frequency,value in zip(f,r) if lo <= frequency <= hi]
    minimum,frequency = min(samples) if samples else (None,None)
    return {'band_Hz':[lo,hi], 'fully_covered':fully, 'coverage_fraction':coverage/(hi-lo),
            'covered_width_Hz':coverage, 'invalid_gaps_Hz':invalid_gaps,
            'average_R':integral/(hi-lo) if fully else None,
            'average_RL_dB':_db(integral/(hi-lo)) if fully else None,
            'average_RL_definition':'10*log10(average_R); not the average of dB samples',
            'average_zero_R':fully and integral == 0,
            'threshold_R':threshold, 'threshold_intervals_Hz':merged,
            'width_scope':'covered_intervals_only', 'total_threshold_width_Hz':sum(widths),
            'longest_threshold_width_Hz':max(widths,default=0),
            'sampled_min_R':minimum, 'sampled_min_frequency_Hz':frequency,
            'sampled_min_RL_dB':_db(minimum) if minimum is not None else None,
            'sampled_min_zero_R':minimum == 0, 'minimum_kind':'sampled_only'}


def _read_csv(path, columns):
    with Path(path).open('r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not set(columns).issubset(reader.fieldnames or []):
            raise ValueError(f'CSV missing columns: {sorted(set(columns)-set(reader.fieldnames or []))}')
        rows = list(reader)
    if not rows: raise ValueError('CSV has no rows')
    return rows


def analyze_exports(case, spectra_path, power_path):
    """Analyze one explicitly specified design; file analysis never certifies CST."""
    required = {(x['m'],x['n'],x['polarization']) for x in required_modes(case)}
    frequencies = [float(x) for x in case['scenario']['frequencies_Hz']]
    planned = set(frequencies)
    analysis = dict(case['analysis'])
    tolerance = _finite(analysis.get('power_tolerance',1e-6),'power_tolerance')
    gamma_rtol = _finite(analysis.get('gamma_relative_tolerance',1e-6),'gamma_relative_tolerance')
    gamma_atol = _finite(analysis.get('gamma_absolute_tolerance_per_m',1e-8),'gamma_absolute_tolerance_per_m')
    if not 0 < tolerance <= .01 or not 0 < gamma_rtol <= .01 or not 0 < gamma_atol <= .01:
        raise ValueError('declared power/gamma tolerances must be positive and <= .01')
    rows = _read_csv(spectra_path, ['frequency_Hz','m','n','polarization','s_re','s_im','gamma_re_per_m','gamma_im_per_m','normalization'])
    groups = {f:{} for f in frequencies}; retained=[]
    for raw in rows:
        row = {key:_finite(raw[key],key) for key in ['frequency_Hz','m','n','s_re','s_im','gamma_re_per_m','gamma_im_per_m']}
        if row['m'] != int(row['m']) or row['n'] != int(row['n']): raise ValueError('modal orders must be integers')
        row['m'],row['n'] = int(row['m']),int(row['n'])
        row['polarization'], row['normalization'] = raw['polarization'],raw['normalization']
        f = row['frequency_Hz']; key=(row['m'],row['n'],row['polarization'])
        if f not in planned: raise ValueError('unexpected frequency; actual axis must equal planned points')
        if row['polarization'] not in ('TE','TM'): raise ValueError('unsupported modal polarization')
        if row['normalization'] != 'power': raise ValueError('normalization must be power')
        if key in groups[f]: raise ValueError('duplicate modal descriptor at frequency')
        state = mode_state(case,f,row['m'],row['n'])
        real,imag = row['gamma_re_per_m'], row['gamma_im_per_m']
        expected_real, expected_imag = state['alpha_per_m'],state['kz_per_m']
        if not math.isclose(real,expected_real,rel_tol=gamma_rtol,abs_tol=gamma_atol) or not math.isclose(abs(imag),expected_imag,rel_tol=gamma_rtol,abs_tol=gamma_atol):
            raise ValueError('actual gamma inconsistent with vacuum lattice exp(-Gamma*z) convention')
        row['physical_state'] = state['state']; groups[f][key] = row; retained.append(row)
    for f in frequencies:
        if not required.issubset(groups[f]): raise ValueError(f'missing physical mode descriptors at frequency {f}')
    powers={}
    for raw in _read_csv(power_path,['frequency_Hz','incident_W','reflected_W','absorbed_W','transmitted_W']):
        row={key:_finite(raw[key],key) for key in ['frequency_Hz','incident_W','reflected_W','absorbed_W','transmitted_W']}
        f=row['frequency_Hz']
        if f not in planned: raise ValueError('unexpected power frequency')
        if f in powers: raise ValueError('duplicate power frequency')
        if row['incident_W'] <= 0 or any(row[x]<0 for x in ['reflected_W','absorbed_W','transmitted_W']): raise ValueError('positive incident and nonnegative exported powers required')
        powers[f]=row
    if set(powers) != planned: raise ValueError('missing planned power frequencies')
    reflection=[]; fundamental=[]; absorption=[]; transmission=[]; points=[]
    for f in frequencies:
        modal=groups[f]
        r=sum(row['s_re']**2+row['s_im']**2 for row in modal.values() if row['physical_state']=='propagating')
        r00=sum(row['s_re']**2+row['s_im']**2 for (m,n,p),row in modal.items() if m==n==0 and row['physical_state']=='propagating')
        power=powers[f]; incident=power['incident_W']
        rp,ap,tp=[power[key]/incident for key in ['reflected_W','absorbed_W','transmitted_W']]
        if abs(r-rp)>tolerance: raise ValueError('independent reflected power disagrees with normalized modal reflection')
        if abs(rp+ap+tp-1)>tolerance or abs(r+ap+tp-1)>tolerance: raise ValueError('power closure failed')
        if tp>tolerance: raise ValueError('PEC boundary requires zero transmitted power')
        reflection.append(r); fundamental.append(r00); absorption.append(ap); transmission.append(tp)
        points.append({'frequency_Hz':f,'R':r,'R00':r00,'A':ap,'T':tp,'power_closure_residual':r+ap+tp-1})
    metrics=band_metrics(frequencies,reflection,analysis['band_Hz'],analysis['max_gap_Hz'],analysis.get('threshold_R',.1))
    absorption_metrics=band_metrics(frequencies,absorption,analysis['band_Hz'],analysis['max_gap_Hz'],analysis.get('threshold_R',.1))
    metrics['average_A']=absorption_metrics['average_R']
    metrics['average_A_definition']='piecewise-linear integral of exported absorbed_W / incident_W, normalized by full band width; withheld if incomplete'
    result={'schema_version':'1.0','case_id':case.get('id'),'status':'screening_only','quality_state':'screening_only',
            'numerically_qualified':False,'execution_verified_by_analyzer':False,'solver_evidence':'not_verified',
            'frequency_Hz':frequencies,'R':reflection,'R00':fundamental,'A':absorption,'T':transmission,
            'T_source':'independent_export_checked_against_pec_boundary','T_boundary':0.0,'T_boundary_source':'PEC_boundary_assumption',
            'RLtotal_dB':[_db(x) for x in reflection], 'RL00_dB':[_db(x) for x in fundamental],
            'zero_R':[x==0 for x in reflection],'zero_R00':[x==0 for x in fundamental],
            'RL00_definition':'total fundamental reflected power, including cross polarization',
            'points':points,'analysis':analysis,
            'metrics':metrics,
            'provenance':{'modal_rows':retained,'power_rows':[powers[f] for f in frequencies],
                          'spectra_sha256':hashlib.sha256(Path(spectra_path).read_bytes()).hexdigest(),
                          'power_sha256':hashlib.sha256(Path(power_path).read_bytes()).hexdigest(),
                          'incident_polarization':case['scenario']['polarization'], 'power_tolerance':tolerance,
                          'gamma_relative_tolerance':gamma_rtol,'gamma_absolute_tolerance_per_m':gamma_atol,
                          'scope':'file analysis only; solver origin and convergence are not verified',
                          'gamma_convention':'exp(-Gamma*z); abs(imag Gamma)=vacuum kz',
                          'required_modes':[{'m':m,'n':n,'polarization':p} for m,n,p in sorted(required)]}}
    return result
