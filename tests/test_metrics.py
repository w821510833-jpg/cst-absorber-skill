import csv
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
try:
    from cst_absorber.metrics import analyze_exports, band_metrics
    from cst_absorber.modal import required_modes, mode_state
except ImportError:
    analyze_exports = band_metrics = required_modes = mode_state = None

class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.case = {'id': 'synthetic', 'geometry': {'cell': {'Lx_m': .01, 'Ly_m': .01}},
                     'scenario': {'frequencies_Hz': [1e9, 1.2e9, 2e9], 'theta_deg': 0, 'azimuth_deg': 0, 'polarization': 'TE'},
                     'analysis': {'band_Hz': [1e9, 2e9], 'max_gap_Hz': 1e9, 'threshold_R': .1, 'power_tolerance': 1e-6}}
    def tearDown(self): self.tmp.cleanup()
    def exports(self, reflections=(.2, .04, 0), mutation=None, power_mutation=None):
        self.assertIsNotNone(analyze_exports, 'CSV metrics implementation missing')
        rows = []; powers = []
        for f, r in zip(self.case['scenario']['frequencies_Hz'], reflections):
            for mode in required_modes(self.case):
                beta = mode_state(self.case, f, mode['m'], mode['n'])['kz_per_m']
                rows.append(dict(frequency_Hz=f, **mode, s_re=0, s_im=math.sqrt(r) if mode['polarization']=='TE' and mode['m']==mode['n']==0 else 0,
                                 gamma_re_per_m=0, gamma_im_per_m=beta, normalization='power'))
            powers.append(dict(frequency_Hz=f, incident_W=2, reflected_W=2*r, absorbed_W=2*(1-r), transmitted_W=0))
        if mutation: mutation(rows)
        if power_mutation: power_mutation(powers)
        for name, data in [('spectra.csv', rows), ('power.csv', powers)]:
            with (self.root / name).open('w', newline='') as handle:
                writer=csv.DictWriter(handle, fieldnames=list(data[0])); writer.writeheader(); writer.writerows(data)
        return self.root/'spectra.csv', self.root/'power.csv'

    def test_complex_s_real_axis_zero_and_pec_provenance(self):
        result = analyze_exports(self.case, *self.exports())
        self.assertEqual(result['frequency_Hz'], [1e9, 1.2e9, 2e9])
        self.assertAlmostEqual(result['R'][0], .2)
        self.assertEqual(result['RLtotal_dB'][-1], None)
        self.assertTrue(result['zero_R'][-1])
        self.assertEqual(result['T_source'], 'independent_export_checked_against_pec_boundary')
        self.assertFalse(result['numerically_qualified'])
        self.assertEqual(result['status'], 'screening_only')
        json.dumps(result, allow_nan=False)
        self.assertEqual(result['provenance']['modal_rows'][0]['s_re'], 0)

    def test_rejects_untrusted_normalization_gamma_and_missing_cross_polar(self):
        for mutation, message in [(lambda rows: rows[0].update(normalization='voltage'), 'normalization'),
                                  (lambda rows: rows[0].update(gamma_im_per_m=1), 'gamma'),
                                  (lambda rows: rows.pop(1), 'missing'),
                                  (lambda rows: rows.append(dict(rows[0])), 'duplicate')]:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message): analyze_exports(self.case, *self.exports(mutation=mutation))

    def test_rejects_closure_and_reflection_power_mismatch(self):
        for mutation in [lambda rows: rows[0].update(absorbed_W=0), lambda rows: rows[0].update(reflected_W=.6, absorbed_W=1.4), lambda rows: rows[0].update(transmitted_W=.1)]:
            with self.assertRaises(ValueError): analyze_exports(self.case, *self.exports(power_mutation=mutation))

    def test_required_orders_outside_configured_modes_must_be_exported(self):
        self.case['geometry']['cell']['Lx_m']=1
        with self.assertRaisesRegex(ValueError, 'missing'):
            analyze_exports(self.case, *self.exports(mutation=lambda rows: rows.__setitem__(slice(None), [x for x in rows if x['m']==0])))

    def test_evanescent_amplitude_is_retained_and_excluded_from_power(self):
        self.case['geometry']['cell']['Lx_m'] = .2
        def add_evanescent(rows):
            for row in rows:
                state = mode_state(self.case,row['frequency_Hz'],row['m'],row['n'])
                row['gamma_re_per_m'] = state['alpha_per_m']
                if state['state']=='evanescent': row['s_re']=2
        result=analyze_exports(self.case,*self.exports(mutation=add_evanescent))
        self.assertAlmostEqual(result['R'][0],.2)
        self.assertTrue(any(x['physical_state']=='evanescent' and x['s_re']==2 for x in result['provenance']['modal_rows']))

    def test_missing_frequency_and_nonfinite_export_fail(self):
        for mutation in [lambda rows: rows.__setitem__(slice(None),[x for x in rows if x['frequency_Hz']!=1.2e9]),lambda rows: rows[0].update(s_re='nan')]:
            with self.assertRaises(ValueError): analyze_exports(self.case,*self.exports(mutation=mutation))

    def test_raw_reflection_above_unity_within_tolerance_is_unclipped(self):
        self.case['analysis']['power_tolerance']=1e-4
        result=analyze_exports(self.case,*self.exports(reflections=(1.00001,.04,0),power_mutation=lambda rows:rows[0].update(absorbed_W=0)))
        self.assertGreater(result['R'][0],1)
        self.assertGreater(result['RLtotal_dB'][0],0)

    def test_nonuniform_integral_average_and_sampled_minimum(self):
        self.assertIsNotNone(band_metrics)
        metrics = band_metrics([0, 1, 4], [.4, 0, .2], [0, 4], 3, .1)
        self.assertAlmostEqual(metrics['average_R'], .125)
        self.assertEqual(metrics['sampled_min_R'], 0)
        self.assertEqual(metrics['sampled_min_frequency_Hz'], 1)

    def test_total_and_longest_crossing_widths(self):
        self.assertIsNotNone(band_metrics)
        metrics=band_metrics([0,1,2,3,4], [0,.2,0,.2,0], [0,4], 1, .1)
        self.assertAlmostEqual(metrics['total_threshold_width_Hz'], 2)
        self.assertAlmostEqual(metrics['longest_threshold_width_Hz'], 1)
        self.assertEqual(band_metrics([0,1],[.2,.2],[0,1],1,.1)['total_threshold_width_Hz'],0)
        self.assertEqual(band_metrics([0,1],[0,0],[0,1],1,.1)['total_threshold_width_Hz'],1)

    def test_gaps_and_incomplete_band_withhold_average(self):
        self.assertIsNotNone(band_metrics)
        metrics=band_metrics([0,1,4], [.2,.1,.2], [0,4], 1, .1)
        self.assertIsNone(metrics['average_R'])
        self.assertAlmostEqual(metrics['coverage_fraction'], .25)
        self.assertFalse(metrics['fully_covered'])
        self.assertIsNone(band_metrics([1,2],[.1,.1],[0,2],1,.1)['average_R'])

    def test_band_endpoints_are_interpolated_in_linear_R(self):
        self.assertIsNotNone(band_metrics)
        metrics=band_metrics([0,2],[0,.4],[.5,1.5],2,.2)
        self.assertAlmostEqual(metrics['average_R'],.2)
        self.assertAlmostEqual(metrics['total_threshold_width_Hz'],.5)
        self.assertIsNone(metrics['sampled_min_R'])

    def test_single_frequency_is_diagnostic_with_zero_covered_width(self):
        self.assertIsNotNone(band_metrics)
        metrics=band_metrics([1],[0],[0,2],1,.1)
        self.assertEqual(metrics['sampled_min_R'],0)
        self.assertEqual(metrics['total_threshold_width_Hz'],0)
        self.assertEqual(metrics['coverage_fraction'],0)
        self.assertIsNone(metrics['average_R'])

    def test_average_absorption_integrates_actual_power_on_nonuniform_axis(self):
        self.case['analysis']['power_tolerance']=1e-4
        result=analyze_exports(self.case,*self.exports(power_mutation=lambda rows:rows[0].update(absorbed_W=1.60002)))
        self.assertAlmostEqual(result['metrics'].get('average_A',0),.960001)
        self.assertIn('10*log10(average_R)',result['metrics'].get('average_RL_definition',''))

    def test_average_absorption_is_withheld_for_gap(self):
        self.case['analysis']['max_gap_Hz']=.3e9
        result=analyze_exports(self.case,*self.exports())
        self.assertIn('average_A',result['metrics'])
        self.assertIsNone(result['metrics']['average_A'])

    def test_tiny_transmission_preserves_raw_ratio_and_distinct_boundary_zero(self):
        result=analyze_exports(self.case,*self.exports(power_mutation=lambda rows:rows[0].update(transmitted_W=2e-7,absorbed_W=1.5999998)))
        self.assertAlmostEqual(result['T'][0],1e-7)
        self.assertEqual(result.get('T_boundary'),0.0)
        self.assertEqual(result.get('T_boundary_source'),'PEC_boundary_assumption')
