"""RULA-specific geometry helpers."""

import numpy as np
import numpy.typing as npt

from core.perception.pose.graph.h36m import H36MSkeleton
from core.utils.compute import angle_between_3d, rotate_to

_H36M = H36MSkeleton()


def signed_flexion_angle(
    v: npt.NDArray[np.float32],
    trunk_up: npt.NDArray[np.float32],
) -> float:
    """Signed flexion angle (degrees) of *v* relative to *trunk_up*.

    Positive = forward flexion, negative = extension.
    Computed fully in 3D using the cross product to determine sign.
    """
    angle: float = angle_between_3d(v, trunk_up)
    cross: npt.NDArray[np.float32] = np.cross(trunk_up, v)
    if cross[0] > 0:
        return angle
    else:
        return -angle


def align_to_vertical(
    keypoints: npt.NDArray[np.float32],
) -> npt.NDArray[np.float32]:
    """Rotate 3D keypoints so that spine-to-thorax aligns with -Y (up).

    Convention: x=right, y=down, z=depth.
    Upward in image space is -Y, so trunk (spine→thorax) should point to [0, -1, 0].
    """
    spine_idx: int = _H36M.joint("SPINE")
    thorax_idx: int = _H36M.joint("THORAX")
    spine: npt.NDArray[np.float32] = keypoints[spine_idx]
    thorax: npt.NDArray[np.float32] = keypoints[thorax_idx]
    trunk_vec: npt.NDArray[np.float32] = thorax - spine

    return rotate_to(
        keypoints,
        src_vec=trunk_vec,
        dst_vec=np.array([0.0, -1.0, 0.0], dtype=np.float32),
        center=spine,
    )


def joint_name(idx: int) -> str:
    """Get lowercase joint name for reporting."""
    return H36MSkeleton.JOINT_NAMES.get(idx, str(idx)).lower()
