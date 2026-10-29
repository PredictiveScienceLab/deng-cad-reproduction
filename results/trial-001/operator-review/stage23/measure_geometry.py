import sys,json,math,hashlib
from pathlib import Path
import numpy as np
import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Surface,BRepAdaptor_Curve
from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.gp import gp_Pln,gp_Pnt,gp_Dir
sys.path.insert(0,'@ORIGINAL_PROJECT@/quality/evaluator')
from geometry import circle_info
snapshot=Path('results/trial-001/output')
raw=json.loads(Path('faces.json').read_text());rec=raw['main-part.step']['faces']
S=cq.importers.importStep(str(snapshot/'main-part.step')).val();S2=cq.importers.importStep(str(snapshot/'step-2.step')).val();S3=cq.importers.importStep(str(snapshot/'step-3.step')).val();F=S.Faces()
def a(x):return np.array(x,float)
def vals(x):return [float(i) for i in x]
def dot(x,y):return float(np.dot(x,y))
def norm(x):return float(np.linalg.norm(x))
def unit(x):return a(x)/norm(x)
def position(i):return a(rec[i]['plane']['origin'])
def normal(i):return a(rec[i]['plane']['normal'])
def pdistance(i,j):return abs(dot(position(j)-position(i),normal(i)))
def adjacent(i):return sorted(j for j,f in enumerate(F) if i!=j and any(e.isSame(g) for e in F[i].Edges() for g in f.Edges()))
def circles(i):return [circle_info(e) for e in F[i].Edges() if e.geomType()=='CIRCLE']
def boxtool(x0,x1,y0,y1,z0,z1):return cq.Solid.makeBox(x1-x0,y1-y0,z1-z0,cq.Vector(x0,y0,z0))
def compare(s,t):return {'forward_difference_mm3':s.cut(t).Volume(),'reverse_difference_mm3':t.cut(s).Volume(),'first_volume_mm3':s.Volume(),'second_volume_mm3':t.Volume()}
def probe(start,end,r):
 delta=a(end)-a(start);solid=cq.Solid.makeCylinder(r,norm(delta),cq.Vector(*start),cq.Vector(*unit(delta)))
 return {'start':vals(start),'end':vals(end),'radius_mm':r,'intersection_volume_mm3':S.intersect(solid).Volume()}
m={}
m['frame']={'origin':[0,0,0],'x':[1,0,0],'y':[0,1,0],'z':[0,0,1],'independent_anchors':{'main_axis_face':23,'large_outer_terminal_face':47,'small_outer_terminal_face':4,'vertical_keyway_top_face':15,'radiator_bore_axis_face':46},'note':'Imported geometry directly has this frame; no registration or scaling applied.'}
m['stem']={'corner_radii':[rec[i]['radius_mm'] for i in [26,28,30,33]],'corner_faces':[26,28,30,33],'dimensions':[pdistance(27,31),pdistance(29,34)],'dimension_face_pairs':[[27,31],[29,34]]}
fin_faces=[(48,77),(95,113),(131,149),(132,114),(96,78)]
fin_side_pairs=[[(71,72),(68,75)],[(100,101),(97,104)],[(136,137),(133,140)],[(126,127),(123,130)],[(93,89),(87,91)]]
fin_corners=[[69,70,73,74],[98,99,102,103],[134,135,138,139],[124,125,128,129],[88,90,92,94]]
m['fins']=[{'index_bottom_up':k+1,'planes':list(pair),'z':[position(pair[0])[2],position(pair[1])[2]],'thickness':pdistance(*pair),'dimensions':[pdistance(*p) for p in sidepairs],'side_plane_pairs':[list(p) for p in sidepairs],'corner_radii':[rec[i]['radius_mm'] for i in corners],'corner_faces':corners} for k,(pair,sidepairs,corners) in enumerate(zip(fin_faces,fin_side_pairs,fin_corners))]
m['fin_gaps']=[position(fin_faces[i+1][0])[2]-position(fin_faces[i][1])[2] for i in range(4)]
m['radiator']={'bore_face':46,'bore_diameter':2*rec[46]['radius_mm'],'bore_adjacent_faces':adjacent(46),'top_boss_face':76,'boss_diameter':2*rec[76]['radius_mm'],'boss_height':pdistance(67,78),'axial_station':rec[46]['origin'][0]-position(47)[0],'stack_height':pdistance(48,78),'lowest_fin_height':position(48)[2],'through_probe':probe([rec[46]['origin'][0],0,0],[rec[46]['origin'][0],0,position(67)[2]+.1],rec[46]['radius_mm']-.001)}
m['stem_exposed_intervals']=[{'z':[lo,hi],'flat_faces':flats,'dimensions':[pdistance(flats[0],flats[1]),pdistance(flats[2],flats[3])],'corner_faces':corners,'radii':[rec[i]['radius_mm'] for i in corners]} for lo,hi,flats,corners in [(35,54,[27,31,29,34],[26,28,30,33]),(58,62,[81,85,79,83],[80,82,84,86]),(66,70,[117,121,115,119],[116,118,120,122]),(74,78,[143,147,141,145],[142,144,146,148]),(82,86,[107,111,105,109],[106,108,110,112])]]
tops=circles(22);outer=max(tops,key=lambda c:c['radius']);top=a(outer['center']);axis=unit(rec[19]['axis']);origin=a(rec[19]['origin']);cr=rec[13]['cone_ref_radius'];cx=rec[13]['origin'][0];slope=math.tan(math.radians(rec[13]['semi_angle_degrees']));t=(cr+cx*slope-origin[0]*slope-origin[2])/(axis[2]+axis[0]*slope);foot=origin+t*axis
m['tilted_boss']={'outer_face':19,'top_face':22,'bore_face':25,'supporting_cone_face':13,'outer_diameter':2*rec[19]['radius_mm'],'bore_diameter':2*rec[25]['radius_mm'],'bore_area_mm2':rec[25]['area_mm2'],'top_circle_center':vals(top),'axis':vals(axis),'base_axis_cone_intersection':vals(foot),'height':dot(top-foot,axis),'top_center_to_small_terminal':abs(dot(top-position(4),normal(4))),'axis_from_vertical_deg':math.degrees(math.acos(dot(axis,a([0,0,1])))),'bore_adjacent_faces':adjacent(25)}
# Axis segment goes from the open axial-main-passage interior to above the top mouth.
q=top-axis*(top[2]/axis[2]);m['tilted_boss']['through_probe']=probe(q,top+axis*.1,rec[25]['radius_mm']-.001)
m['tabs']=[]
for endf,sidepair,axialpair,holef in [(63,(38,40),(37,39),64),(65,(42,44),(41,43),66)]:
 end=a(rec[endf]['center']);rad=unit([0,end[1],end[2]]);holeaxis=unit(rec[holef]['axis']);holeorigin=a(rec[holef]['origin']);holecenter=holeorigin-dot(holeorigin-a([holeorigin[0],0,0]),holeaxis)*holeaxis
 cs=circles(holef);centers=[a(c['center']) for c in cs];centers.sort(key=lambda p:dot(p,holeaxis));lo,hi=centers[0],centers[-1]
 m['tabs'].append({'end_face':endf,'thickness_faces':list(sidepair),'axial_faces':list(axialpair),'hole_face':holef,'radial_direction':vals(rad),'reach':dot(end,rad),'hole_center':vals(holecenter),'hole_center_radial_coordinate':dot(holecenter,rad),'hole_to_end':dot(end-holecenter,rad),'angle_from_down_deg':math.degrees(math.acos(dot(rad,a([0,0,-1])))),'thickness':pdistance(*sidepair),'axial_width':pdistance(*axialpair),'axial_midplane':float((position(axialpair[0])[0]+position(axialpair[1])[0])/2),'hole_diameter':2*rec[holef]['radius_mm'],'hole_axial_span':norm(hi-lo),'hole_circle_centers':[vals(c) for c in centers],'hole_adjacent_faces':adjacent(holef),'through_probe':probe(lo-holeaxis*.01,hi+holeaxis*.01,rec[holef]['radius_mm']-.001)})
m['side_blind_holes']=[]
for wall,tip in [(35,61),(36,62)]:
 cyl=rec[wall];cone=rec[tip];o=a(cyl['origin']);d=unit(cyl['axis']);basecir=max(circles(tip),key=lambda c:c['radius']);base=a(basecir['center']);vertices=[a(v) for v in cone['vertices']];apex=max(vertices,key=lambda v:dot(v-o,d));body_radius=rec[23]['radius_mm'];outer=o.copy();outer[1]=math.copysign(body_radius,o[1]);depth=dot(base-outer,d);total=dot(apex-outer,d);main_r=rec[24]['cone_ref_radius']-outer[0]*math.tan(math.radians(rec[24]['semi_angle_degrees']))
 m['side_blind_holes'].append({'cylinder_face':wall,'conical_tip_face':tip,'diameter':2*cyl['radius_mm'],'mouth_axis_intersection':vals(outer),'cylinder_end_center':vals(base),'cylindrical_depth':depth,'tip_apex':vals(apex),'total_axial_depth_including_tip':total,'tip_included_angle':2*rec[tip]['semi_angle_degrees'],'tip_adjacent_faces':adjacent(tip),'cylinder_adjacent_faces':adjacent(wall),'main_bore_radius_at_axis_station':main_r,'axis_material_gap_to_main_bore':abs(apex[1])-main_r,'axial_station':o[0]})
radiator_region=boxtool(14,76,-31,31,20,95);m['stage2_preservation_region']={'box':[14,76,-31,31,20,95],'rationale':'Contains the entire exposed radiator; excludes later tabs, side holes and tilted port by independently measured feature extents. Same imported frame in both exports.',**compare(S.intersect(radiator_region),S2.intersect(radiator_region))}
m['stage3_vs_final']=compare(S3,S)
left=boxtool(34,56,-60,0,-60,-15);right=boxtool(34,56,0,60,-60,-15);L=S.intersect(left);R=S.intersect(right);m['tab_mirror_comparison']={'left_clip':[34,56,-60,0,-60,-15],'right_clip':[34,56,0,60,-60,-15],'mirror_plane':'y=0',**compare(L.mirror('XZ'),R)}
# Section geometry generated independently from the imported final STEP.
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
m['sections']={}
for name,origin,normalvec,axes in [('axial_y0',(0,0,0),(0,1,0),(0,2)),('transverse_x45',(45,0,0),(1,0,0),(1,2)),('core_z60',(0,0,60),(0,0,1),(0,1)),('core_z84',(0,0,84),(0,0,1),(0,1))]:
 sec=BRepAlgoAPI_Section(S.wrapped,gp_Pln(gp_Pnt(*origin),gp_Dir(*normalvec)),False);sec.Build();shape=cq.Shape.cast(sec.Shape());records=[]
 fig,ax=plt.subplots(figsize=(9,6));
 for edge in shape.Edges():
  n=2 if edge.geomType()=='LINE' else 100
  pts=[edge.positionAt(k/(n-1)).toTuple() for k in range(n)];records.append({'type':edge.geomType(),'points':pts});ax.plot([p[axes[0]] for p in pts],[p[axes[1]] for p in pts],color='#1d3557',lw=1)
 ax.set_aspect('equal');ax.grid(alpha=.2);ax.set_xlabel('XYZ'[axes[0]]+' (mm)');ax.set_ylabel('XYZ'[axes[1]]+' (mm)');ax.set_title('Independent final STEP section: '+name);fig.tight_layout();fig.savefig(name+'.png',dpi=160);plt.close(fig)
 m['sections'][name]={'origin':origin,'normal':normalvec,'edge_count':len(records),'file':name+'.png'}
 Path(name+'.json').write_text(json.dumps(records))
Path('measured.json').write_text(json.dumps(m,indent=2));print(json.dumps(m,indent=2))
