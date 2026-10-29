from pathlib import Path
import json,sys
sys.path.insert(0,'@ORIGINAL_PROJECT@/quality/evaluator')
from execution import run_isolated
ROOT=Path('results/trial-001/operator-review/root')
for n in ['work','scratch','control']:(ROOT/n).mkdir(exist_ok=True)
SNAPSHOT=Path('results/trial-001/output')
source='''from pathlib import Path
import json
import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Plane
root=Path("results/trial-001/output")
result={}
for name in ['step-1.step','main-part.step']:
 shape=cq.importers.importStep(str(root/name)).val();rows=[]
 for index,face in enumerate(shape.Faces()):
  surface=BRepAdaptor_Surface(face.wrapped)
  b=face.BoundingBox()
  if surface.GetType()==GeomAbs_Plane and b.xmin>=134.99 and b.zmax>=19.9:
   plane=surface.Plane();n=plane.Axis().Direction()
   rows.append({'face':index,'normal':[n.X(),n.Y(),n.Z()],'area':face.Area(),'bbox':[b.xmin,b.xmax,b.ymin,b.ymax,b.zmin,b.zmax]})
 result[name]={'valid':shape.isValid(),'solids':len(shape.Solids()),'faces':rows}
Path('keyway.json').write_text(json.dumps(result,indent=2)+'\\n')
'''
r=run_isolated(source,work=ROOT/'work',scratch=ROOT/'scratch',control=ROOT/'control',read_roots=[SNAPSHOT],seconds=60)
print(json.dumps({'success':r['success'],'worker_reaped':r['worker_reaped'],'status':r['status'],'stderr':r['stderr']}));print((ROOT/'work/keyway.json').read_text() if (ROOT/'work/keyway.json').exists() else '')
