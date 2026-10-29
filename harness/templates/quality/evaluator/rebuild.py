"""Copy editable inputs only, rebuild in isolation, then compare geometry separately."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
from execution import run_isolated

EDITABLE_SUFFIXES={'.py','.json','.toml','.yaml','.yml','.csv','.txt'}
EXPORTS=('step-1.step','step-2.step','step-3.step','main-part.step','stand.step','assembly.step')

def rebuild(snapshot, destination, *, seconds=60, deadline=None):
    snapshot=Path(snapshot).resolve(strict=True);destination=Path(destination).absolute()
    if destination.exists():raise FileExistsError('Use a new rebuild directory')
    destination.mkdir()
    work=destination/'work';scratch=destination/'scratch';control=destination/'control'
    for p in (work,scratch,control):p.mkdir()
    output=work/'output';output.mkdir()
    copied={}
    for source in sorted(snapshot.rglob('*')):
        if source.is_symlink():raise ValueError('Snapshot must not contain symlinks')
        if source.is_file() and source.suffix.lower() in EDITABLE_SUFFIXES and not source.name.startswith('.'):
            if source.stat().st_size>10_000_000:raise ValueError('Editable input exceeds 10 MB')
            relative=source.relative_to(snapshot);target=output/relative
            target.parent.mkdir(exist_ok=True,parents=True);shutil.copyfile(source,target)
            copied[str(relative)]=hashlib.sha256(target.read_bytes()).hexdigest()
    if not (output/'build.py').is_file():
        result={'status':'fail','reason':'Missing output/build.py','copied_source_files':copied,'export_directory':str(output)}
    else:
        source='import sys,runpy,os\nsys.path[:0]=[os.path.abspath("output"),os.getcwd()]\nsys.argv=["output/build.py"]\nrunpy.run_path("output/build.py",run_name="__main__")\n'
        process=run_isolated(source,work=work,scratch=scratch,control=control,seconds=seconds,deadline=deadline)
        # Process success alone never proves a valid rebuild. Geometry is compared
        # by an independent later worker, after this source process is reaped.
        safe_output=output.is_dir() and not output.is_symlink() and output.resolve()==output
        produced={name:safe_output and (output/name).is_file() and not (output/name).is_symlink() for name in EXPORTS}
        result={'status':'pass' if process['success'] and all(produced.values()) else 'fail',
            'reason':'Fresh source execution and required file production; independent geometry comparison is still required',
            'copied_source_files':copied,'export_directory':str(output) if safe_output else None,'produced_files':produced,'worker':process,
            'excluded_submitted_geometry_filenames':True,'source_environment':'Only allowed editable-text suffixes are copied; no submitted STEP/BREP/mesh/image files are copied. Text may still encode cached geometry, so procedural/editable-source authenticity requires separate source review.'}
    (destination/'rebuild-result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result

def compare_exports(original, rebuilt, *, relative_tolerance=1e-8, absolute_tolerance_mm3=1e-9):
    """Call only inside the independent analysis worker, after rebuilding ends."""
    import math
    from integrity import _load_step,_describe,_boolean_volume,EXPECTED_SOLIDS
    results=[]
    for name in EXPORTS:
        try:
            paths=[Path(original)/name,Path(rebuilt)/name]
            if any(p.is_symlink() or not p.is_file() or p.stat().st_size>100_000_000 for p in paths):raise ValueError('Missing, unsafe or oversized STEP file')
            loaded=[_load_step(p) for p in paths];shapes=[x[0] for x in loaded]
            for shape,units,transfer in loaded:
                details=_describe(shape)
                if not all(details[key] for key in ('valid','finite','positive_valid_solids')) or details['loose_topology'] or details['solid_count']!=EXPECTED_SOLIDS[name] or units['status']!='pass':raise ValueError('Invalid roots, units, topology or solid count in submitted/rebuilt geometry')
            if len(shapes[0].Solids())!=len(shapes[1].Solids()):raise ValueError('Rebuild changes solid count')
            volumes=[sum(s.Volume() for s in shape.Solids()) for shape in shapes]
            if not all(math.isfinite(v) for v in volumes):raise ValueError('Nonfinite volume')
            tolerance=max(absolute_tolerance_mm3,relative_tolerance*max(volumes))
            difference=_boolean_volume('difference',shapes[0],shapes[1])+_boolean_volume('difference',shapes[1],shapes[0])
            results.append({'file':name,'status':'pass' if math.isfinite(difference) and difference<=tolerance else 'fail','symmetric_difference_mm3':difference,'tolerance_mm3':tolerance})
        except Exception as exc:results.append({'file':name,'status':'fail','reason':f'{type(exc).__name__}: {exc}'})
    return {'status':'pass' if all(x['status']=='pass' for x in results) else 'fail','files':results,'method':'Independent STEP reimport and symmetric-difference volume; same coordinates and solid count required'}
