"""Evaluator-owned analytic fixtures; not generated candidate submissions.

The stage-1 fixture instantiates the published analytic dimensions solely to
qualify measurement recognition. It is not distributed to the candidate.
"""
from pathlib import Path
import math
import cadquery as cq


def stage1_fixture(*, body_diameter=70.0, large_hole_radius=5.0,
                   hole_dx=0.0, counterbore_depth=7.0, cone_angle=7.0,
                   remove_large_hole=False, stand_instead=False,
                   first_large_hole_depth=10.0, reverse_outer_cone=False):
    if stand_instead:
        return cq.Workplane("XY").box(100, 70, 10).val()
    large = cq.Workplane("XY").rect(100, 100).extrude(10).edges("|Z").fillet(10).val()
    body = cq.Solid.makeCylinder(body_diameter / 2, 70, cq.Vector(0, 0, 10))
    small = cq.Workplane("XY").workplane(offset=130).rect(70, 70).extrude(12).edges("|Z").fillet(5).val()
    radii = [25 + 50 * math.tan(math.radians(cone_angle)), 25]
    if reverse_outer_cone:
        radii.reverse()
    cone = cq.Solid.makeCone(*radii, 50,
                             cq.Vector(0, 0, 80))
    shape = large.fuse(body, cone, small)
    bore_length = 142 - counterbore_depth
    bore = cq.Solid.makeCone(22.5, 22.5 - bore_length * math.tan(math.radians(3)), bore_length)
    shape = shape.cut(bore)
    shape = shape.cut(cq.Solid.makeCylinder(20, counterbore_depth, cq.Vector(0, 0, bore_length)))
    notch = cq.Workplane("XY").workplane(offset=bore_length).center(0, 12.5).rect(5, 25).extrude(counterbore_depth).val()
    shape = shape.cut(notch)
    for i, (x, y) in enumerate([(-38, -38), (-38, 38), (38, -38), (38, 38)]):
        if remove_large_hole and i == 0:
            continue
        radius = large_hole_radius if i == 0 else 5.0
        x += hole_dx if i == 0 else 0
        depth = first_large_hole_depth if i == 0 else 10
        shape = shape.cut(cq.Solid.makeCylinder(radius, depth, cq.Vector(x, y, 0)))
    for x in [-25, 25]:
        for y in [-25, 25]:
            shape = shape.cut(cq.Solid.makeCylinder(5, 12, cq.Vector(x, y, 130)))
    return shape.clean()


def export_fixture(path, **kwargs):
    path = Path(path)
    cq.exporters.export(stage1_fixture(**kwargs), str(path))
    return path

