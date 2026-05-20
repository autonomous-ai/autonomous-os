"""Tests for RULA ergonomic assessment from H36M keypoints."""

import numpy as np
import pytest

from core.models.pose import RiskLevel
from core.perception.pose.graph.h36m import H36MJoint
from core.perception.pose.predictors.ergo.rula import RULAAssessor, align_to_vertical
from core.utils.compute import ensure_3d


def _upright_skeleton_2d() -> np.ndarray:
    """Neutral upright sitting posture in 2D (17, 2)."""
    kps = np.zeros((17, 2), dtype=np.float32)
    kps[0]  = [160, 400]   # pelvis
    kps[7]  = [160, 300]   # spine
    kps[8]  = [160, 200]   # thorax
    kps[9]  = [160, 150]   # neck
    kps[10] = [160, 100]   # head
    kps[11] = [190, 210]   # L shoulder
    kps[12] = [190, 290]   # L elbow (arms down)
    kps[13] = [190, 300]   # L wrist
    kps[14] = [130, 210]   # R shoulder
    kps[15] = [130, 290]   # R elbow (arms down)
    kps[16] = [130, 300]   # R wrist
    return kps


def _hunched_skeleton_2d() -> np.ndarray:
    """Forward-hunched posture: head forward, arms raised."""
    kps = _upright_skeleton_2d()
    kps[10] = [200, 120]   # head forward
    kps[14] = [100, 220]   # R shoulder
    kps[15] = [70, 200]    # R elbow raised forward
    kps[16] = [50, 180]    # R wrist raised forward
    return kps


def _upright_skeleton_3d() -> np.ndarray:
    """Neutral upright posture in 3D (17, 3). x=right, y=down, z=depth."""
    kps = np.zeros((17, 3), dtype=np.float32)
    kps[0]  = [0, 400, 0]      # pelvis (lowest)
    kps[7]  = [0, 300, 0]      # spine
    kps[8]  = [0, 200, 0]      # thorax
    kps[9]  = [0, 150, 0]      # neck
    kps[10] = [0, 100, 0]      # head (highest, smallest y)
    kps[11] = [30, 210, 0]     # L shoulder
    kps[12] = [30, 290, 0]     # L elbow (arms down)
    kps[13] = [30, 300, 0]     # L wrist
    kps[14] = [-30, 210, 0]    # R shoulder
    kps[15] = [-30, 290, 0]    # R elbow (arms down)
    kps[16] = [-30, 300, 0]    # R wrist
    return kps


def _all_confident() -> np.ndarray:
    return np.ones(17, dtype=np.float32)


class TestEnsure3D:
    def test_2d_padded_to_3d(self):
        kps_2d = np.random.rand(17, 2).astype(np.float32)
        kps_3d = ensure_3d(kps_2d)
        assert kps_3d.shape == (17, 3)
        # z-axis (depth) should be all zeros
        assert np.allclose(kps_3d[:, 2], 0.0)
        # original values preserved in columns 0-1 (x, y)
        assert np.allclose(kps_3d[:, :2], kps_2d)

    def test_3d_unchanged(self):
        kps_3d = np.random.rand(17, 3).astype(np.float32)
        result = ensure_3d(kps_3d)
        assert result.shape == (17, 3)
        assert np.allclose(result, kps_3d)

    def test_does_not_mutate_input(self):
        kps = np.random.rand(17, 2).astype(np.float32)
        original = kps.copy()
        ensure_3d(kps)
        assert np.allclose(kps, original)


class TestAlignToVertical:
    def test_already_vertical_unchanged(self):
        kps = _upright_skeleton_3d()
        aligned = align_to_vertical(kps)
        trunk = aligned[H36MJoint.THORAX] - aligned[H36MJoint.SPINE]
        # After alignment, trunk should point in -Y direction (up in image coords)
        assert abs(trunk[0]) < 1.0
        assert trunk[1] < 0
        assert abs(trunk[2]) < 1.0

    def test_tilted_skeleton_aligned(self):
        kps = _upright_skeleton_3d()
        # Tilt the whole skeleton forward (rotate in Y-Z plane)
        angle = np.radians(30)
        cos_a, sin_a = np.cos(angle), np.sin(angle)
        for i in range(17):
            y, z = kps[i, 1], kps[i, 2]
            kps[i, 1] = y * cos_a - z * sin_a
            kps[i, 2] = y * sin_a + z * cos_a

        aligned = align_to_vertical(kps)
        trunk = aligned[H36MJoint.THORAX] - aligned[H36MJoint.SPINE]
        trunk_norm = trunk / (np.linalg.norm(trunk) + 1e-8)
        # Should be close to [0, -1, 0]
        assert abs(trunk_norm[0]) < 0.05
        assert trunk_norm[1] < -0.95
        assert abs(trunk_norm[2]) < 0.05


class TestRULAAssessor:
    def test_returns_both_sides(self):
        assessor = RULAAssessor()
        result = assessor.predict([(_upright_skeleton_2d(), _all_confident())])[0]
        assert result is not None
        assert result.left is not None
        assert result.right is not None
        assert result.score == max(result.left.score, result.right.score)

    def test_upright_2d_low_risk(self):
        assessor = RULAAssessor()
        result = assessor.predict([(_upright_skeleton_2d(), _all_confident())])[0]
        assert result is not None
        assert result.score <= 4
        assert result.risk_level in (RiskLevel.NEGLIGIBLE, RiskLevel.LOW)

    def test_upright_3d_low_risk(self):
        assessor = RULAAssessor()
        result = assessor.predict([(_upright_skeleton_3d(), _all_confident())])[0]
        assert result is not None
        assert result.score <= 4
        assert result.risk_level in (RiskLevel.NEGLIGIBLE, RiskLevel.LOW)

    def test_hunched_higher_risk(self):
        assessor = RULAAssessor()
        upright = assessor.predict([(_upright_skeleton_2d(), _all_confident())])[0]
        hunched = assessor.predict([(_hunched_skeleton_2d(), _all_confident())])[0]
        assert upright is not None
        assert hunched is not None
        assert hunched.score >= upright.score

    def test_score_range(self):
        assessor = RULAAssessor()
        result = assessor.predict([(_upright_skeleton_2d(), _all_confident())])[0]
        assert result is not None
        assert 1 <= result.score <= 7
        assert 1 <= result.left.score <= 7
        assert 1 <= result.right.score <= 7

    def test_body_scores_present(self):
        assessor = RULAAssessor()
        result = assessor.predict([(_upright_skeleton_2d(), _all_confident())])[0]
        assert result is not None
        for side_result in (result.left, result.right):
            bs = side_result.body_scores
            assert bs.upper_arm >= 1
            assert bs.lower_arm >= 1
            assert bs.wrist >= 1
            assert bs.neck >= 1
            assert bs.trunk >= 1
            assert bs.legs >= 1
            assert bs.table_a >= 1
            assert bs.table_b >= 1


class TestConfidenceFiltering:
    def test_low_spine_returns_none(self):
        assessor = RULAAssessor(confidence_threshold=0.3)
        scores = _all_confident()
        scores[H36MJoint.SPINE] = 0.1
        result = assessor.predict([(_upright_skeleton_2d(), scores)])[0]
        assert result is None

    def test_low_thorax_returns_none(self):
        assessor = RULAAssessor(confidence_threshold=0.3)
        scores = _all_confident()
        scores[H36MJoint.THORAX] = 0.1
        result = assessor.predict([(_upright_skeleton_2d(), scores)])[0]
        assert result is None

    def test_low_arm_confidence_skips_arm(self):
        assessor = RULAAssessor(confidence_threshold=0.3)
        scores = _all_confident()
        scores[14] = 0.1  # R shoulder
        scores[15] = 0.1  # R elbow
        result = assessor.predict([(_upright_skeleton_2d(), scores)])[0]
        assert result is not None
        assert "r_shoulder" in result.right.skipped_joints
        assert "r_elbow" in result.right.skipped_joints
        # Skipped joints should get neutral scores
        assert result.right.body_scores.upper_arm == 1
        # Left side should be unaffected
        assert len(result.left.skipped_joints) == 0

    def test_low_neck_confidence_skips_neck(self):
        assessor = RULAAssessor(confidence_threshold=0.3)
        scores = _all_confident()
        scores[9] = 0.1   # neck
        result = assessor.predict([(_upright_skeleton_2d(), scores)])[0]
        assert result is not None
        assert "neck" in result.left.skipped_joints
        assert result.left.body_scores.neck == 1

    def test_all_low_confidence_except_core(self):
        """Only spine+thorax confident -> arms skipped, neck skipped, still returns."""
        assessor = RULAAssessor(confidence_threshold=0.3)
        scores = np.full(17, 0.1, dtype=np.float32)
        scores[H36MJoint.SPINE] = 1.0
        scores[H36MJoint.THORAX] = 1.0
        result = assessor.predict([(_upright_skeleton_2d(), scores)])[0]
        assert result is not None
        assert len(result.left.skipped_joints) > 0
        assert len(result.right.skipped_joints) > 0
