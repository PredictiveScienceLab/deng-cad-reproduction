"""Read-only integrity checks for the six canonical Deng CAD STEP artifacts.

This module never imports or executes candidate source. It copies ordinary
STEP files into a private temporary directory, then independently imports all
STEP roots. Drawing fidelity and source rebuild are separate evaluations.
Call only after candidate writers have stopped: the copy detects common races,
but is not an atomic filesystem snapshot of a concurrently mutating tree.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile


FILES = ("step-1.step", "step-2.step", "step-3.step", "main-part.step", "stand.step", "assembly.step")
EXPECTED_SOLIDS = {name: 2 if name == "assembly.step" else 1 for name in FILES}
MAX_FILE_BYTES = 100_000_000


class FilePolicyError(ValueError):
    pass


class ChangedInputError(ValueError):
    pass


class InvalidBoolean(ValueError):
    pass


def _status(values):
    values = list(values)
    if "fail" in values:
        return "fail"
    return "pass" if values and all(value == "pass" for value in values) else "inconclusive"


def _error(error):
    return f"{type(error).__name__}: {error}"


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_directory(path):
    """Check trusted, stopped ancestors without requesting directory listings.

    Seatbelt may permit ancestor metadata and a leaf read root while refusing
    open(O_RDONLY) and even open(O_EVTONLY) on ancestors. All components are
    lstat-checked before/after the final no-follow open. This requires stable
    trusted ancestors; it is not atomic against an adversarial ancestor writer.
    Subsequent STEP opens remain anchored to the final directory descriptor.
    """
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode)
    def inspect():
        components = []
        current = Path(path.anchor)
        for part in (None, *path.parts[1:]):
            if part is not None:
                current = current/part
            info = os.stat(current, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise FilePolicyError("Every input path component must be an ordinary directory, not a symlink")
            components.append((str(current), identity(info)))
        return components
    before = inspect()
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if identity(os.fstat(descriptor)) != before[-1][1] or inspect() != before:
            raise ChangedInputError("Input directory or ancestor changed during open")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _quiet_native():
    """Keep OpenCascade parser diagnostics out of machine-readable stdout."""
    sys.stdout.flush()
    sys.stderr.flush()
    originals = [os.dup(1), os.dup(2)]
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, 1)
        os.dup2(null, 2)
        yield
    finally:
        for number, original in zip((1, 2), originals):
            os.dup2(original, number)
            os.close(original)
        os.close(null)


def _copy_file(directory_fd, name, target, max_file_bytes):
    try:
        initial = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise FilePolicyError("Required canonical file is missing") from error
    if not stat.S_ISREG(initial.st_mode):
        raise FilePolicyError("Required STEP must be an ordinary file; symlinks and special files are refused")
    if not 0 < initial.st_size <= max_file_bytes:
        raise FilePolicyError(f"STEP size must be between 1 and {max_file_bytes} bytes")
    source_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    digest, count = hashlib.sha256(), 0
    try:
        opened = os.fstat(source_fd)
        if _fingerprint(opened) != _fingerprint(initial):
            raise ChangedInputError("Input changed before snapshot copy")
        with target.open("xb") as output:
            while chunk := os.read(source_fd, min(1024 * 1024, max_file_bytes - count + 1)):
                count += len(chunk)
                if count > max_file_bytes:
                    raise FilePolicyError("Input grew beyond maximum file size")
                digest.update(chunk)
                output.write(chunk)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (_fingerprint(os.fstat(source_fd)) != _fingerprint(opened)
                or _fingerprint(current) != _fingerprint(opened) or count != opened.st_size):
            raise ChangedInputError("Input changed during snapshot copy")
        return {"sha256": digest.hexdigest(), "bytes": count, "status": "pass"}
    finally:
        os.close(source_fd)


def _unit_names(reader):
    from OCP.TColStd import TColStd_SequenceOfAsciiString
    length, angle, solid_angle = (TColStd_SequenceOfAsciiString() for _ in range(3))
    reader.FileUnits(length, angle, solid_angle)
    return {label: [sequence.Value(i).ToCString() for i in range(1, sequence.Length() + 1)]
            for label, sequence in (("length", length), ("angle", angle), ("solid_angle", solid_angle))}


def _inspect_units(reader):
    """Read length units from STEP shape representation contexts, not a manifest."""
    try:
        names = _unit_names(reader)
    except Exception as error:
        return {"status": "inconclusive", "method": "STEPControl_Reader.FileUnits",
                "error": _error(error)}
    normalized = {name.strip().lower().replace(" ", "") for name in names["length"]}
    millimeters = {"millimetre", "millimeter", "millimetres", "millimeters", "mm"}
    known_other = {"metre", "meter", "m", "centimetre", "centimeter", "cm",
                   "micrometre", "micrometer", "micron", "inch", "inches", "in",
                   "foot", "feet", "ft", "kilometre", "kilometer", "km"}
    if normalized and normalized <= millimeters:
        status, reason = "pass", "All reported representation length units are millimeters"
    elif normalized & known_other:
        status, reason = "fail", "STEP declares a non-millimeter length unit"
    else:
        status, reason = "inconclusive", "Length units are absent or not recognized; millimeter declarations are not established"
    return {"status": status, "method": "STEPControl_Reader.FileUnits", "reported_units": names,
            "reason": reason, "transfer_target_units": "mm"}


def _loose_topology(shape):
    """Reject non-solid geometry that is not contained inside a solid."""
    from OCP.TopAbs import TopAbs_SOLID, TopAbs_COMPOUND, TopAbs_COMPSOLID
    from OCP.TopoDS import TopoDS_Iterator
    found = []
    def walk(current):
        kind = current.ShapeType()
        if kind == TopAbs_SOLID:
            return
        if kind not in (TopAbs_COMPOUND, TopAbs_COMPSOLID):
            found.append(str(kind))
            return
        children = TopoDS_Iterator(current)
        while children.More():
            walk(children.Value())
            children.Next()
    walk(shape.wrapped)
    return found


def _describe(shape):
    from OCP.BRepCheck import BRepCheck_Analyzer
    if shape.isNull():
        raise ValueError("Null imported shape")
    solids = shape.Solids()
    bounds = shape.BoundingBox()
    low, high = [bounds.xmin, bounds.ymin, bounds.zmin], [bounds.xmax, bounds.ymax, bounds.zmax]
    solid_records = [{"valid": bool(BRepCheck_Analyzer(solid.wrapped).IsValid()),
                      "volume_mm3": float(solid.Volume())} for solid in solids]
    volume_sum = sum(item["volume_mm3"] for item in solid_records)
    finite = all(math.isfinite(value) for value in low + high + [volume_sum] + [item["volume_mm3"] for item in solid_records])
    positive = bool(solids) and all(item["valid"] and item["volume_mm3"] > 0 for item in solid_records)
    return {"valid": bool(BRepCheck_Analyzer(shape.wrapped).IsValid()), "finite": finite,
            "positive_valid_solids": positive, "solid_count": len(solids), "solids": solid_records,
            "sum_solid_volume_mm3": volume_sum,
            "bounds_mm": {"min": low, "max": high}, "loose_topology": _loose_topology(shape)}


def _load_step(path):
    import cadquery as cq
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader
    reader = STEPControl_Reader()
    with _quiet_native():
        status = reader.ReadFile(str(path))
    if status != IFSelect_RetDone:
        raise ValueError(f"STEP parser rejected file: {status}")
    units = _inspect_units(reader)
    # OpenCascade uses millimeters as the transfer target (1.0 mm), regardless
    # of source-file units. Unit compliance remains a separate explicit check.
    reader.SetSystemLengthUnit(1.0)
    roots = int(reader.NbRootsForTransfer())
    with _quiet_native():
        transferred = int(reader.TransferRoots())
    if roots <= 0 or transferred != roots:
        raise ValueError(f"STEP roots transferred incompletely: {transferred}/{roots}")
    shape = cq.Shape.cast(reader.OneShape())
    return shape, units, {"roots_available": roots, "roots_transferred": transferred,
                          "shape_outputs": int(reader.NbShapes())}


def _checked_volume(shape):
    """Never treat invalid/null Boolean results as zero overlap or agreement."""
    from OCP.BRepCheck import BRepCheck_Analyzer
    if shape.isNull() or not BRepCheck_Analyzer(shape.wrapped).IsValid():
        raise InvalidBoolean("Null or invalid Boolean result")
    volumes = []
    for solid in shape.Solids():
        volume = float(solid.Volume())
        if not BRepCheck_Analyzer(solid.wrapped).IsValid() or not math.isfinite(volume) or volume <= 0:
            raise InvalidBoolean("Boolean result contains an invalid/nonpositive/nonfinite solid")
        volumes.append(volume)
    # Valid empty compounds and face/edge contact have zero material volume.
    total = sum(volumes)
    if not math.isfinite(total):
        raise InvalidBoolean("Boolean result total volume is not finite")
    return total


def _boolean_volume(operation, first, second):
    try:
        import cadquery as cq
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Common
        operation_type = {"difference": BRepAlgoAPI_Cut, "common": BRepAlgoAPI_Common}[operation]
        builder = operation_type(first.wrapped, second.wrapped)
        if not builder.IsDone():
            raise InvalidBoolean("Boolean algorithm did not complete")
        result = cq.Shape.cast(builder.Shape())
        return _checked_volume(result)
    except Exception as error:
        raise InvalidBoolean(_error(error)) from error


def _pose_matrix(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(not isinstance(row, (list, tuple)) or len(row) != 4 for row in value):
        raise ValueError("Component pose must be a 4x4 rigid homogeneous matrix")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for row in value for x in row):
        raise ValueError("Component pose must contain finite numeric values")
    matrix = [[float(x) for x in row] for row in value]
    if any(abs(x-y) > 1e-12 for x, y in zip(matrix[3], [0, 0, 0, 1])):
        raise ValueError("Invalid homogeneous last row")
    r = [row[:3] for row in matrix[:3]]
    if any(abs(sum(r[i][k]*r[j][k] for k in range(3)) - (1 if i == j else 0)) > 1e-10 for i in range(3) for j in range(3)):
        raise ValueError("Pose rotation must be orthonormal; scaling/shear are not allowed")
    determinant = (r[0][0]*(r[1][1]*r[2][2]-r[1][2]*r[2][1])
                   - r[0][1]*(r[1][0]*r[2][2]-r[1][2]*r[2][0])
                   + r[0][2]*(r[1][0]*r[2][1]-r[1][1]*r[2][0]))
    if abs(determinant-1) > 1e-10:
        raise ValueError("Pose must preserve orientation")
    return matrix


def _apply_pose(shape, matrix):
    import cadquery as cq
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf
    transform = gp_Trsf()
    transform.SetValues(*(x for row in matrix[:3] for x in row))
    result = cq.Shape.cast(BRepBuilderAPI_Transform(shape.wrapped, transform, True).Shape())
    _checked_volume(result)
    return result


def _finite_json(value):
    if isinstance(value, dict):
        return {str(key): _finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def audit(directory, *, component_poses=None, relative_volume_tolerance=1e-8,
          absolute_volume_tolerance_mm3=1e-9, max_file_bytes=MAX_FILE_BYTES):
    for value in (relative_volume_tolerance, absolute_volume_tolerance_mm3):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("Volume tolerances must be finite and nonnegative")
    if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes < 1:
        raise ValueError("max_file_bytes must be a positive integer")
    poses = {} if component_poses is None else component_poses
    if not isinstance(poses, dict) or set(poses) - {"main-part", "stand"}:
        raise ValueError("Only evaluator-owned main-part and stand poses are accepted")
    poses = {key: _pose_matrix(value) for key, value in poses.items()}
    directory = Path(directory).absolute()
    if ".." in directory.parts:
        raise ValueError("Directory traversal is not permitted")
    report = {"schema_version": 1, "audit": "deng_artifact_integrity", "status": "inconclusive",
              "required_files": list(FILES), "files": {}, "checks": [],
              "component_poses": poses, "pose_policy": "Omitted poses mean component exports already use assembly coordinates; no inferred registration",
              "tolerances": {"relative_volume": relative_volume_tolerance,
                             "absolute_volume_mm3": absolute_volume_tolerance_mm3},
              "limitations": ["Drawing fidelity, feature dimensions, stage-to-stage design intent, manufacturability, and source rebuild are not assessed.",
                              "Solid validity and material agreement use OpenCascade numerical predicates and are not a proof against every self-intersection.",
                              "Units are read from STEP shape representation contexts; unrecognized or absent declarations are inconclusive.",
                              "Copy requires stopped candidate writers and stable trusted directory ancestors; metadata checks detect common races but are not an atomic snapshot guarantee.",
                              "Only the six required canonical STEP artifacts are opened; unrelated files are not inspected."]}
    def check(name, status, **details):
        report["checks"].append({"name": name, "status": status, **details})
    try:
        directory_fd = _open_directory(directory)
    except ChangedInputError as error:
        check("input_directory", "inconclusive", error=_error(error))
        return report
    except (FilePolicyError, OSError) as error:
        check("input_directory", "fail", error=_error(error))
        report["status"] = "fail"
        return report
    shapes = {}
    try:
        with tempfile.TemporaryDirectory(prefix="deng-integrity-") as temporary:
            for name in FILES:
                record = report["files"][name] = {"status": "inconclusive"}
                try:
                    record["snapshot"] = _copy_file(directory_fd, name, Path(temporary)/name, max_file_bytes)
                except (FilePolicyError, OSError) as error:
                    record.update(status="fail", error=_error(error))
                    check(name + ":file_contract", "fail", error=_error(error))
                    continue
                except ChangedInputError as error:
                    record.update(status="inconclusive", error=_error(error))
                    check(name + ":stable_input", "inconclusive", error=_error(error))
                    continue
                check(name + ":file_contract", "pass")
                try:
                    shape, units, transfer = _load_step(Path(temporary)/name)
                    details = _describe(shape)
                    record.update(units=units, transfer=transfer, geometry=details)
                    good_geometry = (details["valid"] and details["finite"] and details["positive_valid_solids"]
                                     and details["solid_count"] == EXPECTED_SOLIDS[name] and not details["loose_topology"])
                    geometry_status = "pass" if good_geometry else "fail"
                    check(name + ":geometry", geometry_status, expected_solid_count=EXPECTED_SOLIDS[name],
                          actual_solid_count=details["solid_count"])
                    check(name + ":millimeter_units", units["status"])
                    record["status"] = _status([geometry_status, units["status"]])
                    if good_geometry:
                        shapes[name] = shape
                except (ImportError, OSError) as error:
                    record.update(status="inconclusive", error=_error(error))
                    check(name + ":import_geometry", "inconclusive", error=_error(error))
                except Exception as error:
                    record.update(status="fail", error=_error(error))
                    check(name + ":import_geometry", "fail", error=_error(error))
    finally:
        os.close(directory_fd)

    if all(name in shapes for name in ("main-part.step", "stand.step", "assembly.step")):
        try:
            components = [shapes[name + ".step"] for name in ("main-part", "stand")]
            components = [_apply_pose(shape, poses[name]) if name in poses else shape
                          for name, shape in zip(("main-part", "stand"), components)]
            assembly = shapes["assembly.step"].Solids()
            def tolerance(first, second):
                return max(absolute_volume_tolerance_mm3,
                           relative_volume_tolerance*min(_checked_volume(first), _checked_volume(second)))
            for label, first, second in (("posed_components_no_overlap", *components),
                                         ("assembly_parts_no_overlap", *assembly)):
                try:
                    limit = tolerance(first, second)
                    overlap = _boolean_volume("common", first, second)
                    check(label, "pass" if overlap <= limit else "fail", overlap_volume_mm3=overlap,
                          tolerance_mm3=limit, face_or_edge_contact_allowed=True)
                except Exception as error:
                    check(label, "inconclusive", error=_error(error))
            comparisons = []
            for i, expected in enumerate(components):
                row = []
                for j, actual in enumerate(assembly):
                    try:
                        limit = tolerance(expected, actual)
                        missing = _boolean_volume("difference", expected, actual)
                        excess = _boolean_volume("difference", actual, expected)
                        row.append({"expected_component": ("main-part", "stand")[i], "assembly_solid_index": j,
                                    "status": "pass" if missing+excess <= limit else "fail",
                                    "missing_volume_mm3": missing, "excess_volume_mm3": excess,
                                    "symmetric_difference_mm3": missing+excess, "tolerance_mm3": limit})
                    except Exception as error:
                        row.append({"status": "inconclusive", "error": _error(error)})
                comparisons.append(row)
            assignments = [[comparisons[0][0], comparisons[1][1]], [comparisons[0][1], comparisons[1][0]]]
            assignment_status = [_status(item["status"] for item in pair) for pair in assignments]
            # A definite failed pair rules out that assignment. Acceptance needs
            # a complete passing bijection; uncertainty in viable assignments
            # remains inconclusive instead of becoming agreement evidence.
            match_status = ("pass" if "pass" in assignment_status else
                            "inconclusive" if "inconclusive" in assignment_status else "fail")
            check("assembly_matches_posed_components", match_status, comparisons=comparisons,
                  assignments=assignment_status,
                  matched_assignment=assignment_status.index("pass") if "pass" in assignment_status else None)
        except Exception as error:
            check("assembly_consistency", "inconclusive", error=_error(error))
    else:
        check("assembly_consistency", "inconclusive", reason="Required valid component or two-solid assembly unavailable")
    report["status"] = _status(item["status"] for item in report["checks"])
    return _finite_json(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--poses", type=Path, help="Operator-owned JSON rigid transforms, never candidate metadata")
    parser.add_argument("--max-file-bytes", type=int, default=MAX_FILE_BYTES)
    args = parser.parse_args()
    if args.output and args.output.resolve().is_relative_to(args.directory.resolve()):
        parser.error("Report must be outside the candidate input directory")
    poses = json.loads(args.poses.read_text()) if args.poses else None
    report = audit(args.directory, component_poses=poses, max_file_bytes=args.max_file_bytes)
    serialized = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(serialized)
    print(serialized, end="")
    return {"pass": 0, "fail": 1, "inconclusive": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
