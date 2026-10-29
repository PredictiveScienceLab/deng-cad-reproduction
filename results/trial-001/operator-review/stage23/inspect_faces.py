import sys,json,hashlib,math
from pathlib import Path
import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Surface,BRepAdaptor_Curve
sys.path.insert(0,'@ORIGINAL_PROJECT@/quality/evaluator')
from geometry import surface_record,rounded_rectangle,circle_info
snapshot=Path('results/trial-001/output')
def xyz(p):return list(p.Coord()) if hasattr(p,'Coord') else list(p.toTuple())
def box(s):
 b=s.BoundingBox();return [b.xmin,b.xmax,b.ymin,b.ymax,b.zmin,b.zmax]
data={}
for name in ('main-part.step','step-2.step','step-3.step'):
 p=snapshot/name;s=cq.importers.importStep(str(p)).val();faces=[]
 for i,f in enumerate(s.Faces()):
  r=surface_record(f,i);r.update(bounds=box(f),center=xyz(f.Center()),vertices=[xyz(v.Center()) for v in f.Vertices()])
  a=BRepAdaptor_Surface(f.wrapped)
  if f.geomType()=='PLANE':r['plane']={'origin':xyz(a.Plane().Location()),'normal':xyz(a.Plane().Axis().Direction()),'oriented_normal':xyz(f.normalAt())}
  if f.geomType()=='CONE':r['cone_ref_radius']=a.Cone().RefRadius()
  r['circles']=[circle_info(e) for e in f.Edges() if e.geomType()=='CIRCLE']
  rr=rounded_rectangle(f)
  if rr:r['rounded_rectangle']=rr
  faces.append(r)
 data[name]={'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'valid':s.isValid(),'solids':len(s.Solids()),'volume':s.Volume(),'bounds':box(s),'faces':faces}
Path('faces.json').write_text(json.dumps(data,indent=2))
print({name:{'faces':len(d['faces']),'sha256':d['sha256'],'valid':d['valid'],'solids':d['solids']} for name,d in data.items()})
