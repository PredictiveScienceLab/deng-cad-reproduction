"""Apply protected operator measurements to an existing frozen evaluation."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from evaluate import HERE,PROJECT,verify_freeze,_observations,_supplemental,write_worksheet
from scoring import digest_json,load_operator_review,score

def apply(original,review_path,destination):
    verify_freeze();original=Path(original).resolve(strict=True);destination=Path(destination).absolute()
    if destination.exists():raise FileExistsError('Reviewed report must use a new destination')
    result=json.loads((original/'evaluation.json').read_text())
    if result['evaluator_freeze_sha256']!=hashlib.sha256((HERE.parent/'freeze.json').read_bytes()).hexdigest():raise ValueError('Evaluation used another evaluator version')
    manifest=json.loads((original/'snapshot/.snapshot-manifest.json').read_text())
    if digest_json(manifest['files'])!=result['snapshot_sha256']:raise ValueError('Snapshot manifest changed')
    for relative,item in manifest['files'].items():
        path=original/'snapshot'/relative
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('Snapshot bytes changed')
    rubric_path=HERE.parent/'frozen/rubric.json';rubric=json.loads(rubric_path.read_text())
    if hashlib.sha256(rubric_path.read_bytes()).hexdigest()!=result['rubric_sha256']:raise ValueError('Rubric changed')
    review=load_operator_review(review_path,snapshot_sha256=result['snapshot_sha256'],rubric_sha256=result['rubric_sha256'],candidate_workspace=result['candidate_workspace'],operator_root=HERE.parent/'reviews',candidate_writable_roots=[PROJECT/'sandbox/candidate/work',PROJECT/'sandbox/candidate/tmp'])
    if 'component_pose_review' in review and review['component_pose_review']['poses']!=result['analysis'].get('integrity',{}).get('component_poses',{}):raise ValueError('Changed component poses require a fresh evaluation with --operator-review')
    observations=_observations(result['analysis'],rubric);overrides=[]
    for cid,entry in review['observations'].items():
        if cid in observations:
            if not entry.get('override_reason'):raise ValueError('Override requires a reason: '+cid)
            overrides.append({'id':cid,'automatic':observations[cid],'operator':entry,'reason':entry['override_reason']})
        observations[cid]=entry
    scored=score(rubric,observations);gates=_supplemental(rubric,result['analysis'],result['rebuild'],review)
    gates.extend(x for x in result['gates'] if x['id']=='GATE-DOCUMENTATION')
    accepted=scored['all_criteria_correct'] and all(x['status']=='pass' for x in gates)
    result.update(score=scored,gates=gates,overrides=overrides,operator_review_supplied=True,
        status='pass' if accepted else 'fail' if any(x['status']=='fail' for x in gates) or scored['status_counts']['incorrect'] or scored['status_counts']['absent'] else 'requires_review',
        original_evaluation=str(original),operator_review_sha256=hashlib.sha256(Path(review_path).read_bytes()).hexdigest())
    destination.mkdir(parents=True)
    (destination/'evaluation.json').write_text(json.dumps(result,indent=2)+'\n')
    views=original/'analysis/work/views'
    if views.is_dir():shutil.copytree(views,destination/'analysis/work/views')
    write_worksheet(destination,result,rubric)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--evaluation',type=Path,required=True);p.add_argument('--review',type=Path,required=True);p.add_argument('--destination',type=Path,required=True);a=p.parse_args()
    result=apply(a.evaluation,a.review,a.destination);print(json.dumps({'status':result['status'],'correct':result['score']['confirmed_correct'],'denominator':61}))
