"""Choosing the next setup.

space.py          the settings it works in: a main block and the dampers, in clicks
priors.py         what each main setting does to the balance (direction, fixed size)
balance_model.py  predicted balance per phase × speed band at a layout
platform.py       ride-height safety
laptime.py        predicted lap time: lap model + aero map + residual GP
bo.py             the constrained search: fastest within your balance band
dampers.py        the damper block: contact patch, kerbs no worse, same band
gp.py             a small exact Gaussian process (numpy)
bench.py          a toy car to check the loop converges
"""
