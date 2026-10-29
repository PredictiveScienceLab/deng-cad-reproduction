"""STEP-only analytic measurements for a conservative subset of Deng stage 1.

No submitted Python or parameter manifest is imported or executed.  Recognition
uses intrinsic geometry, not global XYZ or the expected nominal dimensions.
Unrecognized/split representations are explicitly requires_review, never pass.
The fixture suite qualifies these supported analytic representations only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import cadquery as cq
import numpy as np
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface

RECOGNITION_TOL_MM = 1e-5
ANGLE_RECOGNITION_TOL = 1e-6
DEFAULT_LINEAR_TOL_MM = 0.01
AUTO_IDS = [f"S1D{i}" for i in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11,
                               12, 13, 14, 17, 18, 19, 20, 21]]


def vec(value):
    if hasattr(value, "Coord"):
        return np.array(value.Coord(), dtype=float)
    if hasattr(value, "toTuple"):
        return np.array(value.toTuple(), dtype=float)
    return np.array(value, dtype=float)


def unit(value):
    a = vec(value)
    return a / np.linalg.norm(a)


def parallel(a, b):
    return abs(float(np.dot(unit(a), unit(b)))) >= 1 - ANGLE_RECOGNITION_TOL


def line_distance(point, origin, direction):
    delta = vec(point) - vec(origin)
    return float(np.linalg.norm(delta - np.dot(delta, direction) * direction))


def interval(shape, axis):
    # For the recognized cylinders/cones/rounded rectangles, extrema along the
    # intrinsic axis occur at boundary vertices. Other surface types are omitted.
    values = [float(np.dot(vec(v.Center()), axis)) for v in shape.Vertices()]
    if not values:
        raise ValueError("No boundary vertices available for an axial measurement")
    return [min(values), max(values)]


def circle_info(edge):
    a = BRepAdaptor_Curve(edge.wrapped)
    if edge.geomType() != "CIRCLE":
        return None
    c = a.Circle()
    return {"radius": float(c.Radius()), "center": vec(c.Location()).tolist(),
            "axis": vec(c.Axis().Direction()).tolist(),
            "angle_radians": float(edge.Length() / c.Radius())}


def circular_wire(wire):
    parts = [circle_info(e) for e in wire.Edges()]
    if not parts or any(p is None for p in parts):
        return None
    first = parts[0]
    if any(abs(p["radius"] - first["radius"]) > RECOGNITION_TOL_MM
           or np.linalg.norm(vec(p["center"]) - vec(first["center"])) > RECOGNITION_TOL_MM
           or not parallel(p["axis"], first["axis"]) for p in parts):
        return None
    if abs(sum(p["angle_radians"] for p in parts) - 2 * math.pi) > 1e-5:
        return None
    return {"radius": first["radius"], "center": first["center"],
            "axis": first["axis"], "edge_count": len(parts)}


def rounded_rectangle(face):
    """Recognize an exact four-line/four-quarter-arc rounded rectangle.

    Circle radii need not agree: differing radii are measured and may fail a
    corner-radius criterion. B-spline or split-line representations need review.
    """
    if face.geomType() != "PLANE":
        return None
    outer = face.outerWire()
    edges = outer.Edges()
    lines = [e for e in edges if e.geomType() == "LINE"]
    arcs = [circle_info(e) for e in edges if e.geomType() == "CIRCLE"]
    if len(edges) != 8 or len(lines) != 4 or len(arcs) != 4:
        return None
    if any(abs(c["angle_radians"] - math.pi / 2) > 1e-5 for c in arcs):
        return None
    directions = []
    for e in lines:
        vertices = e.Vertices()
        d = unit(vec(vertices[-1].Center()) - vec(vertices[0].Center()))
        if not any(parallel(d, previous) for previous in directions):
            directions.append(d)
    if len(directions) != 2 or abs(float(np.dot(*directions))) > 1e-6:
        return None
    a, b = directions
    vertices = [vec(v.Center()) for v in outer.Vertices()]
    lo = np.array([min(float(np.dot(p, d)) for p in vertices) for d in (a, b)])
    hi = np.array([max(float(np.dot(p, d)) for p in vertices) for d in (a, b)])
    normal = unit(face.normalAt())
    center = a * ((lo[0] + hi[0]) / 2) + b * ((lo[1] + hi[1]) / 2)
    center += normal * np.dot(vec(face.Center()), normal)
    circles = []
    noncircular_inner_wires = 0
    for wire in face.Wires():
        if wire.isSame(outer):
            continue
        circle = circular_wire(wire)
        if circle is None:
            noncircular_inner_wires += 1
        else:
            q = vec(circle["center"])
            circle["local_center"] = [float(np.dot(q - center, d)) for d in (a, b)]
            circles.append(circle)
    return {"center": center.tolist(), "normal": normal.tolist(),
            "basis": [a.tolist(), b.tolist()], "dimensions": (hi - lo).tolist(),
            "corner_radii": [c["radius"] for c in arcs], "circles": circles,
            "noncircular_inner_wires": noncircular_inner_wires}


def surface_record(face, index):
    a = BRepAdaptor_Surface(face.wrapped)
    record = {"face_index": index, "type": face.geomType(), "area_mm2": float(face.Area())}
    if record["type"] not in ("CYLINDER", "CONE"):
        return record
    obj = a.Cylinder() if record["type"] == "CYLINDER" else a.Cone()
    axis, origin = unit(obj.Axis().Direction()), vec(obj.Location())
    u = (a.FirstUParameter() + a.LastUParameter()) / 2
    v = (a.FirstVParameter() + a.LastVParameter()) / 2
    point = vec(a.Value(u, v))
    radial = point - origin - np.dot(point - origin, axis) * axis
    try:
        normal = unit(face.normalAt(cq.Vector(*point)))
        radial_dot = float(np.dot(normal, unit(radial)))
        sense = "external" if radial_dot > 0.5 else "internal" if radial_dot < -0.5 else "unknown"
    except Exception:
        sense = "unknown"
    record.update(axis=axis.tolist(), origin=origin.tolist(), sense=sense,
                  axial_interval=interval(face, axis))
    if record["type"] == "CYLINDER":
        record["radius_mm"] = float(obj.Radius())
    else:
        record["semi_angle_degrees"] = math.degrees(float(obj.SemiAngle()))
        circles = [circle_info(e) for e in face.Edges() if e.geomType() == "CIRCLE"]
        circles = [c for c in circles if parallel(c["axis"], axis)
                   and line_distance(c["center"], origin, axis) < RECOGNITION_TOL_MM]
        record["boundary_circles"] = circles
    return record


def load_step(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("STEP input must be an ordinary file")
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("STEP exceeds 100 MiB")
    shape = cq.importers.importStep(str(path)).val()
    return shape


def extract_step(path):
    """Return serializable STEP evidence without any task criterion decisions."""
    path = Path(path)
    shape = load_step(path)
    faces = shape.Faces()
    surfaces = [surface_record(f, i) for i, f in enumerate(faces)]
    rectangles = []
    for i, face in enumerate(faces):
        rect = rounded_rectangle(face)
        if rect:
            rectangles.append({"face_index": i, **rect})
    return {"input": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "valid": bool(shape.isValid()), "solid_count": len(shape.Solids()),
            "volume_mm3": float(shape.Volume()), "surfaces": surfaces,
            "rounded_rectangular_faces": rectangles}


def _recognize_stage1(analysis):
    if not analysis["valid"] or analysis["solid_count"] != 1:
        raise ValueError("Stage 1 requires one valid solid before dimensional recognition")
    cylinders = [s for s in analysis["surfaces"] if s["type"] == "CYLINDER" and s["sense"] == "external"]
    if not cylinders:
        raise ValueError("No external analytic cylinder supports a body-axis datum")
    # Dominant radius is a structural recognition heuristic, independent of the
    # target 35 mm radius. A near-tie is not resolved using expected dimensions.
    cylinders.sort(key=lambda s: s["radius_mm"], reverse=True)
    body = cylinders[0]
    if len(cylinders) > 1 and cylinders[1]["radius_mm"] > 0.8 * body["radius_mm"]:
        raise ValueError("Dominant external cylinder is not unique; split or ambiguous body surface")
    axis, origin = unit(body["axis"]), vec(body["origin"])
    rectangles = [r for r in analysis["rounded_rectangular_faces"]
                  if parallel(r["normal"], axis)
                  and line_distance(r["center"], origin, axis) < RECOGNITION_TOL_MM]
    if len(rectangles) != 4:
        raise ValueError("Expected exactly four coaxial rounded-rectangle flange faces")
    rectangles.sort(key=lambda r: float(np.dot(vec(r["center"]), axis)))
    first, last = rectangles[0], rectangles[-1]
    if abs(np.prod(first["dimensions"]) - np.prod(last["dimensions"])) < RECOGNITION_TOL_MM:
        raise ValueError("Terminal flanges cannot be distinguished by intrinsic outer size")
    if np.prod(first["dimensions"]) > np.prod(last["dimensions"]):
        large_front, large_back, small_back, small_front = rectangles
    else:
        small_front, small_back, large_back, large_front = rectangles
    for front, back in [(large_front, large_back), (small_front, small_back)]:
        if max(abs(a - b) for a, b in zip(sorted(front["dimensions"]), sorted(back["dimensions"]))) > RECOGNITION_TOL_MM:
            raise ValueError("Flange front/back outer dimensions do not match")
    return {"body": body, "axis": axis, "origin": origin,
            "large_front": large_front, "large_back": large_back,
            "small_front": small_front, "small_back": small_back}


def _bolt_pattern(face, central_circle):
    circles = list(face["circles"])
    if central_circle:
        if len(circles) != 5 or face["noncircular_inner_wires"] != 0:
            raise ValueError("Terminal face does not have four circular bolt holes plus a circular bore")
        circles.sort(key=lambda c: c["radius"])
        if circles[-1]["radius"] <= 1.5 * max(c["radius"] for c in circles[:-1]):
            raise ValueError("Central bore is not distinguishable from the bolt-hole loops")
        bore = circles.pop()
        if np.linalg.norm(bore["local_center"]) > RECOGNITION_TOL_MM:
            raise ValueError("Largest circular loop is not centered on the body axis")
    elif len(circles) != 4 or face["noncircular_inner_wires"] != 1:
        raise ValueError("Small terminal face does not have four circular bolt holes and one keyed bore")
    quadrants = [(int(c["local_center"][0] > 0), int(c["local_center"][1] > 0)) for c in circles]
    if len(set(quadrants)) != 4 or any(min(abs(x) for x in c["local_center"]) < RECOGNITION_TOL_MM for c in circles):
        raise ValueError("Four bolt loops do not occupy the four flange quadrants uniquely")
    return circles


def _measurement_spec(criteria, cid, expected):
    if not criteria:
        # The two rounded printed values are intervals, not exact decimals.
        width = 0.006 if cid in ("S1D4", "S1D13") else DEFAULT_LINEAR_TOL_MM
        return float(expected), [expected - width, expected + width], width
    rows = criteria.get("criteria", []) if isinstance(criteria, dict) else criteria
    row = next((r for r in rows if r.get("id") == cid), None)
    if row is None:
        raise ValueError(f"Frozen rubric is missing implemented criterion {cid}")
    nominal = float(row["expected"]["value"])
    if abs(nominal - float(expected)) > 1e-9:
        raise ValueError(f"Frozen rubric nominal for {cid} differs from audited mapping")
    tolerance = row.get("tolerance")
    if isinstance(tolerance, dict) and tolerance.get("kind") == "rounded_interval":
        lower, upper = row["expected"]["interval"]
        margin = float(tolerance["numerical_margin"])
        return nominal, [float(lower) - margin, float(upper) + margin], margin
    if isinstance(tolerance, (float, int)):
        tolerance = float(tolerance)
        return nominal, [nominal - tolerance, nominal + tolerance], tolerance
    if isinstance(tolerance, dict):
        for key in ("absolute_mm", "absolute", "value", "linear_mm"):
            if isinstance(tolerance.get(key), (int, float)):
                value = float(tolerance[key])
                return nominal, [nominal - value, nominal + value], value
    raise ValueError(f"Unsupported tolerance policy for {cid}")


def evaluate_stage1(path, criteria=None):
    """Evaluate declared analytic criteria; output leaves others requires_review.

    Criterion nominal values are the Appendix B values, with all multiplicities
    checked. Recognition error is not a geometric failure. STEP integrity is
    separately owned by the general evaluator; invalid STEP cannot earn passes.
    """
    results = {f"S1D{i}": {"id": f"S1D{i}", "status": "requires_review",
               "rubric_status": "requires_review",
               "reason": "Not implemented by this analytic measurement module", "evidence": []}
               for i in range(1, 23)}
    try:
        analysis = extract_step(path)
        context = _recognize_stage1(analysis)
    except Exception as exc:
        return {"analysis": locals().get("analysis"), "criteria": list(results.values()),
                "recognition_status": "requires_review", "recognition_reason": str(exc)}
    axis, origin = context["axis"], context["origin"]
    large, small, body = context["large_front"], context["small_front"], context["body"]

    def measure(cid, actual, expected, evidence, note=""):
        actual = [float(a) for a in (actual if isinstance(actual, (list, tuple, np.ndarray)) else [actual])]
        nominal, accepted_interval, tolerance = _measurement_spec(criteria, cid, expected)
        expected_values = [nominal] * len(actual)
        errors = [abs(a - e) for a, e in zip(actual, expected_values)]
        passed = bool(actual) and all(math.isfinite(a) and accepted_interval[0] - 1e-9 <= a <= accepted_interval[1] + 1e-9 for a in actual)
        results[cid] = {"id": cid, "status": "pass" if passed else "fail",
                        "rubric_status": "correct" if passed else "incorrect",
                        "measurement": actual, "expected": expected_values, "unit": "mm",
                        "accepted_interval_mm": accepted_interval,
                        "absolute_errors": errors, "tolerance_mm": tolerance,
                        "evidence": evidence, "reason": note or "Measured independently from reimported STEP geometry"}

    def guarded(ids, function):
        try:
            function()
        except Exception as exc:
            for cid in ids:
                results[cid].update(status="requires_review", rubric_status="requires_review", reason=str(exc))

    measure("S1D1", large["dimensions"], 100, [large["face_index"]])
    measure("S1D5", large["corner_radii"], 10, [large["face_index"]])
    measure("S1D14", small["corner_radii"], 5, [small["face_index"]])
    measure("S1D19", small["dimensions"], 70, [small["face_index"]])
    measure("S1D7", 2 * body["radius_mm"], 70, [body["face_index"]])
    measure("S1D9", body["axial_interval"][1] - body["axial_interval"][0], 70, [body["face_index"]])
    for cid, front, back, expected in [("S1D8", large, context["large_back"], 10),
                                       ("S1D11", small, context["small_back"], 12)]:
        measure(cid, abs(np.dot(vec(front["center"]) - vec(back["center"]), axis)), expected,
                [front["face_index"], back["face_index"]])

    def require_through(cid, holes, front, back):
        checks = []
        for hole in holes:
            candidates = [s for s in analysis["surfaces"]
                          if s["type"] == "CYLINDER" and s["sense"] == "internal"
                          and parallel(s["axis"], axis)
                          and abs(s["radius_mm"] - hole["radius"]) < RECOGNITION_TOL_MM
                          and line_distance(hole["center"], s["origin"], unit(s["axis"])) < RECOGNITION_TOL_MM]
            if len(candidates) != 1:
                raise ValueError("Hole opening lacks one uniquely associated internal cylindrical wall")
            wall = candidates[0]
            wall_axis = unit(wall["axis"])
            targets = sorted(float(np.dot(vec(f["center"]), wall_axis)) for f in (front, back))
            errors = [abs(a - b) for a, b in zip(wall["axial_interval"], targets)]
            checks.append({"face_index": wall["face_index"], "actual_interval": wall["axial_interval"],
                           "flange_interval": targets, "through": max(errors) <= RECOGNITION_TOL_MM})
        results[cid]["through_hole_evidence"] = checks
        if not all(check["through"] for check in checks):
            results[cid].update(status="fail", rubric_status="incorrect",
                                reason="At least one cylindrical hole does not span the flange thickness")

    def large_holes():
        holes = _bolt_pattern(large, True)
        measure("S1D2", [2 * h["radius"] for h in holes], 10, [large["face_index"]], "All four hole loops measured")
        require_through("S1D2", holes, large, context["large_back"])
        offsets = [large["dimensions"][i] / 2 - abs(h["local_center"][i]) for h in holes for i in range(2)]
        measure("S1D6", offsets, 12, [large["face_index"]], "All eight center-to-nearest-edge offsets measured")
        bore = max(large["circles"], key=lambda h: h["radius"])
        measure("S1D3", 2 * bore["radius"], 45, [large["face_index"]])
    guarded(["S1D2", "S1D3", "S1D6"], large_holes)

    def small_holes():
        holes = _bolt_pattern(small, False)
        measure("S1D17", [2 * h["radius"] for h in holes], 10, [small["face_index"]], "All four hole loops measured")
        require_through("S1D17", holes, small, context["small_back"])
        offsets = [small["dimensions"][i] / 2 - abs(h["local_center"][i]) for h in holes for i in range(2)]
        measure("S1D18", offsets, 10, [small["face_index"]], "All eight center-to-nearest-edge offsets measured")
    guarded(["S1D17", "S1D18"], small_holes)

    def cone(sense):
        candidates = [s for s in analysis["surfaces"] if s["type"] == "CONE" and s["sense"] == sense
                      and parallel(s["axis"], axis) and line_distance(s["origin"], origin, axis) < RECOGNITION_TOL_MM]
        if len(candidates) != 1:
            raise ValueError(f"Expected one unique {sense} coaxial analytic conical face")
        c = candidates[0]
        boundaries = []
        for circle in c["boundary_circles"]:
            station = float(np.dot(vec(circle["center"]), axis))
            if not any(abs(station - b["station"]) < RECOGNITION_TOL_MM for b in boundaries):
                boundaries.append({"station": station, "radius": circle["radius"]})
        if len(boundaries) != 2:
            raise ValueError("Cone does not have two identifiable circular boundary stations")
        small_station = float(np.dot(vec(small["center"]), axis))
        boundaries.sort(key=lambda b: abs(b["station"] - small_station))
        return c, boundaries

    def external_cone():
        c, boundaries = cone("external")
        near_small, near_large = boundaries
        small_back = float(np.dot(vec(context["small_back"]["center"]), axis))
        large_back = float(np.dot(vec(context["large_back"]["center"]), axis))
        shoulder = max(body["axial_interval"], key=lambda p: abs(p - large_back))
        if abs(near_small["station"] - small_back) > RECOGNITION_TOL_MM or abs(near_large["station"] - shoulder) > RECOGNITION_TOL_MM:
            raise ValueError("Outer cone does not join body shoulder to small flange")
        measure("S1D10", c["axial_interval"][1] - c["axial_interval"][0], 50, [c["face_index"]])
        measure("S1D12", 2 * near_small["radius"], 50, [c["face_index"]])
        measure("S1D13", 2 * near_large["radius"], 62.28, [c["face_index"]])
    guarded(["S1D10", "S1D12", "S1D13"], external_cone)

    def internal_cone():
        c, boundaries = cone("internal")
        near_small, near_large = boundaries
        large_front = float(np.dot(vec(large["center"]), axis))
        if abs(near_large["station"] - large_front) > RECOGNITION_TOL_MM:
            raise ValueError("Main bore cone does not meet large terminal face")
        measure("S1D4", 2 * near_small["radius"], 30.85, [c["face_index"]])
    guarded(["S1D4"], internal_cone)

    def counterbore():
        candidates = [s for s in analysis["surfaces"] if s["type"] == "CYLINDER" and s["sense"] == "internal"
                      and parallel(s["axis"], axis) and line_distance(s["origin"], origin, axis) < RECOGNITION_TOL_MM]
        if len(candidates) != 1:
            raise ValueError("Expected one unique internal coaxial counterbore cylinder")
        c = candidates[0]
        # An arbitrary internal cylinder must not be interpreted as the terminal
        # counterbore: its axial extent must meet the small terminal face.
        terminal = float(np.dot(vec(small["center"]), unit(c["axis"])))
        if min(abs(terminal - v) for v in c["axial_interval"]) > RECOGNITION_TOL_MM:
            raise ValueError("Internal cylindrical surface does not meet small terminal face")
        measure("S1D20", c["axial_interval"][1] - c["axial_interval"][0], 7, [c["face_index"]])
        measure("S1D21", 2 * c["radius_mm"], 40, [c["face_index"]])
    guarded(["S1D20", "S1D21"], counterbore)

    return {"analysis": analysis, "criteria": list(results.values()), "recognition_status": "recognized",
            "recognition": {"method": "dominant external cylinder plus four coaxial rounded-rectangle flange faces",
                            "axis": axis.tolist(), "origin": origin.tolist()},
            "scope": "Only listed analytic stage-1 measurements; not an overall drawing-fidelity acceptance"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("step")
    parser.add_argument("--rubric")
    parser.add_argument("--output")
    args = parser.parse_args()
    rubric = json.loads(Path(args.rubric).read_text()) if args.rubric else None
    report = evaluate_stage1(args.step, rubric)
    encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        Path(args.output).write_text(encoded)
    else:
        print(encoded, end="")
