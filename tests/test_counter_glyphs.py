"""Regression tests for lightweight card-counter glyph matching."""

import cv2
import numpy as np

from scanner import _multiply_score


def test_multiply_score_accepts_dense_anti_aliased_x_contour() -> None:
    """A thick X can have a filled external contour denser than 45%."""
    raster = np.zeros((12, 12), dtype=np.uint8)
    cv2.line(raster, (2, 2), (9, 9), 255, 3)
    cv2.line(raster, (9, 2), (2, 9), 255, 3)

    contours, _ = cv2.findContours(
        raster, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    assert contours

    contour = contours[0]
    x, y, width, height = cv2.boundingRect(contour)
    shifted = contour - np.array([[[x, y]]], dtype=np.int32)
    filled_contour = np.zeros((height, width), dtype=np.uint8)
    cv2.drawContours(filled_contour, [shifted], -1, 255, thickness=-1)

    assert _multiply_score(filled_contour) >= 0.30


def test_multiply_score_rejects_solid_bright_blob() -> None:
    """Raising the density ceiling must not make a solid blob an X."""
    solid_blob = np.full((12, 12), 255, dtype=np.uint8)

    assert _multiply_score(solid_blob) == 0.0
