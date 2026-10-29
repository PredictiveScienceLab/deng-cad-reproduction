import json,math
from pathlib import Path
import cadquery as cq
S=Path('results/trial-001/output')
stand=cq.importers.importStep(str(S/'stand.step')).val()
main=cq.importers.importStep(str(S/'main-part.step')).val()
face=stand.Faces()[26]
result={'shoulder_contact':{'stand_face_index':26,'area':face.Area(),'common_area':face.intersect(main).Area(),'gap_distance_mm':face.distance(main)},'holes':[]}
for y in (-25,25):
    tool=cq.Solid.makeCylinder(5,22,cq.Vector(120,y,-25),cq.Vector(1,0,0))
    result['holes'].append({'axis_y':y,'axis_z':-25,'diameter_mm':10,'x_extent':[120,142],
        'stand_intersection_volume_mm3':stand.intersect(tool).Volume(),
        'main_intersection_volume_mm3':main.intersect(tool).Volume()})
Path('contact-and-holes.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
