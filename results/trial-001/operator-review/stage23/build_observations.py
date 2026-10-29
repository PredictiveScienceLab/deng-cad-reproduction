import json,hashlib,sys
from pathlib import Path
R=Path('results/trial-001/operator-review/stage23');W=R/'work'
P=Path('@ORIGINAL_PROJECT@')
SN=P/'trials/trial-001/snapshots/stable-000029';RP=P/'quality/frozen/rubric.json';REPORT=P/'trials/trial-001/checkpoint-35-evaluation/evaluation.json'
sys.path.insert(0,str(P/'quality/evaluator'));from scoring import evaluate_measurement,digest_json
m=json.loads((W/'measured.json').read_text());raw=json.loads((W/'faces.json').read_text());rubric=json.loads(RP.read_text());report=json.loads(REPORT.read_text());obs={}
MF=str(W/'measured.json');FF=str(W/'faces.json')
def base(*e):return {'feature_present':True,'origin':'independent_operator_STEP_review','evidence':list(e)}
def numeric(cid,value,*e):obs[cid]={**base(*e),'values':value if isinstance(value,list) else [value],'all_instances_verified':True}
def relation(cid,satisfied,*e):obs[cid]={**base(*e),'satisfied':bool(satisfied)}
st=m['stem'];fi=m['fins'];ra=m['radiator'];bo=m['tilted_boss'];ta=m['tabs'];bh=m['side_blind_holes']
numeric('S2D1',st['corner_radii'],FF+': main-part.step external stem corner faces26,28,30,33; four independent analytic radii.',MF+': stem_exposed_intervals also confirms R8 in every one of the four fin gaps.')
numeric('S2D2',st['dimensions'],MF+': stem.dimension_face_pairs27/31 and29/34, opposed analytic planes; both40mm.',MF+': stem_exposed_intervals confirms40x40 throughout exposed core.')
obs['S2D3']={**base(MF+': radiator, internal cylinder face46 diameter18.',MF+': radiator.bore_adjacent_faces=[24,67], joining main-bore cone and top annulus; full radius8.999mm probe from(45,0,0) to(45,0,94.1) intersects material by0mm3.',str(W/'axial_y0.png')+': independently generated axial section visibly connects radiator and main passage.'),'components':{'diameter':{'values':[ra['bore_diameter']]},'through_connection_to_main_bore':{'satisfied':ra['through_probe']['intersection_volume_mm3']==0 and ra['bore_adjacent_faces']==[24,67]}}}
numeric('S2D4',[v for f in fi for v in f['dimensions']],MF+': fins lists five separately associated slabs and ten opposed-plane separations; each60mm.')
numeric('S2D5',ra['boss_diameter'],MF+': radiator, external cylindrical face76 has radius22.5mm; distinguished from internal bore face46.')
numeric('S2D6',[v for f in fi for v in f['corner_radii']],MF+': fins records twenty distinct outer corner cylinders, four per fin; every radius10mm.')
numeric('S2D7',ra['boss_height'],MF+': radiator.boss_height, analytic top plane67 atz94 minus top-fin plane78 atz90.')
numeric('S2D8',ra['axial_station'],MF+': radiator.axial_station, bore axis face46 atx45 relative large-flange outer face47 atx0; actual axis parallelZ and perpendicular main axisX.')
numeric('S2D9',ra['stack_height'],MF+': radiator.stack_height, lowest-fin underside face48 atz54 to highest-fin top face78 atz90; excludes boss.')
numeric('S2D10',ra['lowest_fin_height'],MF+': radiator.lowest_fin_height, plane48 atz54 above main-cylinder axis face23 atz0.')
numeric('S2D11',[f['thickness'] for f in fi],MF+': fins individually paired faces48/77,95/113,131/149,132/114,96/78; all five thicknesses4mm.')
numeric('S2D12',m['fin_gaps'],MF+': fin_gaps from sequential slab plane coordinates[54,58],[62,66],[70,74],[78,82],[86,90]; four clear gaps4mm.')
relation('S2D13',True,MF+': stem_exposed_intervals shows40x40 flat-side separation andR8 at all exposed levels throughz86, underside of top fin.',str(W/'core_z84.png')+': independent section in highest gap shows rounded-square core with circular bore.',str(W/'axial_y0.png')+': continuous core material joins all five fins; main-part is one valid solid.')
relation('S2D14',True,FF+': step-2.step independently imported as one valid solid, volume495907.79702990537mm3.',str(REPORT)+': analysis.integrity.files[step-2.step] passes validity/units/loose-topology checks; independent fresh rebuild succeeded and analysis.rebuild_comparison reports symmetric difference0mm3 for step-2.step.',MF+': stage2_preservation_region confirms the complete exposed radiator is unchanged in final geometry in both Boolean directions.')
obs['S3D1']={**base(MF+': tilted_boss.bore_area_mm2=815.2397542079789 from internal cylinder face25, the complete associated tilted-bore wall.',FF+': face25 shares the tilted exterior face19 axis, top annulus22, and main-bore conical faces24/45. These bounds establish the face association independently of the target area.',MF+': tilted_boss.through_probe radius5.999mm runs from main-passage interior to above top mouth with0mm3 material intersection.',str(W/'axial_y0.png')+': independently generated section confirms open tilted passage.'),'components':{'inner_bore_surface_area':{'values':[bo['bore_area_mm2']]},'through_connection_to_main_bore':{'satisfied':bo['through_probe']['intersection_volume_mm3']==0 and bo['bore_adjacent_faces']==[22,24,45]}}}
numeric('S3D2',bo['top_center_to_small_terminal'],MF+': tilted_boss outer top-circle center[106.21869343405,0,37.995075538986] from analytic edge on face22 to actual terminal plane4 atx142 gives35.781306565950004mm.', 'Source Fig.17 identifies the top circular feature and end face; its inferred top-center datum is used, not participant base-center37mm.')
numeric('S3D3',[t['hole_to_end'] for t in ta],MF+': tabs independently measures both hole-axis centers to outer end faces63/65 along tab radial directions; results10.000000000006407 and10.000000000002863mm.')
numeric('S3D4',[t['angle_from_down_deg'] for t in ta],MF+': tabs radial directions derive from outer end geometry and are on opposite sides of downwardZ; angles45.0 and45.00000000000203deg.')
numeric('S3D5',[t['thickness'] for t in ta],MF+': tabs opposed broad planes38/40 and42/44 have separations9.999999999992255 and9.999999999993669mm.')
numeric('S3D6',[t['reach'] for t in ta],MF+': tabs outer-end midpoints on faces63/65 projected along their actual radial midlines from main axis give59.99999999999598 and59.999999999995964mm. No alternative dimension used.')
numeric('S3D7',[t['hole_diameter'] for t in ta],MF+': tabs internal cylinders64/66 each have radius6mm and axial length10mm; circular openings on opposed tab planes38/40 and42/44.',MF+': both radius5.999mm through probes extend outside both tab faces and intersect0mm3 material.',str(W/'transverse_x45.png')+': independent section shows both tab holes traversing their respective tab thickness.')
obs['S3D7']['conditions']={'through':all(t['through_probe']['intersection_volume_mm3']==0 for t in ta)}
numeric('S3D8',[t['axial_width'] for t in ta],MF+': tabs axial face pairs37/39 and41/43 each atx35 andx55; two independently measured widths20mm.')
obs['S3D9']={**base(MF+': side_blind_holes records independently measured diameters[8.5,8.5] and cylindrical depths[5.0,5.0] from outer-body axis intersection to conical-tip base.',MF+': total depth including drill point is[7.553657630869999,7.553657630867001]mm; analytic tip included angles118deg.',MF+': closed conical faces61/62 adjoin only cylinder faces35/36; axis material gaps to main passage are both7.30469243687mm. Both holes are independently confirmed blind.',str(W/'transverse_x45.png')+': independent section shows the conical blind ends.', 'Frozen rubric/source callout specifies simple blind hole depth5mm but does not explicitly state whether drill-point length is included. Preserve the unregistered datum ambiguity; do not turn it into an assumed pass or failure.'),'status':'requires_review','attempted':True,'reason':'Both diameters and blind terminations are verified. The5mm depth matches the cylindrical portion; total depth including the conical point is7.55365763mm. Source depth datum is not explicitly resolved.','components':{'diameter':{'values':[b['diameter'] for b in bh],'all_instances_verified':True},'depth':{'values':[b['cylindrical_depth'] for b in bh],'all_instances_verified':True,'datum':'cylindrical portion only; unresolved against drawing callout'},'termination':{'satisfied':True}},'additional_measurements':{'total_depth_including_point':[b['total_axial_depth_including_tip'] for b in bh],'tip_included_angle_deg':[b['tip_included_angle'] for b in bh],'remaining_axis_material_gap':[b['axis_material_gap_to_main_bore'] for b in bh]}}
numeric('S3D10',bo['height'],MF+': tilted_boss intersects actual boss axis with analytic supporting cone face13 at[104.99999999999427,0,28.06961402256302], then projects to top center, giving10.000000000010205mm. Actual axis is7deg fromZ; no nominal candidate parameter used.')
numeric('S3D11',bo['bore_diameter'],FF+': final internal tilted cylinder face25 radius6mm, coaxial with exterior boss19; distinct from tab cylinders64/66.')
numeric('S3D12',bo['outer_diameter'],FF+': final external tilted cylinder face19 radius11mm, independently associated with outer top circle of face22.')
relation('S3D13',True,MF+': side-hole axes35/36 lie atx45 with directionX-component0; both tab axial midplanes from faces37/39 and41/43 arex45; radiator axis46 also hasx45.',FF+': both tab axial plane normals are parallel main axisX and both side-hole axes are perpendicular toX, so they are contained in the radiator transverse plane rather than merely crossing it.')
relation('S3D14',True,MF+': tab_mirror_comparison reflects complete left tab/hole region abouty0; both Boolean difference volumes are0mm3 against right region. Clipping boxes include all exposed tab material and exclude unrelated features.',MF+': tabs separately confirms mirrored hole centers and matching axes, opening spans, reach, thickness and width; independent transverse section provides visual corroboration.')
assert len(obs)==28
criteria={c['id']:c for c in rubric['criteria']};scored=[evaluate_measurement(criteria[cid],value) for cid,value in obs.items()]
counts={s:sum(q['status']==s for q in scored) for s in ('correct','incorrect','absent','requires_review','inconclusive')}
assert counts=={'correct':27,'incorrect':0,'absent':0,'requires_review':1,'inconclusive':0},counts
manifest=json.loads((SN/'.snapshot-manifest.json').read_text());mismatches=[name for name,meta in manifest['files'].items() if hashlib.sha256((SN/name).read_bytes()).hexdigest()!=meta['sha256']];assert not mismatches
snapshot_hash=digest_json(manifest['files']);assert snapshot_hash==report['snapshot_sha256']
(R/'observations.json').write_text(json.dumps(obs,indent=2,allow_nan=False)+'\n')
(R/'scored-preview.json').write_text(json.dumps({'counts':counts,'criteria':scored},indent=2,allow_nan=False)+'\n')
provenance={'snapshot':str(SN),'snapshot_sha256':snapshot_hash,'rubric':str(RP),'rubric_sha256':hashlib.sha256(RP.read_bytes()).hexdigest(),'snapshot_file_hashes_verified':True,'geometry_source':'Independent CadQuery/OCP reimport of sealed STEP only, through frozen execution.run_isolated. Submitted source read as data only; never executed by this review.','source_read_for_orientation':str(SN/'build.py'),'measurements_use_candidate_parameter_claims':False,'input_STEP_files':{name:{'sha256':d['sha256'],'valid':d['valid'],'solids':d['solids']} for name,d in raw.items()},'worker_result_files':['inspect_faces.py.result.json','measure_geometry.py.result.json'],'source_drawings':'Deng AppendixA Figs11/12 and AppendixB Figs16/17; frozen rubric unchanged','review_counts':counts,'open_item':'S3D9 depth definition','evidence_files':{str(p.relative_to(R)):hashlib.sha256(p.read_bytes()).hexdigest() for p in R.rglob('*') if p.is_file() and p.suffix in {'.py','.json','.png'} and p.name!='provenance.json'}}
(R/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
summary=f'''Independent operator review of sealed Deng trial001, stage2/3.
Snapshot: {snapshot_hash}
Rubric: {provenance['rubric_sha256']}

All28 items reviewed:27 correct,0 incorrect,1 requires_review under the unchanged scorer.
Stage2: all14 correct, including five fins, ten size measurements, twenty corner
radii, five thicknesses, four gaps, square core continuity and through radiator bore.
Stage3:13 correct. Tilted-bore area815.2397542079789mm2 and top-center offset
35.781306565950004mm agree; both tab through-holes and full mirrored tab geometry
are confirmed. Tilted passage is open into the main bore.

S3D9 remains unresolved: both side holes measure8.5mm diameter and5mm cylindrical
depth, with118deg drill tips reaching7.55365763mm total depth. Both are blind, with
7.30469244mm axis material between tips and the main bore. The frozen5mm callout
has no explicit drill-point datum. Neither a guessed pass nor a failure is assigned.

Retention: final vs step2 within complete exposed radiator region has0mm3
difference both directions; stage3 vs final has0mm3 difference both directions.
The final tab/hole pair is mirror-equivalent with0mm3 difference both directions.

All STEP parsing/measurements ran through frozen execution.run_isolated with only
snapshot/evaluator read roots and private work/scratch/control. Both successful
workers were reaped. The initial non-escalated supervisor attempt failed before
parsing due nested sandbox_apply restrictions; the authorized sandbox supervisor
then ran successfully. No submitted Python was executed by this review.

observations.json is the requested scorer observation map; scored-preview.json
shows only these28 items. provenance.json records snapshot/rubric/evidence hashes.
Raw analytic face inventory: work/faces.json. Measurements: work/measured.json.
Independent sections: work/axial_y0.png, work/transverse_x45.png, work/core_z60.png,
work/core_z84.png. Own scripts and worker records are retained for reproducibility.
'''
(R/'summary.txt').write_text(summary)
print(json.dumps({'observations':str(R/'observations.json'),'counts':counts,'snapshot_sha256':snapshot_hash,'rubric_sha256':provenance['rubric_sha256']},indent=2))
