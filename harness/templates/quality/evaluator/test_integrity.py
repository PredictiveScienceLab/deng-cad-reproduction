import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cadquery as cq
import integrity


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fixture-", dir=Path(__file__).resolve().parent)
        self.directory = Path(self.temp.name)
        self.main = cq.Solid.makeBox(12, 10, 4)
        self.stand = cq.Solid.makeBox(12, 10, 3).translate((0, 0, -3))
        for name in integrity.FILES:
            shape = (cq.Compound.makeCompound([self.main, self.stand]) if name == "assembly.step"
                     else self.stand if name == "stand.step" else self.main)
            cq.exporters.export(shape, str(self.directory/name))

    def tearDown(self):
        self.temp.cleanup()

    def export(self, name, shape):
        cq.exporters.export(shape, str(self.directory/name))

    def check(self, report, name):
        return next(item for item in report["checks"] if item["name"] == name)

    def test_valid_face_contact_and_read_only_source(self):
        before = {name: hashlib.sha256((self.directory/name).read_bytes()).hexdigest() for name in integrity.FILES}
        report = integrity.audit(self.directory)
        self.assertEqual(report["status"], "pass", report)
        self.assertEqual({name: record["snapshot"]["sha256"] for name, record in report["files"].items()}, before)
        self.assertEqual(self.check(report, "posed_components_no_overlap")["overlap_volume_mm3"], 0)
        after = {name: hashlib.sha256((self.directory/name).read_bytes()).hexdigest() for name in integrity.FILES}
        self.assertEqual(before, after)
        json.dumps(report, allow_nan=False)

    def test_disjoint_parts_and_reversed_assembly_order(self):
        stand = self.stand.translate((0, 0, -2))
        self.export("stand.step", stand)
        self.export("assembly.step", cq.Compound.makeCompound([stand, self.main]))
        self.assertEqual(integrity.audit(self.directory)["status"], "pass")

    def test_missing_canonical_file(self):
        (self.directory/"step-2.step").unlink()
        report = integrity.audit(self.directory)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["files"]["step-2.step"]["status"], "fail")

    def test_assembly_missing_part_or_extra_debris(self):
        for shape in (self.main, cq.Compound.makeCompound([self.main, self.stand, cq.Solid.makeBox(1, 1, 1).translate((100, 0, 0))])):
            with self.subTest(solids=len(shape.Solids())):
                self.export("assembly.step", shape)
                report = integrity.audit(self.directory)
                self.assertEqual(self.check(report, "assembly.step:geometry")["status"], "fail")

    def test_displaced_assembly_part_fails_independent_matching(self):
        self.export("assembly.step", cq.Compound.makeCompound([self.main, self.stand.translate((20, 0, 0))]))
        report = integrity.audit(self.directory)
        self.assertEqual(self.check(report, "assembly_matches_posed_components")["status"], "fail")
        self.assertEqual(report["status"], "fail")

    def test_positive_volume_interference_fails_even_when_assembly_matches(self):
        stand = self.stand.translate((0, 0, 1))
        self.export("stand.step", stand)
        self.export("assembly.step", cq.Compound.makeCompound([self.main, stand]))
        report = integrity.audit(self.directory)
        self.assertEqual(self.check(report, "assembly_matches_posed_components")["status"], "pass")
        self.assertEqual(self.check(report, "posed_components_no_overlap")["status"], "fail")
        self.assertEqual(report["status"], "fail")

    def test_component_and_stage_require_one_solid(self):
        for name in ("main-part.step", "step-1.step"):
            with self.subTest(name=name):
                self.export(name, cq.Compound.makeCompound([self.main, self.main.translate((50, 0, 0))]))
                self.assertEqual(self.check(integrity.audit(self.directory), name+":geometry")["status"], "fail")
                self.export(name, self.main)

    def test_loose_non_solid_debris_is_rejected(self):
        face = cq.Face.makePlane(length=2, width=2, basePnt=(40, 0, 0))
        self.export("main-part.step", cq.Compound.makeCompound([self.main, face]))
        report = integrity.audit(self.directory)
        self.assertEqual(report["status"], "fail")
        self.assertTrue(report["files"]["main-part.step"]["geometry"]["loose_topology"])

    def test_invalid_export_fails(self):
        (self.directory/"step-3.step").write_text("not a STEP export\n")
        report = integrity.audit(self.directory)
        self.assertEqual(report["files"]["step-3.step"]["status"], "fail")

    def test_open_shell_presented_as_solid_is_rejected(self):
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Shell, TopoDS_Solid
        builder = BRep_Builder()
        shell, solid = TopoDS_Shell(), TopoDS_Solid()
        builder.MakeShell(shell)
        for face in self.main.Faces()[:-1]:
            builder.Add(shell, face.wrapped)
        builder.MakeSolid(solid)
        builder.Add(solid, shell)
        broken = cq.Shape.cast(solid)
        self.assertFalse(broken.isValid())
        self.export("step-3.step", broken)
        self.assertEqual(integrity.audit(self.directory)["files"]["step-3.step"]["status"], "fail")

    def test_symlink_special_file_size_and_root_symlink_rejected(self):
        path = self.directory/"step-1.step"
        original = path.read_bytes()
        path.unlink()
        path.symlink_to(self.directory/"step-2.step")
        self.assertEqual(integrity.audit(self.directory)["status"], "fail")
        path.unlink()
        os.mkfifo(path)
        self.assertEqual(integrity.audit(self.directory)["status"], "fail")
        path.unlink()
        path.write_bytes(original)
        self.assertEqual(integrity.audit(self.directory, max_file_bytes=10)["status"], "fail")
        alias = self.directory/"linked-directory"
        alias.symlink_to(self.directory, target_is_directory=True)
        self.assertEqual(integrity.audit(alias)["status"], "fail")
        child = self.directory/"child"
        child.mkdir()
        self.assertEqual(integrity.audit(alias/"child")["status"], "fail")

    def test_directory_ancestors_need_no_directory_open_permission(self):
        real_open = os.open
        opened = []
        def recording_open(path, flags, **kwargs):
            opened.append((path, flags))
            return real_open(path, flags, **kwargs)
        with patch("integrity.os.open", side_effect=recording_open):
            descriptor = integrity._open_directory(self.directory)
        os.close(descriptor)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0][0], self.directory)
        self.assertTrue(all(flags & os.O_NOFOLLOW and flags & os.O_DIRECTORY for _, flags in opened))

    def test_directory_identity_change_during_open_is_rejected(self):
        real_open = os.open
        backup = self.directory.with_name(self.directory.name+'-moved')
        def replacing_open(path, flags, **kwargs):
            descriptor = real_open(path, flags, **kwargs)
            self.directory.rename(backup)
            self.directory.mkdir()
            return descriptor
        try:
            with patch("integrity.os.open", side_effect=replacing_open):
                with self.assertRaises(integrity.ChangedInputError):
                    integrity._open_directory(self.directory)
        finally:
            if backup.exists():
                self.directory.rmdir()
                backup.rename(self.directory)

    def test_non_mm_step_units_fail_even_when_geometry_scales_consistently(self):
        for name in integrity.FILES:
            path = self.directory/name
            text = path.read_text()
            self.assertIn(".MILLI.,.METRE.", text)
            path.write_text(text.replace(".MILLI.,.METRE.", "$,.METRE."))
        report = integrity.audit(self.directory)
        self.assertEqual(report["status"], "fail")
        self.assertTrue(all(record["units"]["status"] == "fail" for record in report["files"].values()))

    def test_absent_or_unknown_unit_evidence_is_inconclusive(self):
        for lengths in ([], ["unrecognized-unit"]):
            with self.subTest(lengths=lengths), patch("integrity._unit_names", return_value={"length": lengths, "angle": [], "solid_angle": []}):
                self.assertEqual(integrity.audit(self.directory)["status"], "inconclusive")

    def test_invalid_boolean_results_never_count_as_zero_or_pass(self):
        from OCP.TopoDS import TopoDS_Shape
        # CadQuery refuses construction from a null shape. Bypass only its
        # constructor to exercise our independent null-result guard.
        empty_null = object.__new__(cq.Shape)
        empty_null.wrapped = TopoDS_Shape()
        with self.assertRaises(integrity.InvalidBoolean):
            integrity._checked_volume(empty_null)
        with patch("integrity._boolean_volume", side_effect=integrity.InvalidBoolean("injected invalid Boolean")):
            report = integrity.audit(self.directory)
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(self.check(report, "assembly_matches_posed_components")["status"], "inconclusive")

    def test_boolean_not_done_and_nonfinite_volume_are_rejected(self):
        with patch("OCP.BRepAlgoAPI.BRepAlgoAPI_Common") as operation:
            operation.return_value.IsDone.return_value = False
            with self.assertRaises(integrity.InvalidBoolean):
                integrity._boolean_volume("common", self.main, self.stand)
        with patch.object(cq.Solid, "Volume", return_value=float("nan")):
            with self.assertRaises(integrity.InvalidBoolean):
                integrity._checked_volume(self.main)

    def test_detected_input_mutation_is_inconclusive(self):
        real_read = os.read
        changed = False
        def changing_read(descriptor, count):
            nonlocal changed
            data = real_read(descriptor, count)
            if data and not changed:
                changed = True
                with (self.directory/"step-1.step").open("ab") as stream:
                    stream.write(b"\n")
            return data
        with patch("integrity.os.read", side_effect=changing_read):
            report = integrity.audit(self.directory)
        self.assertEqual(report["files"]["step-1.step"]["status"], "inconclusive")
        self.assertEqual(report["status"], "inconclusive")

    def test_unrelated_candidate_source_is_not_executed(self):
        sentinel = self.directory/"source-was-executed"
        (self.directory/"candidate.py").write_text(f"open({str(sentinel)!r}, 'w').write('executed')\n")
        self.assertEqual(integrity.audit(self.directory)["status"], "pass")
        self.assertFalse(sentinel.exists())

    def test_evaluator_owned_pose_and_rejection_of_scale_transform(self):
        local_stand = cq.Solid.makeBox(12, 10, 3)
        self.export("stand.step", local_stand)
        identity = [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
        pose = [[1,0,0,0],[0,1,0,0],[0,0,1,-3],[0,0,0,1]]
        report = integrity.audit(self.directory, component_poses={"main-part":identity, "stand":pose})
        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(integrity.audit(self.directory)["status"], "fail")
        with self.assertRaises(ValueError):
            integrity.audit(self.directory, component_poses={"stand":[[2,0,0,0],[0,2,0,0],[0,0,2,0],[0,0,0,1]]})

    def test_consistent_geometric_scaling_is_not_drawing_fidelity(self):
        for name in integrity.FILES:
            shape = (cq.Compound.makeCompound([self.main.scale(2), self.stand.scale(2)]) if name == "assembly.step"
                     else self.stand.scale(2) if name == "stand.step" else self.main.scale(2))
            self.export(name, shape)
        self.assertEqual(integrity.audit(self.directory)["status"], "pass")


if __name__ == "__main__":
    unittest.main(verbosity=2)
