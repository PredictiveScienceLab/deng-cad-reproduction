import json
import math
from pathlib import Path
import tempfile
import unittest

import cadquery as cq
from fixtures import export_fixture, stage1_fixture
from geometry import AUTO_IDS, evaluate_stage1


class Stage1Measurements(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="deng-geometry-fixtures-")
        cls.root = Path(cls.tmp.name)
        cls.nominal = export_fixture(cls.root / "nominal.step")
        cls.nominal_report = evaluate_stage1(cls.nominal)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def report(self, name, **kwargs):
        return evaluate_stage1(export_fixture(self.root / f"{name}.step", **kwargs))

    @staticmethod
    def by_id(report):
        return {r["id"]: r for r in report["criteria"]}

    def test_nominal_all_implemented_and_only_implemented_pass(self):
        result = self.by_id(self.nominal_report)
        self.assertEqual(self.nominal_report["recognition_status"], "recognized", self.nominal_report)
        self.assertEqual({k for k, v in result.items() if v["status"] == "pass"}, set(AUTO_IDS), result)
        self.assertEqual({k for k, v in result.items() if v["status"] == "requires_review"}, {"S1D15", "S1D16", "S1D22"})

    def test_rigid_transform_and_sign_invariance(self):
        shape = stage1_fixture().rotate((0, 0, 0), (1, 2, 3), 127).translate((42, -73, 19))
        path = self.root / "transformed.step"
        cq.exporters.export(shape, str(path))
        result = self.by_id(evaluate_stage1(path))
        self.assertEqual({k for k, v in result.items() if v["status"] == "pass"}, set(AUTO_IDS), result)

    def test_wrong_body_dimension_detected(self):
        result = self.by_id(self.report("wrong_diameter", body_diameter=69.5))
        self.assertEqual(result["S1D7"]["status"], "fail", result)
        self.assertAlmostEqual(result["S1D7"]["measurement"][0], 69.5)

    def test_single_wrong_hole_diameter_detected(self):
        result = self.by_id(self.report("wrong_hole", large_hole_radius=5.2))
        self.assertEqual(result["S1D2"]["status"], "fail", result)
        self.assertEqual(len(result["S1D2"]["measurement"]), 4)

    def test_blind_hole_with_correct_opening_diameter_does_not_pass(self):
        result = self.by_id(self.report("blind_hole", first_large_hole_depth=8.0))
        self.assertEqual(result["S1D2"]["status"], "fail", result)
        self.assertTrue(all(abs(x - 10) < 1e-8 for x in result["S1D2"]["measurement"]))
        self.assertEqual(sum(x["through"] for x in result["S1D2"]["through_hole_evidence"]), 3)

    def test_single_hole_offset_detected(self):
        result = self.by_id(self.report("wrong_offset", hole_dx=0.5))
        self.assertEqual(result["S1D6"]["status"], "fail", result)
        self.assertEqual(len(result["S1D6"]["measurement"]), 8)

    def test_missing_hole_cannot_pass_pattern(self):
        result = self.by_id(self.report("missing_hole", remove_large_hole=True))
        self.assertEqual(result["S1D2"]["status"], "requires_review", result)
        self.assertEqual(result["S1D6"]["status"], "requires_review", result)

    def test_counterbore_depth_detected(self):
        result = self.by_id(self.report("wrong_depth", counterbore_depth=6.5))
        self.assertEqual(result["S1D20"]["status"], "fail", result)
        self.assertAlmostEqual(result["S1D20"]["measurement"][0], 6.5)

    def test_cone_angle_changes_endpoint_diameter(self):
        result = self.by_id(self.report("wrong_cone", cone_angle=7.5))
        self.assertEqual(result["S1D13"]["status"], "fail", result)

    def test_reversed_cone_endpoints_do_not_pass(self):
        result = self.by_id(self.report("reversed_cone", reverse_outer_cone=True))
        self.assertEqual(result["S1D12"]["status"], "fail", result)
        self.assertEqual(result["S1D13"]["status"], "fail", result)

    def test_rounded_interval_is_not_replaced_by_wider_direct_tolerance(self):
        angle = math.degrees(math.atan((62.287 - 50) / 100))
        result = self.by_id(self.report("rounded_interval", cone_angle=angle))
        self.assertEqual(result["S1D13"]["status"], "fail", result)
        self.assertEqual(result["S1D13"]["accepted_interval_mm"], [62.274, 62.286])

    def test_wrong_part_never_passes(self):
        result = self.by_id(self.report("wrong_part", stand_instead=True))
        self.assertTrue(all(v["status"] == "requires_review" for v in result.values()))

    def test_changed_metadata_never_affects_measurements(self):
        (self.root / "manifest.json").write_text(json.dumps({"S1D7": 0, "all_checks_passed": True}))
        result = self.by_id(evaluate_stage1(self.nominal))
        self.assertEqual(result["S1D7"]["measurement"], self.by_id(self.nominal_report)["S1D7"]["measurement"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
