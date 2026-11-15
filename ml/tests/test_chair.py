import numpy as np
import pytest

from fsd_ml import chair


def test_paths_are_shifted_from_the_lens_to_the_chair() -> None:
    setup = chair.ChairSetup(
        chair.Camera(left_m=-0.3, from_rear_m=1.0), chair.Body(length_m=1.2), measured=True
    )
    # Straight ahead of a lens on the front right corner is right of the chair's middle.
    shifted = setup.to_chair(np.array([[1.0, 0.0], [2.0, 0.0]]))
    assert shifted == pytest.approx(np.array([[1.4, -0.3], [2.4, -0.3]]))
