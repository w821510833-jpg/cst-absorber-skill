import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
try:
    from cst_absorber.plotting import plot_rl
except ImportError:
    plot_rl = None

class PlottingTests(unittest.TestCase):
    def test_real_exports_and_independent_rerun(self):
        self.assertIsNotNone(plot_rl, 'real Matplotlib export implementation missing')
        result={'frequency_Hz':[1e9,1.1e9,2e9,2.1e9], 'R':[.1,1e-8,.2,0], 'R00':[.09,1e-8,.18,0],
                'RLtotal_dB':[-10,-80,-6.9897,None], 'RL00_dB':[-10.4576,-80,-7.4473,None], 'zero_R':[False,False,False,True],
                'analysis':{'band_Hz':[1e9,2.1e9],'max_gap_Hz':.2e9,'threshold_R':.1}, 'status':'screening_only', 'CST_execution':'not_run'}
        case={'id':'synthetic','materials':{'demo':{'measured_ranges_Hz':{'epsilon':[1e9,1.1e9],'mu':[1e9,2.1e9]}}}}
        with tempfile.TemporaryDirectory() as directory:
            paths=plot_rl(result,Path(directory),case,{'dpi':300, 'font_family':['DejaVu Sans'], 'linewidth':1.3})
            for key in ['png','svg','csv','script','style','metadata']:
                self.assertTrue(Path(paths[key]).is_file(), key)
            svg=Path(paths['svg']).read_text(encoding='utf-8')
            self.assertIn('<text',svg)
            self.assertIn('screening_only',svg)
            self.assertIn('demo: measured epsilon',svg)
            metadata=json.loads(Path(paths['metadata']).read_text())
            self.assertLess(metadata['ylim_dB'][0], -80)
            self.assertEqual(metadata['segment_count'],2)
            self.assertEqual(metadata['dpi'],300)
            self.assertIn('matplotlib',metadata['versions'])
            self.assertIn('demo',metadata['material_domains'])
            self.assertLess(metadata['xlim_Hz'][0],result['frequency_Hz'][0])
            self.assertGreater(metadata['xlim_Hz'][1],result['frequency_Hz'][-1])
            from PIL import Image
            with Image.open(paths['png']) as png:
                self.assertGreaterEqual(png.info['dpi'][0],299)
            rerun=subprocess.run([sys.executable,paths['script']],cwd=directory,capture_output=True,text=True)
            self.assertEqual(rerun.returncode,0,rerun.stderr)

    def test_requested_low_resolution_is_rejected(self):
        self.assertIsNotNone(plot_rl)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'300'):
                plot_rl({},Path(directory),style={'dpi':72})

    def test_exact_zero_fundamental_has_labeled_display_marker(self):
        self.assertIsNotNone(plot_rl)
        result={'frequency_Hz':[1e9,2e9],'R':[.1,.1],'R00':[0,.1],
                'RLtotal_dB':[-10,-10],'RL00_dB':[None,-10],'zero_R':[False,False],
                'analysis':{'band_Hz':[1e9,2e9],'max_gap_Hz':1e9}}
        with tempfile.TemporaryDirectory() as directory:
            paths=plot_rl(result,Path(directory))
            svg=Path(paths['svg']).read_text(encoding='utf-8')
            self.assertIn('Exact R00=0',svg)
            self.assertIn('zero_R00',Path(paths['csv']).read_text())

    def test_single_actual_frequency_is_rendered(self):
        self.assertIsNotNone(plot_rl)
        result={'frequency_Hz':[1e9],'R':[.1],'R00':[.1],
                'RLtotal_dB':[-10],'RL00_dB':[-10],'zero_R':[False],
                'analysis':{'band_Hz':[.5e9,1.5e9],'max_gap_Hz':1e9}}
        with tempfile.TemporaryDirectory() as directory:
            paths=plot_rl(result,Path(directory))
            self.assertTrue(Path(paths['png']).is_file())
            metadata=json.loads(Path(paths['metadata']).read_text())
            self.assertLess(metadata['xlim_Hz'][0],1e9)
            self.assertGreater(metadata['xlim_Hz'][1],1e9)

    def visual_result(self):
        return {'frequency_Hz':[1e9,1.2e9,2e9],'R':[.2,.04,0],'R00':[.2,.04,0],
                'RLtotal_dB':[-6.9897,-13.9794,None],'RL00_dB':[-6.9897,-13.9794,None],
                'zero_R':[False,False,True],'analysis':{'band_Hz':[1e9,2e9],'max_gap_Hz':1e9}}

    def test_domain_labels_are_physically_outside_plot_and_auto_floor_is_near_data(self):
        case={'materials':{'demo':{'measured_ranges_Hz':{'epsilon':[1e9,1.2e9],'mu':[1e9,2e9]}}}}
        with tempfile.TemporaryDirectory() as directory:
            paths=plot_rl(self.visual_result(),Path(directory),case)
            metadata=json.loads(Path(paths['metadata']).read_text())
            self.assertEqual(metadata.get('domain_label_placement'),'outside_plot_footer')
            self.assertLessEqual(metadata['domain_label_bbox_figure'][3],metadata['plot_bbox_figure'][1])
            self.assertGreaterEqual(metadata['domain_label_bbox_figure'][1],0)
            self.assertGreater(metadata['zero_display_floor_dB'],-25)
            self.assertLess(metadata['zero_display_floor_dB'],-19)
            self.assertEqual(metadata['zero_floor_policy'],'auto_below_finite_minimum')
            self.assertIsNone(json.loads(Path(paths['style']).read_text())['zero_floor_dB'])
            self.assertIn('demo: measured epsilon',Path(paths['svg']).read_text(encoding='utf-8'))

    def test_explicit_floor_is_preserved_and_invalid_floor_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=plot_rl(self.visual_result(),Path(directory),style={'zero_floor_dB':-60})
            metadata=json.loads(Path(paths['metadata']).read_text())
            self.assertEqual(metadata['zero_display_floor_dB'],-60)
            self.assertEqual(metadata['zero_floor_policy'],'explicit_below_finite_minimum')
            for floor in [-13.9794,-10,float('nan'),float('inf')]:
                with self.subTest(floor=floor):
                    with self.assertRaisesRegex(ValueError,'zero_floor_dB'):
                        plot_rl(self.visual_result(),Path(directory),style={'zero_floor_dB':floor})
