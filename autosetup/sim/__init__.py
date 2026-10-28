"""Lap-time model: a quasi-steady-state point-mass lap over a speed profile from your own
laps, with the car's drag, downforce and grip calibrated from those laps.

extract.py      what each recording contributes (at import): profile, straights, envelope
track.py        the layout's curvature, sectors and speed traps
lapsim.py       the simulation
aero.py         per-recording fits and the per-car aero map (wing, ride heights, rake)
residual.py     sim vs real: residual GP on sector times, speed-trap drag correction
sensitivity.py  what a click of wing or a mm of rake is worth here
"""
