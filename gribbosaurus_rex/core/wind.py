import numpy as np


def to_uv(speed, direction_deg):
    """
    Convert wind speed + direction (deg FROM) into u/v components.

    Meteorological convention:
    direction = where wind is coming FROM
    """

    rad = np.deg2rad(direction_deg)

    u = -speed * np.sin(rad)
    v = -speed * np.cos(rad)

    return u, v


def to_speed_dir(u, v):
    """
    Convert u/v wind components back to speed + direction (deg FROM)
    """

    speed = np.sqrt(u**2 + v**2)

    direction = (np.rad2deg(np.arctan2(-u, -v)) + 360) % 360

    return speed, direction


def circular_error(a, b):
    """
    Minimum angular difference between two directions (degrees)
    Handles wrap-around correctly (e.g. 350° vs 10° = 20° error)
    """

    diff = np.abs(a - b) % 360
    return np.minimum(diff, 360 - diff)