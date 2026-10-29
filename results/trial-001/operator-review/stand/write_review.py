"""Assemble AI-assisted review evidence; JSON/text only, no CAD/source execution."""
import hashlib,json,math,sys
from pathlib import Path

ROOT=Path('results/trial-001/operator-review/stand')
PROJECT=Path('@ORIGINAL_PROJECT@')
SNAPSHOT=PROJECT/'trials/trial-001/snapshots/stable-000029'
EVALUATION=PROJECT/'trials/trial-001/checkpoint-35-evaluation/evaluation.json'
EVALUATOR=PROJECT/'quality/evaluator'
sys.path.insert(0,str(EVALUATOR))
from scoring import digest_json,score,evaluate_measurement,load_operator_review

def load(path):return json.loads(Path(path).read_text())
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def save(path,value):Path(path).write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')
rubric=load(PROJECT/'quality/frozen/rubric.json')
evaluation=load(EVALUATION)
manifest=load(SNAPSHOT/'.snapshot-manifest.json')
snapshot_sha=digest_json(manifest['files'])
assert snapshot_sha==evaluation['snapshot_sha256']
assert sha(PROJECT/'quality/frozen/rubric.json')==evaluation['rubric_sha256']
for name,entry in manifest['files'].items():
    assert sha(SNAPSHOT/name)==entry['sha256'],name
data_path=ROOT/'evidence/work/independent-measurements.json'
contact_path=ROOT/'evidence/work-contact/contact-and-holes.json'
data=load(data_path);contact=load(contact_path)
for name,entry in data['files'].items():
    assert entry['sha256']==manifest['files'][name+'.step']['sha256']
assert load(ROOT/'evidence/control/worker-result.json')['success'] is True
assert load(ROOT/'evidence/control-contact/worker-result.json')['success'] is True
f={r['index']:r for r in data['stand_faces']}
m={r['index']:r for r in data['main_faces']}
datum=m[23]['cylinder']['origin']
measurements={
 'S4D1': {'satisfied':abs(f[0]['plane']['origin'][0]-m[47]['plane']['origin'][0])<=.01},
 'S4D2': {'satisfied': abs((f[13]['plane']['origin'][1]+f[14]['plane']['origin'][1])/2-datum[1])<=.01 and data['stand_main_overlap_mm3']==0 and all(x['area']==x['common_with_main_area'] for x in data['support_faces']) and contact['shoulder_contact']['area']==contact['shoulder_contact']['common_area']},
 'S4D3': {'satisfied':all(abs(f[i]['plane']['origin'][2]-datum[2])<=.01 for i in (6,20))},
 'S4D4': {'values':[f[13]['plane']['origin'][1]-f[14]['plane']['origin'][1]]},
 'S4D5': {'values':[f[9]['plane']['origin'][1]-f[10]['plane']['origin'][1],f[19]['plane']['origin'][1]-f[17]['plane']['origin'][1]]},
 'S4D6': {'values':[f[10]['plane']['origin'][1]-f[1]['plane']['origin'][1],f[3]['plane']['origin'][1]-f[19]['plane']['origin'][1]]},
 'S4D7': {'values':[f[i]['plane']['origin'][0]-f[0]['plane']['origin'][0] for i in (8,16)]},
 'S4D8': {'values':[f[11]['plane']['origin'][0]-f[8]['plane']['origin'][0],f[18]['plane']['origin'][0]-f[16]['plane']['origin'][0]]},
 'S4D9': {'values':[f[7]['plane']['origin'][0]-f[5]['plane']['origin'][0]]},
 'S4D10': {'values':[f[3]['plane']['origin'][1]-f[1]['plane']['origin'][1]]},
 'S4D11': {'values':[f[2]['plane']['origin'][2]-f[4]['plane']['origin'][2]]},
}
details={
 'S4D1':'Stand face0 and main face47 are planes at x=0: displacement0mm; shared assembly frame, no independent registration.',
 'S4D2':'Rib side faces13/14 lie at y=+/-7.5 (center y=0). Main axis is y=z=0. Rib cylinder face24 and cone face25, upright cone face21, and shoulder face26 each have exactly equal total/common-with-main surface areas. Stand-main overlap0mm3.',
 'S4D3':'Both upright shoulder planes (stand faces6,20) lie at z=0, identical to the main cylindrical axis z=0; measured shoulder angular directions are parallel to the transverse horizontal plane.',
 'S4D4':'Rib side planes13/14 at y=+7.5,-7.5 -> width15mm.',
 'S4D5':'Negative-y slot walls9/10 at y=-20,-28 and positive-y walls17/19 at y=20,28 -> widths8,8mm. Faces8-11 and16-19 establish two separate openings through z=-80..-70.',
 'S4D6':'Outer base sides1/3 at y=-35,+35; near outer slot walls10/19 at y=-28,+28 -> margins7,7mm.',
 'S4D7':'Both near slot end planes8/16 lie at x=30, base end plane0 at x=0 -> offsets30,30mm.',
 'S4D8':'Slot end planes8/11 and16/18 lie at x=30 and100 -> lengths70,70mm.',
 'S4D9':'Upright planes5/7 lie at x=120 and130 -> thickness10mm.',
 'S4D10':'Outside stand side planes1/3 lie at y=-35 and35 -> width70mm.',
 'S4D11':'Base lower/upper planes4/2 lie at z=-80,-70 -> thickness10mm.'}
observations={}
for cid,value in measurements.items():
    observations[cid]={**value,'feature_present':True,'all_instances_verified':True,
        'origin':'ai_assisted_operator_geometry_review','evidence':[str(data_path),details[cid]]}
observations['S4D2']['evidence'].append(str(contact_path))

source_review='''AI-assisted source/dependency review, not human signoff.
Submitted build.py was read as text only; it was not imported or executed by
this reviewer. Binding is in evidence/binding.json.

Master contact: build.py lines91-99 constructs exterior(p) using the shared
Dimensions object's body diameter, shoulder/front stations and taper angle.
stage1() uses that same master for the main body (111-119) and returns it.
build() obtains the master at211 and passes it to make_stand at218.
make_stand builds plate/rib stock then subtracts master_contact at192. This
actual cut drives cylinder, cone, shoulder, and upright supporting surfaces.
The exported stand is this function result, at219. It is not dead code.

Hole dependence: bolt_centers(pitch), lines88-89, supplies the main front
flange holes at117-118. make_stand uses the same p.front_hole_pitch and same
bolt_centers at197-200, filters the lower pair, and uses the same flange_hole_d.
Both parts share the same Dimensions instance P; coordinates are not re-entered
as independent literal hole locations. The upright axial station also uses
the main-body p.front_x at187 and200.

Independent geometry confirms the dependency's nominal output: support areas
match final main exactly, zero overlap, both upright/flange hole axes at
y=+/-25,z=-25 have radius5, and both nominal through cylinders x120..142 have
zero intersection volume with either part. Source comments are not the sole
evidence. The frozen independent clean rebuild has zero symmetric-difference
volume for all six required STEP exports, binding the executed procedural
source to submitted geometry. No parameter perturbation was needed or used.

Cumulative source chain: build lines211-217 passes stage1 -> stage2 -> stage3
and exports identical 'three' to step-3.step and main-part.step. Stage2 adds
the radiator and recuts bore_tools at131-133. Stage3 adds tabs, the two side
holes and the tilted port; it recuts the same bore_tools at172. Stand creation
uses an independent stock shape and does not replace/mutate 'three'.
'''
(ROOT/'evidence/source-dependency-review.txt').write_text(source_review)

keyway_source=Path('results/trial-001/operator-review/root/work/keyway.json')
stage23_source=Path('results/trial-001/operator-review/stage23/work/measured.json')
keyway=load(keyway_source);stage23=load(stage23_source)
save(ROOT/'evidence/root-keyway.json',keyway)
save(ROOT/'evidence/peer-stage23-measured.json',stage23)
cone=m[24]['cone']
final_bore_d=2*(cone['radius']-135*math.tan(cone['semi_angle_rad']))
assert m[45]['cone']==cone
assert abs(final_bore_d-30.849899593578)<1e-9
retention={'stage1_automatic_file':str(EVALUATION),'automatic_final_bore_status':'requires_review due to split conical faces, independently resolved for retention below',
 'automatic_scoring_behavior':'The frozen _observations adapter retains the correct stage1 S1D4 observation as a fallback because final_stage1 S1D4 is requires_review. This retention evidence does not silently reclassify the automatic final association or override the numbered criterion.',
 'final_main_cone_faces':[24,45],'analytic_cone':cone,'counterbore_shoulder_x_mm':135,
 'final_junction_diameter_mm':final_bore_d,'keyway_evidence':str(keyway_source),
 'stage2_preservation_region':stage23['stage2_preservation_region'],
 'stage3_main_difference':data['step3_main_difference'],
 'source_chain_evidence':str(ROOT/'evidence/source-dependency-review.txt')}
save(ROOT/'evidence/retention.json',retention)
assert all(stage23['stage2_preservation_region'][k]==0 for k in ('forward_difference_mm3','reverse_difference_mm3'))
assert all(v==0 for v in data['step3_main_difference'].values())
stand_components={
 'overall_height':{'values':[f[6]['plane']['origin'][2]-f[4]['plane']['origin'][2]]},
 'base_length':{'values':[f[7]['plane']['origin'][0]-f[0]['plane']['origin'][0]]},
 'slot_end_to_upright_inner_face':{'values':[f[5]['plane']['origin'][0]-f[i]['plane']['origin'][0] for i in (11,18)],'all_instances_verified':True},
 'slot_center_separation':{'values':[((f[17]['plane']['origin'][1]+f[19]['plane']['origin'][1])/2)-((f[9]['plane']['origin'][1]+f[10]['plane']['origin'][1])/2)]},
}
supplemental={
 'SUP-STAND-DIMENSIONS':{'components':stand_components,'feature_present':True,'evidence':[str(data_path),'Overall height80mm, base length130mm, both far slot ends x100 to upright inner face x120 ->20mm each, slot centers y=-24,+24 ->48mm. Derived from associated imported B-rep planes, not candidate parameters.']},
 'SUP-SEPARATE-STAND':{'satisfied':True,'evidence':[str(data_path),'stand.step is one valid positive-volume solid (155495.5351022963mm3); main-part.step is a separate valid solid (504630.73717243987mm3). assembly.step contains two solids; independent symmetric-difference comparisons match one to main and one to stand, both0mm3. Stand supports body at its matched faces and overlap volume0mm3.']},
 'SUP-SUPPORT-DEPENDENCIES':{'satisfied':True,'evidence':[str(ROOT/'evidence/source-dependency-review.txt'),str(data_path),str(contact_path),str(EVALUATION)+' analysis.rebuild_comparison: all six export symmetric-difference volumes0mm3.','Submitted build.py SHA256 '+sha(SNAPSHOT/'build.py')+'; master contact source lines91-99,110-119,180-201,211-219; shared front-hole drivers lines88-89,117-118,197-200.']},
 'SUP-CUMULATIVE-GEOMETRY':{'satisfied':True,'evidence':[str(ROOT/'evidence/retention.json'),str(ROOT/'evidence/root-keyway.json'),str(ROOT/'evidence/peer-stage23-measured.json'),str(ROOT/'evidence/source-dependency-review.txt'),'Frozen automatic stage1/final measurements preserve flange/body/counterbore dimensions and hole conditions; independent final cone-face association resolves the split-bore retention ambiguity (diameter30.849899593579mm atx135). Root isolated measurements show identical stage1/final keyway planes and validity.','Independent final-vs-step2 radiator-region forward/reverse differences are both0mm3; region x[14,76],y[-31,31],z[20,95] contains exposed radiator and excludes later features. Step3/final main whole-solid differences are both0mm3. Source chain and clean rebuild bind the staged geometry; expected newly introduced cuts are not treated as retention failures.']},
}
header={'reviewer':'Codex AI-assisted operator review (stand/source/retention); not human signoff',
 'snapshot_sha256':snapshot_sha,'rubric_sha256':evaluation['rubric_sha256']}
save(ROOT/'observations.json',{**header,'observations':observations})
save(ROOT/'supplemental.json',{**header,'observations':{},'supplemental':supplemental})
save(ROOT/'review-combined.json',{**header,'observations':observations,'supplemental':supplemental})
binding={'snapshot_path':str(SNAPSHOT),'snapshot_files_digest_sha256':snapshot_sha,'snapshot_manifest_raw_sha256':sha(SNAPSHOT/'.snapshot-manifest.json'),
 'rubric_sha256':evaluation['rubric_sha256'],'all_snapshot_file_hashes_verified':True,'candidate_source_executed_by_this_reviewer':False,
 'build_py_sha256':sha(SNAPSHOT/'build.py'),'provided_evaluation_sha256':sha(EVALUATION),
 'peer_evidence':[{'source':str(p),'sha256':sha(p)} for p in (keyway_source,stage23_source)],
 'native_cad_workers':[{'record':str(p),'sha256':sha(p),'success':load(p)['success'],'worker_reaped':load(p)['worker_reaped']} for p in (ROOT/'evidence/control/worker-result.json',ROOT/'evidence/control-contact/worker-result.json')]}
save(ROOT/'evidence/binding.json',binding)
judged=score(rubric,observations)
rows=[r for r in judged['criteria'] if r['stage']==4]
assert len(rows)==11 and all(r['status']=='correct' for r in rows)
stand_rule=next(r for r in rubric['supplemental_requirements'] if r['id']=='SUP-STAND-DIMENSIONS')
stand_judgment=evaluate_measurement({**stand_rule,'stage':4},supplemental['SUP-STAND-DIMENSIONS'])
assert stand_judgment['status']=='correct'
load_operator_review(ROOT/'review-combined.json',snapshot_sha256=snapshot_sha,rubric_sha256=evaluation['rubric_sha256'],
 candidate_workspace=PROJECT/'sandbox/candidate/work',operator_root=ROOT,candidate_writable_roots=[PROJECT/'sandbox/candidate/tmp'])
save(ROOT/'evidence/scoring-validation.json',{'stage4':rows,'stand_dimensions':stand_judgment,'operator_review_schema':'passed'})
summary='''AI-ASSISTED OPERATOR REVIEW: STAND, DEPENDENCIES, AND RETENTION
Scope: immutable stable-000029 only. No human signoff is claimed.
No candidate/frozen files changed. Submitted source was read as data only.
All native STEP parsing/Boolean measurements used frozen run_isolated workers;
both worker records report success and confirmed reaping.

S4D1-S4D11: 11/11 correct under frozen tolerances and scoring.py.
S4D1 axial offset0mm; S4D2 centered/full supporting-face contact, overlap0mm3;
S4D3 both shoulders at main-axis height z0; S4D4 rib15mm;
S4D5 slot widths8,8mm; S4D6 margins7,7mm; S4D7 offsets30,30mm;
S4D8 slot lengths70,70mm; S4D9 upright10mm; S4D10 width70mm;
S4D11 base thickness10mm.

SUP-STAND-DIMENSIONS: satisfied; height80, length130, gaps20/20,
slot-center separation48mm, each judged with frozen0.01mm tolerance.
SUP-SEPARATE-STAND: satisfied; two matching distinct assembly components,
valid stand, independent component differences0mm3, body/stand overlap0mm3.
SUP-SUPPORT-DEPENDENCIES: satisfied; master exterior directly cuts support
stock, shared lower-flange hole drivers, matched nominal geometry, clean
rebuild equivalence. No optional perturbation was required or performed.
SUP-CUMULATIVE-GEOMETRY: satisfied using stage1/final analytic evidence plus
root keyway review, peer stage2 preservation comparison, and own whole-solid
step3/final comparison. Frozen automatic final S1D4 was requires_review due to
split cone faces; this review resolves retention from their identical analytic
surface and the x135 shoulder, rather than claiming the automatic item passed.
The frozen numbered S1D4 score used the correct stage1 fallback; no numbered
criterion override is needed or supplied by this stand review.

No unresolved stand-specific item or contradiction found. These findings do
not by themselves establish overall61-criterion acceptance; stages1-3 and
other gates remain the responsibility of the combined review.

Use observations.json and supplemental.json for merge, or review-combined.json
for the complete scoring.py operator-review schema. Evidence contains exact
face indices, measurements, source-line audit, snapshot bindings, worker policy
hashes, copied peer evidence, and schema/scoring validation.
'''
(ROOT/'summary.txt').write_text(summary)
print(json.dumps({'stage4_correct':len(rows),'supplemental_gates_satisfied':4,'schema_validation':'passed','snapshot_sha256':snapshot_sha},indent=2))
