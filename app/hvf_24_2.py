"""Humphrey Visual Field 24-2 test grid coordinates (project-agnostic).

The 24-2 pattern tests 54 points on a 6° grid centred on fixation.

Printout vs visual-field coordinate convention:
  The Humphrey printout puts temporal on the left for BOTH eyes.
  In visual-field coordinates:
    x > 0 = right VF (nasal for OD, temporal for OS)
    x < 0 = left VF  (temporal for OD, nasal for OS)
    y > 0 = superior VF
  For OS, the printout x must be NEGATED to get the VF x.
"""
import math

# x-coordinates below are for OD; for OS, negate.
ROWS_24_2 = [
    (21,  [-9, -3, 3, 9]),
    (15,  [-15, -9, -3, 3, 9, 15]),
    (9,   [-21, -15, -9, -3, 3, 9, 15, 21]),
    (3,   [-27, -21, -15, -9, -3, 3, 9, 15, 21]),
    (-3,  [-27, -21, -15, -9, -3, 3, 9, 15, 21]),
    (-9,  [-21, -15, -9, -3, 3, 9, 15, 21]),
    (-15, [-15, -9, -3, 3, 9, 15]),
    (-21, [-9, -3, 3, 9]),
]

TOTAL_POINTS = sum(len(xs) for _, xs in ROWS_24_2)  # = 54
MAX_COLS = max(len(xs) for _, xs in ROWS_24_2)      # = 9

# Blind spot is at printout col 7, row 4 for both eyes
# (OD: VF x=+15, y=-3; OS: VF x=-15, y=-3)
BS_ROW = 4
BS_COL = 7


def eccentricity(x, y):
    return math.sqrt(x * x + y * y)


def get_vf_x(printout_x, eye):
    return -printout_x if eye == "OS" else printout_x


def quadrant_anatomical(vf_x, y, eye):
    v = "S" if y >= 0 else "I"
    if eye == "OD":
        h = "N" if vf_x > 0 else "T"
    else:
        h = "N" if vf_x < 0 else "T"
    return v + h
