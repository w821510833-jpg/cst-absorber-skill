"""Invocation association tests using existing synthetic software inputs only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from cst_absorber.contracts import build_plan
try:
    from cst_absorber.native_provenance import make_solver_binding, bind_raw_provenance
except ImportError:
    make_solver_binding = bind_raw_provenance = None


class NativeProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.case = build_plan(json.loads((ROOT / 'examples/single.json').read_text(encoding='utf-8')),
                               ROOT / 'examples')['cases'][0]
        (self.root / 'inputs.json').write_text(json.dumps(self.case), encoding='utf-8')
        (self.root / 'runtime.json').write_text('{"max_cpus":2}', encoding='utf-8')
        (self.root / 'model.cst').write_bytes(b'ORIGINAL_SYNTHETIC_SOFTWARE_FIXTURE')
        self.completion = {'completion_method': 'Model3D.run_solver', 'operation_id': 'a'*32,
                           'archive_integrity': {'status': 'pinned', 'archive_integrity_verified': True,
                               'sha256': hashlib.sha256((self.root/'model.cst').read_bytes()).hexdigest()}}

    def binding(self):
        self.assertIsNotNone(make_solver_binding, 'solver invocation binding is missing')
        return make_solver_binding(self.case, self.root, {'state': 'SUCCESS', 'message': None},
                                   completion=self.completion,
                                   process_identity={'session_id': 'fixture-only'},
                                   evidence_kind='injected_test_interface')

    def test_binding_retains_actual_info_without_inventing_native_run_id(self):
        binding = self.binding()
        self.assertEqual(binding['solver_info'], {'state': 'SUCCESS', 'message': None})
        self.assertEqual(binding['case_signature'], self.case['signature'])
        self.assertTrue(binding['input_snapshot']['matches_prepared_case'])
        self.assertIsNone(binding['native_run_id'])
        self.assertFalse(binding['native_run_linkage_verified'])
        self.assertFalse(binding['numerically_qualified'])

    def test_changed_immutable_inputs_cannot_be_bound_to_config(self):
        self.assertIsNotNone(make_solver_binding, 'solver invocation binding is missing')
        changed = copy.deepcopy(self.case); changed['id'] = 'changed'
        (self.root/'inputs.json').write_text(json.dumps(changed), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'input.*case|case.*input'):
            self.binding()

    def test_different_archive_content_cannot_use_completion_pin(self):
        self.assertIsNotNone(make_solver_binding, 'solver invocation binding is missing')
        (self.root/'model.cst').write_bytes(b'OTHER_BYTES')
        with self.assertRaisesRegex(ValueError, 'archive|digest'):
            self.binding()

    def test_curve_association_keeps_actual_role_and_unknown_solver_linkage(self):
        binding = self.binding()
        self.assertIsNotNone(bind_raw_provenance, 'raw provenance is missing')
        frequencies = self.case['scenario']['frequencies_Hz']
        raw = {'project_path': str(self.root/'model.cst'), 'status':'completed',
               'curves': [{'treepath': "1D Results\\Materials\\mat_a\\Dispersive\\Eps' (FD - Interpolated)",
                           'run_id':0, 'parameters':{}, 'module_parameters':{},
                           'xlabel':'Frequency / GHz', 'x':[x/1e9 for x in frequencies]}]}
        before = copy.deepcopy(raw)
        provenance = bind_raw_provenance(raw, binding, self.case)
        self.assertEqual(raw, before)
        self.assertEqual(provenance['curves'][0]['source_role'], 'fd_interpolated')
        self.assertEqual(provenance['curves'][0]['actual_frequencies_Hz'], frequencies)
        self.assertFalse(provenance['solver_material_linkage_verified'])
        self.assertFalse(provenance['native_run_linkage_verified'])
        self.assertIsNone(provenance['actual_fd_policy'])
        self.assertIsNone(provenance['actual_reference_plane_m'])

    def test_raw_from_other_project_is_not_associated(self):
        binding = self.binding()
        self.assertIsNotNone(bind_raw_provenance, 'raw provenance is missing')
        provenance = bind_raw_provenance({'project_path':str(self.root/'other.cst'),'curves':[]}, binding, self.case)
        self.assertFalse(provenance['same_owned_project'])
        self.assertIn('raw_project_association', provenance['unresolved_gates'])

    def test_raw_binding_rechecks_archive_before_reusing_hash(self):
        binding = self.binding()
        raw = {'project_path':str(self.root/'model.cst'),'curves':[]}
        (self.root/'model.cst').write_bytes(b'REPLACED_AFTER_BINDING')
        provenance = bind_raw_provenance(raw,binding,self.case)
        self.assertFalse(provenance['same_owned_project'])
        self.assertIn('archive_binding_drift',provenance['unresolved_gates'])

    def test_hardlink_created_during_archive_hash_is_rejected(self):
        self.assertIsNotNone(make_solver_binding, 'solver invocation binding is missing')
        original = hashlib.sha256
        class RacingDigest:
            def __init__(inner, data=b''): inner.digest=original(data)
            def update(inner, block):
                inner.digest.update(block)
                if block == b'ORIGINAL_SYNTHETIC_SOFTWARE_FIXTURE':
                    os.link(self.root/'model.cst',self.root/'archive-alias')
            def hexdigest(inner): return inner.digest.hexdigest()
        with patch('cst_absorber.native_provenance.hashlib.sha256',RacingDigest):
            with self.assertRaisesRegex(ValueError,'alias|changed|regular'):
                self.binding()

    def test_input_hash_and_parser_share_the_same_descriptor_bytes(self):
        self.assertIsNotNone(make_solver_binding, 'solver invocation binding is missing')
        inputs=self.root/'inputs.json'; original_read=Path.read_text
        wrong=copy.deepcopy(self.case); wrong['id']='wrong-input'
        wrong_bytes=json.dumps(wrong).encode('utf-8'); inputs.write_bytes(wrong_bytes)
        stamp=inputs.stat()
        def race(path,*args,**kwargs):
            if path.resolve()==inputs.resolve():
                inputs.write_text(json.dumps(self.case),encoding='utf-8')
                value=original_read(path,*args,**kwargs)
                inputs.write_bytes(wrong_bytes)
                os.utime(inputs,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
                return value
            return original_read(path,*args,**kwargs)
        with patch.object(Path,'read_text',race):
            with self.assertRaisesRegex(ValueError,'input.*case|case.*input'):
                self.binding()

    def test_resolving_bound_link_cannot_hide_reparse_guard(self):
        binding=self.binding(); bound=Path(binding['project_path'])
        relocated=bound.with_name('relocated.cst'); bound.replace(relocated)
        original_resolve=Path.resolve; original_lstat=Path.lstat
        def resolve(path,*args,**kwargs):
            if path==bound: return relocated
            return original_resolve(path,*args,**kwargs)
        def lstat(path,*args,**kwargs):
            if path==bound:
                return SimpleNamespace(st_mode=stat.S_IFLNK,st_file_attributes=0x400)
            return original_lstat(path,*args,**kwargs)
        with patch.object(Path,'resolve',resolve), patch.object(Path,'lstat',lstat):
            provenance=bind_raw_provenance({'project_path':str(bound),'curves':[]},binding,self.case)
        self.assertFalse(provenance['same_owned_project'])
        self.assertIn('archive_binding_drift',provenance['unresolved_gates'])

    def test_binding_checks_caller_budget_before_and_during_archive_hash(self):
        self.assertIsNotNone(make_solver_binding)
        calls=[]
        def stopped():
            calls.append(True)
            raise RuntimeError('caller budget expired')
        with self.assertRaisesRegex(RuntimeError,'caller budget expired'):
            make_solver_binding(self.case,self.root,{'state':'SUCCESS'},completion=self.completion,
                                checkpoint=stopped)
        self.assertTrue(calls)

    def test_raw_revalidation_checks_stop_during_archive_read(self):
        binding=self.binding(); original=hashlib.sha256; stopped=[False]
        class StopDigest:
            def __init__(inner,data=b''): inner.digest=original(data)
            def update(inner,block):
                inner.digest.update(block); stopped[0]=True
            def hexdigest(inner): return inner.digest.hexdigest()
        def checkpoint():
            if stopped[0]: raise RuntimeError('caller stopped')
        with patch('cst_absorber.native_provenance.hashlib.sha256',StopDigest):
            with self.assertRaisesRegex(RuntimeError,'caller stopped'):
                bind_raw_provenance({'project_path':binding['project_path'],'curves':[]},binding,self.case,
                                    checkpoint=checkpoint)


if __name__ == '__main__':
    unittest.main()
