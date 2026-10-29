"""Snapshot, rebuild, independently measure, and score a Deng submission.

This program has no model client and cannot launch a CAD trial.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import hashlib
from html import escape
import json
import os
from pathlib import Path
import sys
import time

HERE=Path(__file__).resolve().parent
PROJECT=Path('@PROJECT@')
sys.path.insert(0,str(PROJECT/'sandbox/controller'))
from controller import snapshot_output
from execution import run_isolated
from rebuild import rebuild,EXPORTS
from scoring import digest_json,load_operator_review,score,validate_rubric,evaluate_measurement

def verify_freeze():
    manifest=json.loads((HERE.parent/'freeze.json').read_text())
    if manifest.get('status')!='FROZEN':raise ValueError('Evaluator has not been frozen')
    known={name for name in manifest['files'] if name.startswith('evaluator/') and name.endswith('.py')}
    if known!={'evaluator/'+p.name for p in HERE.glob('*.py')}:raise ValueError('Evaluator module inventory changed')
    for relative,expected in manifest['files'].items():
        path=HERE.parent/relative
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise ValueError('Frozen evaluator/rubric changed: '+relative)
    for absolute,expected in manifest.get('external_dependencies',{}).items():
        path=Path(absolute)
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise ValueError('Frozen external dependency changed: '+absolute)
    return manifest

def analyze(snapshot,rebuilt,destination, *, rubric_path,deadline=None,component_poses=None):
    destination=Path(destination);destination.mkdir()
    work=destination/'work';scratch=destination/'scratch';control=destination/'control'
    for path in (work,scratch,control):path.mkdir()
    if rebuilt is None:
        rebuilt=destination/'empty-rebuild';rebuilt.mkdir()
    rebuilt=Path(rebuilt)
    if rebuilt.is_symlink() or rebuilt.resolve()!=rebuilt or not rebuilt.is_dir():raise ValueError('Unsafe rebuild export directory')
    source=f'''import sys,json
from pathlib import Path
sys.path.insert(0,{str(HERE)!r})
from integrity import audit
from geometry import evaluate_stage1
from rebuild import compare_exports
from render_views import render
snapshot=Path({str(snapshot)!r});rubric=json.loads(Path({str(rubric_path)!r}).read_text())
poses={component_poses!r}
result={{'integrity':audit(snapshot,component_poses=poses),'stage1':evaluate_stage1(snapshot/'step-1.step',rubric),'final_stage1':evaluate_stage1(snapshot/'main-part.step',rubric)}}
result['rebuild_comparison']=compare_exports(snapshot,Path({str(rebuilt)!r}))
result['rebuilt_integrity']=audit(Path({str(rebuilt)!r}),component_poses=poses)
result['render']=render(snapshot,Path('views'))
Path('analysis.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\\n')
'''
    reads=[HERE,Path(rubric_path).parent,Path(snapshot),Path(rebuilt)]
    process=run_isolated(source,work=work,scratch=scratch,control=control,read_roots=reads,seconds=60,deadline=deadline)
    if not process['success'] or not (work/'analysis.json').is_file():
        return {'status':'inconclusive','reason':'Independent geometry worker failed or timed out','worker':process}
    data=json.loads((work/'analysis.json').read_text());data['worker']=process
    return data

def _preflight(output):
    count=total=0
    for root,dirs,files in os.walk(output,followlinks=False):
        for name in dirs+files:
            p=Path(root)/name
            if p.is_symlink():raise ValueError('Submission contains symlink: '+str(p))
            if p.is_file():
                size=p.stat().st_size;count+=1;total+=size
                if size>100_000_000 or count>256 or total>500_000_000:raise ValueError('Submission exceeds file/byte limits')
            elif not p.is_dir():raise ValueError('Submission contains special file')

def _observations(analysis,rubric):
    observations={}
    for row in analysis.get('stage1',{}).get('criteria',[]):
        if row['status'] in {'pass','fail'}:
            observations[row['id']]={'values':row['measurement'],'feature_present':True,'all_instances_verified':True,
                'evidence':['Independent STEP face indices: '+json.dumps(row.get('evidence',[])),row.get('reason','Analytic measurement')],
                'origin':'automatic_intermediate_STEP_measurement',
                'structural_failure':row['status']=='fail','structural_evidence':row.get('through_hole_evidence',[]),
                'conditions':{'through':all(x['through'] for x in row['through_hole_evidence'])} if row.get('through_hole_evidence') else {}}
    for row in analysis.get('final_stage1',{}).get('criteria',[]):
        if row['status'] in {'pass','fail'}:
            observations[row['id']]={'values':row['measurement'],'feature_present':True,'all_instances_verified':True,
                'evidence':['Final main-part STEP face indices: '+json.dumps(row.get('evidence',[])),row.get('reason','Analytic measurement')],
                'origin':'automatic_final_STEP_measurement',
                'structural_failure':row['status']=='fail','structural_evidence':row.get('through_hole_evidence',[]),
                'conditions':{'through':all(x['through'] for x in row['through_hole_evidence'])} if row.get('through_hole_evidence') else {}}
    return observations

def _supplemental(rubric,analysis,rebuild_result,review):
    gates=[{'id':'GATE-INTEGRITY','status':analysis.get('integrity',{}).get('status','inconclusive')},
           {'id':'GATE-REBUILD','status':'pass' if rebuild_result['status']=='pass' and analysis.get('rebuild_comparison',{}).get('status')=='pass' and analysis.get('rebuilt_integrity',{}).get('status')=='pass' else 'fail'}]
    provided=(review or {}).get('supplemental',{})
    requirements=[*rubric['supplemental_requirements'],{'id':'GATE-EDITABLE-SOURCE','description':'Source is editable and parameterized; a fresh execution alone does not establish that geometry was procedurally reconstructed rather than loaded from an encoded cache.'}]
    for requirement in requirements:
        cid=requirement['id'];entry=provided.get(cid)
        if entry and requirement.get('expected'):
            judged=evaluate_measurement({**requirement,'stage':4},entry)
            gates.append({'id':cid,'status':{'correct':'pass','incorrect':'fail'}.get(judged['status'],'requires_review'),'measurement':judged,'reviewer':review['reviewer']})
        elif entry and type(entry.get('satisfied')) is bool and entry.get('evidence'):
            gates.append({'id':cid,'status':'pass' if entry['satisfied'] is True else 'fail','evidence':entry['evidence'],'reviewer':review['reviewer']})
        else:gates.append({'id':cid,'status':'requires_review','reason':requirement['description']})
    return gates

def evaluate(workspace,destination, *, review_path=None,seconds=180,writers_stopped=False):
    if not writers_stopped:raise ValueError('The controller must stop candidate writers before evaluation')
    freeze=verify_freeze()
    rubric_path=HERE.parent/'frozen/rubric.json';rubric=json.loads(rubric_path.read_text());validate_rubric(rubric)
    rubric_hash=hashlib.sha256(rubric_path.read_bytes()).hexdigest()
    workspace=Path(workspace).absolute();destination=Path(destination).absolute()
    if workspace.is_symlink() or workspace.resolve()!=workspace:raise ValueError('Canonical workspace required')
    if workspace==destination or workspace in destination.parents:raise ValueError('Evaluation reports must be outside candidate workspace')
    if destination.exists():raise FileExistsError('Evaluation destination must be new')
    output=workspace/'output';_preflight(output)
    destination.mkdir(parents=True)
    manifest=snapshot_output(output,destination/'snapshot',candidate_workspace=workspace)
    snapshot_hash=digest_json(manifest['files'])
    review=None
    if review_path:review=load_operator_review(review_path,snapshot_sha256=snapshot_hash,rubric_sha256=rubric_hash,candidate_workspace=workspace,operator_root=HERE.parent/'reviews',candidate_writable_roots=[PROJECT/'sandbox/candidate/work',PROJECT/'sandbox/candidate/tmp'])
    deadline=time.monotonic()+seconds
    rebuilt=rebuild(destination/'snapshot',destination/'rebuild',seconds=min(60,seconds),deadline=deadline)
    analysis=analyze(destination/'snapshot',rebuilt['export_directory'],destination/'analysis',rubric_path=rubric_path,deadline=deadline,component_poses=(review or {}).get('component_pose_review',{}).get('poses'))
    observations=_observations(analysis,rubric)
    overrides=[]
    for cid,item in (review or {}).get('observations',{}).items():
        if cid in observations:
            # Keep conflicting evidence visible. A human override must explicitly
            # explain why the automatic face association is inappropriate.
            if not item.get('override_reason'):raise ValueError('Automatic measurement override requires explicit reason: '+cid)
            overrides.append({'id':cid,'automatic':observations[cid],'operator':item,'reason':item['override_reason']})
        observations[cid]=item
    scored=score(rubric,observations)
    gates=_supplemental(rubric,analysis,rebuilt,review)
    readme=destination/'snapshot/README.txt'
    gates.append({'id':'GATE-DOCUMENTATION','status':'pass' if readme.is_file() and 0<readme.stat().st_size<=1_000_000 else 'fail','reason':'Required nonempty README.txt; its substantive claims still require operator review'})
    complete=scored['all_criteria_correct'] and all(x['status']=='pass' for x in gates)
    status='pass' if complete else 'fail' if any(x['status']=='fail' for x in gates) or scored['status_counts']['incorrect'] or scored['status_counts']['absent'] else 'requires_review'
    result={'schema_version':1,'created_utc':datetime.now(timezone.utc).isoformat(),'status':status,
        'scope':'Composite adapted-61 evaluator: qualified automatic measurements plus explicit operator geometry/source review',
        'candidate_workspace':str(workspace),'snapshot_sha256':snapshot_hash,'rubric_sha256':rubric_hash,
        'evaluator_freeze_sha256':hashlib.sha256((HERE.parent/'freeze.json').read_bytes()).hexdigest(),
        'score':scored,'gates':gates,'automatic_measurement_count':len(_observations(analysis,rubric)),
        'operator_review_supplied':review is not None,'component_pose_review':(review or {}).get('component_pose_review'),'overrides':overrides,'rebuild':rebuilt,'analysis':analysis,
        'cad_trial_started':False,'real_model_inference':False}
    (destination/'evaluation.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    template={'reviewer':'','snapshot_sha256':snapshot_hash,'rubric_sha256':rubric_hash,
        'observations':{},'supplemental':{},'instructions':'Add independent geometric measurements and evidence. Numeric statuses are computed from values. Do not copy candidate parameter declarations. Review all unresolved criteria and source dependencies; see worksheet.html.'}
    (destination/'review-template.json').write_text(json.dumps(template,indent=2)+'\n')
    write_worksheet(destination,result,rubric)
    return result

def write_worksheet(destination,result,rubric):
    rows=[];by_id={x['id']:x for x in result['score']['criteria']}
    for item in rubric['criteria']:
        row=by_id[item['id']]
        rows.append('<tr><td>'+escape(item['id'])+'</td><td>'+escape(item['description'])+'</td><td><code>'+escape(json.dumps(item['expected']))+'</code></td><td><code>'+escape(json.dumps(item['tolerance']))+'</code></td><td>'+escape(row['status'])+'</td></tr>')
    images=''.join(f'<figure><img src="analysis/work/views/{name}{suffix}.png" alt="Independent {name} views"><figcaption>{name}{suffix}</figcaption></figure>' for name in ('step-1','step-2','step-3','main-part','stand','assembly') for suffix in ('','-sections') if (destination/'analysis/work/views'/f'{name}{suffix}.png').is_file())
    html='''<!doctype html><html><meta charset="utf-8"><title>Deng quality review worksheet</title><style>body{font:15px/1.5 system-ui;max-width:1250px;margin:30px auto;padding:0 20px;color:#263641}table{border-collapse:collapse;width:100%}th,td{padding:9px;border-bottom:1px solid #dde3e6;text-align:left;vertical-align:top}code{font-size:12px;overflow-wrap:anywhere}img{width:100%;max-width:1000px}figure{margin:25px 0}.notice{padding:15px;background:#fff1cd}</style><h1>Deng quality review worksheet</h1>'''
    html+='<p class="notice">Status: '+escape(result['status'])+'. Confirmed correct: '+str(result['score']['confirmed_correct'])+'/61. Unresolved items receive no credit; all required review gates must close before acceptance.</p>'
    html+='<p><a href="evaluation.json">Full evidence</a>'+(' · <a href="review-template.json">Operator review template</a>' if (destination/'review-template.json').is_file() else '')+'. Measurements must come from reimported geometry. Source dependency review remains separate from nominal geometric fit.</p>'
    html+='<ul>'+''.join('<li>'+escape(gate['id'])+': <strong>'+escape(gate['status'])+'</strong></li>' for gate in result['gates'])+'</ul>'
    html+='<p>Snapshot SHA-256: <code>'+result['snapshot_sha256']+'</code><br>Rubric SHA-256: <code>'+result['rubric_sha256']+'</code></p>'
    html+='<table><thead><tr><th>Criterion</th><th>Requirement</th><th>Expected</th><th>Tolerance</th><th>Status</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table>'+images+'</html>'
    (destination/'worksheet.html').write_text(html)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,required=True);parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--operator-review',type=Path);parser.add_argument('--writers-stopped',action='store_true')
    args=parser.parse_args()
    result=evaluate(args.workspace,args.destination,review_path=args.operator_review,writers_stopped=args.writers_stopped)
    print(json.dumps({'status':result['status'],'correct':result['score']['confirmed_correct'],'denominator':61,'report':str(args.destination/'worksheet.html')}))
