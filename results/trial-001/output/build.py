"""Drawing reconstruction, millimeters. Run from the workspace: python output/build.py.

X: main shaft from the rear flange (0) to the front flange (142).
Y: transverse; Z: up. Main axis is Y=Z=0. The stand is built in place.
See README.txt for the interpretation of the underdimensioned details.
"""
from dataclasses import dataclass, asdict
from pathlib import Path
import math
import json
import cadquery as cq

OUT = Path(__file__).resolve().parent
V = cq.Vector

@dataclass(frozen=True)
class Dimensions:
    length: float = 142.0
    rear_size: float = 100.0
    rear_thickness: float = 10.0
    rear_corner: float = 10.0
    rear_hole_pitch: float = 76.0
    flange_hole_d: float = 10.0
    body_d: float = 70.0
    shoulder_x: float = 80.0
    front_x: float = 130.0
    taper_end_d: float = 50.0
    outer_taper_deg: float = 7.0
    front_size: float = 70.0
    front_corner: float = 5.0
    front_hole_pitch: float = 50.0
    rear_bore_d: float = 45.0
    bore_taper_deg: float = 3.0
    counterbore_d: float = 40.0
    counterbore_depth: float = 7.0
    key_width: float = 5.0
    key_top: float = 25.0
    tower_x: float = 45.0
    tower_core_size: float = 40.0
    tower_core_corner: float = 8.0
    tower_fin_size: float = 60.0
    tower_fin_corner: float = 10.0
    fin_bottom: float = 54.0
    fin_thickness: float = 4.0
    fin_gap: float = 4.0
    fin_count: int = 5
    tower_boss_d: float = 45.0
    tower_boss_height: float = 4.0
    tower_bore_d: float = 18.0
    side_hole_d: float = 8.5
    side_hole_depth: float = 5.0
    drill_included_deg: float = 118.0
    lug_angle_deg: float = 45.0
    lug_outer_radius: float = 60.0
    lug_hole_radius: float = 50.0
    lug_axial_width: float = 20.0
    lug_thickness: float = 10.0
    lug_hole_d: float = 12.0
    port_from_front: float = 37.0
    port_d: float = 22.0
    port_bore_d: float = 12.0
    port_height: float = 10.0
    stand_width: float = 70.0
    stand_total_height: float = 80.0
    stand_thickness: float = 10.0
    rib_start: float = 30.0
    rib_length: float = 70.0
    rib_width: float = 15.0
    slot_width: float = 8.0
    slot_spacing: float = 48.0

P = Dimensions()

def cyl(radius, length, point, direction=(1, 0, 0)):
    return cq.Solid.makeCylinder(radius, length, V(*point), V(*direction))

def box(x0, x1, y0, y1, z0, z1):
    return cq.Solid.makeBox(x1-x0, y1-y0, z1-z0, V(x0, y0, z0))

def rounded_flange(x, size, radius, thickness):
    return (cq.Workplane('YZ', origin=(x,0,0)).rect(size,size)
            .extrude(thickness).edges('|X').fillet(radius).val())

def rounded_vertical(x, z, size, radius, height):
    return (cq.Workplane('XY', origin=(x,0,z)).rect(size,size)
            .extrude(height).edges('|Z').fillet(radius).val())

def bolt_centers(pitch):
    return [(y,z) for y in (-pitch/2, pitch/2) for z in (-pitch/2, pitch/2)]

def exterior(p=P):
    """Unbored turning envelope: also the stand's master contact geometry."""
    angle = math.radians(p.outer_taper_deg)
    big_taper_r = p.taper_end_d/2 + (p.front_x-p.shoulder_x)*math.tan(angle)
    tube = cyl(p.body_d/2, p.shoulder_x-p.rear_thickness, (p.rear_thickness,0,0))
    cone = cq.Solid.makeCone(big_taper_r, p.taper_end_d/2,
                            p.front_x-p.shoulder_x,
                            V(p.shoulder_x,0,0), V(1,0,0))
    return tube.fuse(cone).clean()

def bore_tools(p=P):
    xb = p.length - p.counterbore_depth
    end_r = p.rear_bore_d/2 - xb*math.tan(math.radians(p.bore_taper_deg))
    cone = cq.Solid.makeCone(p.rear_bore_d/2, end_r, xb, V(0,0,0), V(1,0,0))
    cb = cyl(p.counterbore_d/2, p.counterbore_depth, (xb,0,0))
    # The keyway is confined to the counterbore, terminating at its shoulder.
    key = box(xb, p.length, -p.key_width/2, p.key_width/2, 0, p.key_top)
    return cone.fuse(cb).fuse(key).clean()

def stage1(p=P):
    master = exterior(p)
    rear = rounded_flange(0, p.rear_size, p.rear_corner, p.rear_thickness)
    front = rounded_flange(p.front_x, p.front_size, p.front_corner, p.length-p.front_x)
    s = master.fuse(rear).fuse(front).clean().cut(bore_tools(p))
    for y,z in bolt_centers(p.rear_hole_pitch):
        s = s.cut(cyl(p.flange_hole_d/2, p.rear_thickness, (0,y,z)))
    for y,z in bolt_centers(p.front_hole_pitch):
        s = s.cut(cyl(p.flange_hole_d/2, p.length-p.front_x, (p.front_x,y,z)))
    return s.clean(), master

def stage2(s1, p=P):
    fin_top = p.fin_bottom + p.fin_count*p.fin_thickness + (p.fin_count-1)*p.fin_gap
    tower = rounded_vertical(p.tower_x, 0, p.tower_core_size,
                             p.tower_core_corner, fin_top)
    for k in range(p.fin_count):
        z = p.fin_bottom + k*(p.fin_thickness+p.fin_gap)
        tower = tower.fuse(rounded_vertical(p.tower_x,z,p.tower_fin_size,
                                           p.tower_fin_corner,p.fin_thickness))
    tower = tower.fuse(cyl(p.tower_boss_d/2, p.tower_boss_height,
                           (p.tower_x,0,fin_top), (0,0,1)))
    s = s1.fuse(tower).clean().cut(bore_tools(p))
    s = s.cut(cyl(p.tower_bore_d/2, fin_top+p.tower_boss_height+1,
                  (p.tower_x,0,0), (0,0,1)))
    return s.clean()

def stage3(s2, p=P):
    s = s2
    a = math.radians(p.lug_angle_deg)
    for side in (-1,1):
        radial = V(0,side*math.sin(a),-math.cos(a))
        tangent = V(1,0,0).cross(radial)
        # Embedded root creates a clean, continuous union with the barrel.
        root = p.body_d/2 - p.lug_thickness/2
        plane = cq.Plane(origin=(p.tower_x-p.lug_axial_width/2,0,0),
                         xDir=radial, normal=(1,0,0))
        lug = (cq.Workplane(plane).center((root+p.lug_outer_radius)/2,0)
               .rect(p.lug_outer_radius-root,p.lug_thickness)
               .extrude(p.lug_axial_width).val())
        s = s.fuse(lug)
        hp = V(p.tower_x,0,0) + radial*p.lug_hole_radius
        start = hp - tangent*(p.lug_thickness+1)
        s = s.cut(cq.Solid.makeCylinder(p.lug_hole_d/2,
                    2*(p.lug_thickness+1), start, tangent))
    # Both radial blind holes are present in section B-B.
    for side in (-1,1):
        direction = V(0,-side,0)
        start = V(p.tower_x,side*p.body_d/2,0)
        drill = cq.Solid.makeCylinder(p.side_hole_d/2,p.side_hole_depth,start,direction)
        tip_h = (p.side_hole_d/2)/math.tan(math.radians(p.drill_included_deg/2))
        tip = cq.Solid.makeCone(p.side_hole_d/2,0,tip_h,
                               start+direction*p.side_hole_depth,direction)
        s = s.cut(drill.fuse(tip))
    # Normal port on the conical surface: 25 axial mm from x=130.
    px = p.length-p.port_from_front
    pa = math.radians(p.outer_taper_deg)
    pz = p.taper_end_d/2+(p.front_x-px)*math.tan(pa)
    foot = V(px,0,pz)
    normal = V(math.sin(pa),0,math.cos(pa))
    embed = p.port_d/2
    boss = cq.Solid.makeCylinder(p.port_d/2,embed+p.port_height,
                                foot-normal*embed,normal)
    s = s.fuse(boss).clean().cut(bore_tools(p))
    # End the drill in the axial bore; it must not reach the opposite wall.
    bore_start = foot-normal*(p.taper_end_d/2)
    drill = cq.Solid.makeCylinder(p.port_bore_d/2,
                p.taper_end_d/2+p.port_height+1,bore_start,normal)
    s = s.cut(drill)
    return s.clean()

def make_stand(master_contact, p=P):
    """All saddle/rib surfaces cut with the main external master geometry.
    Lower front bolt centers are shared with the main flange, not re-entered.
    """
    zbottom = -p.stand_total_height
    zbase = zbottom+p.stand_thickness
    base = box(0,p.front_x,-p.stand_width/2,p.stand_width/2,zbottom,zbase)
    plate = box(p.front_x-p.stand_thickness,p.front_x,
                -p.stand_width/2,p.stand_width/2,zbase,0)
    # The stock is below the main cavity; use the solid exterior as a die.
    rib = box(p.rib_start,p.rib_start+p.rib_length,
              -p.rib_width/2,p.rib_width/2,zbase,0)
    support = plate.fuse(rib).cut(master_contact)
    s = base.fuse(support).clean()
    for yc in (-p.slot_spacing/2,p.slot_spacing/2):
        s = s.cut(box(p.rib_start,p.rib_start+p.rib_length,
                      yc-p.slot_width/2,yc+p.slot_width/2,zbottom,zbase))
    for y,z in bolt_centers(p.front_hole_pitch):
        if z < 0:
            s = s.cut(cyl(p.flange_hole_d/2,p.stand_thickness,
                          (p.front_x-p.stand_thickness,y,z)))
    return s.clean()

def summary(shape):
    bb=shape.BoundingBox()
    return dict(valid=shape.isValid(), solids=len(shape.Solids()),
                volume_mm3=shape.Volume(),
                bounds_mm=[bb.xmin,bb.xmax,bb.ymin,bb.ymax,bb.zmin,bb.zmax])

def build():
    OUT.mkdir(exist_ok=True)
    one, master = stage1()
    cq.exporters.export(one,str(OUT/'step-1.step'))
    two = stage2(one)
    cq.exporters.export(two,str(OUT/'step-2.step'))
    three = stage3(two)
    cq.exporters.export(three,str(OUT/'step-3.step'))
    cq.exporters.export(three,str(OUT/'main-part.step'))
    stand = make_stand(master)
    cq.exporters.export(stand,str(OUT/'stand.step'))
    assy = cq.Assembly(name='SupportedHousing')
    assy.add(three,name='MainPart',color=cq.Color(0.74,0.76,0.79))
    assy.add(stand,name='Stand',color=cq.Color(0.28,0.43,0.60))
    assy.save(str(OUT/'assembly.step'),exportType='STEP',mode='default')
    stats={name:summary(s) for name,s in [('step-1',one),('step-2',two),
                                          ('step-3',three),('stand',stand)]}
    stats['main_stand_overlap_mm3']=three.intersect(stand).Volume()
    stats['derived']={
        'taper_shoulder_d':P.taper_end_d+2*(P.front_x-P.shoulder_x)*math.tan(math.radians(P.outer_taper_deg)),
        'bore_end_d':P.rear_bore_d-2*(P.length-P.counterbore_depth)*math.tan(math.radians(P.bore_taper_deg)),
        'port_surface_distance_to_front':(P.front_x-(P.length-P.port_from_front))/math.cos(math.radians(P.outer_taper_deg))}
    (OUT/'parameters.json').write_text(json.dumps(asdict(P),indent=2)+'\n')
    (OUT/'measurements.json').write_text(json.dumps(stats,indent=2)+'\n')
    for name,stat in stats.items():
        print(name,stat)
    return one,two,three,stand,master

if __name__=='__main__':
    build()
