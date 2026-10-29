import hashlib,json,math
from pathlib import Path
import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Surface

SNAPSHOT=Path('results/trial-001/output')
def xyz(point): return [point.X(),point.Y(),point.Z()]
def bounds(shape):
    b=shape.BoundingBox()
    return [b.xmin,b.xmax,b.ymin,b.ymax,b.zmin,b.zmax]
def faceinfo(shape):
    rows=[]
    for index,f in enumerate(shape.Faces()):
        a=BRepAdaptor_Surface(f.wrapped,True)
        r={'index':index,'kind':f.geomType(),'area':f.Area(),'bounds':bounds(f),'center':f.Center().toTuple()}
        if r['kind']=='PLANE':
            p=a.Plane();r['plane']={'normal':xyz(p.Axis().Direction()),'origin':xyz(p.Location())}
        if r['kind']=='CYLINDER':
            c=a.Cylinder();r['cylinder']={'radius':c.Radius(),'axis':xyz(c.Axis().Direction()),'origin':xyz(c.Location())}
        if r['kind']=='CONE':
            c=a.Cone();r['cone']={'radius':c.RefRadius(),'semi_angle_rad':c.SemiAngle(),'axis':xyz(c.Axis().Direction()),'origin':xyz(c.Location()),'apex':xyz(c.Apex())}
        rows.append(r)
    return rows
names=['stand','main-part','step-1','step-2','step-3','assembly']
shapes={name:cq.importers.importStep(str(SNAPSHOT/(name+'.step'))).val() for name in names}
data={'method':'Independent STEP parsing and OpenCascade B-rep face/Boolean measurements; no submitted source was executed.',
      'files':{name:{'sha256':hashlib.sha256((SNAPSHOT/(name+'.step')).read_bytes()).hexdigest(),
                     'valid':shapes[name].isValid(),'solids':len(shapes[name].Solids()),'volume':shapes[name].Volume(),'bounds':bounds(shapes[name])} for name in names},
      'stand_faces':faceinfo(shapes['stand']),'main_faces':faceinfo(shapes['main-part'])}
stand,main=shapes['stand'],shapes['main-part']
data['stand_main_overlap_mm3']=stand.intersect(main).Volume()
data['step3_main_difference']={'missing':shapes['step-3'].cut(main).Volume(),'excess':main.cut(shapes['step-3']).Volume()}
data['assembly_component_differences']=[]
for index,s in enumerate(shapes['assembly'].Solids()):
    data['assembly_component_differences'].append({'index':index,'volume':s.Volume(),
        'versus_main':s.cut(main).Volume()+main.cut(s).Volume(),
        'versus_stand':s.cut(stand).Volume()+stand.cut(s).Volume()})
data['support_faces']=[]
for r,face in zip(data['stand_faces'],stand.Faces()):
    if r['kind'] in ('CYLINDER','CONE') and (r.get('cylinder',{}).get('radius',0)>10 or r['kind']=='CONE'):
        common=face.intersect(main)
        data['support_faces'].append({'stand_face_index':r['index'],'area':face.Area(),'common_with_main_area':common.Area(),'common_faces':len(common.Faces()),'gap_distance_mm':face.distance(main)})
Path('independent-measurements.json').write_text(json.dumps(data,indent=2,allow_nan=False)+'\n')
print(json.dumps({'stand_faces':len(data['stand_faces']),'main_faces':len(data['main_faces']),'support_faces':data['support_faces'],'step3_main_difference':data['step3_main_difference'],'stand_main_overlap_mm3':data['stand_main_overlap_mm3']}))
