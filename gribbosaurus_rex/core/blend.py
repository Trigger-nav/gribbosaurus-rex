from gribbosaurus_rex.core.wind import to_speed_dir


def blend(models):
    """
    Confidence-weighted wind blending using vector averaging.

    Parameters
    ----------
    models : dict
        Example:
        {
            "IFS": {
                "u": float,
                "v": float,
                "w": confidence_weight
            },
            "AIFS": {
                "u": float,
                "v": float,
                "w": confidence_weight
            }
        }

    Returns
    -------
    (speed, direction)
    """

    u_sum = 0.0
    v_sum = 0.0
    w_sum = 0.0

    for model_name, m in models.items():
        w = m.get("w", 1.0)

        u_sum += m["u"] * w
        v_sum += m["v"] * w
        w_sum += w

    # Avoid divide-by-zero
    if w_sum == 0:
        raise ValueError("No valid model weights provided for blending.")

    u_blend = u_sum / w_sum
    v_blend = v_sum / w_sum

    return to_speed_dir(u_blend, v_blend)