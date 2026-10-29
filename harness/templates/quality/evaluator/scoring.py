"""Deterministic adapted-61 scoring; unknown measurements never earn credit."""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import stat

STATUSES={'correct','incorrect','absent','requires_review','inconclusive'}

def digest_json(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def validate_rubric(rubric):
    criteria=rubric['criteria'];ids=[c['id'] for c in criteria]
    expected=[f'S{s}D{i}' for s,count in [(1,22),(2,14),(3,14),(4,11)] for i in range(1,count+1)]
    if sorted(ids)!=sorted(expected) or len(set(ids))!=61:raise ValueError('The frozen rubric must retain the exact 61 numbered criteria')
    if rubric['scoring']['adapted_denominator']!=61:raise ValueError('Frozen denominator is 61')
    if any(c.get('weight')!=1 for c in criteria):raise ValueError('Every numbered criterion has unit weight')
    return criteria

def _finite(value):return not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value)

def _numeric(expected,tolerance,values,covered=False):
    if not isinstance(values,list):values=[values]
    required=expected.get('multiplicity',1)
    if len(values)<required or not values or not all(_finite(v) for v in values):
        return {'status':'inconclusive','reason':'Missing repeated measurements or non-finite/non-numeric values','values':values}
    if required>1 and not covered:
        return {'status':'requires_review','reason':'All-instance coverage must be established; a subset cannot pass','values':values}
    if expected.get('interval') is not None:
        low,high=expected['interval'];margin=tolerance.get('numerical_margin',0)
        lower,upper=low-margin,high+margin
    else:
        margin=tolerance.get('absolute',expected.get('absolute_tolerance'))
        if margin is None:raise ValueError('Missing frozen numeric tolerance')
        lower,upper=expected['value']-margin,expected['value']+margin
    # The tiny epsilon is only floating-point arithmetic protection, not an
    # additional geometric tolerance; the explicit bounds remain in the report.
    ok=all(lower-1e-12<=v<=upper+1e-12 for v in values)
    return {'status':'correct' if ok else 'incorrect','values':values,'unit':expected['unit'],
            'acceptance_interval':[lower,upper],'required_instances':required,'measured_instances':len(values)}

def evaluate_measurement(criterion,observation,already_scored=None):
    cid=criterion['id'];base={'id':cid,'description':criterion['description'],'stage':criterion['stage'],'weight':1}
    if observation is None:return {**base,'status':'requires_review','attempted':None,'reason':'No qualified measurement or operator review'}
    evidence=observation.get('evidence')
    if not isinstance(evidence,list) or not evidence:
        return {**base,'status':'inconclusive','attempted':None,'reason':'Measurement lacks independent evidence'}
    base.update(evidence=evidence,origin=observation.get('origin','operator'))
    if observation.get('feature_present') is False:
        return {**base,'status':'absent','attempted':False,'reason':'Feature absence established by evidence'}
    if observation.get('status') in {'requires_review','inconclusive'}:
        return {**base,'status':observation['status'],'attempted':observation.get('attempted'),'reason':observation.get('reason','Unresolved measurement')}
    expected=criterion['expected'];tolerance=criterion['tolerance']
    if observation.get('alternative'):
        alternatives={a['source_label']:a for a in criterion.get('alternatives',[])}
        alternative=alternatives.get(observation['alternative'])
        if cid!='S3D6' or alternative is None:raise ValueError('Unknown alternative measurement')
        if (already_scored or {}).get('S1D7',{}).get('status')!='correct':
            return {**base,'status':'requires_review','attempted':True,'reason':'Alternative S3D6 measurement requires independently correct S1D7'}
        expected=alternative['expected']
        base['alternative_used']=observation['alternative']
    kind=expected.get('type','numeric')
    if kind=='numeric':
        measured=_numeric(expected,tolerance,observation.get('values'),observation.get('all_instances_verified') is True)
    elif kind=='compound':
        components=[];observations=observation.get('components',{})
        for component in expected['components']:
            actual=observations.get(component['name'],{})
            if 'relation' in component:
                satisfied=actual.get('satisfied')
                status='correct' if satisfied is True else 'incorrect' if satisfied is False else 'requires_review'
                result={'status':status,'relation':component['relation']}
            else:
                component_tolerance={'numerical_margin':component['numerical_margin']} if component.get('interval') is not None else {'absolute':component['absolute_tolerance']}
                result=_numeric(component,component_tolerance,actual.get('values'),actual.get('all_instances_verified') is True)
            components.append({'name':component['name'],**result})
        status='incorrect' if any(x['status']=='incorrect' for x in components) else 'correct' if all(x['status']=='correct' for x in components) else 'requires_review'
        measured={'status':status,'components':components}
    elif kind=='qualitative':
        satisfied=observation.get('satisfied')
        measured={'status':'correct' if satisfied is True else 'incorrect' if satisfied is False else 'requires_review','relation':expected['relation']}
    else:raise ValueError('Unsupported expected-value kind')
    conditions=[]
    for condition in criterion.get('required_conditions',[]):
        supplied=observation.get('conditions',{}).get(condition['id'])
        conditions.append({'id':condition['id'],'satisfied':supplied,'description':condition['description']})
    structural_failure=observation.get('structural_failure') is True
    if structural_failure or any(c['satisfied'] is False for c in conditions):measured['status']='incorrect'
    elif measured['status']=='correct' and any(c['satisfied'] is not True for c in conditions):measured['status']='requires_review'
    if conditions:measured['conditions']=conditions
    if structural_failure:measured.update(structural_failure=True,structural_evidence=observation.get('structural_evidence',[]))
    return {**base,**measured,'attempted':True if observation.get('feature_present') is True or measured['status'] in {'correct','incorrect'} else observation.get('attempted')}

def score(rubric,observations):
    criteria=validate_rubric(rubric)
    if not isinstance(observations,dict):raise ValueError('Observation map required')
    if set(observations)-{c['id'] for c in criteria}:raise ValueError('Unknown criterion observation')
    ledger=[];seen={}
    for criterion in criteria:
        row=evaluate_measurement(criterion,observations.get(criterion['id']),seen)
        ledger.append(row);seen[row['id']]=row
    counts={status:sum(x['status']==status for x in ledger) for status in sorted(STATUSES)}
    attempted=sum(x['attempted'] is True for x in ledger)
    unresolved_attempts=sum(x['attempted'] is None for x in ledger)
    unresolved=sum(counts[s] for s in ('requires_review','inconclusive'))
    return {'rubric_id':rubric['rubric_id'],'denominator':61,'status_counts':counts,
        'confirmed_correct':counts['correct'],'confirmed_attempted':attempted,'unresolved_attempt_count':unresolved_attempts,
        'overall_coverage':counts['correct']/61,'completeness':attempted/61,
        'conditional_correctness':counts['correct']/attempted if attempted else None,
        'metrics_provisional':bool(unresolved),'possible_coverage_interval':[counts['correct']/61,(counts['correct']+unresolved)/61],
        'all_criteria_correct':counts['correct']==61,'criteria':ledger}

def load_operator_review(path, *, snapshot_sha256,rubric_sha256,candidate_workspace,operator_root,candidate_writable_roots=()):
    path=Path(path).absolute();workspace=Path(candidate_workspace).resolve(strict=True)
    operator_root=Path(operator_root).absolute()
    if operator_root.is_symlink() or operator_root.resolve()!=operator_root:raise ValueError('Operator root must be canonical')
    resolved=path.resolve(strict=True)
    if path!=resolved or not resolved.is_relative_to(operator_root):raise ValueError('Review must be inside designated protected operator root without symlinks')
    for root in [workspace,*map(lambda p:Path(p).resolve(),candidate_writable_roots)]:
        if operator_root==root or operator_root in root.parents or root in operator_root.parents:raise ValueError('Operator root overlaps a candidate writable root')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size>2_000_000:raise ValueError('Review must be a bounded ordinary file')
        raw=os.read(fd,2_000_001)
        after=os.fstat(fd)
        if len(raw)>2_000_000 or len(raw)!=info.st_size or (info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ValueError('Review changed while reading')
    finally:os.close(fd)
    def unique(pairs):
        out={}
        for key,value in pairs:
            if key in out:raise ValueError('Duplicate JSON key')
            out[key]=value
        return out
    data=json.loads(raw,object_pairs_hook=unique,parse_constant=lambda s:(_ for _ in ()).throw(ValueError('Non-finite JSON')))
    if data.get('snapshot_sha256')!=snapshot_sha256 or data.get('rubric_sha256')!=rubric_sha256:raise ValueError('Review is bound to another snapshot or rubric')
    if not isinstance(data.get('reviewer'),str) or not data['reviewer'].strip():raise ValueError('Named operator reviewer required')
    if not isinstance(data.get('observations'),dict):raise ValueError('Observation map required')
    for cid,entry in data['observations'].items():
        if not isinstance(entry,dict) or not isinstance(entry.get('evidence'),list) or not entry['evidence'] or not all(isinstance(x,str) and x.strip() for x in entry['evidence']):raise ValueError('Operator observation requires a nonempty list of textual evidence')
        if 'override_reason' in entry and (not isinstance(entry['override_reason'],str) or not entry['override_reason'].strip()):raise ValueError('Override reason must be nonempty text')
        entry['origin']='operator:'+data['reviewer']
    permitted={'SUP-SEPARATE-STAND','SUP-SUPPORT-DEPENDENCIES','SUP-CUMULATIVE-GEOMETRY','SUP-STAND-DIMENSIONS','GATE-EDITABLE-SOURCE'}
    if not isinstance(data.get('supplemental',{}),dict) or set(data.get('supplemental',{}))-permitted:raise ValueError('Unknown supplemental review gate')
    for cid,entry in data.get('supplemental',{}).items():
        if not isinstance(entry,dict) or not isinstance(entry.get('evidence'),list) or not entry['evidence'] or not all(isinstance(x,str) and x.strip() for x in entry['evidence']):raise ValueError('Supplemental gate needs nonempty textual evidence')
        if cid=='SUP-STAND-DIMENSIONS':
            if not isinstance(entry.get('components'),dict):raise ValueError('Stand dimensions require measured components')
        elif type(entry.get('satisfied')) is not bool:raise ValueError('Supplemental relation gate needs exact Boolean')
    if 'component_pose_review' in data:
        entry=data['component_pose_review']
        if not isinstance(entry,dict) or not isinstance(entry.get('evidence'),list) or not entry['evidence'] or not all(isinstance(x,str) and x.strip() for x in entry['evidence']):raise ValueError('Component poses require independent operator evidence')
        poses=entry.get('poses')
        if not isinstance(poses,dict) or set(poses)-{'main-part','stand'}:raise ValueError('Unknown component pose')
        from integrity import _pose_matrix
        entry['poses']={key:_pose_matrix(value) for key,value in poses.items()}
    return data
