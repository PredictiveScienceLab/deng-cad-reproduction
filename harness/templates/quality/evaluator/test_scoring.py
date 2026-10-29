import copy
import json
from pathlib import Path
import tempfile
import unittest
from scoring import score,evaluate_measurement,validate_rubric,load_operator_review

RUBRIC=Path(__file__).resolve().parents[1]/'frozen/rubric.json'

class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.rubric=json.loads(RUBRIC.read_text());self.items={x['id']:x for x in self.rubric['criteria']}
    def observation(self,values):return {'values':values,'feature_present':True,'all_instances_verified':True,'conditions':{'through':True},'evidence':['Synthetic evaluator test measurement']}
    def test_exact_ids_and_denominator(self):
        self.assertEqual(len(validate_rubric(self.rubric)),61)
        for modification in ('drop','duplicate','denominator'):
            r=copy.deepcopy(self.rubric)
            if modification=='drop':r['criteria'].pop()
            if modification=='duplicate':r['criteria'][-1]=r['criteria'][0]
            if modification=='denominator':r['scoring']['adapted_denominator']=60
            with self.assertRaises(ValueError):validate_rubric(r)
    def test_every_rubric_item_accepts_complete_synthetic_measurement_evidence(self):
        # This tests scorer completeness only. Synthetic measurements are not
        # evidence that any real CAD artifact satisfies the full task.
        observations={}
        for c in self.rubric['criteria']:
            e=c['expected'];o={'feature_present':True,'evidence':['Synthetic scorer fixture, not CAD validation'],'all_instances_verified':True,'conditions':{x['id']:True for x in c.get('required_conditions',[])}}
            if e['type']=='numeric':o['values']=[e['value']]*e.get('multiplicity',1)
            elif e['type']=='qualitative':o['satisfied']=True
            else:
                o['components']={x['name']:({'satisfied':True} if 'relation' in x else {'values':[x['value']]*x.get('multiplicity',1),'all_instances_verified':True}) for x in e['components']}
            observations[c['id']]=o
        r=score(self.rubric,observations)
        self.assertEqual(r['confirmed_correct'],61);self.assertTrue(r['all_criteria_correct'])
    def test_derived_area_uses_component_rounding_interval(self):
        o={'feature_present':True,'evidence':['Synthetic selected tilted-bore face-area fixture'],'components':{'inner_bore_surface_area':{'values':[815.24]},'through_connection_to_main_bore':{'satisfied':True}}}
        self.assertEqual(evaluate_measurement(self.items['S3D1'],o)['status'],'correct')
        o['components']['inner_bore_surface_area']['values']=[815.35]
        self.assertEqual(evaluate_measurement(self.items['S3D1'],o)['status'],'incorrect')
    def test_repeated_pattern_requires_every_offset_and_fin(self):
        for cid,count in [('S1D6',8),('S1D18',8),('S2D4',10),('S2D6',20)]:
            c=self.items[cid];self.assertEqual(c['expected']['multiplicity'],count)
            self.assertEqual(evaluate_measurement(c,self.observation([c['expected']['value']]*count))['status'],'correct')
            self.assertEqual(evaluate_measurement(c,self.observation([c['expected']['value']]*2))['status'],'inconclusive')
    def test_unscored_stand_dimensions_are_numeric_acceptance_gates(self):
        from evaluate import _supplemental
        components={'overall_height':{'values':[80]},'base_length':{'values':[130]},'slot_end_to_upright_inner_face':{'values':[20,20],'all_instances_verified':True},'slot_center_separation':{'values':[48]}}
        review={'reviewer':'Synthetic reviewer','supplemental':{'SUP-STAND-DIMENSIONS':{'feature_present':True,'evidence':['Synthetic independent dimensional test'],'components':components}}}
        def gate():return next(x for x in _supplemental(self.rubric,{}, {'status':'fail'},review) if x['id']=='SUP-STAND-DIMENSIONS')
        self.assertEqual(gate()['status'],'pass');components['overall_height']['values']=[79]
        self.assertEqual(gate()['status'],'fail')
    def test_unmeasured_never_passes_or_shrinks_denominator(self):
        r=score(self.rubric,{})
        self.assertEqual(r['denominator'],61);self.assertEqual(r['confirmed_correct'],0)
        self.assertIsNone(r['conditional_correctness']);self.assertEqual(r['status_counts']['requires_review'],61)
        self.assertFalse(r['all_criteria_correct'])
    def test_boundary_and_single_wrong_dimension(self):
        for values,expected in [([100,100.01],'correct'),([100,100.0101],'incorrect'),([100,99.98],'incorrect')]:
            self.assertEqual(evaluate_measurement(self.items['S1D1'],self.observation(values))['status'],expected)
    def test_each_hole_must_pass(self):
        self.assertEqual(evaluate_measurement(self.items['S1D2'],self.observation([10,10,10,10.02]))['status'],'incorrect')
        self.assertEqual(evaluate_measurement(self.items['S1D2'],self.observation([10]))['status'],'inconclusive')
    def test_coverage_attestation_is_required(self):
        o=self.observation([10]*4);o.pop('all_instances_verified')
        self.assertEqual(evaluate_measurement(self.items['S1D2'],o)['status'],'requires_review')
    def test_rounding_interval_not_generic_linear_tolerance(self):
        for value,expected in [(30.8498997946,'correct'),(30.856,'correct'),(30.857,'incorrect')]:
            self.assertEqual(evaluate_measurement(self.items['S1D4'],self.observation(value))['status'],expected)
    def test_nonfinite_bool_or_text_measurements_do_not_pass(self):
        for value in (float('nan'),float('inf'),True,'45'):
            self.assertEqual(evaluate_measurement(self.items['S1D3'],self.observation(value))['status'],'inconclusive')
    def test_missing_evidence_does_not_pass(self):
        o=self.observation(45);o['evidence']=[]
        self.assertEqual(evaluate_measurement(self.items['S1D3'],o)['status'],'inconclusive')
    def test_absence_is_distinct_from_unmeasured(self):
        r=score(self.rubric,{'S1D3':{'feature_present':False,'evidence':['Independent model has no central opening']}})
        self.assertEqual(r['status_counts']['absent'],1);self.assertEqual(r['confirmed_attempted'],0)
    def test_alternative_requires_main_body_dimension(self):
        o=self.observation([25,25]);o['alternative']='S3D6-Alt'
        self.assertEqual(evaluate_measurement(self.items['S3D6'],o,{})['status'],'requires_review')
        self.assertEqual(evaluate_measurement(self.items['S3D6'],o,{'S1D7':{'status':'correct'}})['status'],'correct')
    def test_alternative_is_not_an_extra_criterion(self):
        with self.assertRaises(ValueError):score(self.rubric,{'S3D6-Alt':self.observation([25,25])})
    def test_blind_hole_diameter_depth_and_termination_all_required(self):
        o={'feature_present':True,'evidence':['Synthetic hole sections'],'components':{'diameter':{'values':[8.5,8.5],'all_instances_verified':True},'depth':{'values':[5,5],'all_instances_verified':True},'termination':{'satisfied':True}}}
        self.assertEqual(evaluate_measurement(self.items['S3D9'],o)['status'],'correct')
        o['components']['termination']['satisfied']=False
        self.assertEqual(evaluate_measurement(self.items['S3D9'],o)['status'],'incorrect')
        del o['components']['termination']
        self.assertEqual(evaluate_measurement(self.items['S3D9'],o)['status'],'requires_review')
    def test_partial_model_cannot_earn_full_coverage(self):
        r=score(self.rubric,{'S1D3':self.observation(45)})
        self.assertEqual(r['conditional_correctness'],1);self.assertEqual(r['overall_coverage'],1/61)
        self.assertTrue(r['metrics_provisional']);self.assertFalse(r['all_criteria_correct'])
    def test_structural_failure_cannot_be_overridden_by_correct_diameters(self):
        from evaluate import _observations
        row={'id':'S1D2','status':'fail','measurement':[10]*4,'evidence':[3],'reason':'A blind hole does not span the flange','through_hole_evidence':[{'through':False}]}
        observations=_observations({'stage1':{'criteria':[row]}},self.rubric)
        self.assertEqual(score(self.rubric,observations)['criteria'][1]['status'],'incorrect')
    def test_required_through_condition_must_be_explicit(self):
        o=self.observation([10]*4);o['conditions']={}
        self.assertEqual(evaluate_measurement(self.items['S1D2'],o)['status'],'requires_review')
    def test_review_binding_and_candidate_location(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as d:
            root=Path(d);work=root/'candidate';work.mkdir();operator=root/'operator';operator.mkdir();p=operator/'review.json'
            data={'reviewer':'Fixture operator','snapshot_sha256':'snap','rubric_sha256':'rubric','observations':{}}
            p.write_text(json.dumps(data))
            self.assertEqual(load_operator_review(p,snapshot_sha256='snap',rubric_sha256='rubric',candidate_workspace=work,operator_root=operator)['reviewer'],'Fixture operator')
            with self.assertRaises(ValueError):load_operator_review(p,snapshot_sha256='different',rubric_sha256='rubric',candidate_workspace=work,operator_root=operator)
            bad=work/'review.json';bad.write_text(json.dumps(data))
            with self.assertRaises(ValueError):load_operator_review(bad,snapshot_sha256='snap',rubric_sha256='rubric',candidate_workspace=work,operator_root=operator)
    def test_review_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as d:
            root=Path(d);work=root/'candidate';work.mkdir();operator=root/'operator';operator.mkdir();p=operator/'review.json';p.write_text('{"reviewer":"a","reviewer":"b"}')
            with self.assertRaises(ValueError):load_operator_review(p,snapshot_sha256='s',rubric_sha256='r',candidate_workspace=work,operator_root=operator)
    def test_supplemental_evidence_cannot_be_a_boolean(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as d:
            root=Path(d);work=root/'candidate';work.mkdir();operator=root/'operator';operator.mkdir();p=operator/'review.json'
            data={'reviewer':'Fixture reviewer','snapshot_sha256':'s','rubric_sha256':'r','observations':{},'supplemental':{'SUP-SUPPORT-DEPENDENCIES':{'satisfied':True,'evidence':True}}}
            p.write_text(json.dumps(data))
            with self.assertRaises(ValueError):load_operator_review(p,snapshot_sha256='s',rubric_sha256='r',candidate_workspace=work,operator_root=operator)
    def test_operator_root_cannot_overlap_private_scratch(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as d:
            root=Path(d);work=root/'candidate';work.mkdir();operator=root/'scratch';operator.mkdir();p=operator/'review.json'
            p.write_text('{}')
            with self.assertRaises(ValueError):load_operator_review(p,snapshot_sha256='s',rubric_sha256='r',candidate_workspace=work,operator_root=operator,candidate_writable_roots=[operator])
    def test_operator_review_fifo_rejected_without_wait(self):
        import os
        with tempfile.TemporaryDirectory(dir='/private/tmp') as d:
            root=Path(d);work=root/'candidate';work.mkdir();operator=root/'operator';operator.mkdir();p=operator/'review.json';os.mkfifo(p)
            with self.assertRaises(ValueError):load_operator_review(p,snapshot_sha256='s',rubric_sha256='r',candidate_workspace=work,operator_root=operator)
    def test_operator_pose_review_requires_evidence_and_rigid_matrix(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as d:
            root=Path(d);work=root/'candidate';work.mkdir();operator=root/'operator';operator.mkdir();p=operator/'review.json'
            data={'reviewer':'Fixture operator','snapshot_sha256':'s','rubric_sha256':'r','observations':{},'component_pose_review':{'poses':{'stand':[[1,0,0,120],[0,1,0,0],[0,0,1,0],[0,0,0,1]]},'evidence':['Independent assembly coordinate measurements']}}
            def read():
                p.write_text(json.dumps(data));return load_operator_review(p,snapshot_sha256='s',rubric_sha256='r',candidate_workspace=work,operator_root=operator)
            self.assertEqual(read()['component_pose_review']['poses']['stand'][0][3],120)
            data['component_pose_review']['poses']['stand'][0][0]=2
            with self.assertRaises(ValueError):read()
            data['component_pose_review']['poses']={};data['component_pose_review']['evidence']=[]
            with self.assertRaises(ValueError):read()

if __name__=='__main__':unittest.main()
